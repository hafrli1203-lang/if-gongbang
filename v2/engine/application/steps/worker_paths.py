"""Pure path builders shared by station.py, station_sandbox.py and
station_gate.py -- kept in their own module so none of those three has
to import the others just to compute a path (which would make a
circular import, since each of them needs at least one path the other
owns conceptually). Every function here is a pure Path computation: no
I/O, no ledger access.
"""
from __future__ import annotations

from pathlib import Path

from engine.domain import ids
from engine.application.processor import StepContext


def stations_dir(ctx: StepContext) -> Path:
    return ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "stations"


def stdout_path(ctx: StepContext) -> Path:
    return stations_dir(ctx) / f"{ctx.request.request_id}.stdout"


def result_path(ctx: StepContext) -> Path:
    return stations_dir(ctx) / f"{ctx.request.request_id}.result.json"


def record_path(ctx: StepContext) -> Path:
    return stations_dir(ctx) / f"{ctx.request.request_id}.json"


def gate_log_dir(ctx: StepContext) -> Path:
    # Worker-writable (inside the stations dir): the log itself stays
    # audit-only by design (a worker could forge lines into it; see
    # station_gate.import_gate_log's docstring), so there is nothing to
    # protect by moving it -- unlike gate_facts.json/engine_config.json
    # below.
    return stations_dir(ctx) / ctx.request.request_id


def gate_log_path(ctx: StepContext) -> Path:
    return gate_log_dir(ctx) / "gate.log"


def gate_ro_dir(ctx: StepContext) -> Path:
    """An engine-owned directory OUTSIDE every worker-writable tree --
    a sibling of the writable stations dir under the same hidden
    (tmpfs, then --remount-ro'd -- see engine.infrastructure.sandbox.wrap) $HOME, so
    once mounted read-only in the worker's sandbox, nothing the worker
    runs can rename, delete or replace it OR any ancestor directory
    entry up to $HOME itself (verified empirically: mkdir/mv both fail
    anywhere inside a remounted-ro tmpfs, even several plain-directory
    levels deep, while a SEPARATE deeper mount nested in the same tmpfs
    -- this one -- stays exactly as writable/readonly as it was bound).
    The worker CAN rename a plain writable directory it has access to
    (e.g. stations/<R> -> stations/<R>.old, then recreate an empty
    stations/<R> with a fake gate_facts.json at the same pathname the
    gate hook was told to read), so the facts file must never live
    inside such a directory -- its pathname is never inside any
    directory the worker has write access to, at any point in its
    lifetime."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "gate-ro" / ctx.request.request_id)


def gate_facts_path(ctx: StepContext) -> Path:
    return gate_ro_dir(ctx) / "gate_facts.json"


def gate_config_copy_path(ctx: StepContext) -> Path:
    return gate_ro_dir(ctx) / "engine_config.json"
