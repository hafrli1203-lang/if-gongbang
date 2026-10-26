// interface (server action) -- validates the input, wires the real
// infrastructure adapters, then calls application. Never does the
// greeting rule itself; that stays in domain/greeting/rules.js.
import { z } from "zod";

import { getGreeting } from "../application/greeting/get-greeting.js";
import { greetingLogRepository } from "../infrastructure/db/greeting-log-repository.js";
import { systemClock } from "../infrastructure/integrations/system-clock.js";

const RequestSchema = z.object({ name: z.string().min(1) });

export function requestGreeting(input) {
  const { name } = RequestSchema.parse(input);
  return getGreeting({ name, clock: systemClock, repository: greetingLogRepository });
}
