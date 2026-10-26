// infrastructure/integrations -- a real ClockPort (see
// ../../domain/greeting/ports.js) backed by the actual system clock.
// The one place in this skeleton that calls `new Date()` directly.
export const systemClock = {
  now: () => new Date(),
};
