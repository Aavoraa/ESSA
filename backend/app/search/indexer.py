"""Writing documents: bulk indexing, and rebuilding an index from PostgreSQL.

Idempotent throughout: a document's `_id` comes from primary keys, so indexing
the same bill twice overwrites it rather than adding a second.
"""
import logging
import time

from sqlalchemy import text

from . import indexes, sources

log = logging.getLogger("uvicorn.error")


class IndexingError(RuntimeError):
    pass


def bulk_index(es, index, docs, refresh=False):
    """Index [(id, body)] in chunks; failed items are retried once, then raised.

    Nothing is dropped silently: if an item still fails after the retry the
    whole call raises, the caller's watermark does not move, and the next sync
    reads the same rows again."""
    from elasticsearch import helpers
    docs = list(docs)
    if not docs:
        return 0
    pending = docs
    for attempt in (1, 2):
        failed = []
        by_id = {d[0]: d for d in pending}
        for ok, item in helpers.streaming_bulk(
                es, ({"_op_type": "index", "_index": index, "_id": i, "_source": b} for i, b in pending),
                chunk_size=2000, max_retries=3, initial_backoff=1, raise_on_error=False,
                raise_on_exception=False):
            if not ok:
                failed.append(by_id.get(item.get("index", {}).get("_id")))
        if not failed:
            break
        log.warning("search: %d of %d documents failed to index (attempt %d)", len(failed), len(pending), attempt)
        pending = [f for f in failed if f]
    else:
        raise IndexingError(f"{len(failed)} documents could not be indexed into {index}")
    if refresh:
        es.indices.refresh(index=index)
    return len(docs)


def replace_docs(es, index, key, ids, docs):
    """Replace every document of these bills / notes with `docs` — for a change
    that can alter a document's id (a shop product relinked to another
    warehouse product), where plain overwriting would leave the old one."""
    ids = sorted({int(i) for i in ids})
    for i in range(0, len(ids), 5000):
        es.delete_by_query(index=index, query={"terms": {key: ids[i:i + 5000]}},
                           refresh=True, conflicts="proceed")
    return bulk_index(es, index, docs, refresh=True)


# ---------------------------------------------------------------- rebuild ---
_PLANS = {
    indexes.SALES: ("invoices", sources.sales_docs_between, 20000),
    indexes.RETURNS: ("credit_notes", sources.returns_docs_between, 20000),
}


def _pg_expected(conn, name):
    """What PostgreSQL says the index must hold: (documents, Σqty, Σamount)."""
    if name == indexes.SALES:
        sql, cols = sources._sales_sql("TRUE").text, "id, wid, q, a, d, l, live"
    else:
        sql, cols = sources._returns_sql("TRUE").text, "id, wid, q, a, d"
    return conn.execute(text(f"SELECT COUNT(*), COALESCE(SUM(c.q), 0), COALESCE(SUM(c.a), 0) "
                             f"FROM ({sql}) AS c({cols})")).one()


def _es_totals(es, index):
    r = es.search(index=index, size=0, track_total_hits=True,
                  aggs={"q": {"sum": {"field": "qty"}}, "a": {"sum": {"field": "amount"}}})
    return r["hits"]["total"]["value"], r["aggregations"]["q"]["value"], r["aggregations"]["a"]["value"]


def close(a, b):
    return abs(float(a or 0) - float(b or 0)) <= 1e-6 * max(1.0, abs(float(a or 0)))


def verify(es, conn, name, index=None):
    """Compare an index with PostgreSQL. Floats are compared to 1e-6 relative:
    both sides add doubles, in different orders."""
    index = index or indexes.alias(name)
    pg = _pg_expected(conn, name)
    got = _es_totals(es, index)
    ok = pg[0] == got[0] and close(pg[1], got[1]) and close(pg[2], got[2])
    return {"ok": ok, "postgres": {"documents": pg[0], "qty": float(pg[1]), "amount": float(pg[2])},
            "elasticsearch": {"documents": got[0], "qty": got[1], "amount": got[2]}}


def rebuild(es, engine, name, progress=None):
    """Build `name` afresh from PostgreSQL, validate it, then switch the alias.

    The watermarks are taken BEFORE reading, so anything written while the
    build runs is picked up by the next sync (re-indexing is idempotent)."""
    from . import sync
    table, read, step = _PLANS[name]
    t0 = time.perf_counter()
    with engine.connect() as conn:
        start_marks = sync.watermarks_now(conn)
        top = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM " + sources.pos_sales.q(table))).scalar()
    index = indexes.create(es, name)
    es.indices.put_settings(index=index, settings={"refresh_interval": "-1"})
    done = 0
    for lo in range(0, int(top) + 1, step):
        with engine.connect() as conn:
            docs = read(conn, lo, lo + step - 1)
        done += bulk_index(es, index, docs)
        if progress:
            progress(name, min(lo + step - 1, top), top, done)
    es.indices.put_settings(index=index, settings={"refresh_interval": "1s"})
    es.indices.refresh(index=index)
    with engine.connect() as conn:
        check = verify(es, conn, name, index=index)
    if not check["ok"]:
        raise IndexingError(f"{index} does not match PostgreSQL: {check} — alias left where it was")
    old = indexes.point_alias(es, name, index)
    sync.save_state(es, name, {**start_marks, "built_at": time.time(), "last_sync_at": time.time(),
                               "index": index})
    _retire(es, name, keep={index, old})
    log.info("search: rebuilt %s -> %s (%d documents, %.0fs); previous %s kept",
             indexes.alias(name), index, done, time.perf_counter() - t0, old)
    return {"index": index, "documents": done, "seconds": round(time.perf_counter() - t0, 1),
            "previous": old, "verify": check}


def _retire(es, name, keep):
    """Delete versions older than the current and the one before it."""
    prefix = f"{indexes.alias(name)}-v"
    try:
        existing = list(es.indices.get(index=prefix + "*"))
    except Exception:                                   # noqa: BLE001
        return
    for ix in existing:
        if ix not in keep:
            es.indices.delete(index=ix)
