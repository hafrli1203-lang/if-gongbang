// tests/fakes -- the only place a fake implementation of a domain
// port (see src/server/domain/greeting/ports.js) lives; src/ never
// imports from here.
export function createFakeClock(isoTimestamp) {
  const fixed = new Date(isoTimestamp);
  return { now: () => fixed };
}
