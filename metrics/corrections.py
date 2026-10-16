#!/usr/bin/env python3
"""How often the owner had to correct / re-explain to the AI.

Reads Claude Code transcripts (cfg["transcripts_root"]/*/*.jsonl), keeps only the owner's
typed messages (entrypoint=cli, no tool results / system reminders / hook output / pasted
blocks / relayed agent messages), and flags messages that carry a correction marker. Writes
counts only - no message text - so the result can be kept and compared later with the same
method.

Heuristic, not a classifier: hand-read samples give roughly 60-65% precision on this marker
list. Compare runs only against runs of this same script.

usage: python3 corrections.py --since YYYY-MM-DD --until YYYY-MM-DD [--out file.json]
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/hooks")
import gongbang_config  # noqa: E402

MARK = [r'아니[야고라지잖]?(?![^\s]*(?:면|었|에요))', r'다시', r'그게 아니', r'말했잖', r'했잖아', r'(?<![가-힣])왜(?![냐]?하면)',
        r'하지\s?마', r'이해\s?(?:를\s?)?못', r'틀렸', r'틀리', r'거짓', r'확인\s?안', r'(?<![가-힣])또(?!한|는)', r'몇\s?번', r'엉뚱',
        r'원복', r'되돌려', r'제대로', r'멋대로', r'마음대로', r'시키지도', r'누가 .{0,10}하래', r'장난치', r'그러지 말',
        r'헤깔리지|헷갈리지']
RX = re.compile('|'.join('(?:%s)' % p for p in MARK))

# Theme families, deliberately with no industry/brand-name branch (this tool stays
# industry-agnostic).
FAMILIES = {
    'invented_rules_hardcode': r'하드코[딩팅]|규칙[을 ]*(?:만들|짜|정해)|이상한 규칙|강제(?:로)? ?(?:하지|막|규정)',
    'old_logic_back': r'옛날|옛 ?(?:조각|지시|로직)|예전 로직',
    'lying_unverified': r'거짓|속인|속이|검증도 ?안|확인 ?안 ?하고|본척',
    'source_not_read': r'자료를? ?(?:제대로|안 ?|못 ?)(?:읽|봐|보|찾|쓰)|메인 ?자료|원본 ?자료|자료를 줘도|읽지도',
    'unasked_scope': r'시키지도|누가 .{0,12}하래|일을 키|하라는 ?것만|쓸데없는 ?(?:짓|기능)',
    'repeat_told': r'몇\s?번|말했잖|했잖아|말랬|하랬|아직도',
    'token_waste': r'토큰',
}
FAM_RX = {k: re.compile(p, re.I) for k, p in FAMILIES.items()}

STRIP = [
    (r'<system-reminder>.*?</system-reminder>', re.S),
    (r'<(command-[a-z]+|local-command-[a-z]+|bash-[a-z]+|task-notification|user-prompt-submit-hook)>.*?</\1>', re.S),
    (r'<pasted_content[^>]*>.*?</pasted_content[^>]*>', re.S),
]
RELAY = re.compile(r'^\s*(?:<(?:teammate-message|agent-message|cross-session-message)\b|Another .* session sent a message)')
SKIP_PREFIX = ('[Request interrupted', 'Caveat:', 'This session is being continued')


def owner_text(e):
    if e.get('entrypoint') != 'cli' or e.get('isMeta') or e.get('isSidechain') or e.get('isCompactSummary'):
        return None
    if e.get('type') == 'attachment':
        a = e.get('attachment') or {}
        if not (a.get('type') == 'queued_command' and a.get('commandMode') == 'prompt'
                and (a.get('origin') or {}).get('kind') == 'human' and a.get('humanTurn') is True):
            return None
        c = a.get('prompt')
    elif e.get('type') == 'user':
        c = (e.get('message') or {}).get('content')
    else:
        return None
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get('type') == 'tool_result' for x in c):
            return None
        c = '\n'.join(x.get('text', '') for x in c if isinstance(x, dict) and x.get('type') == 'text')
    if not isinstance(c, str) or RELAY.match(c):
        return None
    for pat, fl in STRIP:
        c = re.sub(pat, '', c, flags=fl)
    c = c.strip()
    if not c or c.startswith(SKIP_PREFIX):
        return None
    return c


def in_window(ts, since, until):
    try:
        t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return False
    return since <= t < until


def scan(root: str, since: datetime, until: datetime) -> dict:
    total = 0
    corrections = 0
    families = collections.Counter()
    for path in glob.glob(os.path.join(root, '*', '*.jsonl')):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    ts = e.get('timestamp')
                    if not ts or not in_window(ts, since, until):
                        continue
                    text = owner_text(e)
                    if text is None:
                        continue
                    total += 1
                    if RX.search(text):
                        corrections += 1
                        for fam, rx in FAM_RX.items():
                            if rx.search(text):
                                families[fam] += 1
        except Exception:
            continue
    rate = (corrections / total) if total else 0.0
    return {'total_owner_messages': total, 'correction_hits': corrections, 'rate': round(rate, 4),
            'families': dict(families)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', required=True)
    ap.add_argument('--until', required=True)
    ap.add_argument('--out')
    ap.add_argument('--root', help='override transcripts_root from config')
    args = ap.parse_args()
    cfg = gongbang_config.load()
    root = args.root or cfg['transcripts_root']
    since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    until = datetime.strptime(args.until, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    result = scan(root, since, until)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
