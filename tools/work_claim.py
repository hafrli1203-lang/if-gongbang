#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""Work-queue self-claim (2026-11-01, team-lead: "작업판 일을 작업조가 스스로 맡기" — Agentic Mesh 글 요지,
중앙 배정자 없이 작업조가 WORK_QUEUE.md 의 「(자율)」 표시 줄을 스스로 보고 맡는다).

서버 한 대 기준의 가벼운 잠금(flock)만 쓴다 — claim 은 원자적이어야 한다(동시에 둘이 같은 줄을
맡으려 하면 하나만 성공). done·release 는 자기가 맡은 줄인지 확인한 뒤에만 바꾼다.
다른 줄·운영자 원문은 한 글자도 바꾸지 않는다 — 대상 줄 한 줄만, 정해진 모양으로만 바꾼다.

사용:
  python3 work_claim.py list   [--file WORK_QUEUE.md]
  python3 work_claim.py claim  --line N --by <작업조 이름> [--file ...]
  python3 work_claim.py done   --line N --by <작업조 이름> --evidence "<커밋/증거 한 줄>" [--file ...]
  python3 work_claim.py release --line N --by <작업조 이름> [--file ...]
  python3 work_claim.py restore --from <백업 파일> --by <이름> [--file ...]
      디스크 판이 비었거나(0바이트) 백업보다 줄이 짧을 때만 백업으로 되돌린다(2026-11-04,
      밤 자가 점검이 쓰기 오류로 작업판을 0바이트로 만든 사고). 그 외(디스크 판이 백업과
      같거나 더 길면) 거부 — 진행 중인 내용을 덮어쓰지 않는다.
  python3 work_claim.py selftest   # 임시 사본으로 동시 claim 2개 → 하나만 성공하는지 자체 확인
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

DEFAULT_QUEUE = Path("/apps/_ops/WORK_QUEUE.md")

UNCLAIMED_RE = re.compile(r"^- \[ \] .*\(자율\).*$")
CLAIM_TAG_RE = re.compile(r" \(맡음: (?P<by>.+?) (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)$")


class ClaimError(Exception):
    pass


def _lock_path(queue_file: Path) -> Path:
    return queue_file.with_suffix(queue_file.suffix + ".lock")


def _read_lines(queue_file: Path) -> list[str]:
    with open(queue_file, "r", encoding="utf-8") as f:
        return f.readlines()


def _write_lines_atomic(queue_file: Path, lines: list[str]) -> None:
    # 같은 폴더에 임시 파일을 쓰고 os.replace로 바꿔치기 — 쓰는 중간 상태가 보이지 않는다.
    fd, tmp_path = tempfile.mkstemp(dir=str(queue_file.parent), prefix=f".{queue_file.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, queue_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_unclaimed(queue_file: Path) -> list[tuple[int, str]]:
    lines = _read_lines(queue_file)
    out = []
    for i, line in enumerate(lines, start=1):
        text = line.rstrip("\n")
        if UNCLAIMED_RE.match(text):
            out.append((i, text))
    return out


def _with_flock(queue_file: Path, fn):
    lock_path = _lock_path(queue_file)
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def claim(queue_file: Path, line_no: int, by: str) -> str:
    def work() -> str:
        lines = _read_lines(queue_file)
        if not (1 <= line_no <= len(lines)):
            raise ClaimError(f"{line_no}번째 줄이 없습니다(파일에 {len(lines)}줄)")
        raw = lines[line_no - 1]
        text = raw.rstrip("\n")
        if not UNCLAIMED_RE.match(text):
            raise ClaimError(f"{line_no}번째 줄은 맡을 수 있는 자율 할 일이 아닙니다(이미 맡겨졌거나 「(자율)」 표시가 없음): {text!r}")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_text = "- [~] " + text[len("- [ ] ") :] + f" (맡음: {by} {ts})"
        lines[line_no - 1] = new_text + "\n"
        _write_lines_atomic(queue_file, lines)
        return new_text

    return _with_flock(queue_file, work)


def _claimed_by(text: str, line_no: int) -> re.Match:
    if not text.startswith("- [~] "):
        raise ClaimError(f"{line_no}번째 줄은 맡겨진 상태가 아닙니다: {text!r}")
    m = CLAIM_TAG_RE.search(text)
    if not m:
        raise ClaimError(f"{line_no}번째 줄에서 맡은 사람 표시를 찾지 못했습니다: {text!r}")
    return m


def _strip_claim_tag(text: str, m: re.Match) -> str:
    return text[: m.start()]


def done(queue_file: Path, line_no: int, by: str, evidence: str) -> str:
    def work() -> str:
        lines = _read_lines(queue_file)
        if not (1 <= line_no <= len(lines)):
            raise ClaimError(f"{line_no}번째 줄이 없습니다(파일에 {len(lines)}줄)")
        text = lines[line_no - 1].rstrip("\n")
        m = _claimed_by(text, line_no)
        if m.group("by") != by:
            raise ClaimError(f"{line_no}번째 줄은 '{m.group('by')}'가 맡았습니다 — '{by}'가 끝낼 수 없습니다")
        body = _strip_claim_tag(text, m)[len("- [~] ") :]
        new_text = "- [x] " + body + f" (끝: {evidence})"
        lines[line_no - 1] = new_text + "\n"
        _write_lines_atomic(queue_file, lines)
        return new_text

    return _with_flock(queue_file, work)


def force_done(queue_file: Path, line_no: int, by: str, evidence: str) -> str:
    """「(자율)」 표시도 claim 표시도 없는 줄(반장이 직접 배정한 줄)을 그대로 완료 처리한다.
    대상 줄만 바꾼다 — 이미 [x]로 끝난 줄은 다시 못 바꾼다(중복 완료 방지)."""

    def work() -> str:
        lines = _read_lines(queue_file)
        if not (1 <= line_no <= len(lines)):
            raise ClaimError(f"{line_no}번째 줄이 없습니다(파일에 {len(lines)}줄)")
        text = lines[line_no - 1].rstrip("\n")
        if text.startswith("- [x] "):
            raise ClaimError(f"{line_no}번째 줄은 이미 완료 처리됐습니다: {text!r}")
        if text.startswith("- [~] "):
            m = _claimed_by(text, line_no)
            body = _strip_claim_tag(text, m)[len("- [~] ") :]
        elif text.startswith("- [ ] "):
            body = text[len("- [ ] ") :]
        else:
            raise ClaimError(f"{line_no}번째 줄은 할 일 형식(- [ ] 또는 - [~] )이 아닙니다: {text!r}")
        new_text = "- [x] " + body + f" (끝: {evidence}, 처리: {by})"
        lines[line_no - 1] = new_text + "\n"
        _write_lines_atomic(queue_file, lines)
        return new_text

    return _with_flock(queue_file, work)


def release(queue_file: Path, line_no: int, by: str) -> str:
    def work() -> str:
        lines = _read_lines(queue_file)
        if not (1 <= line_no <= len(lines)):
            raise ClaimError(f"{line_no}번째 줄이 없습니다(파일에 {len(lines)}줄)")
        text = lines[line_no - 1].rstrip("\n")
        m = _claimed_by(text, line_no)
        if m.group("by") != by:
            raise ClaimError(f"{line_no}번째 줄은 '{m.group('by')}'가 맡았습니다 — '{by}'가 놓을 수 없습니다")
        body = _strip_claim_tag(text, m)
        new_text = "- [ ] " + body[len("- [~] ") :]
        lines[line_no - 1] = new_text + "\n"
        _write_lines_atomic(queue_file, lines)
        return new_text

    return _with_flock(queue_file, work)


def restore(queue_file: Path, from_file: Path, by: str) -> str:
    """디스크 판이 비었거나(0바이트) 백업보다 줄 수가 적을 때만 백업 전체 내용으로 되돌린다.
    디스크 판이 백업과 같거나 더 길면(이미 정상이거나 그 뒤로 더 쌓였으면) 거부한다 —
    실수로 새로 쌓인 내용을 백업으로 덮어쓰는 사고를 막는다."""

    def work() -> str:
        if not from_file.is_file():
            raise ClaimError(f"백업 파일이 없습니다: {from_file}")
        backup_text = from_file.read_text(encoding="utf-8")
        if not backup_text.strip():
            raise ClaimError(f"백업 파일이 비어 있습니다: {from_file}")
        backup_lines = backup_text.count("\n") + (0 if backup_text.endswith("\n") else 1)
        current_text = queue_file.read_text(encoding="utf-8") if queue_file.exists() else ""
        current_lines = current_text.count("\n") + (0 if (current_text and not current_text.endswith("\n")) else 0)
        if current_text.strip() and current_lines >= backup_lines:
            raise ClaimError(
                f"디스크 판({current_lines}줄)이 백업({backup_lines}줄)보다 짧지 않습니다 — "
                "되돌리지 않습니다(진행 중인 내용을 덮어쓸 위험)."
            )
        fd, tmp_path = tempfile.mkstemp(
            dir=str(queue_file.parent), prefix=f".{queue_file.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(backup_text)
            os.replace(tmp_path, queue_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"복원 완료: {queue_file} ← {from_file} ({backup_lines}줄, by {by} {ts})"

    return _with_flock(queue_file, work)


def _cmd_list(args: argparse.Namespace) -> int:
    for line_no, text in list_unclaimed(Path(args.file)):
        print(f"{line_no}\t{text}")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    try:
        new_text = claim(Path(args.file), args.line, args.by)
    except ClaimError as e:
        print(f"맡기 실패: {e}", file=sys.stderr)
        return 1
    print(new_text)
    return 0


def _cmd_done(args: argparse.Namespace) -> int:
    try:
        if args.force_line:
            new_text = force_done(Path(args.file), args.line, args.by, args.evidence)
        else:
            new_text = done(Path(args.file), args.line, args.by, args.evidence)
    except ClaimError as e:
        print(f"끝내기 실패: {e}", file=sys.stderr)
        return 1
    print(new_text)
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    try:
        msg = restore(Path(args.file), Path(args.from_file), args.by)
    except ClaimError as e:
        print(f"복원 실패: {e}", file=sys.stderr)
        return 1
    print(msg)
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    try:
        new_text = release(Path(args.file), args.line, args.by)
    except ClaimError as e:
        print(f"놓기 실패: {e}", file=sys.stderr)
        return 1
    print(new_text)
    return 0


def _selftest() -> int:
    """임시 사본 WORK_QUEUE로: ①자율 줄 하나를 두 작업조가 동시에 claim → 하나만 성공
    ②다른 줄·운영자 원문은 한 글자도 안 바뀜 ③done·release가 맡은 사람만 되는지 확인."""
    tmp_dir = tempfile.mkdtemp(prefix="work_claim_selftest_")
    queue_file = Path(tmp_dir) / "WORK_QUEUE.md"
    original = (
        "# WORK_QUEUE 시험 사본\n"
        "- [ ] (자가 점검 2026-11-01) exampleapp: 손대지 않는 줄 1\n"
        "- [ ] (자율) exampleapp: 손대지 않고 맡을 수 있어야 하는 줄\n"
        "- [ ] 운영자 원문 그대로 둬야 하는 줄 — 자율 표시 없음\n"
    )
    queue_file.write_text(original, encoding="utf-8")
    target_line = 3  # "(자율)" 이 있는 줄

    results: list[tuple[str, bool, str]] = []
    lock = threading.Lock()

    def try_claim(name: str) -> None:
        try:
            text = claim(queue_file, target_line, name)
            with lock:
                results.append((name, True, text))
        except ClaimError as e:
            with lock:
                results.append((name, False, str(e)))

    t1 = threading.Thread(target=try_claim, args=("작업조A",))
    t2 = threading.Thread(target=try_claim, args=("작업조B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = sum(1 for _, ok, _ in results if not ok)
    print(f"동시 claim 결과: 성공 {ok_count}건, 실패 {fail_count}건 — {results}")
    assert ok_count == 1, f"동시 claim은 정확히 1건만 성공해야 한다 (실제 {ok_count}건)"
    assert fail_count == 1, f"동시 claim 중 1건은 실패해야 한다 (실제 {fail_count}건)"

    final_lines = queue_file.read_text(encoding="utf-8").splitlines()
    assert final_lines[0] == "# WORK_QUEUE 시험 사본", "제목 줄이 바뀌었다"
    assert final_lines[1] == "- [ ] (자가 점검 2026-11-01) exampleapp: 손대지 않는 줄 1", "1번 줄이 바뀌었다"
    assert final_lines[3] == "- [ ] 운영자 원문 그대로 둬야 하는 줄 — 자율 표시 없음", "4번 줄(자율 표시 없는 줄)이 바뀌었다"
    winner = next(name for name, ok, _ in results if ok)
    assert final_lines[2].startswith("- [~] (자율) exampleapp: 손대지 않고 맡을 수 있어야 하는 줄 (맡음: "), f"맡은 줄 모양이 다르다: {final_lines[2]!r}"
    assert f"맡음: {winner} " in final_lines[2]
    print("PASS: 동시 claim 1건만 성공, 다른 줄은 한 글자도 안 바뀜")

    loser = next(name for name, ok, _ in results if not ok)
    try:
        done(queue_file, target_line, loser, "증거")
        raise AssertionError("맡지 않은 작업조가 done에 성공했다 — 막아야 한다")
    except ClaimError:
        print(f"PASS: {loser}(맡지 않음)은 done을 거부당했다")

    done(queue_file, target_line, winner, "시험 통과 — 가짜 커밋 abc123")
    final_lines = queue_file.read_text(encoding="utf-8").splitlines()
    assert final_lines[2] == "- [x] (자율) exampleapp: 손대지 않고 맡을 수 있어야 하는 줄 (끝: 시험 통과 — 가짜 커밋 abc123)", final_lines[2]
    print("PASS: done이 (맡음: ...) 표시를 지우고 (끝: ...)으로 바꾼다")

    # release 왕복 시험: 새 줄로 다시 맡고 놓아서 원문이 그대로 돌아오는지 본다.
    queue_file.write_text(original, encoding="utf-8")
    claim(queue_file, target_line, "작업조C")
    release(queue_file, target_line, "작업조C")
    restored = queue_file.read_text(encoding="utf-8")
    assert restored == original, "release 뒤 원문과 달라졌다"
    print("PASS: release가 원문을 그대로 되돌린다")

    # force-line 시험: 자율 표시 없는 줄도 반장이 직접 완료 처리할 수 있어야 하고, 다른 줄은 안 바뀌어야 한다.
    queue_file.write_text(original, encoding="utf-8")
    force_line = 4  # "자율 표시 없음" 줄
    before = queue_file.read_text(encoding="utf-8")
    force_done(queue_file, force_line, "반장", "직접 배정 완료 증거")
    after_lines = queue_file.read_text(encoding="utf-8").splitlines()
    assert after_lines[3] == "- [x] 운영자 원문 그대로 둬야 하는 줄 — 자율 표시 없음 (끝: 직접 배정 완료 증거, 처리: 반장)", after_lines[3]
    assert after_lines[0] == before.splitlines()[0], "제목 줄이 바뀌었다"
    assert after_lines[1] == before.splitlines()[1], "1번 줄이 바뀌었다"
    assert after_lines[2] == before.splitlines()[2], "2번 줄이 바뀌었다"
    try:
        force_done(queue_file, force_line, "반장", "다시")
        raise AssertionError("이미 완료된 줄을 force-line으로 다시 완료 처리했다 — 막아야 한다")
    except ClaimError:
        print("PASS: force-line은 이미 [x]인 줄을 다시 처리하지 않는다")
    print("PASS: force-line은 자율 표시 없는 줄도 완료 처리하고 다른 줄은 그대로 둔다")

    print(f"selftest 전부 통과 (임시 폴더 {tmp_dir})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="맡을 수 있는 「(자율)」 할 일 목록")
    p_list.add_argument("--file", default=str(DEFAULT_QUEUE))
    p_list.set_defaults(func=_cmd_list)

    p_claim = sub.add_parser("claim", help="한 줄을 원자적으로 맡는다")
    p_claim.add_argument("--line", type=int, required=True)
    p_claim.add_argument("--by", required=True)
    p_claim.add_argument("--file", default=str(DEFAULT_QUEUE))
    p_claim.set_defaults(func=_cmd_claim)

    p_done = sub.add_parser("done", help="맡은 일을 끝낸다")
    p_done.add_argument("--line", type=int, required=True)
    p_done.add_argument("--by", required=True)
    p_done.add_argument("--evidence", required=True)
    p_done.add_argument("--file", default=str(DEFAULT_QUEUE))
    p_done.add_argument(
        "--force-line",
        action="store_true",
        help="「(자율)」 표시나 claim 표시가 없는 줄(반장이 직접 배정)도 --by·--evidence로 완료 처리",
    )
    p_done.set_defaults(func=_cmd_done)

    p_release = sub.add_parser("release", help="맡은 일을 놓는다(원문 복원)")
    p_release.add_argument("--line", type=int, required=True)
    p_release.add_argument("--by", required=True)
    p_release.add_argument("--file", default=str(DEFAULT_QUEUE))
    p_release.set_defaults(func=_cmd_release)

    p_restore = sub.add_parser("restore", help="디스크 판이 비었거나 더 짧을 때만 백업으로 되돌린다")
    p_restore.add_argument("--from", dest="from_file", required=True)
    p_restore.add_argument("--by", required=True)
    p_restore.add_argument("--file", default=str(DEFAULT_QUEUE))
    p_restore.set_defaults(func=_cmd_restore)

    sub.add_parser("selftest", help="임시 사본으로 동시 claim 등 자체 확인")

    args = parser.parse_args()
    if args.cmd == "selftest":
        return _selftest()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
