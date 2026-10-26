"""Compatibility package: the test fakes now live in `tests.fakes`, and the
in-memory publisher in `engine.infrastructure.fake_publisher`
(docs/standards/engine-layout.md). Each module here is an alias of its
new module. `hooks/` still holds the policy-hook test scripts: tests pass
them to the config by file path, and the config resolves that path.
"""
