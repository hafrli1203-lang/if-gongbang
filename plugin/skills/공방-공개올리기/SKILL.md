---
name: 공방-공개올리기
description: Re-extract this toolset's own private-source changes into its public repository, check it is clean of internal paths/names, run its tests, and push. Use on "push the public update", "sync the public repo", "publish the latest tool changes".
---

# Publish an update to the public repository

## When
The private source the public repository's `harness/export_v3_public.py` /
`harness/export_ops_public.py` read from has new commits that should reach the public copy.

## Steps
1. Get the exact commit hash of each private source this install extracts from
   (`git -C <private source> rev-parse HEAD`). Extraction always reads a named commit, never
   "whatever the working tree looks like right now" - that keeps the public bundle
   reproducible from a fixed point in history.
2. Run the extractor(s) against the public repository checkout:
   ```bash
   python3 harness/export_v3_public.py <private _shared dir> <commit> <public repo>/v3
   python3 harness/export_ops_public.py <private ops dir> <commit> <public repo>
   ```
   Each one scrubs internal paths/names via its own substitution rules and refuses (exit 1,
   with a list) if anything still looks like it leaked through - fix the extractor's rule
   table or the private source, never hand-patch the generated output.
3. Update `README.md`/`docs/*.md` to describe **only the current, implemented design** -
   no internal dates, no quoted internal directives, no internal file paths, no "the owner"
   or an equivalent phrase naming who runs this install.
4. Run the full test suite in the public repository, and the public-readiness scan with no
   local extra-identifier file active (so the result matches what CI, which has no such local
   file, will actually see):
   ```bash
   python3 -m pytest tests -q -p no:cacheprovider
   GONGBANG_PUBLIC_READY_EXTRA=/nonexistent python3 harness/public_ready_scan.py .
   ```
   A hit found only with a local extra-identifier file active and not present in the files you
   changed is a pre-existing, local-only condition - note it, do not try to "fix" something you
   did not touch unless it is actually blocking the push (see step 5).
5. Commit (English, conventional-commit style) and push. If a push hook runs the same
   public-readiness scan and blocks on something real, fix that exact line - never bypass the
   hook.
6. Check the repository's own CI result for the pushed commit before reporting it as done.

## Report
The pushed commit hash, the test suite's exact pass/fail numbers, the CI run's result, and a
list of what moved from the private source to which public file.

## Watch for
- A worked-example comment that happens to name a real, private app/account/project can leak
  through even when the surrounding code is already generic - the extractor's own leftover
  scan exists specifically to catch this, run it, do not skip it because "it's just a comment."
- Never push a different local branch of the public repository by mistake - confirm the
  branch before pushing.
