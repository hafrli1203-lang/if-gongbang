// domain -- pure rule, no I/O. The time of day comes in through
// `clock` (see ./ports.js), never from Date/Date.now() called directly
// here -- that direct access belongs to an infrastructure adapter.

/**
 * Build this app's one greeting line for `name`, using `clock` (a
 * ClockPort -- see ./ports.js) to decide morning/afternoon/evening.
 * Pure: same name + same clock reading always returns the same string.
 */
export function buildGreeting(name, clock) {
  const hour = clock.now().getUTCHours();
  const timeOfDay = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  return `Good ${timeOfDay}, ${name}! __APP__ is running.`;
}
