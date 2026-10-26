// Skeleton entry point -- `npm run dev`/`npm run build` both resolve
// this through index.html's own `<script type="module" src="/src/main.js">`.
// A real build task replaces/extends this; its only job here is to
// boot the layered skeleton (interface -> application -> domain,
// infrastructure behind a port) end to end.
import { renderHomePage } from "./app/home-page.js";
import { env } from "./config/env.js";

renderHomePage(document.querySelector("#app"), env.VITE_APP_NAME);
