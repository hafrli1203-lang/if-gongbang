#!/usr/bin/env bash
# Generated from private source at d72495d955c050e98c98a283b5521f31f3eff43a; do not edit by hand (see harness/export_v3_public.py).
# Post-deploy check for one app (G2 A2 / A0 table): unit active, each health path answers 200, memory printed.
# Reads $WT_APPS_ROOT/<app>/deploy.env: UNIT, PORT, HEALTH_PATHS (space separated, default "/"), WORKER_UNIT (optional).
# usage: deploy-check.sh <app>      exit 0 = all good, 1 = something failed (prints what)
set -uo pipefail
app="${1:?app}"
envf="$WT_APPS_ROOT/$app/deploy.env"
[ -f "$envf" ] || { echo "FAIL no $envf" >&2; exit 1; }
# shellcheck disable=SC1090
. "$envf"
UNIT="${UNIT:-${WT_UNIT_PREFIX:-ifapp}-$app}"; PORT="${PORT:?PORT in deploy.env}"; HEALTH_PATHS="${HEALTH_PATHS:-/}"
HEALTH_CODES="${HEALTH_CODES:-200}"   # codes that prove the app answers; e.g. "200 403" for an app that refuses a loopback request by design
HEALTH_HOST="${HEALTH_HOST:-}"   # Host header to send, for apps that only trust their tailscale host (e.g. a specific tailnet address:PORT)
fail=0
state=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
if [ "$state" = active ]; then echo "OK unit $UNIT active (NRestarts=$(systemctl --user show "$UNIT" -p NRestarts --value), mem=$(( $(systemctl --user show "$UNIT" -p MemoryCurrent --value) / 1048576 ))MB)"; else echo "FAIL unit $UNIT $state"; fail=1; fi
if [ -n "${WORKER_UNIT:-}" ]; then
  ws=$(systemctl --user is-active "$WORKER_UNIT" 2>/dev/null || true)
  if [ "$ws" = active ]; then echo "OK worker $WORKER_UNIT active"; else echo "FAIL worker $WORKER_UNIT $ws"; fail=1; fi
fi
for p in $HEALTH_PATHS; do
  hostArgs=(); [ -n "$HEALTH_HOST" ] && hostArgs=(-H "Host: $HEALTH_HOST")
  code=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "${hostArgs[@]}" "http://127.0.0.1:$PORT$p" || echo 000)
  t=$(curl -s -o /dev/null -m 20 -w '%{time_total}' "${hostArgs[@]}" "http://127.0.0.1:$PORT$p" || echo "?")
  case " $HEALTH_CODES " in *" $code "*) echo "OK $p $code ${t}s";; *) echo "FAIL $p $code (expected: $HEALTH_CODES)"; fail=1;; esac
done
exit $fail
