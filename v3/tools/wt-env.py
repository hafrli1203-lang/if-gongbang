#!/usr/bin/env python3
# Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
"""Environment helper for the worktree tools (wt-*.sh). Never prints a URL, password or env value.

Reads an app's env file (.env.local, else .env) and runs a command with a derived environment, so database
addresses only ever live in the child's environment (psql takes PG* variables; its error text then shows no URL).

usage:
  wt-env.py pg    --env-file F [--key DATABASE_URL] [--db NAME] -- cmd...   # PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
  wt-env.py app   --app-dir D --db NAME [--verify-env J] [--set K=V ...] -- cmd...
                  # DATABASE_URL, TEST_DATABASE_URL and every other *DATABASE*URL* key -> slot DB NAME (each key keeps
                  # its own host/user); other env-file keys -> values from J (verify-env.json) or fake "x-<KEY>"
                  # --role-file R: every DB address uses the slot role in R instead (apps whose role is a superuser)
  wt-env.py role-sql --file R --app A                # creates R (0600, random password, role wt_<a>_<hash>) when missing;
                  # prints the SQL that makes it a plain login role with that password (piped into psql, never shown)
  wt-env.py users --env-file F [--admin-env A]       # prints only the role names of every *DATABASE*URL* address;
                  # with A: exit 3 unless every address is on A's server with plain parameters (see unpinned)
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
# sqlite-file database settings (not a postgres URL): *_DB_PATH, anything with SQLITE in the name. Relocated into the
# copy's own data folder unconditionally (not only when the value already starts with LIVE_DATA) so a copy never
# opens the live sqlite file even if an env default or override points somewhere else (Codex 11-02, L1-0230 class).
SQLITE_KEY = re.compile(r"_DB_PATH$|SQLITE", re.IGNORECASE)


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


# code-default sqlite paths (observed on one app class): the app has no .env key at all for its sqlite
# path - only a code-level default (e.g. `const DEFAULT_DB_PATH = "<data-root>/<app>/app.sqlite"`, then
# a zod schema defaulting to it). wt-sandbox hiding <data-root>/<app> was the only
# protection for that case; this reads the same source to learn the key name and default path (Codex 11-02).
_SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "out", "coverage", ".venv", "__pycache__", ".cache",
              "logs", "tmp", "vendor", "target", ".verify"}
_CONST_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\"']([^\"']+)[\"']")
_DEFAULT_RE = re.compile(
    r"([A-Z][A-Z0-9_]*(?:_DB_PATH|SQLITE[A-Z0-9_]*))\s*:[^\n]{0,200}?\bdefault\(\s*(?:[\"']([^\"']+)[\"']|([A-Za-z_][A-Za-z0-9_]*))\s*\)",
    re.IGNORECASE)


def sqlite_defaults_of(app_dir: str) -> dict:
    """{KEY: default path} learned from source (not loaded or executed) for every *_DB_PATH/SQLITE key this app's own
    config code gives a default, bounded so a worktree scan stays fast and safe (text read only, no node_modules)."""
    out: dict = {}
    seen_files = 0
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith((".js", ".ts", ".mjs", ".cjs")) or name.endswith((".test.js", ".test.ts", ".d.ts")):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > 300_000:
                    continue
            except OSError:
                continue
            seen_files += 1
            if seen_files > 2000:
                return out
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            if "SQLITE" not in text.upper() and "_DB_PATH" not in text.upper():
                continue
            consts = {m.group(1): m.group(2) for m in _CONST_RE.finditer(text)}
            for m in _DEFAULT_RE.finditer(text):
                key, lit, var = m.group(1), m.group(2), m.group(3)
                val = lit if lit is not None else consts.get(var or "", "")
                if key.upper() not in out and SQLITE_KEY.search(key) and val and not is_pg_url(val):
                    out[key.upper()] = val
    return out


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


# query parameters that would override the address's database or login (libpq lets ?dbname=/?user=/?password= win)
DB_PARAMS = {"dbname"}
AUTH_PARAMS = {"user", "password", "passfile", "service", "servicefile", "sslpassword"}


def query_pairs(query: str) -> list:
    """(raw segment, key, value) of a URL query, decoded the way libpq does: %XX only, "+" stays "+" (parse_qsl would
    read "+" as a space and see another role than the one libpq logs in as; Codex 11-02)."""
    out = []
    for seg in query.split("&"):
        if seg:
            k, _, v = seg.partition("=")
            out.append((seg, unquote(k), unquote(v)))
    return out


def drop_params(query: str, names: set) -> str:
    """The query without the named parameters; every other segment kept byte for byte (no re-encoding)."""
    return "&".join(seg for seg, k, _ in query_pairs(query) if k.lower() not in names)


def with_role(url: str, role: dict) -> str:
    """The same address logged in as the slot role: user and password replaced, login parameters in the query removed
    (Codex 11-02: ?user=postgres&password=... would otherwise win over the new user), host/port/other options kept."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    cred = quote(role["user"], safe="") + ":" + quote(role["password"], safe="")
    return urlunsplit((parts.scheme, cred + "@" + host, parts.path, drop_params(parts.query, AUTH_PARAMS),
                       parts.fragment))


def url_users(env: dict) -> list:
    """Role names of every postgres address under a *DATABASE*URL* key (user part and ?user= both)."""
    out = set()
    for key, val in env.items():
        if not (DB_KEY.search(key) and is_pg_url(val)):
            continue
        parts = urlsplit(val)
        if parts.username:
            out.add(unquote(parts.username))
        out.update(v for _, k, v in query_pairs(parts.query) if k.lower() == "user" and v)
    return sorted(out)


def addrs_of(host: str) -> set:
    """The addresses libpq may try for a host as written: localhost is both loopbacks, an IP literal is itself, any other
    name is unknown (empty)."""
    host = host.lower().strip("[]")
    if host == "localhost":
        return {"127.0.0.1", "::1"}
    try:
        import ipaddress
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        return set()


# The only query parameters a database address of an app may carry for the role check to trust it. Anything else (user,
# password, host, port, service, ...), a "+" (libpq keeps it, node-postgres reads a space) or a "#" fragment (libpq reads
# parameters after it) makes the address ambiguous, and the check stops instead of guessing (Codex 11-02).
SAFE_PARAMS = {"sslmode", "schema", "application_name", "connect_timeout", "options"}


def unpinned(env: dict, admin_url: str) -> str:
    """Why an app database address cannot be vouched for by the admin server's catalog, or "" when every one is on the
    admin address (same host as written in the admin URL, or one of its addresses, and the same port) with plain
    parameters. The admin login is only ever sent where it already goes (wt_pg_admin)."""
    admin = urlsplit(admin_url)
    allowed = addrs_of(admin.hostname or "")
    if not allowed:
        return "관리 DB 주소의 호스트를 정할 수 없음"
    for key, val in env.items():
        if not (DB_KEY.search(key) and is_pg_url(val)):
            continue
        parts = urlsplit(val)
        if "#" in val or "+" in parts.query:
            return f"{key}: # 또는 + 가 있는 주소"
        bad = sorted({k for _, k, _ in query_pairs(parts.query) if k.lower() not in SAFE_PARAMS})
        if bad:
            return f"{key}: 쿼리 매개변수 {', '.join(bad)}"
        if not parts.username:   # no user: the client's default login (maybe an admin) would not be checked
            return f"{key}: 사용자 이름이 없는 주소"
        want = addrs_of(parts.hostname or "")
        if not want or not want <= allowed or (parts.port or 5432) != (admin.port or 5432):
            return f"{key}: 관리 DB와 다른 서버(또는 소켓·호스트 이름)"
    return ""


def read_role(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        die("시험 DB 역할 파일을 읽을 수 없습니다")
    if not isinstance(data, dict) or not data.get("user") or not data.get("password"):
        die("시험 DB 역할 파일 형식이 틀렸습니다")
    return {"user": str(data["user"]), "password": str(data["password"])}


ROLE_NAME = re.compile(r"^wt_[a-z0-9_]{1,30}_[0-9a-f]{8}$")


def role_name(app: str) -> str:
    """wt_<app, lowercased, other characters as _>_<8 hex of the app name>: foo-bar and foo_bar never share a role."""
    import hashlib
    slug = re.sub(r"[^a-z0-9_]", "_", app.lower())[:30] or "app"
    return f"wt_{slug}_{hashlib.sha256(app.encode()).hexdigest()[:8]}"


def cmd_role_sql(args: list) -> None:
    """Slot-only role for an app whose own role is a superuser (Codex 11-02: a superuser in the sandbox can read
    pg_authid or run COPY ... PROGRAM). The password is made once, kept in the 0600 file and only ever piped."""
    o = opts(args)
    path, app = o.get("file") or die("--file 필요"), o.get("app") or die("--app 필요")
    name = role_name(app)
    if not ROLE_NAME.match(name):
        die("역할 이름 형식이 틀렸습니다")
    if not os.path.isfile(path):   # the caller (wt-new.sh) holds the app's role lock; the file still appears whole
        import secrets
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"user": name, "password": secrets.token_hex(24)}, f)
        os.replace(tmp, path)
    role = read_role(path)
    if role["user"] != name:
        die("역할 파일의 이름이 다릅니다")
    pw = role["password"].replace("'", "''")
    sys.stdout.write(f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN CREATE ROLE {name} LOGIN; END IF;
END $$;
ALTER ROLE {name} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{pw}';
""")


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
    # keep at least the real value's length (and 32 chars minimum): apps validate secrets by length
    # (one app: SESSION_SECRET min 32 — every copy build failed on "x-SESSION_SECRET", wire2-web 2026-11-05)
    fake = f"x-{key}"
    want = max(len(v), 32)
    return fake + "-" * (want - len(fake)) if len(fake) < want else fake


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
        if a in ("--env-file", "--key", "--db", "--app-dir", "--verify-env", "--role-file", "--file", "--app", "--admin-env"):
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
    role = read_role(o["role-file"]) if o.get("role-file") else None

    def slot_url(url: str) -> str:
        parts = urlsplit(with_db(url, db))
        url = urlunsplit(parts._replace(query=drop_params(parts.query, DB_PARAMS)))   # ?dbname= would win over the path
        return with_role(url, role) if role else url

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
    copy_dir = sets.get("WT_COPY") or os.getcwd()
    for key in file_env:
        if key in SKIP_KEYS:
            continue
        val = file_env[key]
        if DB_KEY.search(key) and is_pg_url(val):
            env[key] = slot_url(val) if db else f"x-{key}"
        elif key in INTERNAL_URL_KEYS:
            env[key] = DEAD_URL
        elif TOKEN_FILE_KEY.search(key):
            env[key] = f"/nonexistent/wt-{key}"
        elif SQLITE_KEY.search(key):
            env[key] = val   # relocated into the copy's data folder below, whatever path it names
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
    # sqlite keys the app's own code defaults but never names in an .env file at all (observed on one app class: no .env, only
    # a zod default in source) — learned from source text, set unless the env file already gave an explicit value
    for key, val in sqlite_defaults_of(app_dir).items():
        if key not in file_env:
            env[key] = val
    # any non-DB value pointing into the live data root, or any sqlite-file database setting (wherever it points,
    # even a relative path or one outside the live data root), is moved into the copy (11-01: <data-root>/<app> defaults;
    # 11-02: *_DB_PATH/SQLITE keys protected even when the value is not already under the live data root)
    for key in list(env):
        if DB_KEY.search(key):
            continue
        if not (str(env[key]).startswith(LIVE_DATA) or SQLITE_KEY.search(key)):
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
        env["DATABASE_URL"] = slot_url(base)
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
    elif sub == "role-sql":
        cmd_role_sql(rest)
    elif sub == "users":
        o = opts(rest)
        path = o.get("env-file") or ""
        if not os.path.isfile(path):
            die("환경 파일이 없습니다")
        env = parse_env(path)
        if o.get("admin-env"):
            why = unpinned(env, parse_env(o["admin-env"]).get("DATABASE_URL", ""))
            if why:
                die(f"역할 검사를 할 수 없어 멈춥니다 — {why}", 3)
        for name in url_users(env):
            print(name)
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
