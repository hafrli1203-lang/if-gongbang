// interface (page) -- composes the one page this skeleton ships:
// request the greeting through the server action, hand the result to
// a component to render. No business rule lives here.
import { requestGreeting } from "../server/actions/get-greeting.js";
import { renderGreetingBanner } from "../components/greeting-banner.js";

export function renderHomePage(root, appName) {
  const message = requestGreeting({ name: appName });
  root.appendChild(renderGreetingBanner(message));
}
