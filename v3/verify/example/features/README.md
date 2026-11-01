# verify-example feature map (template)

Maintained index of what a marketer/operator/customer can do in this app, and how to prove
each one still works. Read this index, then the one feature file you need. Copy this whole
`verify/example/` folder into the app and replace every placeholder.

## Baseline preconditions

- A work copy launched with `launch.sh`, `doctor.sh` exit 0 (own port, own test-DB slot, no
  job running that would mean another copy's worker is attached to this slot).
- Base URL `http://127.0.0.1:<port>` from the copy's own `.wt.json`. Never drive the
  production port or an instance this run did not start.

## Driving conventions

- Drive through `node $H/drive.mjs <copy> <journey>` with a private browser instance of your
  own, not a browser session shared with other agents.
- Use the real accessible names, roles and placeholders from the running screen. When a label
  is duplicated on the page, scope to the specific form/section in the feature file.
- Any external identifier used in a journey (an order id, a phone number) should be made up
  per run so nothing real is touched; state that convention in the feature file if the app has
  one.

## Proof and skip reporting

- Each feature file ends with: screenshots before/after at two breakpoints, an accessibility
  snapshot, and the database rows the action should have produced or changed.
- Anything that needs a real external service with no local fake (a live payment gateway, a
  real SMS carrier) is marked "외부 — 검증 제외" in that feature's file, with the reason - not
  silently left out of this index.

## Features (two placeholder industries - delete and replace)

- [Book appointment](./book-appointment.md) - a customer reserves a slot with a provider.
  Driven.
- [Check stock](./check-stock.md) - a customer or clerk looks up whether an item is in stock
  at a given location. Driven.
