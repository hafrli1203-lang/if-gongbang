#!/usr/bin/env python3
"""VM/service health watch. Meant to run on a timer (e.g. every 5 minutes).

Looks only at what happened since the previous run and appends findings to
cfg["log_dir"]/vm_health.jsonl; anything that needs the owner's attention also goes to
cfg["alerts_path"], which the main agent should read and report first. Light by design: a
few journalctl/systemctl/df calls, no network beyond localhost app probes.

Detects: VM freezes (kernel soft lockup / hung task, a known "time jump detected" pattern
from clock-sync daemons), failed systemd --user units matching cfg["service_unit_glob"],
low memory, disk use, and unresponsive/restarting app units (probed over HTTP on the port
found in each unit's ExecStart).

The thresholds here are monitoring defaults, not fixed standards - change them in
gongbang.config.json for your environment.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/hooks")
import gongbang_config  # noqa: E402

MEM_AVAIL_MIN_MB = 2048
DISK_MAX_PCT = 90
APP_TIMEOUT_S = 10
JUMP = re.compile(r"time jump detected \(slept (\d+)s\)")
LOCKUP = re.compile(r"soft lockup - CPU#\d+ stuck for (\d+)s|blocked for more than \d+ seconds")
PORT_RX = re.compile(r"(?:-p |--port |PORT=)(\d{4,5})")


def sh(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def journal(since: str, unit: str | None = None, kernel: bool = False) -> list[str]:
    cmd = ["sudo", "-n", "journalctl", "--since", since, "--no-pager", "-o", "short-iso"]
    if kernel:
        cmd.append("-k")
    if unit:
        cmd += ["-u", unit]
    return [l for l in sh(cmd).splitlines() if "UFW BLOCK" not in l]


def freezes(since: str, time_sync_unit: str) -> list[dict]:
    """One finding per freeze event: kernel stall lines and clock-sync time jumps within
    2 minutes of each other are grouped into a single event."""
    hits = []
    for l in journal(since, kernel=True):
        m = LOCKUP.search(l)
        if m:
            hits.append((l[:19], int(m.group(1)) if m.group(1) else 0, "cpu"))
    for l in journal(since, unit=time_sync_unit):
        m = JUMP.search(l)
        if m:
            hits.append((l[:19], int(m.group(1)), "jump"))
    events: list[dict] = []
    for at, secs, src in sorted(hits):
        t = datetime.fromisoformat(at)
        if events and (t - datetime.fromisoformat(events[-1]["at"])).total_seconds() <= 120:
            e = events[-1]
            e["max_seconds"] = max(e["max_seconds"], secs)
            e["stalled_cpu_reports"] += 1 if src == "cpu" else 0
        else:
            events.append({"kind": "vm_freeze", "at": at, "max_seconds": secs, "stalled_cpu_reports": 1 if src == "cpu" else 0})
    return events


def unit_problems(unit_glob: str) -> list[dict]:
    out = []
    for line in sh(["systemctl", "--user", "list-units", unit_glob, "--all", "--no-legend", "--plain"]).splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        name, active = parts[0], parts[2]
        if active == "failed":
            out.append({"kind": "service_failed", "unit": name})
    return out


def resources() -> list[dict]:
    out = []
    mem = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in open("/proc/meminfo") if ":" in l)}
    avail_mb = mem.get("MemAvailable", 0) // 1024
    if avail_mb < MEM_AVAIL_MIN_MB:
        out.append({"kind": "low_memory", "avail_mb": avail_mb})
    for line in sh(["df", "-P", "/"]).splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and int(p[4].rstrip("%")) >= DISK_MAX_PCT:
            out.append({"kind": "disk_full", "mount": p[5], "pct": int(p[4].rstrip("%"))})
    return out


def app_ports(unit_glob: str) -> dict[str, int]:
    ports: dict[str, int] = {}
    for line in sh(["systemctl", "--user", "list-units", unit_glob, "--state=active", "--no-legend", "--plain"]).splitlines():
        unit = line.split()[0] if line.split() else ""
        m = PORT_RX.search(sh(["systemctl", "--user", "cat", unit]))
        if unit and m:
            ports[unit] = int(m.group(1))
    return ports


def app_problems(unit_glob: str, probe_path: str, prev_restarts: dict[str, int]) -> tuple[list[dict], dict[str, int]]:
    out: list[dict] = []
    for unit, port in sorted(app_ports(unit_glob).items()):
        t0 = time.time()
        code = sh(["curl", "-s", "-o", "/dev/null", "-m", str(APP_TIMEOUT_S), "-w", "%{http_code}",
                   f"http://127.0.0.1:{port}{probe_path}"], timeout=APP_TIMEOUT_S + 5).strip()
        took = round(time.time() - t0, 1)
        if code in ("", "000") or code.startswith("5"):
            out.append({"kind": "app_unresponsive", "unit": unit, "http": code or "none", "seconds": took})
    restarts: dict[str, int] = {}
    for line in sh(["systemctl", "--user", "list-units", unit_glob, "--all", "--no-legend", "--plain"]).splitlines():
        unit = line.split()[0] if line.split() else ""
        n = sh(["systemctl", "--user", "show", unit, "-p", "NRestarts", "--value"]).strip()
        if unit and n.isdigit():
            restarts[unit] = int(n)
            if unit in prev_restarts and int(n) > prev_restarts[unit]:
                out.append({"kind": "app_restarted", "unit": unit, "times": int(n) - prev_restarts[unit]})
    return out, restarts


def run(cfg: dict, state_path: str, log_path: str, alerts_path: str) -> dict:
    now = datetime.now(timezone.utc)
    try:
        state = json.load(open(state_path))
    except Exception:
        state = {}
    since = state.get("last", "-10min")
    unit_glob = cfg.get("service_unit_glob", "app-*.service")
    probe_path = cfg.get("health_probe_path", "/__ops_probe")
    time_sync_unit = cfg.get("time_sync_unit", "systemd-timesyncd")
    apps, restarts = app_problems(unit_glob, probe_path, state.get("restarts", {}))
    found = freezes(since, time_sync_unit) + unit_problems(unit_glob) + resources() + apps
    rec = {"at": now.isoformat(timespec="seconds"), "load1": os.getloadavg()[0], "findings": found}
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    ongoing_kinds = {"app_unresponsive", "service_failed", "low_memory", "disk_full"}
    key = lambda x: x["kind"] + ":" + str(x.get("unit") or x.get("mount") or "")
    prev_open = set(state.get("open", []))
    cur_open = {key(x) for x in found if x["kind"] in ongoing_kinds}
    new = [x for x in found if x["kind"] not in ongoing_kinds or key(x) not in prev_open]
    cleared = sorted(prev_open - cur_open)
    stamp = now.astimezone().strftime('%m-%d %H:%M')
    if new or cleared:
        os.makedirs(os.path.dirname(alerts_path), exist_ok=True)
        with open(alerts_path, "a", encoding="utf-8") as f:
            for x in new:
                f.write(f"- [ ] {stamp} {json.dumps(x, ensure_ascii=False)}\n")
            for k in cleared:
                f.write(f"- [x] {stamp} 복구됨: {k}\n")
    json.dump({"last": now.strftime("%Y-%m-%d %H:%M:%S UTC"), "restarts": restarts, "open": sorted(cur_open)},
              open(state_path, "w"))
    return rec


def main() -> int:
    cfg = gongbang_config.load()
    state_path = os.path.join(cfg["log_dir"], "vm_watch.state.json")
    log_path = os.path.join(cfg["log_dir"], "vm_health.jsonl")
    rec = run(cfg, state_path, log_path, cfg["alerts_path"])
    print(f"vm_watch: {len(rec['findings'])} finding(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
