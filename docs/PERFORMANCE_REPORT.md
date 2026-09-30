# ESSA performance optimisation — engineering report

Scope: the FastAPI backend (and one frontend polling fix), measured against the
migrated production data. No UI, workflow, API contract or business rule was
changed; every changed endpoint's response was compared with the pre-change
response (see *Verification*).

## How it was measured

* **Data** — `essa_staging`, an identical copy of the migrated `essa` database
  (417K products, 970K stock movements, 448K outward lines, 35.5K GRNs, 2.1M till
  invoice lines, 2.46M piece codes). The live `essa` database was not touched.
* **Server** — a second instance on `127.0.0.1:8001` with request instrumentation
  switched on, so the user's own server on :8000 was not disturbed.
* **Method** — each endpoint called once, sequentially, with no other traffic, as
  `superadmin` scoped to warehouse 1 (or central where marked). Timings are
  wall-clock at the client; query count and SQL time come from the server.
  Single runs on a desktop PC: expect ±15% run to run.
* **Instrumentation** — `backend/app/perf.py`, **off by default**. With
  `ESSA_PERF_LOG=1` every `/api` and `/pos` request logs total time, SQL time,
  query count, new connections, Python time, the five slowest statements and the
  statement templates repeated most (the N+1 detector). `ESSA_PERF_FILE=<path>`
  also writes one JSON line per request. Unset, nothing is registered.

## Results

Endpoints that were over 0.3 s before or after:

| Endpoint | Before (s) | After (s) | Queries before → after | Speed-up |
|---|---:|---:|---|---:|
| `/api/command/overview` (central) | 70.45 | 4.44 | 37,644 → 136 | 15.9× |
| `/api/command/overview` (warehouse) | 133.09 | 4.58 | 37,626 → 136 | 29.1× |
| `/api/notifications/count` | 7.81 | 2.95 | 585 → 79 | 2.6× |
| `/api/notifications` | 7.90 | 3.11 | 585 → 79 | 2.5× |
| `/api/dashboard/charts` | 8.94 | 1.67 | 3,304 → 14 | 5.4× |
| `/api/locations/overview?days=14` | 4.13 | 0.31 | 27 → 28 | 13.3× |
| `/api/inventory/summary` | 2.06 | 0.14 | 6,399 → 3 | 14.7× |
| `/api/inventory/products?held=1` | 12.99 | 1.85 | 19,703 → 14 | 7.0× |
| `/api/inventory/products?q=saree&limit=500` | 8.44 | 3.08 | 1,534 → 20 | 2.7× |
| `/api/inventory/integrity` | 28.93 | 25.64 | 13 → 13 | 1.1× |
| `/api/inventory/lookup` | 0.31 | 0.01 | 4 → 4 | 31× |
| `/api/inventory/locate` | 6.04 | 0.06 | 35 → 35 | 100× |
| `/api/inventory/product-card` | 0.49 | 0.01 | 8 → 8 | 49× |
| `/api/purchases` | 5.85 | 2.87 | 3,296 → 4 | 2.0× |
| `/api/outward` | 10.88 | 0.88 | 17,525 → 3 | 12.4× |
| `/api/returns` | 0.94 | 0.10 | 3,187 → 2 | 9.4× |
| `/api/payments` | 0.75 | 0.32 | 2,069 → 2 | 2.3× |
| `/api/suppliers` | 2.09 | 0.10 | 8,676 → 12 | 20.9× |
| `/api/dead-stock/summary` | 6.46 | 2.35 | 515 → 9 | 2.7× |
| `/api/dead-stock/register` | 7.50 | 2.44 | 514 → 8 | 3.1× |
| `/api/dead-stock/alerts` | 7.07 | 2.26 | 514 → 8 | 3.1× |
| `/api/physical-audit/options` | 9.27 | 4.08 | 6 → 6 | 2.3× |
| `/api/pricing/products?limit=200` | 6.63 | 0.10 | 3 → 3 | 66× |
| `/api/payments/pending` | 0.50 | 0.67 | 4 → 4 | unchanged (noise) |
| `/api/pricing/options` | 0.53 | 0.62 | 9 → 9 | unchanged (noise) |

Every other benchmarked endpoint (masters, users, audit, LR, purchase orders,
locations, documents …) was already under 0.3 s and remains so; trivial calls
dropped from ~50–160 ms to ~10 ms through connection pooling.

Against the targets: simple APIs are under 500 ms; the dashboard (Command Center)
is 4.4 s; list pages are 0.1–3 s. Still above target: the integrity scan (26 s),
physical-audit options (4 s) and notifications (3 s) — see *Remaining bottlenecks*.

## Bottlenecks found and what was done

### 1. A new database connection for every request — `database.py`
The Postgres engine used `NullPool`, correct for serverless but pure cost on the
long-running warehouse server: each request opened 1–3 fresh connections (TCP +
password auth, 50–150 ms each). A long-lived process now keeps a modest pool
(`pool_size=10, max_overflow=20, pre_ping, recycle 30 min`), sized to one uvicorn
process serving a dashboard's ~15 parallel calls — not enlarged beyond that.
Serverless (`VERCEL`) keeps `NullPool`; `ESSA_DB_POOL=null|queue` forces either.

### 2. N+1 queries (lazy relationship loads in loops)

| Where | Pattern | Fix |
|---|---|---|
| `stock_locations.transfer_summary` | `o.total_qty` loaded every outward's lines — 17.5K loads, called twice per overview | one grouped `SUM` over lines |
| `routers/outward.list_outwards` | lines + 3 names per outward (17.5K) | grouped line totals over the same filtered set; names joined |
| `routers/purchases.list_purchases` | supplier per GRN (3.3K) | `joinedload` supplier, warehouse |
| `routers/suppliers.list_suppliers` | profiles + documents per supplier (8.7K) | `selectinload` profiles; documents **counted** in one grouped query |
| `routers/returns`, `routers/payments` | purchase / supplier per row (3.2K, 2K) | `joinedload` |
| `routers/inventory.list_products` | 2 balance queries + 1 piece-code query + supplier per product (19.7K) | one balance read for the warehouse; piece codes preloaded for the listed products only; `selectinload` supplier |
| `stock_locations.stock_at` (inventory summary) | product per balance row (6.4K) | `joinedload` |
| `dead_stock.product_rows` | `primary_supplier` per product (505) | `selectinload` |
| `charts.top_suppliers` | supplier per GRN (3.3K) | name joined in the query |
| `command_center.outstanding` | supplier per bill | `joinedload` |

### 3. Unnecessary full loads (`.all()` of whole tables)

* `stock_locations.warehouse_totals` loaded all 417K balance rows as objects, three
  times per overview → one `GROUP BY warehouse_id` returning the same qty, value
  and item count.
* `pricing/products` counted its total with `len(find(...))` — every product as an
  object → `COUNT(*)` over the identical filtered query (`pricing.count`).
* `physical_audit.filter_options` loaded every product the warehouse ever held as
  objects to collect distinct dropdown values → the same values from a
  column-only read.
* `charts.purchases_by_month` / `top_suppliers` → two-column reads.
* `pos_insights.low_stock(limit=8)` fetched all 411,259 "low" store products to
  show eight and count them → `COUNT(*)` plus `LIMIT` with the same `WHERE` and
  `ORDER BY` (the unlimited path is unchanged).
* `dead_stock._movement_index` grouped the whole ledger (318K groups returned to
  Python) when only in-stock products are read → restricted to them; EXPLAIN shows
  it uses the existing `product_id` index (185 ms / 318K rows → 81 ms / 4.4K rows).

### 4. Aggregation moved into PostgreSQL

* Purchases list — line count, new products, items and items-pending-detail per
  GRN were computed by streaming 1.27M line and breakdown rows into Python; now
  one grouped query applying the same per-line rules.
* `integrity.Context` — movement count and received quantity per product are now
  `GROUP BY` queries; provenance links use `DISTINCT`. Verified identical to the
  old maps for every product (links, history, received all equal).

### 5. Duplicate work within one request

The Command Center is assembled from independent services that each re-read the
same data: the 2.1M-line till aggregation (`pos_sales.sales_by_product`) ran
**four** times per overview (twice inside `dead_stock.product_rows`, which itself
ran twice), plus `pos_sales.status` (a fifth pass over the same join), warehouse
totals three times, the transfer summary and today's GRNs twice.

`backend/app/request_memo.py` adds a **request-scoped** memo: GET requests only,
discarded when the request ends, never shared between requests or users — so
there is nothing to invalidate and no staleness. Used by `sales_by_product`,
`pos_sales.status` (which now reuses the counts the unwindowed aggregation already
produced — verified identical: last sale 2026-07-30, 340,843 linked products),
`dead_stock.product_rows`, and the Command Center's warehouse totals, transfer
summary and purchases. `product_rows` also takes `last_sold` from the same single
aggregation instead of running it again via `last_sold_index`.

### 6. Provenance read for the whole catalogue to judge a few products

`integrity.Context` read every product's provenance (1.66M link rows, the whole
ledger) even when a screen asked about one product or a few thousand. It now takes
an optional `product_ids` scope (default unchanged = everything). Used by the
product lists, the charts' stock ring, and single-product checks (lookup, locate,
product card, unit labels) — 4.1 s → 0.43 s for the 6.4K in-stock products,
~0 for one. A search with a `limit` reads provenance in slices in list order and
stops once the limit is filled.

## Indexes

No index was added. Every query introduced or changed was checked with
`EXPLAIN ANALYZE`; each uses an existing index (the FK indexes from migration step
85 cover `product_id`, `purchase_id`, `line_id`, `supplier_id`, `outward_id`), and
`stock_balances` already has the unique `(product_id, warehouse_id)` index the
balance reads rely on. Considered and rejected on measurement:

* restricting the till aggregation to in-stock products via `= ANY(ARRAY(...))` —
  2.4 s vs 1.24 s (random index reads beat by one sequential pass);
* the same via `IN (subquery)` — 0.43 s, but only works when the shop shares the
  warehouse's Postgres database (not with a SQLite shop) and would stop
  `status()` reusing the aggregation; not worth the coupling;
* `SET work_mem = 64MB` for that aggregation — 2.7 s vs 1.24 s.

## Verification

* Every benchmarked endpoint's JSON response was saved before any change and
  compared field by field after each batch (floats to 1e-6).
* **Identical:** outward, purchases, returns, payments, suppliers, charts,
  inventory summary, product lookups, dead-stock summary and alerts,
  notifications, locations overview, pricing, physical-audit options, and every
  Command Center figure.
* **Same rows, different order among exact ties:** dead-stock register (rows
  tied on days idle and value), inventory lists (products sharing a description),
  Command Center low stock (411,259 products tied at stock 0). The old order among
  ties was whatever order Postgres happened to scan — it already differed between
  two runs of the unchanged code. The inventory list and dead-stock read now add
  `id` as a tie-breaker so the order is stable; for the saree search that means
  the 500th row is now consistently product 273717 rather than either of the two
  products sharing that description.
* Float noise: `sold_ever_amount` differs in the 13th significant digit between
  runs of the unchanged SQL (Postgres summation order).
* Backend: `backend/tools/run_tests.py` — all 13 suites pass (these run on SQLite,
  so the new SQL was exercised on both engines). Frontend: `npm run check` passes.

## Frontend

Audited statically (no screen was rewritten). Changed: the notification bell's
`visibilitychange` handler polled `/notifications/count` (~3 s of server work) on
*hiding* the tab too; it now polls only when the page is visible. Recommended,
not done (they change when screens mount or refresh):

1. After logout or with a stale saved tab, the warehouse Dashboard renders first
   and fires its 10 calls (including the full `/api/notifications`) before the
   redirect to Command/Central — compute the landing tab before rendering.
2. Opening a second tab changes the tree from unwrapped to keyed wrappers, which
   remounts the current screen and refetches everything — always use the keyed
   wrapper.
3. The LR register polls the full `/api/lr` every 15 s while kept alive in a
   hidden tab — pause when not the active tab, or poll a small `?since=` delta.
4. `/api/suppliers`, `/api/inventory/products?held=1` and `/api/purchases` are
   fetched independently by 3–5 screens — a small TTL + in-flight de-dupe in
   `api.js`, cleared on writes.
5. The warehouse Dashboard loads `/api/outward` twice (posted and draft) — one
   call split on the client.

## Remaining bottlenecks

1. **Till sales aggregation — 1.2–1.6 s**, now once per request. It is a full pass
   over 2.1M invoice lines with a `COUNT(DISTINCT)` that spills its sort to disk.
   It dominates Command Center, notifications and dead stock. Options, in order:
   a nightly/on-post rollup table of sales per product (exact, needs a refresh
   hook in the shop's billing); or a short time-based cache of the per-product
   result (needs your approval — figures could lag new bills by the TTL).
2. **Postgres configuration** — `shared_buffers` is the 128 MB default on a
   database of this size; most reads come from the OS cache. Raising it (e.g. to
   1–2 GB, with `effective_cache_size` to match) is a server change for you to
   approve.
3. **`/api/inventory/integrity` — 26 s.** A repair-screen scan over all 417K
   products, 2.46M piece codes and every carton; inherently proportional to the
   data. Could be cut substantially with column-only reads and set-based checks,
   but it is rarely opened and was left as is.
4. **`/api/physical-audit/options` — 4 s**: collects distinct values over ~340K
   products via a Python id list; a `SELECT DISTINCT` per column scoped by a
   subquery would be faster.
5. **`/api/purchases` — 2.9 s** returns all 35.5K GRNs every time; the remaining
   cost is serialising them. Pagination would fix it but changes the API contract.
6. **`/api/notifications/count` — 3 s**, polled by the bell every 60 s: it runs
   every notification rule to produce counts. A cheaper count path, or a short
   cache, would make the poll nearly free.
7. **Shop sessions left "idle in transaction".** On the live `essa` database two
   connections sat idle inside an open transaction for 31–42 minutes, last
   statement a `shop.categories` read — the query `warehouse_items.sync_if_stale`
   issues from the shop's `before_request`. Flask-SQLAlchemy normally ends that
   session at app-context teardown, so some request's context is not being torn
   down (a streamed response abandoned by the client is the likely candidate —
   not reproduced). Such sessions hold locks and block vacuum. Suggested:
   reproduce and fix the teardown; as a safety net, a per-connection
   `idle_in_transaction_session_timeout` for the shop's engine (a behaviour
   change, so not applied without approval).

## Files changed

New: `backend/app/perf.py`, `backend/app/request_memo.py`.
Changed: `backend/app/main.py` (register the two), `database.py` (pooling),
`services/`: `stock_locations.py`, `command_center.py`, `dead_stock.py`,
`pos_sales.py`, `pos_insights.py`, `integrity.py`, `charts.py`, `pricing.py`,
`physical_audit.py`; `routers/`: `outward.py`, `purchases.py`, `suppliers.py`,
`returns.py`, `payments.py`, `inventory.py`, `pricing.py`;
`frontend/src/App.jsx` (bell visibility), frontend rebuilt.

To apply on the running server (:8000), restart it — it was started before these
changes and does not auto-reload.

## Second pass (2026-09-24, afternoon)

Same method: a separate server on `127.0.0.1:8002` against `essa_staging` with
`ESSA_PERF_LOG=1` (the :8001 instance left from the first pass was not touched),
superadmin, warehouse 1 where marked, three sequential runs each. Times are the
median run (two runs for charts and pending payments "before"), ±15%.

| Endpoint | Before (s) | After (s) | Queries before → after |
|---|---:|---:|---|
| `/api/command/overview` (central) | 5.66 | 4.18 | 136 → 92 |
| `/api/command/overview` (warehouse) | 5.27 | 4.21 | 136 → 92 |
| `/api/notifications/count` | 3.54 | 2.88 | 79 → 35 |
| `/api/notifications` | 3.70 | 3.03 | 79 → 35 |
| `/api/dashboard/charts` | 2.44 | 1.61 | 14 → 14 |
| `/api/payments/pending` | 0.94 | 0.65 | 4 → 4 |
| `/api/lr` | 0.18 | 0.05 | 502 → 3 |
| `/api/lr/search` | 0.17 | 0.05 | 503 → 4 |

What changed:

* **LR register — N+1.** `_row_out` read `purchase_order` and `attachments` per
  row (500 lazy loads per call, and the register polls every 15 s). Now
  `joinedload` / `selectinload` with the page (`routers/lr.py`).
* **Command Center `outstanding()`** loaded every column of all ~14.5K posted
  GRNs and the full supplier row; it now loads the four columns it and its
  callers read (`load_only`). Same rows, same order.
* **Date parsing.** `dates.parse` of a string is now cached (`lru_cache`, 4,096
  entries) — `to_iso` depends only on the text. The ageing reads parsed the same
  few hundred invoice dates ~29K times per Command Center load.
* **Transit notification — N+1.** `o.total_qty` loaded each posted outward's
  lines separately (45 queries); the lines now come with the notes
  (`selectinload`), same Python arithmetic.
* **Frontend — landing tab.** The tab/warehouse correction ran in a `useEffect`,
  i.e. after a stale tab (e.g. `dashboard` after logout) had mounted and fired
  its calls. It is now applied during render, so the wrong screen never mounts.

Considered and not done:

* `payments.pending_bills` with `load_only` — saved ~10 ms warm, but the
  narrower rows let Postgres sort in memory, which reorders bills sharing an
  invoice date. Reverted to keep the order identical; the date cache still
  applies there (0.94 → 0.65 s).
* A separate count-only path for `/notifications/count` — every rule has to run
  to produce its count (the dead-stock bands need the till aggregation), so it
  would save only the recipients read. Not worth a second code path.
* Always wrapping the single tab in the keyed `tabpane` (to stop a remount when
  a second tab opens) — changes the DOM of the one-tab layout, which the code
  keeps deliberately unwrapped.

Verification: every endpoint above was diffed against the saved "before" JSON —
identical, except Command Center `low_stock`, whose eight rows are drawn from
products all tied at stock 0 / reorder 5; two runs of the *unchanged* code return
different ties too. `outstanding()`, `pending_bills()`, and the transit/overdue
rules were also diffed in-process: identical. `run_tests.py`: 13/13 pass;
`npm run check` passes; frontend rebuilt.

What remains is the till-sales aggregation (≈1.5 s SQL + ≈0.4 s Python, inside
both the Command Center and the notification bell) — see *Remaining bottlenecks*
§1 and §2; both options need your approval.

## Third pass (2026-09-24): warehouse dashboard and Store dashboard

Same setup (`:8002` on `essa_staging`). Timings on this run were noisier than
the second pass — the same unchanged Command Center measured 5.0 s against
4.2 s earlier — so the dashboard comparison below was run **interleaved**, old
and new pattern alternately, four rounds each, on the same server.

| Screen | Before | After |
|---|---:|---:|
| Warehouse Dashboard — full page load (its data + documents + status + bell) | 9.11 s median (8.46–9.19) | 4.86 s median (4.78–5.01) |
| Store Dashboard (`/pos/`) — page render, in-process | 1.08–1.15 s, SQL ≈1.0 s | 0.26–0.35 s, SQL ≈0.2 s |

### Warehouse Dashboard

It fetched ten whole lists and counted them in the browser: every GRN
(35.5K rows, 16.9 MB — 2.2–2.5 s to build, 0.6 s to encode), every unpaid bill
(2.2 MB), every debit note, supplier and LR row — plus the notification feed
and the dead-stock summary, which each ran the till aggregation in parallel.

* **New `GET /api/dashboard/summary`** (`routers/dashboard.py`) returns just the
  figures the tiles show (31 kB). Each part is computed by the same function the
  screen's list came from, in the same order; float totals are added exactly as
  the desktop's `sum()` adds them (`purchases.js_sum`). The GRN part
  (`purchases.dashboard_figures`) reads counts, draft totals, the shared
  shortage query (`_claimable_shortages`, now also used by the list) and the
  newest six — 0.01–0.05 s instead of ~3 s. The feed and the dead-stock summary
  share one request, so the till aggregation runs once.
* **Permissions unchanged.** Each part is released only if the account could
  GET the original URL (`security.may_read`: role, screen grant, warehouse
  allotment — the middleware's checks). A refused or failed part is `null`, and
  the screen shows "Some figures did not load" exactly as for a refused call.
  Checked with four floor accounts on staging: payments, returns and dead stock
  withheld, matching their direct calls exactly.
* **`payments.pending_bills`** is memoised per request (`request_memo`, GET
  only): the payables tile and the feed's overdue rule both ask.
* **Frontend** (`Dashboard` in `App.jsx`, `api.dashboardSummary`): one call. A
  server without the endpoint (404/405) falls back to the ten lists, reduced to
  the same shape in the browser.
* Existing endpoints are untouched; `/api/purchases` output verified identical
  after the shortage-query refactor.

Verified: all ten summary parts are identical to the figures computed from the
ten lists the old way (`notifications`/`dead-stock` compared field by field,
timestamps excluded).

### Store Dashboard

`routes/main.py` (the shop's `/`) wrapped `invoice_date` in `date()` for today,
7 days, 30 days and each of the 7 trend days — ten full passes over 726K
invoices (~0.1 s each), because the function hides the column from its index.
Now `date(invoice_date) == d` is written `invoice_date >= d 00:00 AND < d+1
00:00`, and `>= d` as `invoice_date >= d 00:00` — the same condition for a
`timestamp without time zone` (the column's type).

Verified: for all 1,435 trading days and 60 cut-offs, the old and new
conditions select the same invoices with the same exact (`numeric`) totals. The
`double precision` sums differ only in the 13th–16th significant digit, which is
summation order — the old query itself returned four different values in four
runs. The rendered dashboard HTML is byte-identical old vs new. Also checked on
SQLite (the shop's other engine) across midnight edges: identical row sets.

Tests: `run_tests.py` 13/13, `npm run check` passes, frontend rebuilt.

Still the floor under both dashboards: the till sales aggregation (~1.9 s,
inside the notification feed and the bell's poll). Options in *Remaining
bottlenecks* §1–2, both awaiting approval.

## Fourth pass (2026-09-24, evening): parallel Command Center, till read, reports

Same setup (`:8002` on `essa_staging`, perf logging on). "Before" is the state
at the start of this pass. Every changed response was compared with the
pre-change one — see *Verification* below.

| Endpoint / screen | Before | After |
|---|---:|---:|
| `/api/command/overview` (HTTP) | 5.0 s | 1.9 s |
| `/api/notifications/count` | 3.0–3.8 s | 1.7 s |
| `/api/notifications` | 3.3 s | 1.7 s |
| `/api/dashboard/summary` (warehouse dashboard) | 3.5–4.2 s | 2.7 s |
| dead stock `product_rows` (behind all of the above) | 2.1 s | 1.1 s |
| `/api/labels/print`, `/api/inventory/labels?ids=` | 4.4–6.2 s | 0.05–0.2 s |
| `/api/physical-audit/options` | 6.8 s | 2.0 s |
| `/api/inventory/products?q=saree&limit=500` | 3.6 s | 1.6 s |
| `/api/inventory/products` (all, 363K rows — the phone's default) | 66.6 s | 46.8 s |
| `/api/purchases` · `/api/outward` · `/api/payments` | 3.8 · 1.2 · 0.5 s | 3.1 · 0.8 · 0.2 s |
| Store report *Direct stock* (30 days, 98,827 rows) | 33.5 s | 3.6 s |
| Store report *Sales / purchase / stock* (30 days) | 14.3 s | 7.3 s |
| Single-day store reports (e.g. *Day summary*) | 0.1–0.4 s | 0.01–0.2 s |

### What changed

* **Till read for dead stock** (`pos_sales.sales_of_stocked`, `dead_stock._product_rows`).
  Dead stock needs the till's sales of the ~6.4K stocked products; it aggregated
  all 2.1M lines into 341K groups. Now the aggregation is restricted to stocked
  products in SQL (0.45 s vs 1.5 s, existing indexes). The stocked set is read in
  the same statement and returned as `covered`; anything stocked since is fetched
  with `sales_of` — so no product can be read as unsold by a race. SQLite tills
  keep the full aggregation. `status()` now asks its two facts with equivalent
  EXISTS / newest-bill queries (0.6 s vs 0.8 s). Verified: all 6,396 rows identical,
  also with 25 products forced through the fallback; status identical.
* **Notification rules** read the dead-stock bands via `dead_stock.alert_levels`
  (no `status()` query they never used). `/api/dead-stock/alerts` is unchanged.
* **`payments._pending_bills`, `command_center.outstanding`** read plain rows with
  the same SQL the ORM issued (same columns, joins, ORDER BY): same rows, same
  order, 2–5× faster. `outstanding()`'s `purchase` is now that row (same `.id`,
  `.grand_total`, `.invoice_date`, `.supplier_id`; nothing reads more).
* **Parallel Command Center** (`command_center._run_groups`). The sections run
  as six groups on a process-wide pool, **each group in its own Session**
  (SessionLocal, closed after); sections sharing a memoised read stay in one
  group (dead stock + notification rules; warehouse totals + transfers + GRNs +
  places). Only plain values leave a group (`_plain` drops ORM objects). Worker
  sessions stay referenced to the end of the request so a recycled `id()` cannot
  hit a stale memo key. `ESSA_DASHBOARD_WORKERS` (default **2**, max 8; 1 = the
  old single-session path; SQLite always 1). Measured, overview median of 3:
  1 worker 2.2–2.5 s, 2 → 1.7 s, 4 → 1.7 s, 6 → 1.7 s — the rest is Python,
  which threads share one interpreter lock for. Output identical for 1/2/4/6
  workers, central and warehouse scope. `alerts()` runs after the groups (its
  last line reads the dead-stock KPI). A failed section is logged (it used to be
  silent) and its worker session rolled back; `_safe` keeps its fallback values.
  With `ESSA_PERF_LOG=1` the overview logs wall time and the slowest sections.
* **JSON encoding** (`app/fastjson.py`, `@returns_json`). FastAPI's
  `jsonable_encoder` walk took 19.4 s of the 22 s spent encoding the 363K-product
  list; `json.dumps` with the same settings (jsonable_encoder only for values it
  cannot write) takes 2.4 s and gives **byte-identical** output. Applied to the
  12 large list routes; the modules keep the plain functions for internal callers.
* **Label printing** scoped `integrity.Context` to the products on the sheet
  instead of the whole catalogue. Identical sheets.
* **`scope.products(..., as_subquery=True)`** filters by the balances query
  instead of shipping up to 363K ids as literals. Used only where the ORDER BY is
  total (inventory list: description, id) or the result is a set (physical-audit
  options). `candidates()` (count sheets, ordered by description only) keeps the
  literal list so its tie order cannot move.
* **Physical-audit options**: DISTINCT per column in SQL, then the same
  strip/sort in Python. Identical to the pre-change response.
* **Store reports and dashboard** (`Textile Retail Shop/app/datefilter.py`):
  `date(col) >= d` / `<= d` / `== d` → `col >= d 00:00` / `col < d+1 00:00` on
  PostgreSQL DateTime columns with a plain date bound; everything else (SQLite,
  Date columns, string bounds) keeps the original expression. 97 filters in
  `reports_lib`, `retail_reports`, `routes/reports`, `routes/pos`, `routes/main`.
  *Direct stock* and *Sales / purchase / stock* loaded each row's product (and
  category) lazily — now `selectinload`.
* **Indexes** — `migration/tqa/sql/86_date_indexes.sql`, five b-tree indexes on
  date columns now used by range filters (measurements in the file). Applied to
  `essa_staging` only.

### Verification

* All 83 parameter-free GET endpoints swept before and after: 67 byte-identical,
  2 differ only in timestamps, 4 explained — Command Center (time-dependent
  "waiting" text, `generated_at`, the low-stock 8 drawn from 411K products tied
  at stock 0), dead-stock register and outward list (floats equal to 1e-9:
  Postgres summation order), `/api/status` (boot time).
* Store reports: 184 report runs (every report in both registries, 30-day and
  1-day windows), old date() filters vs ranges: 161 identical; all 23 others
  return the same rows in a different order. 21 of those differ between two runs
  of the *old* code too; the other 2 (*Sales by category*, *Sales by customer*)
  swap only rows tied on the report's sort value (equal revenue / spend).
* Tests: backend `run_tests.py` 13/13. Shop test scripts pass (retail reports,
  mounted, store access, stock audit, promotions, bill numbers, counter extras,
  delivery). `test_warehouse_sync.py` fails 47 QR-payload checks — pre-existing:
  neither `barcode_svc.py`, `warehouse_items.py` nor the test was changed
  (last written 2026-09-23).

### Remaining

1. **The phone's default product list** (`/api/inventory/products?status=pending`)
   returns 363K products, 320 MB, 47 s. The cost is the size of the answer;
   only pagination in the mobile app would fix it (not changed — mobile app out
   of scope).
2. **The live :8000 server** had used ~15,800 CPU-seconds and 3.6 GB since 13:00
   and took 23.7 s to answer `/api/status` at 17:36 — consistent with repeated
   calls like (1). It runs the code from before this pass.
3. Nine connections on the live `essa` database idle **in transaction** for up to
   4.5 h (the shop-session teardown issue in *Remaining bottlenecks* §7).
4. `/api/inventory/integrity` (34 s) and `/api/purchases` (3.1 s, whole list).
5. The till aggregation for dead stock is now 0.45 s; further only with a rollup
   table (needs approval).

## Elasticsearch read index (2026-09-25)

### Why this workload, and only this one

Every candidate was measured on `essa_staging` before building anything:

| Workload | Where the time goes | Elasticsearch? |
|---|---|---|
| Till sales per stocked product (dead stock → Command Center, bell, warehouse dashboard, dead-stock screens) | 0.45–0.64 s aggregating 2.1M bill lines | **Yes** — built |
| Text searches (inventory, pricing, bills, customers, deliveries) | SQL 1–114 ms; the inventory search's 1.45 s is ORM loading of every match | No — SQL is already fast |
| Central dashboard, today's sales, top products | milliseconds (indexed) | No |
| GRN list 3.1 s, phone product list 47 s | size of the response | No — needs paging |
| Store Inventory page (the 15–31 s screens on the live server) | QR codes drawn in Python for 416,873 products per render | No — needs paging |

### Design (`backend/app/search/`)

* **PostgreSQL stays authoritative.** Nothing is written to Elasticsearch in a
  business transaction; the index is derived and rebuildable.
* **Indices** (aliases over versioned indices, `dynamic: strict`):
  `essa-sales` — one document per (bill, warehouse product): `invoice_id`,
  `wid`, `qty` (Σ quantity), `amount` (Σ line_total), `invoice_date`,
  `location_id`, `live`; `_id` `"<invoice>:<wid>"`. 2,006,519 documents.
  `essa-sales-returns` — one per (credit note, warehouse product). 4,955.
  `essa-meta` — sync state (no PostgreSQL schema change).
* **Read path** (`search/sales.py`, used by `pos_sales._sales_of_stocked`): the
  stocked set is read from PostgreSQL; the index returns Σqty, Σamount,
  bills (= document count, one per bill), last sale. Declines (→ the original
  SQL) when disabled, unreachable, or older than `ESSA_SEARCH_MAX_LAG_SECONDS`.
* **Sync** (`search/sync.py`, background thread started after the shop has
  loaded): new bills / credit notes by id (+ a re-read window, ids can commit
  out of order), cancellations by `cancelled_at` (one-way in the shop code),
  shop products relinked to another warehouse product by bucketed fingerprints.
  Bill lines are never updated after billing (checked in the shop's code).
  An idle pass costs two `MAX()`es; a sweep runs every 5 minutes. Failures are
  logged and retried; the watermark only moves after indexing succeeds.
* **Rebuild** (`search/indexer.py`): new versioned index, filled in id-range
  batches, verified against PostgreSQL (document count, Σqty, Σamount), then
  the alias moves; the previous version is kept.
* **Security:** Elasticsearch listens on 127.0.0.1 only, TLS + password; ESSA
  uses `essa_app`, granted only on `essa-*`. The browser never reaches it. The
  indexed figures are company-wide per-product facts (dead stock is marked
  company-wide on the Command Center); no request parameter reaches a query.
  `/api/search/status` is superadmin-only.

### Measured (essa_staging, HTTP, p50 of 9, same machine, ES on vs off)

| Endpoint | PostgreSQL only | With Elasticsearch | Change |
|---|---:|---:|---:|
| `/api/command/overview` | 1.61 s | 1.25 s | −22 % |
| `/api/command/overview` (warehouse) | 1.62 s | 1.27 s | −22 % |
| `/api/notifications/count` (bell) | 1.41 s | 0.84 s | −40 % |
| `/api/dashboard/summary` (warehouse dashboard) | 2.67 s | 2.04 s | −24 % |
| `/api/dead-stock/summary` | 2.36 s | 1.68 s | −29 % |
| `/api/dead-stock/alerts` | 2.52 s | 1.88 s | −25 % |
| `/api/locations/overview` (central) | 0.10 s | 0.12 s | not affected |

In-process: the sales read 0.64 s → 0.12 s (2 ES queries, ~40 ms). DB queries
per Command Center 91 → 90. A first version that re-read its whole safety
window every 10 s made the Command Center *slower* (1.91 s vs 1.62 s) — the
idle-pass rule above is the fix, measured.

### Verification

* All 340,843 warehouse products with till activity: index == PostgreSQL
  (`es_admin.py compare`): bills and last-sale dates exact; Σqty and Σamount to
  1e-6 relative (both sides add doubles in different orders).
* Command Center and notification feed output identical ES vs PostgreSQL;
  dead-stock rows identical except `sold_ever_amount` / `sold_ever_qty` at
  ≤ 3×10⁻¹⁴ relative (summation order — PostgreSQL alone varies this much).
* `tools/search_test.py` (opt-in: `ESSA_SEARCH_TEST=1`) against essa_staging:
  new bill, cancellation, credit note, product relink and back, stale index
  and unreachable server both fall back, rebuild switches the alias — 14/14.
* `run_tests.py` 14/14 (search test skipped there by default); shop tests pass.

### Operating it

* Start: `essa-intake\run_elasticsearch.bat` (then `run.bat`). ESSA runs
  without it — reads fall back to PostgreSQL.
* Settings: `essa-intake\.env` — `ELASTICSEARCH_ENABLED`, `_URL`, `_USERNAME`,
  `_PASSWORD`, `_CA_CERTS`, `_VERIFY_CERTS`; optional `ESSA_SEARCH_SYNC_SECONDS`
  (10), `ESSA_SEARCH_MAX_LAG_SECONDS` (60), `ESSA_SEARCH_MAPPING_CHECK_SECONDS` (300).
* First time on a database, and whenever in doubt:
  `backend\.venv\Scripts\python.exe tools\es_admin.py rebuild all` (~70 s,
  read-only on PostgreSQL), then `… es_admin.py verify` / `compare`.
* Health: `es_admin.py health` / `status`, or `GET /api/search/status`.
* Superuser password: `C:\elasticsearch\ELASTIC_SUPERUSER.txt` (admin only).

### Not changed by this, still the largest costs

1. **Store Inventory page** — renders a QR per product for all 416,873 active
   shop products; several renders at once saturated the live server (13.4 GB,
   100 % CPU) and are why the live screens took 15–31 s. Needs paging.
2. Phone default product list (47 s) and GRN list (3.1 s) — response size.
3. Dead-stock row building (~0.5 s Python) and the notification rules.

## Every tab (2026-09-25) — paging approved for huge lists

The user approved one behaviour change: lists that loaded a whole table show a
page at a time (search and filters unchanged). No cache, no staged loading.
Everything else below returns the same result as before, verified.

### Store (the shop app)

Measured page by page (in-process on essa_staging, one page at a time — a first
sweep ran pages while a >60 s page was still rendering, which inflated them).

| Store page | Before | After | How |
|---|---:|---:|---|
| Inventory `/inventory/` | >60 s | 0.80 s | paged (100) — every row drew a QR, 416K rows |
| Label sheet `/inventory/barcodes` | >60 s | 0.81 s | paged |
| Low-stock report | 43 s, 114 MB | 0.26 s, 40 kB | paged |
| Customers | 4.9 s, 84 MB | 0.27 s | paged |
| Billing Counter `/pos/` | 1.65 s | 0.13 s | loaded all 150K customers; the template never used them. HTML byte-identical |
| Floor customer lookup (by phone digits) | 1.48 s | 0.13 s | narrowed in SQL, same Python test after, in physical row order (`ctid`) — 200/200 lookups identical |
| Floor session page | — | — | same unused customer list removed |

`app/paging.py` + `templates/_pager.html` (First/Previous/Next/Last, carries every
filter). Sort orders end in the id so no row is on two pages.

### Warehouse

| Endpoint / screen | Before | After | How |
|---|---:|---:|---|
| GRN screen list | 2.6 s + 17 MB (35,566 GRNs) | 0.15 s (one page) | new `/api/purchases/page` — the screen's filter chips, search and counts moved to the server with the browser's exact rules; 13/13 cases identical |
| Phone product list (default: pending) | ~40 s, 363K rows | 0.35–0.84 s per 100 | optional `offset` on `/api/inventory/products`; ids fetched in keyset chunks (PostgreSQL) instead of sorting 363K; pages identical to slices of the full list |
| Phone GRN list | 17 MB | one page | uses `/api/purchases/page`; Load more |
| Inventory search (desktop, limit 500) | 1.45 s | 0.45 s | chunked loading + balances read for the page's rows only; identical |
| Physical-audit options | 1.98 s | 0.72 s | all 13 columns' distinct values in one scan (`GROUPING SETS`, services/distinct.py); identical |
| Product options | 1.34 s | 1.00 s | same; identical |

Unchanged APIs keep their old behaviour: `/api/purchases` and
`/api/inventory/products` without the new parameters return exactly what they did.

### Verification

* All 85 warehouse GET endpoints re-swept: 63 byte-identical to the previous
  sweep; the 10 others differ only because the date moved on (bill ages +1 day,
  14-day windows shifted, products crossing the 90/180-day dead-stock lines) —
  confirmed same-day: dead-stock alerts/summary identical with Elasticsearch on
  and off, register equal to 1e-9.
* Tests: backend 14/14; shop scripts pass; `npm run check` passes; frontend built.

### Still slow

* `/api/inventory/products` called with NO parameters still returns all 363K
  (34 s) — nothing in the apps calls it that way any more.
* `/api/inventory/integrity` (25 s, repair screen), dashboard charts (~1.9 s:
  provenance checks, payables ageing), dead-stock pages (~1.2–2.2 s: the till's
  linked-products count, 0.6 s, is already the fastest form measured).

## Returning to a screen (2026-09-25) — browser read cache, approved

Leaving a screen unmounts it; coming back fetched everything again. Now:

* **Desktop** (`frontend/src/api.js`): every GET under `/api/` that answers JSON
  is remembered for 60 s, keyed by account + warehouse + URL (≤ 60 answers,
  ≤ 20 MB each). Any write from the browser empties it (before and after the
  write; a read in flight during a write is not kept). Never cached: sign-in,
  `/api/status`, `/api/notifications*` (the bell is live), the LR register's
  15 s poll, `/api/search/*`, anything printed or exported. Identical requests
  made together share one call. Cleared on sign-in/out and warehouse switch.
* **Dashboards** (Command Center, Central, warehouse Dashboard, charts): `swr` —
  the remembered figures at once, then the server's a round-trip later.
* **Refresh buttons** empty the cache and reload (`readCache.refresh`).
* **Phone** (`backend/app/mobile/index.html`): the same rules in its `api()`;
  uploads, logout and the ↻ chips empty it.
* **Store tabs**: not cached on purpose. A Store tab in the tab strip stays
  loaded already; browser-caching the shop's pages would show a page as it was
  before your own save (bill, return), which the rule above forbids.

Trade-off: a change made by another person or device can take up to 60 s to
appear on a non-dashboard screen, unless ↻ is pressed.

Verified by `frontend/tools/cache_test.mjs` (in `npm run check`), 13 rules
against a fake server: hit within 60 s, fresh copy per read, write empties it,
warehouse and account separation, bell and LR poll never cached, shared
in-flight request, Refresh bypass, failures not remembered, read overlapping a
write not kept, dashboard shows remembered then fresh. Phone script syntax
checked; not yet exercised on a device.

## Dashboards always ready, and the reports (2026-09-25)

### Dashboards: kept on screen for the session

Command Center, Central Dashboard, the warehouse Dashboard and the Store
dashboard now stay mounted once opened (`App.jsx`: the dashboard "stash", up to
8, one per warehouse for the warehouse and Store dashboards). Switching between
them shows the one already drawn; `useReshow` then asks the server again in the
background and redraws if anything changed. Their answers are kept in the
browser for the whole session (`api.js` `ALWAYS`): a save elsewhere does not
drop them, a different account or warehouse never sees them, sign-out empties
them.

Measured in Chrome against the staging server (`dash_switch.mjs`): after the
first open, every switch among the four showed in **8–24 ms**, no page errors.

### Warehouse reports: the per-row queries removed

The slow reports walked ORM relationships one object at a time
(`purchase.lines`, `line.splits`, `split.product`, `movement.product`,
`product.movements`, a carton count per GRN, three payment queries per
invoice, a full re-aggregation per supplier). On staging (35K GRNs, 850K GRN
lines, 420K splits, 417K products, 970K movements) that was up to 2 million
queries per report. They now read the same rows in a handful of statements:

* `_lines_of` — every GRN line with its splits, shortages and product, in
  three statements (id order, as the relationships declare). The stand-in
  objects borrow the model's own `received_qty`, `effective_rate`,
  `variant_label`, `signed_qty`, `claimable`, so every figure is computed by
  the same code as before.
* `_outwards` — dispatches with their lines, borrowing the model's
  `total_qty` / `total_accepted` / `shortfall`.
* Products and movements read as plain rows (same statement, same ORDER BY).
* Carton counts, settlements and returns read once, grouped.
* `payments.pending_bills_by_supplier` — every supplier's bills from one pass.
* The report route writes its JSON with `fastjson` (same bytes).

Service time, staging, default filters (before = unchanged code):

| Report | Before | After |
|---|---:|---:|
| WH Entry | 788 s (2.13M queries) | 7.8 s |
| Purchase Items | 541 s (1.69M queries) | 8.1 s |
| Stock Movement | 146 s (363K queries) | 9.3 s |
| Stock – Transactions | 126 s | 8.0 s |
| Stock – As on Date | 149 s (417K queries) | 8.3 s |
| Purchase HSN | 497 s | 6.7 s |
| Purchase Barcode-wise | 540 s (2.05M queries) | 7.9 s |
| Section-wise Purchase | 432 s | 6.5 s |
| Outward Details | 124 s (335K queries) | 8.7 s |
| Supplier Pending Bills | 82 s (13K queries) | 0.4 s |
| Product Master | 12.1 s | 7.1 s |
| Stock Report | 15.1 s | 6.6 s |
| Outward Report | 10.8 s | 2.6 s |
| Stock Movement – Locationwise | 9.7 s | 1.8 s |
| Warehouse Stock Analysis | 10.0 s | 4.9 s |
| Tax / HSN Master | 6.2 s | 1.4 s |
| Purchase Return Audit | 5.2 s | 1.1 s |
| Section-wise Purchase Return | 4.7 s | 0.7 s |

Timings were measured while other benchmark processes shared the machine.
Reports under 1 s before are unchanged.

**Same results.** Every report's output was compared to the unchanged code's
output on staging. 33 reports are byte-for-byte identical, including the
largest (Purchase Items 170 MB, Stock Movement 207 MB, Stock Transactions
224 MB). The other three (Stock Report, Product Master, Stock – As on Date)
have the same rows and totals, in a different order among products whose
description is identical. Those reports sort by description alone, and
PostgreSQL sorts them in parallel, so the old code itself gives a different
order among those rows on every run (two runs of the unchanged code differ in
the same way). Staging has no shortages, cartons or invoice documents, so the
unchanged and new code were also run side by side on a synthetic SQLite
database that has all of them (short, damaged and excess rows; splits with and
without their own rate; lines with no product; outwards of every kind). All
36 reports, each with 4 filter sets, were identical.

### Reports: instant when you come back

The Reports screen keeps each report it has shown, already parsed, for the
session. The key is account + warehouse + report + filters. Kept reports are
capped at 1.5M rows in total, and the least recently viewed are dropped first.
Opening a report again shows it at once, then asks the server and replaces it
with the server's answer. The row filter is memoised, so a 400K-row report is
not re-filtered on every render.

Chrome against staging (`reports_switch.mjs`):

| Report | First open | Opened again |
|---|---:|---:|
| Purchase Report (35K rows) | 0.8 s | 15 ms |
| Stock Report (417K rows) | 5.2 s | 13–14 ms |
| Purchase Items (567K rows) | 11.8 s | 28 ms |
| Outward Details (448K rows) | 7.9 s | — |

No page errors. Backend tests (14 suites) and `npm run check` pass.

## Dashboards under a second, with or without Elasticsearch (2026-09-26)

**Symptom.** Command Center, warehouse dashboard and the bell each took ~14 s.
**Cause.** Elasticsearch was not running. On Windows a refused local connection
takes ~2 s; with the client's retries and several reads per screen, every load
waited ~14 s before PostgreSQL answered (21 reads, 299 s lost on staging).

**Fixes** (same figures — outputs compared with the unchanged code on staging):

* `search/client.py` — a breaker: a connection failure marks Elasticsearch down
  for 60 s and reads go straight to PostgreSQL; reads use a 3 s timeout and no
  retries. The sync thread asks it once per pass and marks it up the moment it
  answers.
* `request_memo` — safe across a request's worker threads: two sections asking
  for the same read share one computation (the second waits for the first).
* Dashboard sections run side by side (`command_center.warm_groups`, the warehouse
  summary on the same pool); the notices no longer queue behind dead stock.
* `dead_stock._product_rows` reads plain rows instead of ORM objects (0.35 s,
  was ~1.1 s) and its own reads before the till's.
* The till's two slowest facts (sales of stocked products, till status) and its
  low-stock list are kept in memory for at most 60 s — the same bound the
  Elasticsearch copy is held to — refreshed in the background while dashboards
  are in use, and read at server start. Our own ledger (stock, GRNs, payables)
  is read live on every load.

| Endpoint (staging, ES down) | Before | After |
|---|---:|---:|
| `/api/command/overview` | ~14.0 s | 0.83–0.99 s |
| `/api/dashboard/summary` | ~14.6 s | 0.54–0.70 s |
| `/api/notifications/count` | ~13.7 s | 0.61–0.91 s |
