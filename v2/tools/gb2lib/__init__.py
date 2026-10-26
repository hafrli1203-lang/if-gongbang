"""gb2lib -- the command implementations behind the `tools/gb2` entry
script. `tools/gb2` itself stays a thin argument parser + dispatch
table; every subcommand's actual body lives in one of these modules,
grouped by area:

  shared        constants + helpers every other module here needs
                (workspace paths, app-name validation, run log, the
                full-orbit summarize/report plumbing shared by both
                `new`/`change`).
  workspace     `init`/`new` -- building an app's engine workspace and
                (for `new`) scaffolding a brand-new app repo first.
  runner        `run`/`fix`/`continue`/`change`/`approve-design`/
                `approve-install` -- driving a job (single-task or
                full-orbit) through the engine.
  release_ops   `ship`/`skip-ship`/`resolve` -- the release/publish
                track.
  ops           `status`/`backup`/`log`/`diff`/`sync`/`board`/`doctor`
                -- read-only reporting plus the environment doctor.
"""
from __future__ import annotations
