# Automatic skill router

A session usually knows only its always-on skills and whatever plugins happen to be enabled.
Once a skill warehouse grows into the thousands of `SKILL.md` files, nobody can remember every
name to invoke by hand. This router takes one plain task sentence and returns the best-matching
skill(s) by score, so a session (or a hook) can pull in the right skill body without the person
typing its exact name.

## Files

- `build_index.py` — scans `~/.claude/skills` (core), `~/.claude/plugins/cache/**` (installed
  plugins) and `~/.claude/skills-library` (a warehouse directory, if you keep one) for every
  `SKILL.md`, and writes name/description/path/source/category/enabled plus a pre-tokenized
  count (for BM25, so a query does not re-tokenize every document) to
  `$SKILL_ROUTER_INDEX` (default `~/.cache/skill-router/index.json`). When the same skill name
  exists at more than one path, core beats an enabled plugin beats a disabled plugin beats the
  warehouse, and only one survives in the index.
- `tokenize_lib.py` — the tokenizer `build_index.py` and `route.py` share. English gets light
  suffix stripping; Korean also uses character bigrams (no morphological analyzer) so a word
  with an attached particle/ending still matches the bare word.
- `aliases.json` — a synonym table between natural-language task words and the (often English)
  words a skill's own description uses. Keep this file free of any one industry's vocabulary —
  it is meant to route any kind of work, not one domain.
- `route.py` — CLI: `python3 route.py "<task sentence>" [--k 3] [--max-chars 6000] [--json]`.
  BM25 (using the index's pre-computed token counts) + alias expansion + an optional
  workflow-stage score boost + a recency boost + a source boost (core/enabled-plugin ranked
  above warehouse). Appends the chosen skills to `$SKILL_ROUTER_INJECTIONS_LOG` (default
  `~/.cache/skill-router/injections.jsonl`) as `{ts, query, skills}` for later review.

## Install

1. Copy this folder somewhere stable (e.g. `~/.claude/skill-router/`).
2. Run `python3 build_index.py` once to build the index, then on a schedule (cron, or a nightly
   job if you already run one) so new or edited skills are picked up.
3. To route automatically on every prompt, wire `route.py` into a `UserPromptSubmit` hook in
   your `settings.json` that runs it against the prompt text and injects the returned skill
   body as extra context. Write the hook yourself against your own `settings.json` layout —
   this bundle intentionally ships only the router, not a hook that edits your settings file
   for you (see the harness's own rule against ever auto-merging hook config without an
   explicit, reviewable diff).
4. To call it from application code instead of a hook, invoke
   `python3 route.py "<task sentence>" --json` as a subprocess and read its stdout.

## Evaluating accuracy

If you maintain your own labeled set of (query, expected top skills) pairs, score the router's
top-k accuracy with a small script that calls `route.py --json` per query and compares the
returned names against the expected ones. This bundle does not ship a company-specific eval set
(it would just be this install's own task vocabulary) — write one against your own skill
library once it is indexed, and keep it alongside your own tests.

## Where this fits in the IF 공방 (gongbang) skills bundle

Every other skill and agent in this plugin assumes the work copy / land / deploy tool chain
(`wt-new.sh`, `wt-run.sh`, `wt-land.sh`, …) described in the repository's `docs/v3.md`. The
router is independent of that — it only helps a session find the right skill among many. If you
installed this plugin for the `일`/`공방-*` skills and don't need automatic routing across a
large warehouse, you can skip this folder entirely.
