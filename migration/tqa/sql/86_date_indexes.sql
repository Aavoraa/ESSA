-- 86 · Indexes on the date columns the dashboards and reports window by
--
-- Performance only: an index changes how fast the same rows are found, never
-- which rows. Each was measured on essa_staging (best of 3, July 2026 window)
-- before being added, and EXPLAIN confirms the planner uses it:
--
--   shop.stock_movements.created_at   (2.66M rows) direct stock, shelf period
--                                     30 days 0.065 s -> 0.011 s, 1 day 0.068 s -> 0.000 s
--   shop.invoice_payments.created_at  (724K) settlement reports
--                                     0.022 s -> 0.001 s
--   shop.transfer_receipts.applied_at (446K) sales / purchase / stock
--                                     0.021 s -> 0.000 s
--   shop.customers.created_at         (151K) new customers in a period
--                                     0.020 s -> 0.000 s
--   public.stock_movements.created_at (971K) Command Center movement tiles and
--                                     14-day series, twice per load
--                                     0.041 s -> 0.001 s
--
-- The shop's report filters were rewritten from date(col) >= d to the
-- equivalent col >= d 00:00 (Textile Retail Shop/app/datefilter.py) so that
-- these indexes — and the existing ones on invoices.invoice_date and
-- credit_notes.created_at — can be used at all.
--
-- Plain CREATE INDEX, like step 85, because migrate.py runs every step inside
-- one transaction (CONCURRENTLY is not allowed there). On a live database that
-- is taking writes, the same five can be built without blocking them by
-- running each statement by hand as `create index concurrently if not exists …`
-- outside a transaction; `if not exists` makes either form safe to repeat.

create index if not exists ix_shop_stock_movements_created_at
    on shop.stock_movements (created_at);
create index if not exists ix_shop_invoice_payments_created_at
    on shop.invoice_payments (created_at);
create index if not exists ix_shop_transfer_receipts_applied_at
    on shop.transfer_receipts (applied_at);
create index if not exists ix_shop_customers_created_at
    on shop.customers (created_at);
create index if not exists ix_stock_movements_created_at
    on public.stock_movements (created_at);

analyze shop.stock_movements;
analyze shop.invoice_payments;
analyze shop.transfer_receipts;
analyze shop.customers;
analyze public.stock_movements;
