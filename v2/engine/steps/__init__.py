"""Fixed step lists per request kind. Each module exposes
steps(ctx: StepContext) -> list[StepDef].

Request payload contracts (plan §7):
  close / abandon {}  (mode comes from the request kind)
  commit          {"message": str}
  publish         {"target": str, "artifact_path": str (rel to repo),
                   "artifact_hash": str}
  install, scripts_change, test_config_change
                  {"manifest": str (rel to repo), "change": {str: object}}
  merge           {"copies": [{"role": str, "station": str, "path": str}]}
                  (allowed prefixes come only from config.station_owners;
                   path must be under home/copies/{attempt_id}/)
  report          {"summary": str}
  review          {"role": str, "fp": str (optional; orbit adds the tree
                   fingerprint so a re-review of a changed tree is a new request)}
  bench           {"fp": str (optional, same reason)}
  run_station     {"station": str, "role": str, "task": str}
  review          {"role": str}
Job files live under config.home / "jobs" / attempt_id (report.md, gates/).
Archives: config.home / "archive" / project / attempt_id / {ts}/ .
Worker copies: config.home / "copies" / attempt_id / role / .
"""
from engine.steps import (  # noqa: F401
    bench_run, close, commit, install, merge, publish, report, review, station,
)

KINDS = {
    "close": close.steps,
    "abandon": close.steps,
    "commit": commit.steps,
    "publish": publish.steps,
    "install": install.steps,
    "scripts_change": install.steps,
    "test_config_change": install.steps,
    "merge": merge.steps,
    "report": report.steps,
    "bench": bench_run.steps,
    "run_station": station.steps,
    "review": review.steps,
}
