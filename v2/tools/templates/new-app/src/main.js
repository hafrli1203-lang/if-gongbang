// Skeleton entry point -- `npm run dev`/`npm run build` both resolve
// this through index.html's own `<script type="module" src="/src/main.js">`.
// A real build task replaces this with the app the plan actually asks
// for; its only job here is to make the empty skeleton itself boot.
document.querySelector("#app").textContent = "__APP__ is running.";
