import { describe, expect, it } from "vitest";

import { buildGreeting } from "../../src/server/domain/greeting/rules.js";
import { createFakeClock } from "../fakes/clock.fake.js";

describe("buildGreeting", () => {
  it("includes the given name", () => {
    const morning = createFakeClock("T09:00:00Z");
    expect(buildGreeting("Yuna", morning)).toContain("Yuna");
  });

  it("picks a time of day from the clock, not the real system clock", () => {
    const evening = createFakeClock("T20:00:00Z");
    expect(buildGreeting("Yuna", evening)).toContain("evening");
  });
});
