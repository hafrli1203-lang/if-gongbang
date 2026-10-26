# e2e

Browser-driven end-to-end specs for this app (e.g. Playwright, against
the running dev server) go here once the app has one.

This is a different thing from `tests/scenes/`: that folder holds the
engine's own acceptance scenes -- small scripts a `plan` station
writes, naming the requirement(s) they cover, that a `bench` run
executes to decide whether a job is done. `tests/e2e/` is this app's
own browser-level test suite, owned by whoever builds the app, not by
the engine's plan/bench machinery.
