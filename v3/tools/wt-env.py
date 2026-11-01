#!/usr/bin/env python3
# Generated from private source at 5b12eebee24334c8a78ac755dc179c5b1c3f0e47; do not edit by hand (see harness/export_v3_public.py).
"""Environment helper for the worktree tools (wt-*.sh). Never prints a URL, password or env value.

Reads an app's env file (.env.local, else .env) and runs a command with a derived environment, so database
addresses only ever live in the child's environment (psql takes PG* variables; its error text then shows no URL).

usage:
  wt-env.py pg    --env-file F [--key DATABASE_URL] [--db NAME] -- cmd...   # PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
  wt-env.py app   --app-dir D --db NAME [--verify-env J] [--set K=V ...] -- cmd...
                  # DATABASE_URL, TEST_DATABASE_URL and every other *DATABASE*URL* key -> slot DB NAME (each key keeps
                  # its own host/user); other env-file keys -> values from J (verify-env.json) or fake "x-<KEY>"
  wt-env.py user  --env-file F [--key DATABASE_URL]   # prints only the role name (needed for createdb -O)
  wt-env.py dbname --env-file F [--key DATABASE_URL]  # prints only the database name
  wt-env.py has   --env-file F [--key DATABASE_URL]   # exit 0 when the key holds a postgres URL
  wt-env.py real  --env-file F -- cmd...              # the env file itself loaded into the child (wt-migrate.sh only)
Every child starts from an allow-list of the caller's environment (BASE_KEYS, LC_*, WT_*); nothing else is inherited.
"""
import json
import os
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit, parse_qs

SKIP_KEYS = {"NODE_ENV", "PORT"}
# Internal-app address keys (configured via WT_INTERNAL_URL_KEYS, comma-separated): always pointed at a dead
# port in a copy, so a copy's own code defaults never reach a live internal service by accident.
# Set even when the env file lacks them; nothing overrides them.
INTERNAL_URL_KEYS = tuple(k for k in os.environ.get("WT_INTERNAL_URL_KEYS", "").split(",") if k)
DEAD_URL = "http://127.0.0.1:1"
TOKEN_FILE_KEY = re.compile(r"_TOKEN_(FILE|PATH)$")
LIVE_DATA = os.environ.get("WT_LIVE_DATA_ROOT", "")   # the apps' live data root (adapter, optional): a copy
# gets its own folder under <copy>/.verify/data/<KEY> for any value under it; unset disables this relocation.
DB_KEY = re.compile(r"DATABASE.*URL|URL.*DATABASE")


def die(msg: str, code: int = 2) -> None:
    print(f"wt-env: {msg}", file=sys.stderr)
    sys.exit(code)


# Children start from an allow-list of the caller's environment, never the whole of it: a calling session
# carries API keys and tokens that must not reach a work copy (Codex 11-01 P0). Only what running a tool needs passes.
BASE_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "COLORTERM", "TMPDIR", "TZ", "PWD",
    "NODE_OPTIONS", "NODE_PATH", "CI", "NEXT_TELEMETRY_DISABLED", "FORCE_COLOR", "NO_COLOR", "DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PLAYWRIGHT_BROWSERS_PATH", "IF_BUILD_DIR",
}


def base_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in BASE_KEYS or k.startswith(("LC_", "WT_"))}


def env_file_of(app_dir: str) -> str:
    for name in (".env.local", ".env"):
        p = os.path.join(app_dir, name)
        if os.path.isfile(p):
            return p
    return ""


def parse_env(path: str) -> dict:
    out: dict = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                out[key] = val
    return out


def is_pg_url(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def with_db(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + quote(db, safe=""), parts.query, parts.fragment))


def pg_vars(url: str, db: str | None) -> dict:
    parts = urlsplit(url)
    out = {}
    if parts.hostname:
        out["PGHOST"] = unquote(parts.hostname)
    if parts.port:
        out["PGPORT"] = str(parts.port)
    if parts.username:
        out["PGUSER"] = unquote(parts.username)
    if parts.password is not None:
        out["PGPASSWORD"] = unquote(parts.password)
    name = db if db else unquote(parts.path.lstrip("/"))
    if name:
        out["PGDATABASE"] = name
    q = parse_qs(parts.query)
    if "sslmode" in q:
        out["PGSSLMODE"] = q["sslmode"][0]
    return out


def fake_for(key: str, val: str, copy_port: str) -> str:
    """A fake of the same shape as the real value (zod number/boolean/url checks), judged in memory, never printed."""
    v = val.strip()
    if v.startswith(LIVE_DATA):
        return v   # relocated into the copy by the last pass in cmd_app
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return copy_port if key.endswith("_PORT") and copy_port else "0"
    if v.lower() in ("true", "false"):
        return "false"
    if re.match(r"https?://", v, re.IGNORECASE):
        return DEAD_URL
    return f"x-{key}"


def split_cmd(argv: list) -> tuple:
    if "--" not in argv:
        die("명령 앞에 -- 가 필요합니다")
    i = argv.index("--")
    return argv[:i], argv[i + 1:]


def opts(args: list) -> dict:
    out: dict = {"set": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--env-file", "--key", "--db", "--app-dir", "--verify-env"):
            if i + 1 >= len(args):
                die(f"{a} 값이 없습니다")
            out[a[2:]] = args[i + 1]
            i += 2
        elif a == "--set":
            out["set"].append(args[i + 1])
            i += 2
        else:
            die(f"모르는 옵션: {a}")
    return out


def url_from(o: dict) -> str:
    path = o.get("env-file") or ""
    if not path or not os.path.isfile(path):
        die("환경 파일이 없습니다")
    key = o.get("key", "DATABASE_URL")
    url = parse_env(path).get(key, "")
    if not is_pg_url(url):
        die(f"{key} 가 postgres 주소가 아닙니다(값은 출력하지 않음)")
    return url


def cmd_pg(args: list) -> None:
    head, cmd = split_cmd(args)
    o = opts(head)
    env = base_env()
    env.update(pg_vars(url_from(o), o.get("db")))
    os.execvpe(cmd[0], cmd, env)


def cmd_app(args: list) -> None:
    head, cmd = split_cmd(args)
    o = opts(head)
    app_dir = o.get("app-dir") or die("--app-dir 필요")
    db = o.get("db")
    envf = env_file_of(app_dir)
    file_env = parse_env(envf) if envf else {}
    fakes: dict = {}
    vpath = o.get("verify-env")
    if vpath and os.path.isfile(vpath):
        with open(vpath, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            die("verify-env.json 은 객체여야 합니다")
        fakes = {str(k): str(v) for k, v in data.items()}
    # inherited DB settings never reach the child: a caller (a calling session, a shell) may carry a production DATABASE_URL
    env = base_env()
    sets = dict(kv.partition("=")[::2] for kv in o["set"])
    copy_port = sets.get("PORT", "")
    for key in file_env:
        if key in SKIP_KEYS:
            continue
        val = file_env[key]
        if DB_KEY.search(key) and is_pg_url(val):
            env[key] = with_db(val, db) if db else f"x-{key}"
        elif key in INTERNAL_URL_KEYS:
            env[key] = DEAD_URL
        elif TOKEN_FILE_KEY.search(key):
            env[key] = f"/nonexistent/wt-{key}"
        else:
            env[key] = fake_for(key, val, copy_port)
    # verify-env.json: every key in it (also keys the env file lacks), except DB settings (PG*, *DATABASE*URL*)
    for key, val in fakes.items():
        if key not in SKIP_KEYS and not key.startswith("PG") and not DB_KEY.search(key):
            env[key] = val
    # last, over the merged environment (inherited, env file, verify-env): internal-app addresses and token files are
    # never live in a copy, whoever set them (Codex 11-01)
    for key in list(env):
        if TOKEN_FILE_KEY.search(key):
            env[key] = f"/nonexistent/wt-{key}"
    for key in INTERNAL_URL_KEYS:
        env[key] = DEAD_URL
    # any non-DB value pointing into the live data root is moved into the copy
    copy_dir = sets.get("WT_COPY") or os.getcwd()
    for key in list(env):
        if DB_KEY.search(key) or not str(env[key]).startswith(LIVE_DATA):
            continue
        local = os.path.join(copy_dir, ".verify", "data", key)
        base = os.path.basename(str(env[key]).rstrip("/"))
        if "." in base:   # a file path (state.json): keep the file name inside the copy's folder, create only the folder
            os.makedirs(local, exist_ok=True)
            local = os.path.join(local, base)
        else:
            os.makedirs(local, exist_ok=True)
        env[key] = local
    base = file_env.get("DATABASE_URL", "")
    if db and is_pg_url(base):
        env["DATABASE_URL"] = with_db(base, db)
        env["TEST_DATABASE_URL"] = env["DATABASE_URL"]
    elif db:
        die("앱 환경 파일에 postgres DATABASE_URL 이 없습니다")
    for kv in o["set"]:
        k, _, v = kv.partition("=")
        env[k] = v
    os.execvpe(cmd[0], cmd, env)


def main() -> None:
    if len(sys.argv) < 2:
        die("하위 명령: pg | app | user | has")
    sub, rest = sys.argv[1], sys.argv[2:]
    if sub == "pg":
        cmd_pg(rest)
    elif sub == "app":
        cmd_app(rest)
    elif sub == "user":
        print(unquote(urlsplit(url_from(opts(rest))).username or ""))
    elif sub == "real":
        # the app's own env file loaded into the child (production migrate in wt-migrate.sh); values never printed
        head, cmd = split_cmd(rest)
        o = opts(head)
        path = o.get("env-file") or ""
        if not os.path.isfile(path):
            die("환경 파일이 없습니다")
        env = base_env()
        env.update(parse_env(path))
        os.execvpe(cmd[0], cmd, env)
    elif sub == "dbname":
        print(unquote(urlsplit(url_from(opts(rest))).path.lstrip("/")))
    elif sub == "has":
        o = opts(rest)
        path = o.get("env-file") or ""
        ok = bool(path) and os.path.isfile(path) and is_pg_url(parse_env(path).get(o.get("key", "DATABASE_URL"), ""))
        sys.exit(0 if ok else 1)
    else:
        die(f"모르는 하위 명령: {sub}")


if __name__ == "__main__":
    main()
