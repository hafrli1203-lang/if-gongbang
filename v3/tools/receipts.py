#!/usr/bin/env python3
# Generated from private source at 495481ceddef11d8ccd4438d8948d5cc71c484d2; do not edit by hand (see harness/export_v3_public.py).
"""Deploy receipts (gaps 190/183): one JSON file per deploy under <state>/<app>.receipts/, shared by deploy.sh and
wt-land.sh so both judge a receipt the same way.

usage:
  receipts.py self [pid]                prints "boot_id pid_start" of <pid> (default: the caller) — deploy.sh records them
  receipts.py set <file> k=v ...        atomic merge; "step:<name>=<sec>" goes into steps{}; integers stay integers
  receipts.py mark-stale <dir>          a "started" receipt whose process is gone becomes "unknown"
  receipts.py blocking <dir>            prints receipts that block a land: unknown, or started with the process gone,
                                        and not acknowledged
  receipts.py ack <file>                acknowledged by a person (status started -> unknown, acked=<time>)
  receipts.py prune <dir> <keep>        deletes the oldest receipts beyond <keep>, but only passed/failed or acknowledged
                                        ones: an unacknowledged unknown is evidence and is never pruned

A process counts as alive only when it runs under the same boot (/proc/sys/kernel/random/boot_id) with the same
start time (/proc/<pid>/stat field 22): a pid reused after a reboot or by another process is not the deploy.
"""
import datetime
import glob
import json
import os
import sys


def boot_id() -> str:
    try:
        return open("/proc/sys/kernel/random/boot_id").read().strip()
    except OSError:
        return ""


def start_time(pid: int) -> str:
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return ""
    return stat.rsplit(")", 1)[1].split()[19]  # field 22 counted after the "(comm)" field


def alive(d: dict) -> bool:
    pid = int(d.get("pid") or 0)
    if pid <= 0 or d.get("boot_id") != boot_id():
        return False
    return bool(d.get("pid_start")) and start_time(pid) == str(d.get("pid_start"))


def load(p: str) -> dict:
    try:
        d = json.load(open(p))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(p: str, d: dict) -> None:
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def files(directory: str) -> list:
    return sorted(glob.glob(os.path.join(directory, "*.json")), key=os.path.getmtime)


def blocking(d: dict) -> bool:
    if d.get("acked"):
        return False
    return d.get("status") == "unknown" or (d.get("status") == "started" and not alive(d))


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "self":
        print(boot_id(), start_time(int(args[0]) if args else os.getppid()))
    elif cmd == "set":
        p = args[0]
        d = load(p)
        for kv in args[1:]:
            k, _, v = kv.partition("=")
            if k.startswith("step:"):
                d.setdefault("steps", {})[k[5:]] = float(v)
            else:
                d[k] = int(v) if v.lstrip("-").isdigit() and len(v) < 12 else v
        save(p, d)
    elif cmd == "mark-stale":
        for p in files(args[0]):
            d = load(p)
            if d.get("status") == "started" and not alive(d):
                d.update(status="unknown", note="process gone before it finished")
                save(p, d)
                print(f"이전 배포 결과 모름으로 표시: {os.path.basename(p)}")
    elif cmd == "blocking":
        for p in files(args[0]):
            if blocking(load(p)):
                print(p)
    elif cmd == "ack":
        d = load(args[0])
        if d.get("status") == "started":
            d["status"] = "unknown"
        d["acked"] = datetime.datetime.now().astimezone().isoformat()
        save(args[0], d)
    elif cmd == "prune":
        keep = int(args[1])
        all_files = files(args[0])
        for p in all_files[: max(0, len(all_files) - keep)]:
            d = load(p)
            if d.get("status") in ("passed", "failed") or d.get("acked"):
                os.remove(p)
    else:
        sys.exit(f"모르는 명령: {cmd}")


if __name__ == "__main__":
    main()
