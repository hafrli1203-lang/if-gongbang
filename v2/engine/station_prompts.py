"""Plain-text task prompts engine.full_orbit hands to run_station for
each non-build station, plus the per-task builder text for the build
loop. These are instructions TO a worker (claude/codex/scripted), never
business content of their own -- no industry, brand or keyword specifics
belong here. The only business content is `request` (the owner's own
request text) and the plan's own goal/task text, both supplied by the
caller."""
from __future__ import annotations

from engine.plan import Task


def facts_prompt(request: str, job: str) -> str:
    return (
        f"요청: {request}\n\n"
        f".gongbang/{job}/facts.md 를 쓴다. 이 요청을 실행하는 데 필요한 "
        "사실(기준·숫자·외부 API 형식 등)을 표로 적는다: "
        "| 주장 | 판정(통과/확인 필요/탈락) | 출처 |. 「통과」 행은 반드시 "
        "출처(파일:줄, 문서 URL, 실행 로그)를 같은 행의 출처 칸에 적는다 — "
        "출처 없는 「통과」는 없다. 코드에 그대로 넣을 숫자·기준·API 형식이 "
        "있으면 「코드에 넣는 값」이라는 제목의 절에 모은다 — 그 절에 확인 "
        "필요가 남아 있으면 이 일은 다음 단계로 넘어가지 못한다. 모르는 값은 "
        "지어내지 말고 확인 필요로 남긴다.\n\n"
        "마지막으로 \"## 외부 대상\" 절을 쓴다 — 이 일이 의존하는 외부 "
        "API·CLI·파일 형식을 모두 한 줄씩 \"- <이름>: <공식 문서 URL>\" "
        "형태로 적는다(probe 단계는 이 절만 보고 무엇을 실제로 호출해 "
        "볼지 정한다). 의존하는 외부 대상이 없으면 그 절 아래에 "
        "\"(없음)\"이라고만 적는다."
    )


def prober_prompt(request: str, job: str, targets: tuple[tuple[str, str], ...]) -> str:
    targets_text = "\n".join(f"- {name}: {url}" for name, url in targets)
    return (
        f"요청: {request}\n\n"
        "이 일이 의존하는 외부 대상(API·CLI·파일 형식)마다 공식 문서를 "
        "열고 실제로 한 번 호출해 본다. 대상:\n"
        f"{targets_text}\n\n"
        f"대상마다 .gongbang/{job}/probe/<이름>.py (또는 .sh, .ts)에 "
        "10~30줄짜리 실제 호출 스크립트를 쓰고 한 번 실행한다. 그다음 "
        f".gongbang/{job}/probe/<이름>.log 를 쓴다 -- 첫 줄은 그 대상의 "
        "공식 문서 URL이고, 성공하면 \"PROBE OK\", 실패하면 \"PROBE "
        "FAIL\"과 에러 내용을 적은 뒤 실제 요청·응답 형태도 남긴다. "
        "재사용할 어댑터는 만들지 않는다 -- 호출이 되는지 확인하는 "
        "스크립트와 로그만 남긴다. 비밀값은 로그에 남기지 않는다."
    )


def planner_prompt(request: str, job: str, facts_md: str, tool_catalog: str = "",
                   probe_text: str = "") -> str:
    probe_part = f"\n\n{probe_text}" if probe_text else ""
    tools_part = ""
    if tool_catalog:
        tools_part = (
            "\n\n이 일에 쓸 도구표도 plan.json의 \"tools\"에 적는다: "
            '[{"kind": "skill"|"mcp", "name": str, "stations": [칸...], "why": str}]. '
            "칸은 facts·plan·design·build·review 중에서 고른다. 아래 목록 전체에서 "
            "이 일에 실제로 도움이 되는 스킬과 MCP를 칸마다 고르고, 왜 쓰는지 "
            "한 줄로 적는다. 고른 스킬은 그 칸 작업자의 지시문에 본문이 들어간다. "
            "목록에 없는데 꼭 필요한 것은 그대로 적으면 \"설치 필요(사람 승인)\"로 "
            "보고된다.\n\n## 고를 수 있는 도구\n" + tool_catalog
        )
    return (
        f"요청: {request}\n\n"
        f"사실확정 결과(.gongbang/{job}/facts.md):\n{facts_md}"
        f"{probe_part}\n\n"
        f".gongbang/{job}/plan.json 과, 필요하면 tests/scenes/ 아래 장면 "
        "스크립트를 쓴다. plan.json 스키마: "
        '{"goal": str, "ui": bool, '
        '"scenes": [{"id","actor_does","app_shows","where","cmd": [argv...]}], '
        '"tasks": [{"id","title","detail","owns": [관련 경로...], '
        '"depends_on": [task id...]}], "acceptance": [str]}. '
        "장면(scene)마다 cmd가 실행할 스크립트를 tests/scenes/ 아래에 실제로 "
        "만든다 — 통과 장면은 사람의 행동 → 결과를 그대로 자동으로 확인하는 "
        "스크립트이고, exit 0이면 통과다. 작업(task)마다 owns에 그 작업이 "
        "쓸 파일·폴더만 적는다(다른 작업의 소유 경로와 겹치지 않게 나눈다), "
        "depends_on으로 작업 순서를 정한다. 화면이 있는 요청이면 ui를 "
        "true로 둔다."
        + tools_part
    )


def designer_prompt(request: str, job: str, plan_goal: str) -> str:
    return (
        f"요청: {request}\n\n목표: {plan_goal}\n\n"
        f".gongbang/{job}/design/design.md 와 필요한 시안 파일을 쓴다. "
        "화면 구조·레이아웃·상태(빈 화면·로딩·오류 포함)를 글과 시안으로 "
        "보여준다. 이 시안은 사람이 보고 승인해야 다음 단계(구현)로 "
        f"넘어간다. 만든 화면의 스크린샷은 .gongbang/{job}/screens/ 아래에 "
        "저장한다."
    )


def builder_task_text(task: Task, plan_goal: str, job: str | None = None,
                      ui: bool = False) -> str:
    owns_list = ", ".join(task.owns)
    text = (
        f"목표: {plan_goal}\n\n작업: {task.title}\n\n{task.detail}\n\n"
        f"이 작업에서 고칠 수 있는 경로는 다음뿐이다: {owns_list}. 이 범위 "
        "밖은 고치지 않는다. tests/scenes/ 는 쓰지 않는다(그 경로는 계획 "
        "단계가 이미 끝냈다)."
    )
    if ui and job:
        text += (
            f"\n\n화면을 만들었으면 그 화면의 스크린샷을 .gongbang/{job}/"
            "screens/ 아래에 저장한다."
        )
    return text


def fix_task_text(task: Task, plan_goal: str, failing_scenes: tuple[str, ...],
                  job: str | None = None, ui: bool = False) -> str:
    base = builder_task_text(task, plan_goal, job, ui)
    scenes_list = ", ".join(failing_scenes)
    return (
        f"{base}\n\n수락 시험에서 다음 통과 장면이 실패했다: {scenes_list}. "
        "원인을 고쳐 다시 통과하게 만든다."
    )


__all__ = [
    "facts_prompt", "prober_prompt", "planner_prompt", "designer_prompt",
    "builder_task_text", "fix_task_text",
]
