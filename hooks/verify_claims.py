#!/usr/bin/env python3
"""Stop hook: no completion claims without execution evidence in the same turn.

General claim words (완료/통과/성공/정상 동작 ...) still only require that the turn contained
at least one tool call, as before - this hook does not judge whether that call was related or
successful.

A "테스트 통과" claim is held to a stricter bar: the turn must contain a Bash call whose
ACTUAL first command (not merely a word appearing somewhere in the command text - `rg pytest
README.md` doesn't count) is a real test runner matching cfg["test_command_patterns"], with a
tool_result that actually came back (not missing, not a background-launch acknowledgement) and
was not an error. "빌드 성공" is held to the same bar against cfg["build_command_patterns"]
instead - a passing test run does not justify a build claim, and vice versa. Each claim
sentence is checked independently: an exemption word ("미검증") in one sentence does not excuse
an unexempted claim in another sentence of the same turn.

Guarded against loops via stop_hook_active.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fence_parse  # noqa: E402 - shares the one human-turn-boundary definition with preflight_gate.py
import gongbang_config  # noqa: E402
import scope_guard as _sg  # noqa: E402 - shared shell tokenizer/segment-splitter/first-command finder

CLAIM = re.compile(r"(완료했|완료됐|완료되었|작업 완료|통과했|통과됐|테스트 통과|빌드 성공|배포 성공|정상 동작|정상 작동|성공했|성공적으로)")
EXEMPT = re.compile(r"(미검증|확인하지 못했|추측|실행하지 않았|근거 없)")
TEST_CLAIM = re.compile(r"테스트\s*통과")
BUILD_CLAIM = re.compile(r"빌드\s*성공")
# A claim is judged sentence-by-sentence (EXEMPT in one sentence must not excuse a different,
# unexempted claim elsewhere in the same turn's text).
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+")

# Programs that can actually RUN a test or build, as the segment's own first command (see
# scope_guard._first_command) - a word that merely appears somewhere in the command text
# ("rg pytest README.md", "grep pytest") never counts, only the real invocation does.
KNOWN_TEST_BUILD_PROGRAMS = {
    "pytest", "python", "python3", "vitest", "jest", "npm", "pnpm", "yarn",
    "tsc", "cargo", "go", "mvn", "gradle", "gradlew",
}

BACKGROUND_RE = re.compile(r"running in background|백그라운드에서\s*실행", re.I)


def _result_status(rec: dict, result_block: dict) -> str:
    """Classifies a tool_result block as "ok", "error", or "background" (a background-launch
    acknowledgement, not a completed run - must never count as a passing test/build)."""
    if bool(result_block.get("is_error")):
        return "error"
    meta = rec.get("toolUseResult")
    if isinstance(meta, dict) and meta.get("backgroundTaskId"):
        return "background"
    content = result_block.get("content")
    if isinstance(content, list):
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        text = str(content or "")
    if BACKGROUND_RE.search(text):
        return "background"
    return "ok"


def final_turn(lines: list[str]) -> tuple[str, int, list[tuple[str | None, str]], dict]:
    """Returns (assistant text joined, tool_use count, [(tool_use_id, bash_command), ...],
    {tool_use_id: result_status}) for the turn after the last human message. A human turn
    boundary is recognized via fence_parse.is_human_turn_message (origin.kind == "human") -
    shared with preflight_gate.py - so a peer/subagent relay message or a queued notification
    (also type "user") arriving mid-turn is never mistaken for a new human turn and does not
    cut off the evidence collected earlier in the SAME turn."""
    text_parts: list[str] = []
    tool_uses = 0
    bash_commands: list[tuple[str | None, str]] = []
    results_by_id: dict = {}
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        typ = rec.get("type")
        msg = rec.get("message", {}) or {}
        content = msg.get("content", [])
        if typ == "user":
            if fence_parse.is_human_turn_message(rec):
                break
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        results_by_id[c.get("tool_use_id")] = _result_status(rec, c)
            continue
        if typ == "assistant" and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    tool_uses += 1
                    if c.get("name") == "Bash":
                        cmd = str((c.get("input") or {}).get("command", ""))
                        bash_commands.append((c.get("id"), cmd))
                elif c.get("type") == "text":
                    text_parts.append(str(c.get("text", "")))
    return "\n".join(reversed(text_parts)), tool_uses, bash_commands, results_by_id


def _actually_ran_matching_command(cmd: str, patterns: list["re.Pattern[str]"]) -> bool:
    """True if `cmd` contains a shell segment whose ACTUAL first command (scope_guard.py's
    shared tokenizer/segment-splitter, after var=value assignments and sudo/env/xargs/... are
    skipped) is a real test/build runner AND that segment's own text matches one of
    `patterns`."""
    tokens = _sg._tokenize(cmd)
    if tokens is None:
        return False
    segments = _sg._split_segments(tokens)
    for seg in list(segments):
        segments.extend(_sg._expand_nested_shell_segment(seg))
    for seg in segments:
        name, i = _sg._first_command(seg)
        if name not in KNOWN_TEST_BUILD_PROGRAMS:
            continue
        text = " ".join(seg[i:])
        if any(p.search(text) for p in patterns):
            return True
    return False


def _claim_ran_ok(bash_commands: list[tuple[str | None, str]], results_by_id: dict,
                   patterns: list[str]) -> bool:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.I))
        except re.error:
            continue
    for tool_id, cmd in bash_commands:
        if _actually_ran_matching_command(cmd, compiled) and results_by_id.get(tool_id) == "ok":
            return True
    return False


def _unverified_claims(final: str, bash_commands: list[tuple[str | None, str]], results_by_id: dict,
                        tool_uses: int, cfg: dict) -> list[str]:
    """Checks each sentence of `final` independently, so an exemption phrase in one sentence
    never excuses a different, unexempted claim elsewhere in the same text. Returns the list of
    reasons for sentences that claim something without the matching evidence."""
    reasons: list[str] = []
    for sentence in SENTENCE_SPLIT_RE.split(final):
        if not sentence.strip() or EXEMPT.search(sentence):
            continue
        if TEST_CLAIM.search(sentence):
            patterns = cfg.get("test_command_patterns") or []
            if not _claim_ran_ok(bash_commands, results_by_id, patterns):
                reasons.append(
                    "'테스트 통과' 주장에 이 턴에서 실제로 실행된 테스트 명령과 그 성공 결과(오류·미실행 아님)가 "
                    "없다. 지금 검증 명령을 실제로 실행해 출력을 붙이거나, 그 표현을 '미검증'으로 바꿔 다시 답한다."
                )
            continue
        if BUILD_CLAIM.search(sentence):
            patterns = cfg.get("build_command_patterns") or []
            if not _claim_ran_ok(bash_commands, results_by_id, patterns):
                reasons.append(
                    "'빌드 성공' 주장에 이 턴에서 실제로 실행된 빌드 명령과 그 성공 결과(오류·미실행 아님)가 "
                    "없다. 지금 검증 명령을 실제로 실행해 출력을 붙이거나, 그 표현을 '미검증'으로 바꿔 다시 답한다."
                )
            continue
        if tool_uses == 0 and CLAIM.search(sentence):
            reasons.append(
                "완료·통과·성공 보고에 이 턴의 실행 근거(명령 출력)가 없다. 지금 검증 명령을 실제로 실행해 출력을 "
                "붙이거나, 그 표현을 '미검증'으로 바꿔 다시 답한다."
            )
    return reasons


def decide(data: dict, cfg: dict | None = None) -> dict | None:
    if data.get("stop_hook_active"):
        return None
    path = data.get("transcript_path")
    if not path:
        return None
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return None
    final, tool_uses, bash_commands, results_by_id = final_turn(lines)
    if not final:
        return None
    cfg = cfg if cfg is not None else gongbang_config.load()
    reasons = _unverified_claims(final, bash_commands, results_by_id, tool_uses, cfg)
    if not reasons:
        return None
    return {"decision": "block", "reason": " / ".join(dict.fromkeys(reasons))}


def main() -> int:
    try:
        data = json.load(sys.stdin)
        out = decide(data)
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
