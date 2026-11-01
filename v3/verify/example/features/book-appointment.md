# Book appointment (placeholder - service-business industry)

A customer picks a service and an open slot, and the app reserves it and shows a confirmation.

## Sub-features

- `book-appointment-save` creates one booking row tied to the chosen service and slot.
- `book-appointment-conflict` picking an already-taken slot shows a clear conflict message
  instead of creating a second booking.
- `book-appointment-invalid` submitting with no service selected shows a validation message.

## How to get to it (user POV)

- Board `/book`, pick a service card, pick an open slot, confirm.

## Driving it with drive.mjs

Preconditions:

- `doctor.sh` exit 0.

- **Open.** Navigate to `/book`.
- **Fill.** Click the service card named `<TODO: real service name from the running app>`.
  Pick the first slot marked available.
- **Save.** Click the button named `예약 확정` (TODO: replace with the app's real label). URL
  becomes `/book/<id>`; heading shows the chosen service and time; status badge reads `확정`.
- **Run.** `node $H/drive.mjs <copy> book-appointment`.
- **Stored.** `bash $H/sql.sh <copy> "select service_id, status from bookings order by created_at desc limit 1"`
  returns the chosen service with `status = 'confirmed'`.

## Gotchas

- TODO: note any duplicate labels on the same screen that need scoping to a specific form, the
  way a real feature file would (see naverplace's store-add.md for the pattern this template
  is based on).
