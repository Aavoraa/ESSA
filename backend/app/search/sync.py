"""Keeping the index in step with PostgreSQL — after the fact, never in the way.

Nothing here runs inside a business transaction. A bill is written, committed
and answered exactly as before; a background thread notices it within
ESSA_SEARCH_SYNC_SECONDS and indexes it. If Elasticsearch is down the thread
logs, keeps its watermark where it was and tries again; reads meanwhile see the
index going stale and use PostgreSQL (see sales.fresh). No event can be lost:
the watermark only moves after the documents behind it are indexed.

What changes, and how each change is caught (the shop's code was read for
every write to these tables):

* new bills / credit notes — by id. A pass that finds new ids also re-reads
  RECENT_IDS below the watermark, because a transaction that took a lower id
  can commit after a higher one; every RESCAN_SECONDS a sweep re-reads
  RESCAN_IDS. Re-indexing is idempotent, so the overlap is harmless. A pass
  with nothing new reads two MAX()es and stops — the sync must not cost the
  server the time it exists to save (measured: re-reading the window every
  10 s made the Command Center slower, not faster).
* a bill cancelled — cancellation.py sets payment_status and cancelled_at, one
  way. Caught by cancelled_at; the sweep re-reads the last RESCAN_CANCELLED.
* bill lines — never updated after the bill is written (the quantity edits in
  routes/floor.py are SaleSessionItem, before billing).
* a shop product linked to a different warehouse product
  (warehouse_items.sync sets products.warehouse_id) — the document's `wid`.
  Caught by fingerprinting the link in buckets of ids; the bills and credit
  notes of a changed bucket are re-indexed in full.
"""
import datetime as dt
import hashlib
import logging
import threading
import time

from ..database import IS_SQLITE, engine as _default_engine
from . import client as es_client, config, indexer, indexes, sources

log = logging.getLogger("uvicorn.error")

RESCAN_IDS = 2000               # the periodic sweep below the watermark
RECENT_IDS = 200                # re-read with every pass that finds new rows
RESCAN_SECONDS = 300            # how often the full sweep runs
RESCAN_CANCELLED = dt.timedelta(minutes=10)
BUCKET = 500

_state = {}                  # name -> state, as last saved by THIS process
_lock = threading.Lock()
_thread = None
_stop = threading.Event()


# ------------------------------------------------------------------ state ---
def load_state(es, name):
    try:
        doc = es.get(index=indexes.alias(indexes.META), id=name)
        return doc["_source"]["state"]
    except Exception:                                   # noqa: BLE001 — none yet
        return None


def save_state(es, name, state):
    indexes.ensure_meta(es)
    es.index(index=indexes.alias(indexes.META), id=name, document={"state": state}, refresh=True)
    _state[name] = state


def cached_state(es, name, max_age=5.0):
    """State for freshness checks: this process's own copy, or re-read every
    few seconds (a sync running in another process — the CLI — also counts)."""
    st = _state.get(name)
    if st is not None and time.time() - st.get("_read_at", st.get("last_sync_at", 0)) < max_age:
        return st
    st = load_state(es, name)
    if st is not None:
        st["_read_at"] = time.time()
        _state[name] = st
    return st


def watermarks_now(conn):
    inv, cn = sources.max_ids(conn)
    from sqlalchemy import text
    newest_cancel = conn.execute(text("SELECT MAX(cancelled_at) FROM " + sources.pos_sales.q("invoices"))).scalar()
    return {"invoice_id": int(inv), "credit_note_id": int(cn),
            "cancelled_at": newest_cancel.isoformat() if newest_cancel else None,
            "links": _link_buckets(sources.product_links(conn))}


def _link_buckets(links):
    by = {}
    for pid in sorted(links):
        by.setdefault(pid // BUCKET, []).append(f"{pid}:{links[pid]}")
    return {str(b): hashlib.md5(",".join(v).encode()).hexdigest()[:16] for b, v in by.items()}


# ------------------------------------------------------------------- sync ---
def run_once(es=None, engine=None):
    """One pass. Returns what it did; raises on failure (watermarks unmoved).

    Imports are at module level, not here: the shop is loaded by swapping the
    `app` package for its own while its startup sync runs (pos_mount), and an
    import made from this thread inside that window resolves to the shop."""
    es = es or es_client.client()
    engine = engine or _default_engine
    if es is None:
        return {"skipped": "elasticsearch disabled"}
    with _lock:
        sales, rets = load_state(es, indexes.SALES), load_state(es, indexes.RETURNS)
        if not sales or not rets:
            return {"skipped": "not built yet — run tools/es_admin.py rebuild"}
        t0 = time.time()
        did = {"bills": 0, "cancelled": 0, "credit_notes": 0, "relinked_bills": 0}
        with engine.connect() as conn:
            top_inv, top_cn = sources.max_ids(conn)
            # An idle pass reads two MAX()es and nothing else. New rows are read
            # with a short window below the watermark (a lower id can commit
            # after a higher one); every RESCAN_SECONDS a longer sweep re-reads
            # RESCAN_IDS and the last RESCAN_CANCELLED of cancellations, for a
            # late commit that arrived while nothing else was being written.
            sweep = time.time() - float(sales.get("swept_at", 0)) >= RESCAN_SECONDS
            wm_inv, wm_cn = int(sales["invoice_id"]), int(rets["credit_note_id"])
            if sweep or top_inv > wm_inv:
                lo = max(0, wm_inv - (RESCAN_IDS if sweep else RECENT_IDS))
                for a in range(lo + 1, top_inv + 1, 20000):
                    did["bills"] += indexer.bulk_index(es, indexes.alias(indexes.SALES),
                                                       sources.sales_docs_between(conn, a, min(a + 19999, top_inv)))
            # cancellations: newer than the last one seen, or the sweep window
            mark = sales.get("cancelled_at")
            if not mark:
                since = dt.datetime(1900, 1, 1)
            else:
                since = dt.datetime.fromisoformat(mark) - (RESCAN_CANCELLED if sweep else dt.timedelta(0))
            ids, newest = sources.cancelled_since(conn, since)
            if not sweep and mark:
                # only if something was cancelled after the last one seen
                ids = ids if newest and newest.isoformat() > mark else []
            if ids:
                did["cancelled"] = indexer.bulk_index(es, indexes.alias(indexes.SALES),
                                                      sources.sales_docs_for(conn, ids))
            # new credit notes
            if sweep or top_cn > wm_cn:
                lo = max(0, wm_cn - (RESCAN_IDS if sweep else RECENT_IDS))
                for a in range(lo + 1, top_cn + 1, 20000):
                    did["credit_notes"] += indexer.bulk_index(es, indexes.alias(indexes.RETURNS),
                                                              sources.returns_docs_between(conn, a, min(a + 19999, top_cn)))
            # shop products relinked to another warehouse product
            links_state = sales.get("links") or {}
            if time.time() - float(sales.get("links_checked_at", 0)) >= config.MAPPING_CHECK_SECONDS:
                links = sources.product_links(conn)
                now = _link_buckets(links)
                changed = {b for b in set(now) | set(links_state) if now.get(b) != links_state.get(b)}
                if changed:
                    pids = [p for p in links if str(p // BUCKET) in changed]
                    inv_ids, cn_ids = sources.invoices_with_products(conn, pids)
                    indexer.replace_docs(es, indexes.alias(indexes.SALES), "invoice_id", inv_ids,
                                         sources.sales_docs_for(conn, inv_ids))
                    indexer.replace_docs(es, indexes.alias(indexes.RETURNS), "credit_note_id", cn_ids,
                                         sources.returns_docs_for(conn, cn_ids))
                    did["relinked_bills"] = len(inv_ids)
                    log.info("search: %d shop-product bucket(s) relinked; %d bills, %d credit notes re-indexed",
                             len(changed), len(inv_ids), len(cn_ids))
                links_state, links_checked = now, time.time()
            else:
                links_checked = float(sales.get("links_checked_at", 0))
        if any(did.values()):
            es.indices.refresh(index=indexes.alias(indexes.SALES))
            es.indices.refresh(index=indexes.alias(indexes.RETURNS))
        newest_iso = newest.isoformat() if newest else mark
        if mark and newest_iso and newest_iso < mark:
            newest_iso = mark
        save_state(es, indexes.SALES, {**sales, "invoice_id": int(top_inv), "cancelled_at": newest_iso,
                                       "links": links_state, "links_checked_at": links_checked,
                                       "swept_at": t0 if sweep else float(sales.get("swept_at", 0)),
                                       "last_sync_at": t0})
        save_state(es, indexes.RETURNS, {**rets, "credit_note_id": int(top_cn), "last_sync_at": t0})
        did["seconds"] = round(time.time() - t0, 3)
        return did


# ------------------------------------------------------ background thread ---
def start_background():
    """Run `run_once` every ESSA_SEARCH_SYNC_SECONDS in a daemon thread.

    Only where the shop shares the warehouse's PostgreSQL database — the index
    is built from both — and only on a long-lived server (not serverless)."""
    global _thread
    if not config.ENABLED or IS_SQLITE or _thread is not None:
        return False
    _stop.clear()

    def loop():
        failures = 0
        while not _stop.is_set():
            # One quick question first. The pass below swallows a missing server
            # (no state -> "not built yet"), so without this a dead Elasticsearch
            # was never noticed here — and every dashboard read found out alone.
            es = es_client.client()
            if es is not None:
                try:
                    es_client.for_reads(es).info()
                    es_client.mark_up()
                except Exception as exc:                # noqa: BLE001 — reads fall back
                    es_client.mark_down(exc)
                    _stop.wait(config.SYNC_SECONDS)
                    continue
            try:
                did = run_once()
                if failures:
                    log.info("search: sync recovered after %d failed pass(es)", failures)
                failures = 0
                if isinstance(did, dict) and any(did.get(k) for k in ("cancelled", "relinked_bills")):
                    log.info("search: sync %s", did)
            except Exception as exc:                    # noqa: BLE001 — logged; reads fall back
                failures += 1
                if failures in (1, 10) or failures % 100 == 0:
                    log.warning("search: sync failed (%d in a row) — reads use PostgreSQL until it "
                                "recovers: %s: %s", failures, type(exc).__name__, str(exc)[:300])
            _stop.wait(config.SYNC_SECONDS)

    _thread = threading.Thread(target=loop, name="essa-search-sync", daemon=True)
    _thread.start()
    return True


def stop_background():
    _stop.set()
