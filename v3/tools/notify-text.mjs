// Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
// Owner-alert text helpers (2026-11-05, owner "모든 디스코드 알림과 카톡 알림을 쉬운말로 바꿔라"). Alerts are three plain
// parts — what happened / why it matters / "대표가 할 일: …". Imported by notify-owner.mjs and its tests.

export const DISCORD_MAX = 700;
export const KAKAO_MAX = 200; // KakaoTalk "나에게 보내기" text template limit

/** First sentence + the "대표가 할 일" part, within Kakao's limit. */
export function kakaoText(full, max = KAKAO_MAX) {
  if (full.length <= max) return full;
  const ownerAt = full.indexOf("대표가 할 일");
  const owner = ownerAt >= 0 ? full.slice(ownerAt).trim() : "";
  const head = ownerAt >= 0 ? full.slice(0, ownerAt) : full;
  const end = head.search(/[.!?]\s/);
  const first = (end > 0 ? head.slice(0, end + 1) : head).trim();
  const joined = owner ? `${first} ${owner}` : first;
  return joined.length <= max ? joined : `${joined.slice(0, max - 1)}…`;
}

/** Plain-language check (recorded in notify-log, never blocks a send). */
export function plainIssues(t) {
  const issues = [];
  if (!t.includes("대표가 할 일")) issues.push("대표가 할 일 없음");
  if (/(^|[\s(])\/(srv|tmp|home|etc|var)\//.test(t)) issues.push("파일 경로");
  if (/\b(rc|exit|land|ack|scope|unit|commit|wt-[a-z]+|HTTP \d{3})\b/i.test(t)) issues.push("영어 개발 용어");
  if (/코드 \d+/.test(t)) issues.push("오류 코드");
  if (/^\[[^\]]+\]/.test(t)) issues.push("[꼬리표]로 시작");
  return issues;
}
