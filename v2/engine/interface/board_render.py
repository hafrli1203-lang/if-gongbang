"""engine.interface.board's rendering half: turns the AppBoard/JobBoard/EventRow
rows engine.interface.board_data derives from a ledger into the static HTML page
(no LLM, no network, stdlib only -- a plain data report, not a
generated summary). Borrowed idea: Hermes Memory Wiki.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from engine.interface.board_data import AppBoard, EventRow, POSITION_EVENT_KINDS, group_lessons

_CSS = """
:root {
  --page: #ffffff; --card: #ffffff; --soft: #f2f3f6;
  --line: #eaebee; --line-strong: #dcdee3;
  --ink: #212124; --ink-2: #4d5159; --muted: #6e737b; --faint: #868b94;
  --accent: #2f6fed; --accent-soft: #eaf1ff;
  --pos: #1a8a4b; --pos-soft: #e8f7ee;
  --warn: #b5790a; --warn-soft: #fff3df;
  --neg: #c13f3f; --neg-soft: #fcebeb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #16171a; --card: #1d1f23; --soft: #22242a;
    --line: #2b2d33; --line-strong: #3a3d45;
    --ink: #eceef1; --ink-2: #c7cad1; --muted: #9499a3; --faint: #6e737b;
    --accent: #5b94ff; --accent-soft: #1d2740;
    --pos: #4fd17f; --pos-soft: #16271c;
    --warn: #e2b14d; --warn-soft: #2e2512;
    --neg: #f08080; --neg-soft: #2e1a1a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
    "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif;
  font-size: 15px; line-height: 1.7; -webkit-font-smoothing: antialiased;
  word-break: keep-all; overflow-wrap: anywhere;
}
main { max-width: 1024px; margin: 0 auto; padding: 24px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 12px; color: var(--ink); }
.meta { font-size: 13px; color: var(--muted); margin-bottom: 24px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 13px; }
tr:last-child td { border-bottom: 1px solid var(--line-strong); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
.badge-pos { background: var(--pos-soft); color: var(--pos); }
.badge-warn { background: var(--warn-soft); color: var(--warn); }
.badge-neg { background: var(--neg-soft); color: var(--neg); }
.badge-soft { background: var(--soft); color: var(--ink-2); }
.empty { color: var(--faint); font-size: 14px; }
.day { font-size: 13px; color: var(--muted); margin: 16px 0 4px; }
.err { color: var(--neg); font-size: 13px; }
"""

_STATUS_BADGE = {
    "완료": "badge-pos",
    "배포됨": "badge-pos",
    "건너뜀": "badge-soft",
    "사람 확인 대기": "badge-warn",
    "실패": "badge-neg",
    "중단": "badge-neg",
    "진행 중": "badge-soft",
}


def _status_badge_class(status: str) -> str:
    """_STATUS_BADGE, matched by prefix for "사람 확인 대기: <reason>"
    ."""
    if status in _STATUS_BADGE:
        return _STATUS_BADGE[status]
    if status.startswith("사람 확인 대기"):
        return _STATUS_BADGE["사람 확인 대기"]
    return "badge-soft"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _fmt_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "확인 필요"
    return f"${cost_usd:.2f}"


def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _event_label(app: str, event: EventRow) -> str:
    p = event.payload
    if event.kind == "job.opened":
        return f"{app}: 작업 시작 - {p.get('job_name', '?')}"
    if event.kind == "attempt.finished":
        return f"{app}: 시도 종료 ({p.get('status', '?')})"
    if event.kind == "station.finished":
        return (f"{app}: {p.get('station', '?')}/{p.get('role', '?')} 완료 "
                f"(ok={p.get('ok')})")
    if event.kind in POSITION_EVENT_KINDS:
        return f"{app}: 진행 - {p.get('stage', '?')}"
    if event.kind == "report.written":
        return f"{app}: 보고서 작성"
    if event.kind == "request.submitted":
        return f"{app}: 요청 제출 ({p.get('kind', '?')})"
    if event.kind == "request.status":
        return f"{app}: 요청 상태 ({p.get('status', '?')})"
    if event.kind == "approval.granted":
        return f"{app}: 승인 ({p.get('action', '?')} {p.get('target', '?')})"
    if event.kind == "gate_log.imported":
        return (f"{app}: 게이트 로그 가져옴 "
                f"({p.get('allowed', 0)} 허용 / {p.get('denied', 0)} 거부)")
    return f"{app}: {event.kind}"


def render_board_html(apps: list[AppBoard], *, lessons: list[dict[str, Any]] | None = None,
                      queue_text: str | None = None, generated_ts: float | None = None,
                      recent_limit: int = 30) -> str:
    """Build the full static page. Pure given its arguments (no file
    I/O, no clock reads unless `generated_ts` is None) -- callers that
    want a reproducible test render pass a fixed `generated_ts`."""
    if generated_ts is None:
        generated_ts = datetime.now(tz=timezone.utc).timestamp()

    all_events: list[tuple[str, EventRow]] = []
    for app_board in apps:
        for event in app_board.events:
            all_events.append((app_board.app, event))
    all_events.sort(key=lambda pair: pair[1].seq)

    jobs_html = _render_jobs_table(apps)
    day_log_html = _render_day_log(all_events)
    recent_html = _render_recent(all_events, recent_limit)
    lessons_html = _render_lessons(lessons or [])
    queue_html = _render_queue(queue_text)
    errors_html = _render_errors(apps)

    generated_label = _fmt_day(generated_ts) + " " + _fmt_time(generated_ts) + " UTC"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IF 공방 작업판</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>IF 공방 작업판</h1>
<div class="meta">생성 시각: {_esc(generated_label)} (고정 파일 기준, 자동 새로고침 없음)</div>
{errors_html}
<h2>작업 (앱별)</h2>
{jobs_html}
<h2>최근 활동</h2>
{recent_html}
<h2>날짜별 기록</h2>
{day_log_html}
<h2>작업판(큐)</h2>
{queue_html}
<h2>경험 묶음 (제목 키워드별)</h2>
{lessons_html}
</main>
</body>
</html>
"""


def _render_errors(apps: list[AppBoard]) -> str:
    rows = [app for app in apps if app.error]
    if not rows:
        return ""
    lines = "".join(f'<div class="err">{_esc(app.app)}: {_esc(app.error)}</div>' for app in rows)
    return f'<div class="card">{lines}</div>'


def _render_jobs_table(apps: list[AppBoard]) -> str:
    jobs = [job for app in apps for job in app.jobs]
    if not jobs:
        return '<p class="empty">작업이 없습니다.</p>'
    jobs = sorted(jobs, key=lambda j: (j.last_ts or 0), reverse=True)
    rows = []
    for job in jobs:
        badge_cls = _status_badge_class(job.status)
        rows.append(
            "<tr>"
            f"<td>{_esc(job.app)}</td>"
            f"<td>{_esc(job.job_name)}</td>"
            f'<td><span class="badge {badge_cls}">{_esc(job.status)}</span></td>'
            f"<td>{_esc(job.stage)}</td>"
            f"<td>{job.attempt_count}</td>"
            f"<td>{_esc(_fmt_duration(job.duration_sec))}</td>"
            f"<td>{_esc(_fmt_cost(job.cost_usd))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>앱</th><th>작업</th><th>상태</th><th>단계</th>"
        "<th>시도</th><th>걸린 시간</th><th>비용</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_recent(all_events: list[tuple[str, EventRow]], limit: int) -> str:
    if not all_events:
        return '<p class="empty">활동이 없습니다.</p>'
    recent = sorted(all_events, key=lambda pair: pair[1].seq, reverse=True)[:limit]
    rows = [
        f"<tr><td>{_esc(_fmt_day(event.ts))} {_esc(_fmt_time(event.ts))}</td>"
        f"<td>{_esc(_event_label(app, event))}</td></tr>"
        for app, event in recent
    ]
    return (
        "<table><thead><tr><th>시각(UTC)</th><th>내용</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_day_log(all_events: list[tuple[str, EventRow]]) -> str:
    if not all_events:
        return '<p class="empty">기록이 없습니다.</p>'
    by_day: dict[str, list[tuple[str, EventRow]]] = {}
    for app, event in all_events:
        by_day.setdefault(_fmt_day(event.ts), []).append((app, event))
    parts = []
    for day in sorted(by_day):
        rows = "".join(
            f"<li>{_esc(_fmt_time(event.ts))} - {_esc(_event_label(app, event))}</li>"
            for app, event in by_day[day]
        )
        parts.append(f'<div class="day">{_esc(day)}</div><ul>{rows}</ul>')
    return "".join(parts)


def _render_queue(queue_text: str | None) -> str:
    if not queue_text:
        return '<p class="empty">작업판 파일이 없습니다.</p>'
    return f'<div class="card"><pre>{_esc(queue_text)}</pre></div>'


def _render_lessons(lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return '<p class="empty">경험 기록이 없습니다.</p>'
    groups = group_lessons(lessons)
    parts = []
    for topic in sorted(groups):
        items = "".join(
            f"<li>{_esc(item.get('title', '(제목 없음)'))}</li>" for item in groups[topic]
        )
        parts.append(f'<div class="day">{_esc(topic)} ({len(groups[topic])})</div><ul>{items}</ul>')
    return "".join(parts)


__all__ = ["render_board_html"]
