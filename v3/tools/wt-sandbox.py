#!/usr/bin/env python3
# Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
"""Runs one work-copy command inside bubblewrap (gap 23). Called by wt-run.sh after wt-env.py built the environment
(so the app's env file is read outside the sandbox and never inside it). WT_SANDBOX_BWRAP=0 in wt-run.sh skips this.

usage: wt-sandbox.py --copy <copy> --data <dir> --app <app> [--net on|off] -- <command...>

Inside the sandbox (later mounts cover earlier ones)
  read-only  the whole filesystem, except what is hidden below
  hidden     a private /tmp; /run except /run/systemd/resolve (DNS); all of /srv (other apps, other copies, the app's
             own folder, data, backups, archives, logs) except the copy, _shared, <dir> and the app's own .git (below);
             the home folder except .local/{bin,lib,node22}, .cache/ms-playwright, .gitconfig, inbox (read-only) and
             .npm; every .env* in the copy or _shared (an unreadable /dev/null in its place)
  read-only  <app>/.git (the DIRECTORY, not the copy's own working-tree files) — a `git worktree add` copy's own
             .git is a FILE pointing OUTSIDE itself ("gitdir: ${WT_APPS_ROOT}/<app>/.git/worktrees/<name>"); objects,
             refs and that worktrees/<name> state all live in the app's real .git, which hiding all of ${WT_APPS_ROOT}
             above would otherwise hide too, breaking every git command run inside the sandbox (observed once: an app's own
             backup script's `git bundle`/`git log` died "not a git repository")
  writable   the copy, <dir> (${WT_DATA}/<app>/<name>, this copy's own), ~/.npm (npx package cache), /tmp
  read-only  the copy's node_modules: its files are hard links shared with the app folder, so an in-place write
             would change the running app (its .cache/.vite/.vite-temp folders get a private tmpfs)
  own        pid and ipc namespaces: processes outside are invisible (/proc/<pid>/root cannot reach the host)
--net off (every foreground command, wt-run.sh): a new network namespace with nothing but loopback. Database
  addresses on loopback in the environment (any *DATABASE*URL*, e.g. 127.0.0.1:5432) still work: a helper inside
  opens those listening ports in the sandbox's loopback and hands them over a socket file in <dir> to the relay
  outside, which accepts on them and connects to the real server, only for the database names in those addresses
  (the slot test DBs; the PostgreSQL startup is read and checked). Nothing else gets out. One relay hop, not two:
  a second hop inside tripled the cost per query (2000 x "select 1": 2.0 s direct, 6.3 s with two Python hops).
A sandbox that fails to start is a failure (exit code of bwrap); it is never retried without the sandbox.
"""
import glob
import os
import re
import socket
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit

APPS = os.environ.get("WT_APPS", "${WT_APPS_ROOT}")
WT_ROOT = os.environ.get("WT_ROOT", f"{APPS}/_wt")
HOME = os.path.expanduser("~")
DB_KEY = re.compile(r"DATABASE.*URL|URL.*DATABASE")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


SSL_REQUEST, GSS_REQUEST, CANCEL_REQUEST = 80877103, 80877104, 80877102


def pg_gate(client: socket.socket, allowed: set) -> bytes:
    """Reads the PostgreSQL startup of one connection and returns the bytes to forward, or raises OSError when the
    database is not one of `allowed` (the slot test DBs): the relay must never reach another database with the same
    credentials (Codex 11-02). SSL/GSS requests are answered "N" here so the startup that follows is readable."""
    while True:
        head = recv_exact(client, 8)
        length = int.from_bytes(head[:4], "big")
        code = int.from_bytes(head[4:8], "big")
        # every shape is checked exactly: an SSL/GSS request is 8 bytes, a cancel 16, a startup protocol 3.x; anything
        # else is refused (Codex 11-02: a 35-byte "SSLRequest" was taken by the server as SSL and passed unchecked)
        if code in (SSL_REQUEST, GSS_REQUEST):
            if length != 8:
                raise OSError("bad ssl request")
            client.sendall(b"N")
            continue
        if code == CANCEL_REQUEST:
            if length != 16:
                raise OSError("bad cancel request")
            return head + recv_exact(client, 8)
        if code >> 16 != 3 or length < 8 or length > 10000:
            raise OSError("bad startup")
        body = recv_exact(client, length - 8)
        parts = body.split(b"\0")
        params = dict(zip(parts[0::2], parts[1::2]))
        db = (params.get(b"database") or params.get(b"user") or b"").decode("utf-8", "replace")
        if db not in allowed:
            raise OSError(f"database not allowed through the relay")
        return head + body


def nodelay(sock: socket.socket) -> None:
    """TCP_NODELAY on the relay's TCP ends: PostgreSQL traffic is many small request/answer messages, and Nagle plus
    delayed ACK on a relayed hop made a query-heavy test 2.4x slower (search pipeline 33 s vs 14 s, 11-02)."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def serve(listener: socket.socket, connect, allowed: set | None = None) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            nodelay(client)
            first = pg_gate(client, allowed) if allowed is not None else b""
            upstream = connect()
            nodelay(upstream)
            if first:
                upstream.sendall(first)
        except OSError:
            client.close()
            continue
        for x, y in ((client, upstream), (upstream, client)):
            threading.Thread(target=pipe, args=(x, y), daemon=True).start()


def db_targets() -> tuple:
    """(host, port) of every loopback database address in the environment (IPv6 kept as IPv6), and the database
    names those addresses name — the only ones the relay lets through."""
    out, names = set(), set()
    for k, v in os.environ.items():
        if DB_KEY.search(k) and v.startswith(("postgres://", "postgresql://")):
            u = urlsplit(v)
            if u.hostname in LOOPBACK:
                out.add(("::1" if u.hostname == "::1" else "127.0.0.1", u.port or 5432))
                name = unquote(u.path.lstrip("/"))
                if name:
                    names.add(name)
    return sorted(out), names


PRUNE = {"node_modules", ".git"}


def env_files(*roots: str) -> list:
    """Every .env* file under the visible roots, any depth (node_modules, .git and .next* build folders pruned)."""
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in PRUNE and not d.startswith(".next")]
            found += [os.path.join(dirpath, f) for f in filenames if f.startswith(".env")]
    return sorted(set(found))


def bwrap_args(copy: str, data: str, app: str, net_off: bool) -> list:
    shared_dirs = sorted({os.path.join(APPS, "_shared"), os.path.dirname(os.path.abspath(__file__))})
    a = ["bwrap", "--die-with-parent", "--unshare-pid", "--unshare-ipc",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    # Hide whole trees, then bring back only what a command needs (later mounts cover earlier ones):
    #  /tmp   private: the host /tmp holds live sockets (tmux, ssh-agent) that would be a way out
    #  /run   host sockets (PostgreSQL, D-Bus, systemd, ...) ignore --unshare-net; DNS comes back below
    #  apps   other apps, other copies, and the app's own folder (untracked files there can be credentials,
    #         e.g. a credential file an app keeps untracked in its own data folder; the copy holds all tracked code)
    #  /srv   all of it (backups, archives and logs there hold copies of production secrets — Codex 11-02); the copy,
    #         _shared and this copy's data folder come back below
    #  home
    for d in ("/tmp", "/run", "/srv", APPS, HOME):
        if os.path.isdir(d):
            a += ["--tmpfs", d]
    if os.path.isdir("/run/systemd/resolve"):   # resolv.conf points here
        a += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]
    # _shared (file: a shared internal package) and this script's own folder, read-only
    for d in shared_dirs:
        if os.path.isdir(d):
            a += ["--ro-bind", d, d]
    # The ops/harness folder (read-only, adapter path WT_OPS_HARNESS_DIR): an app's own test suite may
    # import a test-trust check module from there. Only that one subfolder is ever exposed, never the
    # rest of the ops workspace (plans, task queue, etc.) - unset means nothing is exposed.
    harness_dir = os.environ.get("WT_OPS_HARNESS_DIR", "")
    if os.path.isdir(harness_dir):
        a += ["--ro-bind", harness_dir, harness_dir]
    # team-lead 2026-11-02 (실측: an app's own backup script, git bundle/log inside a copy died "not a git
    # repository") — a `git worktree add` copy's own .git is a FILE, "gitdir: ${WT_APPS_ROOT}/<app>/.git/worktrees/
    # <name>", pointing OUTSIDE the copy into the app's real .git (objects/refs/config live there; worktrees/
    # <name>/commondir points back to it too) — hiding all of ${WT_APPS_ROOT}/<app> above hides that target as well, so
    # every git command inside the sandbox failed, not just backup.sh's. Read-only, the .git DIRECTORY only — the
    # app's own working-tree files (src/, etc.) stay hidden, this never exposes them.
    app_dir = os.path.join(APPS, app)
    app_git_dir = os.path.join(app_dir, ".git")
    if os.path.isdir(app_git_dir):
        # a plain subdirectory inside the one big ${WT_APPS_ROOT} tmpfs is not its own mount point, so --remount-ro
        # below could not find it ("Unable to find ... in mount table") - give the app folder its OWN tmpfs mount
        # first (still empty/hidden otherwise), then the nested .git ro-bind, then lock that tmpfs back to
        # read-only so nothing can be written next to the exposed .git (a write there would only ever land in the
        # sandbox's own ephemeral tmpfs, never on the host - this is about keeping the mount's PURPOSE narrow:
        # "read-only access to .git", not "a writable app folder").
        a += ["--tmpfs", app_dir, "--ro-bind", app_git_dir, app_git_dir, "--remount-ro", app_dir]
    # read-only from home, by name only (~/.local/share and ~/.cache hold CLI auth tokens): installed tools, browser
    # binaries for tests, git identity, and the company library originals (inbox) some app tests verify by hash
    for name in (".local/bin", ".local/lib", ".local/node22", ".cache/ms-playwright", ".gitconfig", "inbox"):
        p = os.path.join(HOME, name)
        if os.path.exists(p):
            a += ["--ro-bind", p, p]
    # writable: this copy's own data folder (another copy's relay socket is never visible), the npx cache, the copy
    for d in (data, os.path.join(HOME, ".npm")):
        os.makedirs(d, exist_ok=True)
        a += ["--bind", d, d]
    a += ["--bind", copy, copy]
    nm = os.path.join(copy, "node_modules")
    if os.path.isdir(nm) and not os.path.islink(nm):
        a += ["--ro-bind", nm, nm]
        # tools write their caches under node_modules (vitest: .vite-temp, .vite; babel/next: .cache). The copy's
        # directories are its own (cp -al links files only), so creating them here leaves the app folder untouched.
        for c in (".cache", ".vite", ".vite-temp"):
            os.makedirs(os.path.join(nm, c), exist_ok=True)
            a += ["--tmpfs", os.path.join(nm, c)]
    for f in env_files(copy, *shared_dirs):
        a += ["--ro-bind", "/dev/null", f]
    if net_off:
        a += ["--unshare-net"]
    return a + ["--chdir", os.getcwd()]


def code(rc: int) -> int:
    return 128 - rc if rc < 0 else rc   # a child killed by signal n exits 128+n, like a shell (Codex 11-02)


def take_listeners(ctrl: socket.socket, connect, allowed: set) -> None:
    """Receives the listening sockets the helper opened inside the sandbox's network and relays each of them."""
    while True:
        try:
            conn, _ = ctrl.accept()
        except OSError:
            return
        try:
            _, fds, _, _ = socket.recv_fds(conn, 16, 8)
        except OSError:
            conn.close()
            continue
        for fd in fds:
            ls = socket.socket(fileno=fd)
            if ls.family not in (socket.AF_INET, socket.AF_INET6) or ls.type != socket.SOCK_STREAM:
                ls.close()
                continue
            threading.Thread(target=serve, args=(ls, connect, allowed), daemon=True).start()
        try:
            conn.sendall(b"ok")
        except OSError:
            pass
        conn.close()


def outer(copy: str, data: str, app: str, net_off: bool, cmd: list) -> int:
    inner = []
    listeners = []
    if net_off:
        targets, allowed = db_targets()
        for host, port in targets:
            path = os.path.join(data, f".dbrelay-{os.getpid()}-{port}{'-6' if ':' in host else ''}.sock")
            if os.path.exists(path):
                os.unlink(path)
            ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ls.bind(path)
            ls.listen(8)
            listeners.append((ls, path))
            threading.Thread(target=take_listeners,
                             args=(ls, lambda h=host, p=port: socket.create_connection((h, p)), allowed),
                             daemon=True).start()
            inner += ["--relay", f"{path}:{port}"]
    if inner:
        cmd = [sys.executable, os.path.abspath(__file__), "--inner", *inner, "--", *cmd]
    try:
        return code(subprocess.run(bwrap_args(copy, data, app, net_off) + ["--", *cmd]).returncode)
    finally:
        for ls, path in listeners:
            ls.close()
            try:
                os.unlink(path)
            except OSError:
                pass


def inner(relays: list, cmd: list) -> int:
    """Inside the sandbox: opens 127.0.0.1:<port> (and ::1 when it exists) in this network namespace and hands the
    listening sockets to the relay outside; nothing of this process stays in the data path."""
    ports = set()
    for spec in relays:
        path, _, port_s = spec.rpartition(":")
        port = int(port_s)
        if port in ports:   # the same port on 127.0.0.1 and ::1: one set of listeners serves both
            continue
        ports.add(port)
        socks = []
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                ls = socket.socket(family, socket.SOCK_STREAM)
                ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ls.bind((host, port))
                ls.listen(128)
                socks.append(ls)
            except OSError:
                continue   # ::1 may not exist in the new namespace
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(path)
            socket.send_fds(c, [b"L"], [x.fileno() for x in socks])
            if c.recv(2) != b"ok":
                sys.exit("DB 중계 연결 실패")
        for x in socks:
            x.close()
    return code(subprocess.run(cmd).returncode)


def main() -> None:
    args = sys.argv[1:]
    if "--" not in args:
        sys.exit("사용: wt-sandbox.py --copy <사본> --data <폴더> --app <앱> [--net on|off] -- <명령...>")
    i = args.index("--")
    head, cmd = args[:i], args[i + 1:]
    if head[:1] == ["--inner"]:
        relays = [head[j + 1] for j, x in enumerate(head) if x == "--relay"]
        sys.exit(inner(relays, cmd))
    opts = dict(zip(head[::2], head[1::2]))
    copy, data, app = opts.get("--copy"), opts.get("--data"), opts.get("--app")
    if not copy or not data or not app or not cmd:
        sys.exit("사용: wt-sandbox.py --copy <사본> --data <폴더> --app <앱> [--net on|off] -- <명령...>")
    os.makedirs(data, exist_ok=True)
    sys.exit(outer(os.path.realpath(copy), os.path.realpath(data), app, opts.get("--net") == "off", cmd))


if __name__ == "__main__":
    main()
