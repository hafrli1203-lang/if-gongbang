# Check stock (placeholder - retail/inventory industry)

A customer or clerk enters a SKU or scans a barcode and sees whether it is in stock at a given
location, and in what quantity.

## Sub-features

- `check-stock-found` a known SKU shows its quantity and location.
- `check-stock-missing` an unknown SKU shows "재고 정보 없음" rather than a blank page or error.
- `check-stock-zero` a SKU with quantity 0 shows "재고 없음", not a stale cached number.

## How to get to it (user POV)

- Board `/stock`, enter a SKU in the search box, press enter.

## Driving it with drive.mjs

Preconditions:

- `doctor.sh` exit 0.
- Fixture data loaded for one known SKU (TODO: name the seed script or fixture file the app
  uses for its test-DB slot, e.g. `npm run -s seed:test`).

- **Open.** Navigate to `/stock`.
- **Fill.** Textbox `SKU 또는 바코드` (TODO: replace with the app's real placeholder) =
  the known fixture SKU.
- **Search.** Press Enter. Card shows the SKU, quantity, and location name.
- **Run.** `node $H/drive.mjs <copy> check-stock <sku>`.
- **Stored (read-only journey).** `bash $H/sql.sh <copy> "select quantity, location from stock where sku = '<sku>'"`
  matches what the card showed - this journey does not change data, so the proof is that the
  screen and the database agree, not that a new row appeared.

## External (excluded from verification)

If stock levels come from a live point-of-sale integration with no local fake, mark the
real-time sync path "외부 — 검증 제외" here and verify only the read path against fixture data,
as above.
