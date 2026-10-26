# __APP__

This app is managed by the IF 공방 v2 engine (`tools/gb2`). It started as
an empty skeleton written by `gb2 new __APP__ "<requirement>"`; nothing
else has been built yet except what that job's own run produced.

After each job finishes, read `.gongbang/<job>/report.md` for what was
requested, what passed, how to run the app, and what is still open.

## Structure

`src/app` and `src/components` are the interface layer (pages, UI
pieces). `src/server/actions` is also interface (validates input,
calls application). `src/server/application` runs one usage scene
end to end; `src/server/domain` holds pure rules (no I/O); 
`src/server/infrastructure/{db,integrations}` implements the ports
application/domain define. `src/config` is the only place env vars are
read. `tests/unit`, `tests/e2e`, `tests/fakes` mirror `src/`; engine
acceptance scenes live separately in `tests/scenes/`.

## Local commands

```
npm install   # install dependencies (owner-approved by the engine's
              # own install-request flow, not run by gb2 new itself)
npm run dev   # start the Vite dev server
npm run build # production build (output: dist/)
npm run preview # serve the production build locally
npm test      # run the test suite (vitest)
```
