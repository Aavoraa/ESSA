"""The search index's health, for whoever looks after the server.

Superadmin only (security.POLICY): it names index versions and sync state, and
nothing on it is business data. Rebuilding and verifying are done from the
command line (tools/es_admin.py), not from here — they are long operations
and belong to whoever is at the server.
"""
import time

from fastapi import APIRouter

from ..search import client as es_client, config, indexes, sales, sync

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/status")
def status():
    h = es_client.health()
    out = {"health": h, "max_lag_seconds": config.MAX_LAG_SECONDS,
           "sync_seconds": config.SYNC_SECONDS, "reads": dict(sales.stats), "indices": {}}
    es = es_client.client()
    if es is None or not h.get("reachable"):
        return out
    for name in (indexes.SALES, indexes.RETURNS):
        st = sync.load_state(es, name) or {}
        try:
            count = es.count(index=indexes.alias(name))["count"]
        except Exception:                               # noqa: BLE001 — not built yet
            count = None
        lag = time.time() - float(st.get("last_sync_at", 0)) if st else None
        out["indices"][name] = {
            "alias": indexes.alias(name), "index": indexes.current_index(es, name),
            "documents": count, "lag_seconds": round(lag, 1) if lag is not None else None,
            "fresh": sales.fresh(es, name),
            "watermarks": {k: st.get(k) for k in ("invoice_id", "credit_note_id", "cancelled_at")}}
    return out
