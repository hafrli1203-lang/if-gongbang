# __APP__

This app is managed by the IF 공방 v2 engine (`tools/gb2`). It started as
an empty skeleton written by `gb2 new __APP__ "<requirement>"`; nothing
else has been built yet except what that job's own run produced.

After each job finishes, read `.gongbang/<job>/report.md` for what was
requested, what passed, how to run the app, and what is still open.

## Local commands

```
npm install   # install dependencies (owner-approved by the engine's
              # own install-request flow, not run by gb2 new itself)
npm test      # run the test suite (vitest)
```
