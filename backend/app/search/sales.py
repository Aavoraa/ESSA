"""Reads served from the sales index — each with the PostgreSQL function it
stands in for, the same return shape, and None whenever it cannot be trusted.

None means "use PostgreSQL": Elasticsearch disabled or unreachable, the index
missing, the last sync older than ESSA_SEARCH_MAX_LAG_SECONDS, or any error.
The PostgreSQL path the caller falls back to is the one it had before this
existed — for the dead-stock read, 0.45 s, not an expensive rescue.

Nothing here is user-scoped: the figures are the company-wide per-product
facts dead stock is defined on (the Command Center marks dead stock
"company_wide"), and no request parameter reaches the query. Any future
user-facing search must apply the caller's warehouse allotment as a filter
here, the way services/scope does for PostgreSQL.
"""
import logging
import time

from . import client as es_client, config, indexes, sync

log = logging.getLogger("uvicorn.error")
_last_warn = [0.0]

#: request-level numbers for the perf log / benchmarks: calls and seconds
stats = {"calls": 0, "seconds": 0.0, "fallbacks": 0}


def _warn(msg, *a):
    if time.time() - _last_warn[0] > 60:              # at most once a minute
        _last_warn[0] = time.time()
        log.warning(msg, *a)


def _unreachable(exc):
    """A connection or timeout failure — the server, not the query."""
    name = type(exc).__name__
    return "Connection" in name or "Timeout" in name or "TransportError" in name


def fresh(es, name):
    st = sync.cached_state(es, name)
    return bool(st) and time.time() - float(st.get("last_sync_at", 0)) <= config.MAX_LAG_SECONDS


def _merge_row(out, wid, qty, amount, bills, last):
    out[int(wid)] = {"qty": float(qty or 0), "amount": float(amount or 0), "bills": int(bills or 0),
                     "last_sold": str(last or "")[:10] or None}


def sales_of(product_ids):
    """`pos_sales.sales_of(ids)` from the index: {wid: {qty, amount, bills,
    last_sold}} — sales net of returns, exactly as `pos_sales._merge` builds it.
    None → use PostgreSQL."""
    es = es_client.usable()
    if es is None:
        if es_client.is_down():
            stats["fallbacks"] += 1
        return None
    es = es_client.for_reads(es)
    t0 = time.perf_counter()
    try:
        if not (fresh(es, indexes.SALES) and fresh(es, indexes.RETURNS)):
            stats["fallbacks"] += 1
            _warn("search: sales index older than %ss — using PostgreSQL", config.MAX_LAG_SECONDS)
            return None
        ids = sorted({int(i) for i in product_ids})
        if not ids:
            return {}
        out = {}
        # sold: live bills only; one document per bill, so doc_count is the
        # number of distinct bills (COUNT(DISTINCT i.id) in the SQL)
        for i in range(0, len(ids), 10000):
            part = ids[i:i + 10000]
            r = es.search(index=indexes.alias(indexes.SALES), size=0, request_cache=False,
                          query={"bool": {"filter": [{"terms": {"wid": part}}, {"term": {"live": True}}]}},
                          aggs={"p": {"terms": {"field": "wid", "size": len(part)}, "aggs": {
                              "q": {"sum": {"field": "qty"}}, "a": {"sum": {"field": "amount"}},
                              "d": {"max": {"field": "invoice_date"}}}}})
            for b in r["aggregations"]["p"]["buckets"]:
                last = b["d"].get("value_as_string") if b["d"].get("value") is not None else None
                _merge_row(out, b["key"], b["q"]["value"], b["a"]["value"], b["doc_count"], last)
            # returns: every credit note, netted off as pos_sales._merge does
            r = es.search(index=indexes.alias(indexes.RETURNS), size=0, request_cache=False,
                          query={"terms": {"wid": part}},
                          aggs={"p": {"terms": {"field": "wid", "size": len(part)}, "aggs": {
                              "q": {"sum": {"field": "qty"}}, "a": {"sum": {"field": "amount"}}}}})
            for b in r["aggregations"]["p"]["buckets"]:
                row = out.setdefault(int(b["key"]), {"qty": 0.0, "amount": 0.0, "bills": 0, "last_sold": None})
                row["qty"] = round(row["qty"] - float(b["q"]["value"] or 0), 3)
                row["amount"] = round(row["amount"] - float(b["a"]["value"] or 0), 2)
        return out
    except Exception as exc:                            # noqa: BLE001 — PostgreSQL answers instead
        if _unreachable(exc):
            es_client.mark_down(exc)
        stats["fallbacks"] += 1
        _warn("search: sales read failed, using PostgreSQL: %s: %s", type(exc).__name__, str(exc)[:200])
        return None
    finally:
        stats["calls"] += 1
        stats["seconds"] += time.perf_counter() - t0
