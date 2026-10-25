from __future__ import annotations

import sys
from pathlib import Path

from engine.context import (
    ContextConfig, ContextItem, LibraryMcpConfig, PER_ITEM_CAP, TOTAL_SECTION_CAP,
    build, gather, render_items,
)

_FAKE_SERVER = str(Path(__file__).resolve().parent.parent / "fixtures" / "fake_library_mcp.py")


class _StubConfig:
    """A minimal stand-in for EngineConfig -- gather()/build() only ever
    read `.context` off whatever they're given."""

    def __init__(self, context: ContextConfig | None) -> None:
        self.context = context


def _library_mcp_config(*, lessons_app: dict[str, str] | None = None,
                        crash: bool = False, flag: str | None = None,
                        timeout_sec: float = 5.0) -> ContextConfig:
    argv = (sys.executable, _FAKE_SERVER) + (("--crash-after-init",) if crash else ())
    if flag:
        argv = argv + (flag,)
    return ContextConfig(
        library_mcp=LibraryMcpConfig(command=argv, timeout_sec=timeout_sec),
        lessons_app=lessons_app or {},
    )


def test_gather_filters_run_output_items() -> None:
    config = _StubConfig(_library_mcp_config())
    ctx = gather(config, "alpha", "facts", "재주문 기준이 뭐야")
    assert not ctx.failed
    titles = [i.title for i in ctx.items]
    assert "안경원 재주문 기준" in titles
    assert "어제 만든 초안" not in titles  # origin=="run_output" -- filtered
    kinds = {i.kind for i in ctx.items}
    assert kinds == {"library"}


def test_gather_includes_lessons_when_app_configured() -> None:
    config = _StubConfig(_library_mcp_config(lessons_app={"alpha": "shortform"}))
    ctx = gather(config, "alpha", "facts", "질문")
    lesson_items = [i for i in ctx.items if i.kind == "lesson"]
    assert len(lesson_items) == 1
    assert lesson_items[0].trust == "experience"


def test_gather_no_lessons_without_app_entry() -> None:
    config = _StubConfig(_library_mcp_config())
    ctx = gather(config, "alpha", "facts", "질문")
    assert not any(i.kind == "lesson" for i in ctx.items)


def test_gather_no_library_mcp_configured_returns_empty() -> None:
    ctx = gather(_StubConfig(None), "alpha", "facts", "질문")
    assert ctx.items == ()
    assert not ctx.failed


def test_gather_client_failure_sets_failed_true_and_does_not_raise() -> None:
    config = _StubConfig(_library_mcp_config(crash=True))
    ctx = gather(config, "alpha", "facts", "질문")
    assert ctx.failed is True


def test_gather_server_hang_times_out_and_sets_failed_true() -> None:
    """A server that accepts a tools/call request but never answers it
    must not hang gather() forever -- the client enforces its own
    configured timeout_sec and reports McpClientError/failed=True,
    same as a crash."""
    import time

    config = _StubConfig(_library_mcp_config(flag="--hang-on-call", timeout_sec=1.0))
    started = time.monotonic()
    ctx = gather(config, "alpha", "facts", "질문")
    elapsed = time.monotonic() - started
    assert ctx.failed is True
    assert elapsed < 10.0  # well under the fake server's own 3600s sleep


def test_gather_skips_notification_sent_before_the_real_reply() -> None:
    """A server that writes an id-less notification before the real
    tools/call reply must not have that notification mistaken for the
    reply -- the client only accepts a message whose id matches the
    request it is waiting on."""
    config = _StubConfig(_library_mcp_config(flag="--notify-before-reply"))
    ctx = gather(config, "alpha", "facts", "재주문 기준이 뭐야")
    assert not ctx.failed
    titles = [i.title for i in ctx.items]
    assert "안경원 재주문 기준" in titles


def test_gather_flood_of_notifications_cannot_extend_the_overall_deadline() -> None:
    """A server that keeps writing a notification every 0.3s, forever,
    instead of ever answering the tools/call must still time out after
    roughly ONE configured timeout_sec -- not be kept alive
    indefinitely by a per-message window that each individual
    notification arrives comfortably inside of."""
    import time

    config = _StubConfig(_library_mcp_config(flag="--flood-notifications", timeout_sec=1.0))
    started = time.monotonic()
    ctx = gather(config, "alpha", "facts", "질문")
    elapsed = time.monotonic() - started
    assert ctx.failed is True
    assert elapsed < 5.0  # well under what an unbounded per-message window would allow


def test_decision_item_included_when_path_configured(tmp_path: Path) -> None:
    decisions = tmp_path / "CURRENT_DECISIONS.md"
    decisions.write_text("# 결정\n화면은 shadcn 부품을 쓴다.\n", encoding="utf-8")
    config = _StubConfig(ContextConfig(decisions_paths={"alpha": str(decisions)}))
    ctx = gather(config, "alpha", "facts", "질문")
    assert len(ctx.items) == 1
    assert ctx.items[0].kind == "decision"
    assert ctx.items[0].trust == "approved"
    assert "shadcn" in ctx.items[0].text


def test_decision_item_missing_file_is_skipped(tmp_path: Path) -> None:
    config = _StubConfig(ContextConfig(decisions_paths={"alpha": str(tmp_path / "nope.md")}))
    ctx = gather(config, "alpha", "facts", "질문")
    assert ctx.items == ()


def test_render_items_empty() -> None:
    assert render_items(()) == ""


def test_render_items_includes_trust_label() -> None:
    items = (ContextItem(kind="decision", title="t", text="본문", source="src", trust="approved"),)
    text = render_items(items)
    assert "승인됨" in text
    assert "본문" in text


def test_render_items_per_item_cap() -> None:
    long_text = "가" * (PER_ITEM_CAP + 500)
    items = (ContextItem(kind="library", title="t", text=long_text, source="s", trust="reference"),)
    text = render_items(items)
    assert len(text) <= TOTAL_SECTION_CAP
    # the item's own text inside the block never exceeds the per-item cap
    assert ("가" * (PER_ITEM_CAP + 1)) not in text


def test_render_items_total_cap_drops_later_items() -> None:
    big = "나" * (PER_ITEM_CAP - 10)
    items = tuple(
        ContextItem(kind="library", title=f"t{i}", text=big, source="s", trust="reference")
        for i in range(10)
    )
    text = render_items(items)
    assert len(text) <= TOTAL_SECTION_CAP
    assert "t0" in text
    assert "t9" not in text  # later items dropped once the total cap is hit


def test_build_with_no_context_config_is_empty() -> None:
    sc = build(_StubConfig(None), "alpha", "facts", "질문")
    assert sc.text == ""
    assert sc.injected == ()
    assert sc.skills == ()
    assert sc.missing == ()
    assert sc.failed is False


def test_build_loads_station_skills_and_reports_missing(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "있는-스킬").mkdir(parents=True)
    (root / "있는-스킬" / "SKILL.md").write_text("스킬 본문", encoding="utf-8")
    config = _StubConfig(ContextConfig(
        skill_roots=(str(root),),
        station_skills={"build": ("있는-스킬", "없는-스킬")},
    ))
    sc = build(config, "alpha", "build", "질문")
    assert sc.skills == ("있는-스킬",)
    assert sc.missing == ("없는-스킬",)
    assert "스킬 본문" in sc.text
    assert "없는-스킬" in sc.text


def test_build_station_with_no_configured_skills_gets_none() -> None:
    config = _StubConfig(ContextConfig(station_skills={"build": ("아무거나",)}))
    sc = build(config, "facts", "facts", "질문")
    assert sc.skills == ()
    assert sc.missing == ()


def test_required_skill_missing_is_flagged_unmet(tmp_path: Path) -> None:
    """A skill name ContextConfig.required_skills actually lists for
    this station, but that is missing on disk, must surface as
    required_missing/unmet_required() -- not just the existing
    "없음" annotation that work continues past."""
    root = tmp_path / "skills"
    (root / "있는-스킬").mkdir(parents=True)
    (root / "있는-스킬" / "SKILL.md").write_text("본문", encoding="utf-8")
    config = _StubConfig(ContextConfig(
        skill_roots=(str(root),),
        station_skills={"build": ("있는-스킬", "필수-없는-스킬")},
        required_skills={"build": ("필수-없는-스킬",)},
    ))
    sc = build(config, "alpha", "build", "질문")
    assert sc.missing == ("필수-없는-스킬",)
    assert sc.required_missing == ("필수-없는-스킬",)
    assert sc.unmet_required() is True


def test_missing_skill_not_marked_required_stays_unmet_false() -> None:
    """Default behavior (no station listed in required_skills at all,
    or that station listed but this particular name not in it) is
    unchanged: a missing skill is still just an annotation, never a
    stop -- MASTER_RULES' "기본값은 필수 아님"."""
    config = _StubConfig(ContextConfig(station_skills={"build": ("없는-스킬",)}))
    sc = build(config, "alpha", "build", "질문")
    assert sc.missing == ("없는-스킬",)
    assert sc.required_missing == ()
    assert sc.unmet_required() is False


def test_required_library_failure_is_flagged_unmet() -> None:
    config = _StubConfig(ContextConfig(
        library_mcp=_library_mcp_config(crash=True).library_mcp,
        require_library=("alpha",),
    ))
    sc = build(config, "alpha", "facts", "질문")
    assert sc.failed is True
    assert sc.required_library_failed is True
    assert sc.unmet_required() is True


def test_library_failure_on_a_project_not_requiring_it_stays_unmet_false() -> None:
    config = _StubConfig(_library_mcp_config(crash=True))  # require_library=() default
    sc = build(config, "alpha", "facts", "질문")
    assert sc.failed is True
    assert sc.required_library_failed is False
    assert sc.unmet_required() is False


def test_build_appends_failure_line_on_library_crash() -> None:
    config = _StubConfig(_library_mcp_config(crash=True))
    sc = build(config, "alpha", "facts", "질문")
    assert sc.failed is True
    assert "곳간 조회 실패" in sc.text


def test_direction_file_injected_whole_into_facts_and_plan_only(tmp_path) -> None:
    from types import SimpleNamespace

    from engine.context import PER_ITEM_CAP, ContextConfig, gather, render_items

    direction = tmp_path / "방향_원문.md"
    body = "# 방향 원문표\n" + ("- · \"한 번 할 때 제대로 해라\"\n" * 300) + "END-OF-DIRECTION"
    direction.write_text(body, encoding="utf-8")
    assert len(body) > PER_ITEM_CAP
    config = SimpleNamespace(context=ContextConfig(direction_path=str(direction)))
    for station in ("facts", "plan"):
        text = render_items(gather(config, "p", station, "q").items)
        assert "END-OF-DIRECTION" in text
    assert "방향 원문표" not in render_items(gather(config, "p", "build", "q").items)


def test_owner_decisions_are_never_cut(tmp_path) -> None:
    from types import SimpleNamespace

    from engine.context import PER_ITEM_CAP, ContextConfig, gather, render_items

    decisions = tmp_path / "CURRENT_DECISIONS.md"
    decisions.write_text("결정\n" * PER_ITEM_CAP + "LAST-DECISION", encoding="utf-8")
    config = SimpleNamespace(context=ContextConfig(decisions_paths={"p": str(decisions)}))
    assert "LAST-DECISION" in render_items(gather(config, "p", "build", "q").items)
