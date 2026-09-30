"""The Elasticsearch read index against PostgreSQL, through every kind of change.

    set ESSA_SEARCH_TEST=1
    .venv\\Scripts\\python.exe tools\\search_test.py            (against essa_staging)

run_tests.py collects it too, and it reports "skipped" unless that is set.

Needs a running Elasticsearch (run_elasticsearch.bat) and a PostgreSQL copy with
the shop schema. It WRITES test bills to that database — so it refuses to run
against the live `essa` database — and removes everything it wrote at the end.
It builds its own `essa-test-*` indices and deletes them afterwards.

Checked: a new bill, a cancellation, a credit note, a shop product relinked to
another warehouse product (and back), a stale index and an unreachable server
(both must fall back to PostgreSQL), and a rebuild switching the alias.
"""
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# Opt-in: run_tests.py collects every *_test.py and its suites are self-contained
# SQLite runs; this one needs Elasticsearch and writes test bills to staging.
if os.environ.get("ESSA_SEARCH_TEST") != "1":
    print("skipped — set ESSA_SEARCH_TEST=1 to run (needs Elasticsearch and essa_staging)")
    sys.exit(0)
sys.path.insert(0, os.path.dirname(HERE))
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
url = os.environ["ESSA_DATABASE_URL"]
if url.rstrip("/").endswith("/essa"):
    url = url.rstrip("/")[:-4] + "essa_staging"            # never the live database
os.environ["ESSA_DATABASE_URL"] = url
os.environ["ELASTICSEARCH_INDEX_PREFIX"] = "essa-test-"
os.environ["ESSA_SEARCH_MAPPING_CHECK_SECONDS"] = "0"      # check links on every pass
assert not url.rstrip("/").endswith("/essa"), "refusing to write test bills to the live database"

from sqlalchemy import text  # noqa: E402
from app.database import engine  # noqa: E402
from app.search import client as es_client, indexer, indexes, sales as es_sales, sync  # noqa: E402
from app.services import pos_sales  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def same(wids, label):
    """The index and PostgreSQL agree on these warehouse products."""
    got, want = es_sales.sales_of(wids), pos_sales.sales_of(wids)
    if got is None:
        return check(label, False, "index not used (stale / unreachable)")
    bad = [w for w in set(got) | set(want)
           if (got.get(w) or {}).get("bills") != (want.get(w) or {}).get("bills")
           or (got.get(w) or {}).get("last_sold") != (want.get(w) or {}).get("last_sold")
           or not indexer.close((got.get(w) or {}).get("qty"), (want.get(w) or {}).get("qty"))
           or not indexer.close((got.get(w) or {}).get("amount"), (want.get(w) or {}).get("amount"))]
    check(label, not bad, f"{bad[:3]}: es {[got.get(w) for w in bad[:1]]} pg {[want.get(w) for w in bad[:1]]}")
    return want


es = es_client.client()
if es is None or not es_client.health().get("reachable"):
    sys.exit("Elasticsearch is not reachable — start run_elasticsearch.bat")
q = pos_sales.q
made = {"invoices": [], "notes": [], "relink": None}
try:
    print("building essa-test-* from PostgreSQL …")
    for name in (indexes.SALES, indexes.RETURNS):
        r = indexer.rebuild(es, engine, name)
        check(f"rebuild {name}: {r['documents']:,} documents, verified against PostgreSQL", r["verify"]["ok"], r["verify"])
    sync.run_once(es, engine)

    with engine.begin() as c:
        p_id, wid = c.execute(text("SELECT id, warehouse_id FROM " + q("products") +
                                   " WHERE warehouse_id IS NOT NULL ORDER BY id LIMIT 1")).one()
        cashier = c.execute(text("SELECT id FROM " + q("users") + " ORDER BY id LIMIT 1")).scalar()
    before = same([wid], "baseline: index == PostgreSQL for the test product")

    # 1 · a new bill
    stamp = int(time.time())
    with engine.begin() as c:
        inv = c.execute(text("INSERT INTO " + q("invoices") + " (invoice_number, cashier_id, invoice_date, payment_status, total) "
                             "VALUES (:n, :c, :d, 'paid', 200) RETURNING id"),
                        {"n": f"ESTEST-{stamp}-1", "c": cashier, "d": dt.datetime.utcnow()}).scalar()
        item = c.execute(text("INSERT INTO " + q("invoice_items") + " (invoice_id, product_id, quantity, unit_price, line_total) "
                              "VALUES (:i, :p, 2, 100, 200) RETURNING id"), {"i": inv, "p": p_id}).scalar()
    made["invoices"].append(inv)
    sync.run_once(es, engine)
    after = same([wid], "new bill appears after one sync pass")
    check("…and it counted: one more bill, two more pieces",
          after[wid]["bills"] == (before.get(wid) or {}).get("bills", 0) + 1)

    # 2 · cancelled
    with engine.begin() as c:
        c.execute(text("UPDATE " + q("invoices") + " SET payment_status='cancelled', cancelled_at=:t WHERE id=:i"),
                  {"t": dt.datetime.utcnow(), "i": inv})
    sync.run_once(es, engine)
    now = same([wid], "cancellation reaches the index")
    check("…and the cancelled bill no longer counts", now[wid]["bills"] == (before.get(wid) or {}).get("bills", 0))

    # 3 · a credit note against a live bill
    with engine.begin() as c:
        inv2 = c.execute(text("INSERT INTO " + q("invoices") + " (invoice_number, cashier_id, invoice_date, payment_status, total) "
                              "VALUES (:n, :c, :d, 'paid', 300) RETURNING id"),
                         {"n": f"ESTEST-{stamp}-2", "c": cashier, "d": dt.datetime.utcnow()}).scalar()
        item2 = c.execute(text("INSERT INTO " + q("invoice_items") + " (invoice_id, product_id, quantity, unit_price, line_total) "
                               "VALUES (:i, :p, 3, 100, 300) RETURNING id"), {"i": inv2, "p": p_id}).scalar()
        note = c.execute(text("INSERT INTO " + q("credit_notes") + " (number, invoice_id, cashier_id, created_at, total) "
                              "VALUES (:n, :i, :c, :d, 100) RETURNING id"),
                         {"n": f"ESTEST-CN-{stamp}", "i": inv2, "c": cashier, "d": dt.datetime.utcnow()}).scalar()
        c.execute(text("INSERT INTO " + q("credit_note_items") + " (credit_note_id, invoice_item_id, product_id, quantity, unit_price, line_total) "
                       "VALUES (:n, :it, :p, 1, 100, 100)"), {"n": note, "it": item2, "p": p_id})
    made["invoices"].append(inv2)
    made["notes"].append(note)
    sync.run_once(es, engine)
    same([wid], "credit note nets off in the index as in PostgreSQL")

    # 4 · a shop product relinked to another warehouse product, then back
    with engine.begin() as c:
        p2, w2 = c.execute(text("SELECT p.id, p.warehouse_id FROM " + q("products") + " p WHERE p.warehouse_id IS NOT NULL "
                                "AND EXISTS (SELECT 1 FROM " + q("invoice_items") + " ii WHERE ii.product_id = p.id) "
                                "ORDER BY p.id DESC LIMIT 1")).one()
        w3 = c.execute(text("SELECT warehouse_id FROM " + q("products") + " WHERE warehouse_id IS NOT NULL "
                            "AND warehouse_id <> :w ORDER BY id LIMIT 1"), {"w": w2}).scalar()
        made["relink"] = (p2, w2)
        c.execute(text("UPDATE " + q("products") + " SET warehouse_id=:w WHERE id=:p"), {"w": w3, "p": p2})
    sync.run_once(es, engine)
    same([w2, w3], "relinked shop product: both warehouse products agree after sync")
    with engine.begin() as c:
        c.execute(text("UPDATE " + q("products") + " SET warehouse_id=:w WHERE id=:p"), {"w": w2, "p": p2})
    made["relink"] = None
    sync.run_once(es, engine)
    same([w2, w3], "…and again after linking it back")

    # 5 · a stale index is not used
    st = sync.load_state(es, indexes.SALES)
    sync.save_state(es, indexes.SALES, {**st, "last_sync_at": 0})
    check("stale index: the index read declines (None)", es_sales.sales_of([wid]) is None)
    pg = pos_sales._sales_of_stocked()
    check("stale index: dead stock still answers, from PostgreSQL", pg is not None and len(pg[1]) > 0)
    sync.save_state(es, indexes.SALES, st)

    # 6 · an unreachable server is not fatal
    from elasticsearch import Elasticsearch
    real = es_client._client
    es_client._client = Elasticsearch("https://127.0.0.1:9299", request_timeout=1, max_retries=0, verify_certs=False)
    t = time.perf_counter()
    got = es_sales.sales_of([wid])
    check("unreachable server: declines (None) quickly", got is None and time.perf_counter() - t < 5,
          f"{time.perf_counter() - t:.1f}s")
    es_client._client = real

    # 7 · a rebuild moves the alias and keeps the previous index
    old = indexes.current_index(es, indexes.RETURNS)
    r = indexer.rebuild(es, engine, indexes.RETURNS)
    check("rebuild switches the alias to the new, verified index",
          indexes.current_index(es, indexes.RETURNS) == r["index"] != old and r["previous"] == old)
finally:
    print("cleaning up …")
    with engine.begin() as c:
        if made["relink"]:
            c.execute(text("UPDATE " + q("products") + " SET warehouse_id=:w WHERE id=:p"),
                      {"w": made["relink"][1], "p": made["relink"][0]})
        if made["notes"]:
            c.execute(text("DELETE FROM " + q("credit_note_items") + " WHERE credit_note_id = ANY(:n)"), {"n": made["notes"]})
            c.execute(text("DELETE FROM " + q("credit_notes") + " WHERE id = ANY(:n)"), {"n": made["notes"]})
        if made["invoices"]:
            c.execute(text("DELETE FROM " + q("invoice_items") + " WHERE invoice_id = ANY(:i)"), {"i": made["invoices"]})
            c.execute(text("DELETE FROM " + q("invoices") + " WHERE id = ANY(:i)"), {"i": made["invoices"]})
    for ix in list(es.indices.get(index="essa-test-*")):
        es.indices.delete(index=ix)
print(f"\n{'all checks passing' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
