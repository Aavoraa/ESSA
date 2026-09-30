"""Elasticsearch as a secondary, rebuildable READ index for ESSA.

PostgreSQL is the source of truth for everything, always. What lives in
Elasticsearch is derived from it, can be thrown away and rebuilt from it at any
time (`tools/es_admin.py rebuild`), and is only ever READ by the application —
no business write goes anywhere but PostgreSQL.

What is indexed, and why only that
----------------------------------
Measured first (docs/PERFORMANCE_REPORT.md, "Elasticsearch"). The one heavy
read left on the Command Center, the notification bell, the warehouse
dashboard and the dead-stock screens is the till's sales per warehouse product:
an aggregation over 2.1M bill lines. That is what is indexed:

  essa-sales          one document per (bill, warehouse product): qty, amount,
                      bill date, store, and whether the bill is live
  essa-sales-returns  one document per (credit note, warehouse product)

Searches, lists and the other dashboard figures were measured too and are
already served by PostgreSQL in milliseconds; they are deliberately left there.

How it stays correct
--------------------
* Bill lines never change after the bill is written and a bill only ever
  changes by being cancelled (one way) — so the sync follows new bills and new
  credit notes by id (re-reading a safety window, because ids can commit out of
  order), cancellations by `cancelled_at`, and re-indexes the bills of any shop
  product whose warehouse link changed. See `sync`.
* Every read checks freshness first. If the last successful sync is too old,
  or the index is missing, or Elasticsearch is down, the caller gets None and
  takes the PostgreSQL path it always had. See `sales.sales_of_stocked`.
* Document ids are derived from primary keys, so indexing is idempotent.
* Indices are versioned behind aliases; a rebuild fills a new index, checks it
  against PostgreSQL, and only then moves the alias. See `indexes`, `indexer`.
"""
