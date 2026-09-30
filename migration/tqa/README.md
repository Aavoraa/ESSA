# TQA legacy data → ESSA migration

Moves the legacy retail ERP export (`TQA - Data.zip`, 237 CSV tables, ~15M rows,
Aug 2022 – Jul 2026) into ESSA's **own** PostgreSQL schema, so the existing
ESSA warehouse app and its mounted POS (`/pos`) run on the real data.
ESSA is the source of truth: nothing here defines a table, and ESSA's schema,
services and business rules are unchanged. The old data is adapted to them.

```
TQA - Data.zip  ──load_legacy.py──▶  legacy_tqa.raw.*          (verbatim, read-only source)
                                          │  COPY as legacy_reader (SELECT-only, read-only role)
                 prepare_target.py ─▶  <target>.legacy.*       (staged copy)
                   ESSA boot builds  ─▶  <target>.public.*      (ESSA's schema, by create_all)
                 migrate.py (sql/)  ─▶  public.* + shop.*       (ESSA warehouse + POS tables)
                                        migration.*             (id maps, exceptions, reports)
                 validate.py        ─▶  output/*.csv
```

## Databases (local PostgreSQL 18, localhost:5432)

| Database | Role | Written by |
|---|---|---|
| `legacy_tqa` | the legacy export, one TEXT table per CSV in schema `raw` | `load_legacy.py` only |
| `essa_staging_base` | pristine: ESSA booted + legacy staged, nothing migrated | `prepare_target.py --save-base` |
| `essa_staging` | test runs (cloned from the base) | the migration |
| `essa` | **the database ESSA runs on** | the migration (final run) |
| `essa-old-data` | pre-existing, 1 table (`items_recovered`, a partial items import); **untouched** | — |

Databases created by these scripts carry the comment `tqa-migration-target`;
the scripts refuse to drop any database without it.

## Running it

Credentials are passed in the environment, never written into files:

```bash
export LEGACY_DATABASE_URL=postgresql://postgres:***@localhost:5432/legacy_tqa
export ESSA_DATABASE_URL=postgresql://postgres:***@localhost:5432/essa
export TQA_TEMP_PASSWORD=...        # the one temporary password for migrated logins

python load_legacy.py "path/to/tqa.zip"          # once: source → legacy_tqa (checks the manifest)
python prepare_target.py --recreate --save-base  # ESSA builds its schema; legacy staged; base saved
python migrate.py                                # all steps in sql/, in order
python validate.py                               # reports → output/, exit 1 on a failed hard check
```

A re-run is `prepare_target.py --from-base` (a one-minute clone of the pristine
base) followed by `migrate.py`; the source is never modified, so every run
produces the same result. Each step runs in one transaction and the run stops
at the first failure, leaving the database as it was before that step.

`load_legacy.py` / `prepare_target.py` / `migrate.py` need `psycopg` (3);
`.py` steps and `validate.py` run with the backend's own venv (they call ESSA's
services), which `migrate.py` does itself.

## Steps (dependency order, from ESSA's foreign keys)

| Step | Writes | From |
|---|---|---|
| 00 setup | `migration` schema, value parsers | — |
| 10 organisation | businesses, warehouses, stores, floors, pos_terminals | company, referencelist LOCATION/FLOOR/COUNTER (+ bills, for which floor/till is where) |
| 20 masters | categories, master_records (product, tax, brand, supplier, agent, transport, employee), attribute_options, suppliers, agents, transports | products, tax, brand, items, supplier+suppliercompany, agent, transport, employee |
| 25 ESSA users | users (warehouse logins) | users |
| 30 products | products (SKU per item × design) | items, stock |
| 40 purchasing | purchase_orders(+lines), purchases (GRN) + purchase_lines + purchase_line_splits, lr_entries | purchaseorder(+items), lrinvoice(+items), lrentry |
| 50 stock | product_units, purchase_returns(+lines), stock_outwards(+lines), stock_movements, stock_balances | stock, salablegoods, lrinvoicereturn(+items), bundles |
| 60 payments | payments, payment_allocations | supplierpayment(+invoice) |
| 65 shop sync | shop schema; shop categories, products, locations/floors/counters — **by the shop's own sync code** | ESSA tables above |
| 68 shop users | shop.users (POS logins) | users |
| 70 customers | shop.companies, shop.customers | company, customer |
| 72 sales | shop.invoices, invoice_items, invoice_payments | bill, billitems, billsettlement(+master) |
| 74 returns | shop.credit_notes(+items) | bill/billitems (returns) |
| 76 store stock | shop.transfer_receipts, location_stock, products.stock_qty, stock_movements, bill_sequences | salablegoods, dispatches |
| 78 deliveries | shop.deliveries, delivery_bills, delivery_lines (what customers carried out) | delivery, bills |
| 80 finalize | master_options (store list), number_sequences — **by ESSA's own services** | — |
| 85 indexes | an index on every foreign key that had none (performance only) | — |

## Mapping (old → ESSA)

**Organisation**
- `company` 1 (ESSA GARMENTS PRIVATE LIMITED) → the business ESSA is configured as (matched on legal name; only blanks filled). `company` 2 (ESSA GARMENTS — it billed every Prozone bill) → a second business `LEGACY-CO2`, flagged for review.
- `referencelist` LOCATION flagged `iswarehouse` → `warehouses` (WAREHOUSE); every other location → `stores` under it (Taqua Silks Tirupur, L2, Prozone, Essa Garments, Zakat, MD, Cherry, Shyamala, TW).
- FLOOR → `floors`, placed in the store their bills came from; `prefix` = the bill series billed there (GROUND TG, FIRST TF, SECOND TS, THIRD TT, L2-GROUND EG). COUNTER → `pos_terminals` on their floor.

**Masters**
- `products` (the product GROUP, e.g. MENS-SHIRT) → ESSA `categories` by name (ESSA ships the same master; 3 unmatched groups added) and the Product master record (HSN, tax, margins).
- `items` + `stock.designid` → `products`: one SKU per legacy item × design (design number is an identity attribute in ESSA's garment catalogue). Current item/design of each piece is used (448K pieces were re-classified after their GRN). Attributes: brand, size, colour, pattern, style, material, type, fit, sleeve, design. Price = latest piece's selling price, MRP = its display MRP. SKUs `ESSA-00001…` oldest first.
- `supplier` + `suppliercompany` → `suppliers` 1:1 (GSTIN validated; PAN from GSTIN; bank as ESSA's `{name, account_no, ifsc, branch}`), commercial terms → Supplier master record by code. Not merged when sharing a GSTIN or name — listed in `migration_duplicates.csv`.
- `agent`, `transport` → `agents`, `transports` (unique by name in ESSA, so same-name rows merge; listed).
- `brand`, `tax`, `employee` → master records with the field keys `master_defs.py` declares.

**Purchasing & stock (warehouse)**
- `lrinvoice` → `purchases` (posted GRNs at the warehouse; the legacy ledger receives purchases only there). GRN no. = legacy LR entry number (`GRN10254`), `#<id>` appended only where one number covered several invoices (go-live import).
- `lrinvoiceitems` → `purchase_lines` as billed; where a line's pieces are now other variants, `purchase_line_splits` per variant. Empty lines (qty 0, no piece — the Aug-2022 go-live import) are not carried.
- `stock` (barcoded piece) → `product_units`, **code = the legacy barcode** (TQ315689), so existing tags scan at the POS (`resolve_scan` → unit code). Status from legacy: in_stock / dispatched / sold / returned.
- `lrinvoicereturn(+items)` → `purchase_returns` (debit notes) + lines, each tied to the GRN line it returns.
- `bundles` (transfer notes) from the warehouse → `stock_outwards` to the store (received when the store recorded receipt).
- Ledger (`stock_movements`, ESSA's kinds): GRN inward / return / outward, store→warehouse notes as adjustments naming the note, and one reconciliation adjustment per product so ESSA's warehouse stock = legacy closing warehouse stock. Balance quantities are the ledger's (as `stock_locations.replay` sums them); the average cost is what the pieces on hand actually cost at legacy buying price (a weighted average replayed through legacy's out-of-order history divided by near-zero balances — ₹12.5 Cr a piece in one case).
- `lrentry` → `lr_entries`; `purchaseorder` → `purchase_orders` (status 0 = never actioned → pending; codes 1/4 kept in notes, flagged).
- `supplierpayment(+invoice)` → `payments` + `payment_allocations` (debit notes as `debit_adjust`).

**POS (shop schema)**
- `users` → `shop.users` (every login — bills name their cashier) and, for back-office roles, ESSA `users`. Roles: legacy 1 → admin; 3/11 → manager (shop), user (ESSA); others → cashier / user. One shared temporary password; legacy passwords are encrypted and cannot be carried.
- `customer` → `shop.customers` 1:1 (shared mobile numbers listed, not merged).
- `bill` sales (retail + cancelled) → `shop.invoices` with legacy bill numbers, floor/till/store/company, fin-year/prefix/seq; cancelled ones as `cancelled`. Lines: legacy net taxable price + legacy tax; invoice total = what was paid; discount = round-off.
- `billsettlementmaster` → `invoice_payments` (cash / card / UPI), split across the bills a settlement paid.
- Return bills → `credit_notes` against the source bill; each line against the same piece on it.
- `salablegoods` (held per place) → `location_stock` per store; `products.stock_qty` = its sum; `transfer_receipts` for every dispatch line.

## Decisions that were NOT guesses (evidence in the data)

| Question | Evidence | Decision |
|---|---|---|
| Current store stock | `hasstock` agrees with the legacy ledger for 99.8% of pieces it covers | held = iqty−oqty where hasstock |
| Current warehouse stock | `stock.hasstock` rows = pieces not yet transferred | qty − transferred − returned |
| Which bills are sales / returns / reversals | series + isreturn/iscancel; reversal total mirrors cancelled sales | sales & cancelled sales → invoices; returns → credit notes; reversals & cancelled returns excluded (listed) |
| Floor ↔ store ↔ bill prefix | the bills themselves | as above |
| Tenders | only cash/card/UPI ever non-zero; they sum to paid on 99.997% | cash/card/upi |
| Goods collected | legacy delivery desk from Dec-2023: 119,579 handed over, 5 pending; before it, goods left at the counter | every live sale collected, except the bills legacy still shows pending |
| LR received | legacy records the received date, not the receiver | `received_by` = "Legacy system" where a received date exists |
| Zero-quantity barcodes (289K) | all from the Aug-2022 go-live; their stock went straight to stores | linked to their GRN line as breakdown rows with 0 received at the warehouse (gives their SKU ESSA provenance) |
| Pack tags (41K tags, 1.2M pieces) | one legacy barcode over several pieces | legacy tag = first piece's code; ESSA codes {SKU}-nnn for the rest, as `units.create_for_receipt` does (≤500 per receipt) |

## Where the legacy data does not say, and what was done (all listed in migration_errors.csv as `review`)

- **PO status codes 1 and 4** (6 POs): actioned, meaning unrecorded → imported pending, legacy code in Notes.
- **Supplier payment mode codes** 0/1/2: unlabelled → 0 left at ESSA's default (NEFT), 1/2 as "Legacy mode 1/2".
- **LR delivery-mode codes**: unlabelled → "Transport" where a transporter is named.
- **Transfer-note status 9**: migrated by whether receipt was recorded.
- **Company 2**: separate business; confirm and set its GSTIN.
- **Refund method** on returns: "exchange" when settled with a sale, else the tender paid back, else cash.
- **Warehouse reconciliation adjustments** (one per product, 68K) are dated at the START of the legacy
  history (2022-08-21): they are balances brought forward from before / outside the documents, and
  dated at the cutoff they showed on the movement chart as one month's receipts.
- **Debit notes over several invoices** reference the invoice they took the most value from (ESSA
  gives a debit note one reference invoice); every line keeps its own GRN line.
- **Go-live payables**: 20,441 GRNs (₹7.79 Cr) from the Aug-2022 go-live import have no payment in
  the legacy data (its `settled` flag is false on every invoice), so ESSA shows them outstanding.
  If they were paid before go-live, record an opening settlement — none was invented.
- **661 SKUs** whose legacy GRN was deleted have no posted GRN in ESSA; ESSA's Inventory Repair lists
  them as removable. Listed by SKU in migration_errors.csv — keep or remove by decision.

## ESSA project changes (the only ones)

Configuration / environment:

1. `backend/requirements.txt` — added `psycopg2-binary==2.9.12` (the Postgres driver the deployment
   requirements already pin; without it a Postgres URL fails at the first connection).
2. `run.bat` — reads `essa-intake\.env` (as `.env.example` says), and never runs `seed.py --reset`
   when `ESSA_DATABASE_URL` is set (that command drops every table).
3. `.env.example` — the Postgres URL example now matches the installed driver.
4. `backend/.venv` — rebuilt on this machine's Python 3.13 (the shipped one pointed at another
   PC's Python 3.11); the old one is kept as `.venv.orig-py311-other-pc`.
5. `essa-intake/.env` (gitignored) — `ESSA_DATABASE_URL` for the `essa` database, `ESSA_AUTH_SECRET`.

Code — fixes found by running ESSA on the real data. No model, schema, business rule or
screen behaviour was changed except where stated; every other fix returns exactly what the
code returned before, found with fewer queries.

| File | Why | What |
|---|---|---|
| `services/pos_sales.py` | **Postgres bug** (500 on Dead Stock, Item Locator bills, POS status): SQLite returns dates as text, Postgres as `datetime`, and `(value or "")[:10]` fails on the latter; one join named `customers` unqualified, which is `shop.customers` on Postgres | `str(value)[:10]` — the form `pos_store_sales.py` already uses — and `q("customers")` like every other table in the file |
| `main.py`, `pos_mount.py` | **Race**: loading the POS swaps `sys.modules["app"]` for as long as its syncs run; with 400K products that is a minute in which warehouse requests fail ("No module named app.services") | long-lived processes build the POS at startup, before serving (serverless keeps the deferred build); the mount records the warehouse state it just synced so the first POS request does not repeat the sync |
| `routers/purchases.py` | GRN list never returned (lazy loads per line × 850K lines) | list-only serializer computing the same fields from three set-based queries |
| `services/payments.py` | pending bills: 4 queries per GRN (71 s) | two grouped queries, same arithmetic |
| `services/notifications.py` | shortage rule walked every GRN line | reads the shortages directly (same set) |
| `services/charts.py` | movement chart loaded the whole ledger; category ring asked every product | two columns for the axis months; only products holding stock (others have no value) |
| `services/integrity.py` | inventory scan: a query per product plus every piece code as an ORM object (4½ min) | piece codes preloaded once as plain rows; the unit-type lookup asked once per unit |
| `routers/inventory.py`, `frontend/src/api.js`, `frontend/src/App.jsx` | **Behaviour change, by decision**: Inventory, Stock Outward, Label Designer and Label Printing downloaded the whole catalogue (417K SKUs) | these screens list what the warehouse holds (`held=1`, ESSA's `include_zero=False` scope); the Inventory search box searches every SKU on the server (3+ characters, up to 500). The endpoint called without parameters is unchanged. Frontend rebuilt (`npm run build`; `npm run check` all passing); the previous `dist` is kept in `ESSA-backups-2026-09-23/` |

Database: `sql/85_indexes.sql` adds a plain index on every foreign-key column that had none
(Postgres does not create them) — performance only.

## Observation for the ESSA team (not changed)

`Textile Retail Shop/app/warehouse_items._apply` opens a product that is new to the shop with the
**warehouse's** stock quantity ("opening" movement). A product created by a GRN and later dispatched
to a store is then counted twice in the shop (opening + transfer-in). The migration sets every
store's stock to the legacy figure explicitly, so migrated data is not affected; new GRNs posted
after go-live will follow ESSA's existing behaviour.
