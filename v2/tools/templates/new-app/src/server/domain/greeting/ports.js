// domain -- the port(s) this feature's rules depend on, as a plain JS
// contract (no TypeScript in this skeleton): an implementation is any
// object shaped like the JSDoc typedef below. infrastructure/ provides
// a real one (system-clock.js); tests/fakes/ provides a deterministic
// one -- rules.js itself never knows which.

/**
 * @typedef {{ now: () => Date }} ClockPort
 */

export {};
