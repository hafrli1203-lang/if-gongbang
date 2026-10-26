// interface (component) -- a single reusable UI piece. Takes the
// already-computed message in; never fetches or decides anything.
export function renderGreetingBanner(message) {
  const el = document.createElement("p");
  el.className = "greeting-banner";
  el.textContent = message;
  return el;
}
