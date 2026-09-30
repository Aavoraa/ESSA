"""Look after ESSA's Elasticsearch read index from the command line.

Run from essa-intake\\backend with the same environment as the server
(run.bat loads essa-intake\\.env; this does too):

    .venv\\Scripts\\python.exe tools\\es_admin.py health
    .venv\\Scripts\\python.exe tools\\es_admin.py rebuild [sales|sales-returns|all]
    .venv\\Scripts\\python.exe tools\\es_admin.py sync
    .venv\\Scripts\\python.exe tools\\es_admin.py verify
    .venv\\Scripts\\python.exe tools\\es_admin.py compare      # every product, ES vs PostgreSQL
    .venv\\Scripts\\python.exe tools\\es_admin.py status

`rebuild` fills a NEW versioned index from PostgreSQL, checks it against
PostgreSQL and only then moves the alias — the index being read keeps serving
until the new one is proven. Safe to run while ESSA is up, and to repeat.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))

# the server's settings, from essa-intake\.env, unless already in the environment
env_file = os.path.join(ROOT, ".env")
if os.path.exists(env_file):
    for line in open(env_file, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from app.search import client as es_client, indexer, indexes, sync  # noqa: E402
from app.database import engine  # noqa: E402


def _es():
    es = es_client.client()
    if es is None:
        sys.exit("Elasticsearch is not enabled (ELASTICSEARCH_ENABLED) or the client is missing.")
    return es


def progress(name, at, top, docs):
    print(f"  {name}: id {at:,} of {top:,} — {docs:,} documents", flush=True)


def cmd_health():
    print(json.dumps(es_client.health(), indent=2))


def cmd_rebuild(which="all"):
    es = _es()
    names = [indexes.SALES, indexes.RETURNS] if which == "all" else [which]
    for name in names:
        print(f"rebuilding {indexes.alias(name)} from PostgreSQL …", flush=True)
        r = indexer.rebuild(es, engine, name, progress=progress)
        print(json.dumps(r, indent=2, default=str))


def cmd_sync():
    print(json.dumps(sync.run_once(_es(), engine), indent=2, default=str))


def cmd_verify():
    es = _es()
    ok = True
    with engine.connect() as conn:
        for name in (indexes.SALES, indexes.RETURNS):
            r = indexer.verify(es, conn, name)
            ok &= r["ok"]
            print(indexes.alias(name), json.dumps(r, indent=2))
    sys.exit(0 if ok else 1)


def cmd_compare():
    """Every warehouse product with till activity: the index's figures against
    PostgreSQL's own (pos_sales.sales_of). Exact for counts and dates, to 1e-6
    relative for sums (both sides add doubles, in a different order)."""
    from sqlalchemy import text
    from app.services import pos_sales
    from app.search import sales as es_sales
    with engine.connect() as conn:
        wids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT warehouse_id FROM " + pos_sales.q("products") + " WHERE warehouse_id IS NOT NULL"))]
    t = time.perf_counter(); got = es_sales.sales_of(wids); t_es = time.perf_counter() - t
    if got is None:
        sys.exit("index not fresh / not reachable — run sync first")
    t = time.perf_counter(); want = pos_sales.sales_of(wids); t_pg = time.perf_counter() - t
    bad = []
    for wid in set(got) | set(want):
        a, b = want.get(wid), got.get(wid)
        if a is None or b is None:
            bad.append((wid, a, b)); continue
        if a["bills"] != b["bills"] or a["last_sold"] != b["last_sold"] or \
                not indexer.close(a["qty"], b["qty"]) or not indexer.close(a["amount"], b["amount"]):
            bad.append((wid, a, b))
    print(f"{len(wids):,} products compared — {len(want):,} with sales in PostgreSQL, {len(got):,} in the index; "
          f"{len(bad)} differ.  PostgreSQL {t_pg:.2f}s, Elasticsearch {t_es:.2f}s")
    for x in bad[:10]:
        print("  ", x)
    sys.exit(0 if not bad else 1)


def cmd_status():
    from app.routers.search import status
    print(json.dumps(status(), indent=2, default=str))


if __name__ == "__main__":
    args = sys.argv[1:] or ["status"]
    fn = globals().get("cmd_" + args[0])
    if not fn:
        sys.exit(__doc__)
    fn(*args[1:])
