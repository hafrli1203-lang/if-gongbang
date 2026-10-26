// application -- the one usage scene this skeleton ships: "show a
// greeting". Calls the domain rule, then records the result through
// whatever repository it is handed -- never imports a concrete
// infrastructure module itself (that wiring is the action's job).
import { buildGreeting } from "../../domain/greeting/rules.js";

export function getGreeting({ name, clock, repository }) {
  const message = buildGreeting(name, clock);
  repository.record({ name, message, at: clock.now().toISOString() });
  return message;
}
