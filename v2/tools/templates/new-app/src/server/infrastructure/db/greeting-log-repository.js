// infrastructure/db -- the greeting feature's own storage. An
// in-memory stand-in (no real database wired up yet) implementing the
// same shape a real one would (record/all), so swapping this for a
// real DB-backed repository later never touches application code.
const entries = [];

export const greetingLogRepository = {
  record(entry) {
    entries.push(entry);
  },
  all() {
    return [...entries];
  },
};
