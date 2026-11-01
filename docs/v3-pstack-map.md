# v3 — mapping onto pstack's playbooks

This workflow's step structure (investigate → reproduce → plan → build → verify → land) is not
invented here. It follows the playbook shapes from **pstack** (`poteto-mode` and its sibling
skills), MIT-licensed, attributed at the point it is reused (see "Attribution" below). This
page is the public equivalent of the private source's own pstack cross-reference table, with
internal identifiers removed and the station/skill mapping generalized.

pstack itself was written for a different host tool. Where this mapping differs from pstack's
own wording, this page's table is the one to follow for v3 — it is the adaptation, not a
restatement.

## Playbooks used

| Playbook | File | When |
|---|---|---|
| Investigation | `playbooks/investigation.md` | "Look at this / report on this" — answer with evidence, do not change anything. |
| Bug fix | `playbooks/bug-fix.md` | Reproduce → find the cause → fix → confirm with the same reproduction. |
| Feature | `playbooks/feature.md` | New or changed behavior. Start from the data shape. |
| Refactoring | `playbooks/refactoring.md` | Behavior unchanged, structure only (rename, extract, merge, move). |
| Autonomous run | `playbooks/autonomous-run.md` | "Until it's done" — run to a stated end condition. |
| Orchestrate | `playbooks/orchestrate.md` | Coordinating several workers/units at once. |
| Session pickup | `playbooks/session-pickup.md` | Resuming a stalled or long-running job from its saved work-copy path. |

Playbook steps are copied to the front of the task's own checklist; a step this workflow skips
gets one line, `skip: <reason>`, not silent omission.

## pstack vocabulary → this workflow

| pstack term | Here |
|---|---|
| Opening a PR / Shipping | `tools/wt-land.sh <app> <name>` — lands, deploys, and pushes in one step. An app with its own deploy mechanism is refused (exit 2); that app is landed and deployed through its own process instead. |
| Worktree off main | A work copy at `$WT_ROOT/<app>/<name>` (`tools/wt-new.sh`), never editing the app's own checkout directly. |
| Second opinion (a different model) | One read-only review pass from a second model, before anything risky (deploy, delete, auth, payments, external integration) ships. |
| `how` / `architect` / `interrogate` | Reserved for database/auth/payment/external-integration work only; skipped elsewhere. |
| control-ui / control-cli | The app's own `verify-<app>` skill (`v3/verify/example/` is the template); failing that, a general browser-automation tool. |
| "Always pause: deploys" | Not followed as written — this workflow does not pause before an ordinary deploy. It does pause before anything destructive to data, anything that reaches a real person, or a push to a public repository. |
| `poteto-agent` sub-agents | Ordinary sub-agents, briefed with a pointer to the pstack skill file to read first. |
| unslop / technical-writing / deslop / no-comments | Short, fact-based reports; code comments only where the "why" is not visible from the code itself. |

## Verification-count table

| Kind of work | Test run | Second-model read-only review |
|---|---|---|
| Docs/text only | 0 | 0 |
| Ordinary code | 1 | 0 |
| Database, auth, payments, deploy, external integration | 1 | 1 |

A failed check gets its specific problem fixed, then that one check re-run — not the whole
suite, and not repeated beyond what changed meaningfully.

## Attribution

Playbook structure and the `poteto-mode` skill concept are from pstack (MIT License). This
mapping is an independent adaptation for this workflow's own tools and vocabulary; it is not a
verbatim copy of pstack's own documentation, and pstack's own license and copyright notice are
preserved wherever its files are reused unmodified.
