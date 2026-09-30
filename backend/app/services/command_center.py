"""The Command Center — the whole business on one screen, for the people who own it.

One call, like the Central Dashboard's, because the screen draws twenty figures,
two charts and three lists at once and asking for each separately turns opening
it into twenty round trips.

WHAT "TODAY" IS: the business's own calendar day (services/business_day), not
UTC's. Everything here is asked of that window.

WHAT EACH FIGURE COUNTS, because two screens disagreeing about a number nobody
can reconcile is worse than either being slightly off:

  sales       store bills (net of customer returns) — the same definition the
              Central Dashboard's store sales use (pos_store_sales)
  purchases   GRNs POSTED today, by `posted_at` — goods that became stock today,
              not invoices dated today
  payments    supplier payments RECORDED today (`created_at`)
  profit      store revenue before tax less what those goods cost at the till's
              current cost price (pos_insights.margin)
  stock value quantity × each warehouse's own average cost (stock_locations)

Figures that belong to a PRODUCT rather than a building — dead stock, store low
stock — are company-wide whatever the account's allotment, and the response says
which ones those are.

Never raises for a missing half. The shop's till database may be absent; the
dashboard still draws from the warehouse's and says the store figures are unknown.
"""
import contextvars
import datetime as dt
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import models
from ..request_memo import memo, remember
from . import (audit as audit_svc, business_day, dead_stock, payments as pay_svc,
               pos_insights, stock_locations as stock_loc)
from .figures import inr, inr_short, qty as fmt_qty
from . import pos_sales

log = logging.getLogger("uvicorn.error")


def _safe(fn, default):
    try:
        return fn()
    except Exception:                                  # noqa: BLE001 — one tile, not the screen
        # still one tile, not the screen — but said in the log, so a tile that is
        # quietly blank because its query broke can be found
        log.warning("command center: a section failed and was left empty", exc_info=True)
        return default


# ---------------------------------------------------------------------------
#  Running the independent sections side by side
# ---------------------------------------------------------------------------
#  Each section below is a read, and most are independent of each other. They
#  are run as GROUPS on a small pool of threads, each group with its OWN
#  session: a SQLAlchemy Session is not safe to share between threads.
#
#  Sections that share a read stay in one group, because the request memo that
#  lets them share it is keyed by session: dead stock and the notification rules
#  both need the stocked products' till sales; the places table reuses the
#  warehouse totals, the transfer summary and the day's GRNs.
#
#  Nothing crosses between groups but plain values — ORM rows are dropped from
#  a group's results before they leave it (`_plain`).
#
#  ESSA_DASHBOARD_WORKERS bounds the threads, and bounds them PROCESS-wide (one
#  shared pool), so two dashboards opened together cannot open twice the
#  connections. 1 runs everything in the request's own session, in order — the
#  behaviour before this existed. SQLite always runs that way.
#
#  Default 2, from measurement (essa_staging, overview median of 3): 1 worker
#  2.2-2.5 s, 2 workers 1.7 s, 4 and 6 workers 1.7 s. What remains is mostly
#  Python, which threads share one interpreter lock for, so more workers add
#  connections and nothing else.
def _worker_count():
    from ..database import IS_SQLITE
    if IS_SQLITE:
        return 1
    try:
        n = int((os.environ.get("ESSA_DASHBOARD_WORKERS") or "").strip() or 6)
    except ValueError:
        n = 6
    return max(1, min(n, 8))


_pool = None
_pool_lock = threading.Lock()


def _executor(n):
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="cc-worker")
        return _pool


def _is_orm(v):
    return hasattr(v, "_sa_instance_state") or (
        isinstance(v, list) and bool(v) and hasattr(v[0], "_sa_instance_state"))


def _plain(value):
    """A section's result without the ORM objects in it (`rows` of GRNs, the
    `purchase` on an outstanding bill), which belong to the session that loaded
    them. Plain rows — dead stock's dicts — are kept; overview reads them."""
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if not _is_orm(v)}
    if (isinstance(value, list) and value and isinstance(value[0], dict)
            and any(_is_orm(v) for v in value[0].values())):
        return [{k: v for k, v in row.items() if not _is_orm(v)} for row in value]
    return value


def _run_group(group, db=None):
    """Run one group's sections in order in one session: `db`, or a new one."""
    from ..database import SessionLocal
    own = db is None
    session = SessionLocal() if own else db
    out, timings = {}, []
    try:
        for name, fn, default in group:
            t = time.perf_counter()
            if own:
                try:
                    value = _plain(fn(session))
                except Exception:                      # noqa: BLE001 — as _safe
                    log.warning("command center: section %s failed and was left empty",
                                name, exc_info=True)
                    value = default
                    session.rollback()                 # the next section starts clean
            else:
                value = _safe(lambda: fn(session), default)
            out[name] = value
            timings.append((name, time.perf_counter() - t))
    finally:
        if own:
            session.close()
    # the (closed) session goes back to the caller and is kept referenced for the
    # rest of the request: memo keys carry id(session), and a freed id could be
    # handed to a later session in the same request and match a stale entry
    return out, timings, session


def warm_groups():
    """The two slowest reads every dashboard needs — the till's sales for the
    stocked products and the till's status — started first and side by side,
    so the sections that need them find them done (or in progress) instead of
    running them one after the other. They return nothing of their own."""
    from . import pos_sales
    return [[("_warm_sales", lambda s: (pos_sales.sales_of_stocked(), None)[1], None)],
            [("_warm_status", lambda s: (pos_sales.status(), None)[1], None)]]


def _run_groups(groups, db):
    """Every group's sections → {name: value}. Parallel when workers > 1."""
    from .. import perf
    n = _worker_count()
    t0 = time.perf_counter()
    results, timings = {}, []
    if n <= 1:
        for g in groups:
            out, tm, _ = _run_group(g, db)
            results.update(out)
            timings += tm
    else:
        pool = _executor(n)
        # each task runs in a copy of the request's context, so the request memo
        # (and the perf counters) are the request's own
        futures = [pool.submit(contextvars.copy_context().run, _run_group, g) for g in groups]
        sessions = []
        for f in futures:
            out, tm, s = f.result()
            results.update(out)
            timings += tm
            sessions.append(s)
        remember(("command_center.worker_sessions", time.perf_counter_ns()), sessions)
    if perf.ENABLED:
        slow = sorted(timings, key=lambda x: -x[1])[:8]
        log.info("PERF command overview sections: %.2fs wall, %d worker(s) — %s",
                 time.perf_counter() - t0, n,
                 " | ".join(f"{name} {secs:.2f}s" for name, secs in slow))
    return results


def _in_allowed(q, col, allowed):
    return q.filter(col.in_(list(allowed))) if allowed else q


# The screen's sections are built independently and several ask for the same
# read: the warehouse totals for the tiles, the places table and its picker; the
# transfer summary for the tiles and the places table; today's GRNs for the
# tiles and the places table. Within one request those are one read each (see
# request_memo — GET only, never beyond the request). Kept local to this module
# so no other caller of stock_locations changes behaviour.
def _ids_key(ids):
    return tuple(sorted(int(i) for i in ids)) if ids is not None else None


def _warehouse_totals(db, warehouse_ids=None):
    return memo(("cc.warehouse_totals", id(db), _ids_key(warehouse_ids)),
                lambda: stock_loc.warehouse_totals(db, warehouse_ids=warehouse_ids))


def _transfer_summary(db):
    return memo(("cc.transfer_summary", id(db)), lambda: stock_loc.transfer_summary(db))


# ---------------------------------------------------------------------------
#  Counts
# ---------------------------------------------------------------------------
def counts(db, allowed=None, with_users=True):
    wh = _in_allowed(db.query(models.Warehouse).filter(models.Warehouse.active.is_(True)),
                     models.Warehouse.id, allowed).count()
    st_q = _in_allowed(db.query(models.Store).filter(models.Store.active.is_(True)),
                       models.Store.warehouse_id, allowed)
    stores = st_q.count()
    tq = (db.query(models.PosTerminal).join(models.Store, models.Store.id == models.PosTerminal.store_id)
            .filter(models.PosTerminal.active.is_(True)))
    tq = _in_allowed(tq, models.Store.warehouse_id, allowed)
    out = {"warehouses": wh, "stores": stores, "counters": tq.count()}
    if with_users:
        out["users"] = db.query(models.User).filter(models.User.active == True).count()  # noqa: E712
    return out


# ---------------------------------------------------------------------------
#  The warehouse's own flows over a UTC window
# ---------------------------------------------------------------------------
def purchases_between(db, start, end, allowed=None, warehouse_id=None):
    return memo(("cc.purchases_between", id(db), start, end, _ids_key(allowed), warehouse_id),
                lambda: _purchases_between(db, start, end, allowed, warehouse_id))


def _purchases_between(db, start, end, allowed=None, warehouse_id=None):
    q = (db.query(models.Purchase)
           .filter(models.Purchase.status == "posted",
                   models.Purchase.posted_at >= start, models.Purchase.posted_at < end))
    q = _in_allowed(q, models.Purchase.warehouse_id, allowed)
    if warehouse_id:
        q = q.filter(models.Purchase.warehouse_id == int(warehouse_id))
    rows = q.order_by(models.Purchase.posted_at.desc()).all()
    ids = [p.id for p in rows]
    units, products = 0.0, set()
    if ids:
        for mv in (db.query(models.StockMovement)
                     .filter(models.StockMovement.ref_type == "purchase",
                             models.StockMovement.kind == "inward",
                             models.StockMovement.ref_id.in_(ids)).all()):
            units += float(mv.qty_delta or 0)
            products.add(mv.product_id)
    return {"grns": len(rows), "value": round(sum(float(p.grand_total or 0) for p in rows), 2),
            "units": round(units, 3), "products": len(products), "rows": rows}


def payments_between(db, start, end):
    rows = (db.query(models.Payment)
              .filter(models.Payment.created_at >= start, models.Payment.created_at < end)
              .order_by(models.Payment.created_at.desc()).all())
    return {"count": len(rows), "value": round(sum(float(p.paid_amount or 0) for p in rows), 2),
            "rows": rows}


def debit_notes_between(db, start, end, allowed=None):
    rows = (db.query(models.PurchaseReturn)
              .filter(models.PurchaseReturn.status == "posted",
                      models.PurchaseReturn.posted_at >= start,
                      models.PurchaseReturn.posted_at < end).all())
    if allowed:
        rows = [r for r in rows if r.purchase and r.purchase.warehouse_id in allowed]
    return {"count": len(rows), "value": round(sum(float(r.total or 0) for r in rows), 2),
            "rows": rows}


def movement_between(db, start, end, allowed=None):
    q = db.query(models.StockMovement).filter(models.StockMovement.created_at >= start,
                                              models.StockMovement.created_at < end)
    q = _in_allowed(q, models.StockMovement.warehouse_id, allowed)
    inward = outward = 0.0
    for mv in q.all():
        d = float(mv.qty_delta or 0)
        if d > 0:
            inward += d
        else:
            outward -= d
    return {"inward": round(inward, 3), "outward": round(outward, 3)}


def outwards_posted_between(db, start, end, allowed=None):
    q = db.query(models.StockOutward).filter(models.StockOutward.posted_at >= start,
                                             models.StockOutward.posted_at < end)
    q = _in_allowed(q, models.StockOutward.from_warehouse_id, allowed)
    return q.count()


def pending_grns(db, allowed=None):
    drafts = _in_allowed(db.query(models.Purchase).filter(models.Purchase.status == "draft"),
                         models.Purchase.warehouse_id, allowed).all()
    docs = db.query(models.Document).filter(models.Document.status == "needs_review")
    if allowed:
        docs = docs.filter((models.Document.warehouse_id.is_(None))
                           | (models.Document.warehouse_id.in_(list(allowed))))
    return {"count": len(drafts), "value": round(sum(float(p.grand_total or 0) for p in drafts), 2),
            "documents": docs.count(), "rows": drafts}


def outstanding(db, supplier_name=None):
    """Every posted bill still owed on: [{purchase, supplier, outstanding, days}].

    The same arithmetic as payments.invoice_outstanding — bill less what was paid
    less what went back on debit notes — but totalled in two grouped queries
    rather than two per bill. This runs on the owner's landing page, and a
    warehouse with four thousand posted GRNs made that eight thousand queries.
    """
    from sqlalchemy import func
    today = dt.date.today()
    settled = dict(db.query(models.PaymentAllocation.purchase_id,
                            func.sum(models.PaymentAllocation.settled))
                     .group_by(models.PaymentAllocation.purchase_id).all())
    returned = dict(db.query(models.PurchaseReturn.purchase_id,
                             func.sum(models.PurchaseReturn.total))
                      .filter(models.PurchaseReturn.status == "posted")
                      .group_by(models.PurchaseReturn.purchase_id).all())
    out = []
    from sqlalchemy import select
    # Only the columns read here and by the callers (id, total, date, supplier
    # name), as plain rows: building an ORM object for every posted GRN was most
    # of the cost. The same statement the ORM form issued — same columns, same
    # outer join — so the same rows in the same order. `purchase` is that row:
    # .id, .supplier_id, .invoice_date, .grand_total, as before.
    P = models.Purchase
    sup = models.Supplier.__table__.alias("suppliers_1")
    for p in db.execute(
            select(P.id, P.supplier_id, P.invoice_date, P.grand_total,
                   sup.c.id.label("id_1"), sup.c.name)
            .select_from(P.__table__.outerjoin(sup, sup.c.id == P.supplier_id))
            .where(P.status == "posted")):
        owed = round(float(p.grand_total or 0) - float(settled.get(p.id) or 0)
                     - float(returned.get(p.id) or 0), 2)
        if owed <= 0.01:
            continue
        name = p.name if p.id_1 is not None else "(no supplier)"
        if supplier_name and supplier_name.lower() not in name.lower():
            continue
        d = pay_svc._parse_date(p.invoice_date)
        out.append({"purchase": p, "supplier": name, "outstanding": round(owed, 2),
                    "days": (today - d).days if d else None})
    return out


def dead_counts(db, days=None):
    rules = dead_stock.get_rules()
    limit = int(days or rules["dead_after_days"])
    rows = [r for r in dead_stock.product_rows(db, rules=rules, include_healthy=True)
            if r["days_idle"] >= limit]
    return {"days": limit, "count": len(rows),
            "value": round(sum(r["stock_value"] for r in rows), 2),
            "critical": sum(1 for r in rows if r["status"] == "critical"),
            "rows": rows}


# ---------------------------------------------------------------------------
#  The series: one bucket per business day
# ---------------------------------------------------------------------------
def _bucket(values, when_value_pairs, days_list):
    index = {d: i for i, d in enumerate(days_list)}
    for when, value in when_value_pairs:
        local = business_day.local(when)
        if local is None:
            continue
        i = index.get(local.date())
        if i is not None:
            values[i] = round(values[i] + float(value or 0), 2)
    return values


def series(db, day, n_days=14, allowed=None, at=None):
    first = day - dt.timedelta(days=n_days - 1)
    start, end = business_day.bounds(first, day)
    days_list = business_day.days(first, day)
    z = lambda: [0.0] * len(days_list)                 # noqa: E731

    sales = _bucket(z(), pos_insights.invoice_times(start, end, at), days_list)
    pos_returns = _bucket(z(), pos_insights.return_times(start, end, at), days_list)
    pur = purchases_between(db, start, end, allowed)["rows"]
    purchases = _bucket(z(), [(p.posted_at, p.grand_total) for p in pur], days_list)
    pays = payments_between(db, start, end)["rows"]
    payments = _bucket(z(), [(p.created_at, p.paid_amount) for p in pays], days_list)
    dn = debit_notes_between(db, start, end, allowed)["rows"]
    returns = _bucket(pos_returns, [(r.posted_at, r.total) for r in dn], days_list)

    inward, outward = z(), z()
    index = {d: i for i, d in enumerate(days_list)}
    mq = _in_allowed(db.query(models.StockMovement).filter(
        models.StockMovement.created_at >= start, models.StockMovement.created_at < end),
        models.StockMovement.warehouse_id, allowed)
    for mv in mq.all():
        i = index.get(business_day.local(mv.created_at).date()) if mv.created_at else None
        if i is None:
            continue
        d = float(mv.qty_delta or 0)
        if d > 0:
            inward[i] = round(inward[i] + d, 3)
        else:
            outward[i] = round(outward[i] - d, 3)
    return {"labels": [d.strftime("%d %b") for d in days_list],
            "days": [d.isoformat() for d in days_list],
            "sales": sales, "purchases": purchases, "payments": payments,
            "returns": returns, "inward": inward, "outward": outward}


# ---------------------------------------------------------------------------
#  Alerts and activity
# ---------------------------------------------------------------------------
_NOTICE_TAB = {"documents": "documents", "purchases": "purchases", "lr": "lr",
               "payments": "payments", "returns": "returns", "outward": "outward",
               "inward": "inward", "deadstock": "deadstock", "inventory": "inventory"}


def alerts(db, day, allowed=None, at=None, k=None, notices=None):
    """`notices`, when given, is `_notices()` already read — the overview reads it
    beside dead stock, which it shares the stocked products' till sales with."""
    start, end = business_day.bounds(day)
    out = []
    for n in (notices if notices is not None else _safe(lambda: _notices(db), [])):
        if n.get("level") in ("critical", "warn"):
            out.append({"level": n["level"], "title": n["title"], "body": n.get("body") or "",
                        "tab": _NOTICE_TAB.get(n.get("module") or "", None),
                        "waiting": n.get("waiting")})
    ac = _safe(lambda: audit_svc.counts_since(db, start, allowed), {})
    if ac.get("refused"):
        out.append({"level": "warn", "tab": "audit",
                    "title": f"{ac['refused']} refused attempt(s) today",
                    "body": "Someone tried a change their account is not allowed to make."})
    if ac.get("failed_signins", 0) >= 3:
        out.append({"level": "warn", "tab": "audit",
                    "title": f"{ac['failed_signins']} failed sign-ins today",
                    "body": "Wrong usernames or passwords — check nobody is guessing."})
    cancel = _safe(lambda: pos_insights.cancelled(start, end, at), {"bills": 0})
    if cancel.get("bills"):
        out.append({"level": "warn", "tab": None,
                    "title": f"{cancel['bills']} store bill(s) cancelled today",
                    "body": f"Worth {inr(cancel['amount'])} — every cancellation is in the till's register."})
    if k and k.get("dead_stock", {}).get("critical"):
        d = k["dead_stock"]
        out.append({"level": "warn", "tab": "deadstock",
                    "title": f"{d['critical']} product(s) idle 180+ days",
                    "body": f"Dead stock totals {inr_short(d['value'])} across {d['count']} product(s)."})
    order = {"critical": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: order.get(a["level"], 3))
    return out


def _notices(db):
    from . import notifications
    return notifications.collect(db)


def activity(db, limit=15, allowed=None, at=None, hide_screens=()):
    """The audit trail's newest lines, with the stores' newest bills woven in —
    "POS Counter 04 generated bill TG26-001245" belongs in the same stream as
    "Ravi posted GRN-2045", and the till is the only one that knows it."""
    events = _safe(lambda: audit_svc.feed(db, limit=limit, allowed_warehouses=allowed,
                                          hide_screens=hide_screens)["events"], [])
    rows = [{"kind": "event", "at": e["at"], "local": e["local"], "who": e["who"],
             "role": e["role_label"], "module": e["module"], "summary": e["summary"],
             "where": e["warehouse"], "ref": e["ref"], "outcome": e["outcome"]}
            for e in events]
    for b in _safe(lambda: pos_insights.recent_bills(limit=limit, at=at), []):
        local = business_day.local(b["at"])
        at_iso = b["at"].replace(" ", "T")[:19] if b["at"] else None
        where = " · ".join(x for x in (b["branch"], b["floor"]) if x)
        rows.append({"kind": "bill", "at": at_iso,
                     "local": local.isoformat(timespec="minutes") if local else None,
                     "who": b["cashier"] or "—", "role": "Store",
                     "module": "POS", "where": where or None, "ref": b["bill_no"],
                     "outcome": "cancelled" if b["status"] == "cancelled" else "ok",
                     "summary": f"{b['counter'] or 'A counter'} generated bill {b['bill_no']} — {inr(b['total'])}"
                                + (" (cancelled)" if b["status"] == "cancelled" else "")})
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
#  The whole screen
# ---------------------------------------------------------------------------
def places(db, day, scope_ids=None, at=None, allotted=None):
    """The business by PLACE: one row per warehouse, per store and per till.

    The tiles above answer "how did the company do today"; this answers "which of
    my six warehouses, seven stores and two tills did it" — the question the
    tiles always provoke, and the reason a dashboard without it sends somebody
    off to three other screens to find out.

    Every row carries both halves: what the warehouse HOLDS (our own ledger) and
    what its stores SOLD (the till's), matched by store name — the only key the
    two databases share.
    """
    start, end = business_day.bounds(day)
    from . import locations as loc_svc

    sold = {r["label"].lower(): r for r in pos_insights.breakdown(start, end, "store", at)}
    back = {r["label"].lower(): r for r in pos_insights.returns_by(start, end, "store", at)}
    bought = {}
    for p in purchases_between(db, start, end, scope_ids)["rows"]:
        b = bought.setdefault(p.warehouse_id, {"grns": 0, "value": 0.0})
        b["grns"] += 1
        b["value"] = round(b["value"] + float(p.grand_total or 0), 2)
    transit = {w["warehouse_id"]: w for w in
               _transfer_summary(db)["warehouses"]}

    stores, by_wh = [], {}
    for s in loc_svc.store_rows(db):
        if scope_ids and s["warehouse_id"] not in scope_ids:
            continue
        key = " ".join((s["name"] or "").split()).lower()
        sale, ret = sold.get(key), back.get(key)
        row = {**{k: s[k] for k in ("id", "name", "code", "warehouse_id", "warehouse",
                                    "terminals", "type", "active")},
               "bills": (sale or {}).get("bills", 0),
               "sales": (sale or {}).get("amount", 0.0),
               "returns": (ret or {}).get("amount", 0.0),
               # A branch the till spells differently sold under a name we cannot
               # match — said here rather than shown as a store that sold nothing.
               "matched": sale is not None or not pos_insights.available()}
        stores.append(row)
        w = by_wh.setdefault(s["warehouse_id"], {"bills": 0, "sales": 0.0, "stores": 0})
        w["stores"] += 1
        w["bills"] += row["bills"]
        w["sales"] = round(w["sales"] + row["sales"], 2)
    stores.sort(key=lambda r: -r["sales"])

    warehouses = []
    for r in _warehouse_totals(db, warehouse_ids=scope_ids):
        wid = r["warehouse_id"]
        s = by_wh.get(wid, {})
        b = bought.get(wid, {})
        warehouses.append({
            "warehouse_id": wid, "name": r["name"], "code": r["code"],
            "active": r.get("active", True), "qty": r["qty"], "value": r["value"],
            "items": r["items"], "stores": s.get("stores", r.get("store_count", 0)),
            "sales": s.get("sales", 0.0), "bills": s.get("bills", 0),
            "purchases": b.get("value", 0.0), "grns": b.get("grns", 0),
            "in_transit": (transit.get(wid) or {}).get("in_transit", 0.0),
        })
    warehouses.sort(key=lambda r: -r["value"])

    counters = pos_insights.breakdown(start, end, "counter", at)
    cash = pos_insights.breakdown(start, end, "cashier", at)
    # The picker lists every building this ACCOUNT may see, never only the one
    # being shown — scoping a screen to Erode must not remove the way back to
    # Karur. The same rule the Central Dashboard's warehouse table follows.
    picker = [{"warehouse_id": r["warehouse_id"], "name": r["name"], "code": r["code"]}
              for r in _warehouse_totals(db, warehouse_ids=allotted)]
    return {"warehouses": warehouses, "stores": stores, "counters": counters,
            "cashiers": cash, "picker": sorted(picker, key=lambda r: r["name"] or "")}


def overview(db, allowed=None, day=None, with_users=True, warehouse_id=None):
    day = day or business_day.today()
    start, end = business_day.bounds(day)
    # One building, or every building this account may see. The allotment is kept
    # apart from the scope: the picker is drawn from what the ACCOUNT may see, so
    # choosing one warehouse never removes the way back to the others.
    allotted = list(allowed) if allowed else None
    scope_ids = [int(warehouse_id)] if warehouse_id else allotted
    loc_ids = (pos_insights.location_ids_for_warehouses(db, scope_ids)
               if scope_ids else None)
    at = {"location_ids": loc_ids} if loc_ids is not None else None
    pos_ok = pos_insights.available()
    allowed = scope_ids
    hide = () if with_users else ("users", "settings")

    # Every read the screen needs, in groups that share nothing but the request
    # (see _run_groups). Within a group the order matters only for the memo:
    # a later section finds an earlier one's read done.
    E_PLACES = {"warehouses": [], "stores": [], "counters": [], "cashiers": [], "picker": []}
    groups = warm_groups() + [
        # the till's stocked-product sales, read once for both: the notices wait
        # for dead stock's rows (the request memo) rather than reading them again
        [("dead", lambda s: dead_counts(s),
          {"days": 90, "count": 0, "value": 0, "critical": 0, "rows": []})],
        [("notices", lambda s: _notices(s), [])],
        [("owed", lambda s: outstanding(s), [])],
        # the warehouse totals, transfer summary and the day's GRNs, read once
        # for the tiles and again (from the memo) for the places table
        [("stock_rows", lambda s: _warehouse_totals(s, warehouse_ids=allowed), []),
         ("transit", lambda s: _transfer_summary(s)["totals"].get("in_transit", 0), 0),
         ("pur", lambda s: purchases_between(s, start, end, allowed),
          {"grns": 0, "value": 0, "units": 0, "products": 0}),
         ("places", lambda s: places(s, day, scope_ids, at, allotted), E_PLACES)],
        [("series", lambda s: series(s, day, 14, allowed, at), None),
         ("moves", lambda s: movement_between(s, start, end, allowed), {"inward": 0, "outward": 0})],
        [("sales", lambda s: pos_insights.totals(start, end, at), {"available": False}),
         ("prof", lambda s: pos_insights.margin(start, end, at), {"available": False}),
         # the till's low-stock list: till data, so kept like the till's sales
         # (pos_sales._till_kept — never older than a minute)
         ("low", lambda s: pos_sales._till_kept("low_stock_8", lambda: pos_insights.low_stock(limit=8)),
          {"available": False, "count": 0, "rows": []}),
         ("top", lambda s: pos_insights.top_products(
             *business_day.bounds(day - dt.timedelta(days=29), day), at, limit=6), []),
         ("top_floors", lambda s: pos_insights.breakdown(start, end, "floor", at, limit=6), [])],
        [("pays", lambda s: payments_between(s, start, end), {"count": 0, "value": 0}),
         ("dn", lambda s: debit_notes_between(s, start, end, allowed), {"count": 0, "value": 0}),
         ("pend", lambda s: pending_grns(s, allowed), {"count": 0, "value": 0, "documents": 0}),
         ("sent_today", lambda s: outwards_posted_between(s, start, end, allowed), 0),
         ("counts", lambda s: counts(s, allowed, with_users), {}),
         ("activity", lambda s: activity(s, 15, allowed, at, hide), [])],
    ]
    r = _run_groups(groups, db)
    sales, prof, pur, pays, dn = r["sales"], r["prof"], r["pur"], r["pays"], r["dn"]
    stock_rows, moves, transit, pend = r["stock_rows"], r["moves"], r["transit"], r["pend"]
    owed, dead, low, sent_today = r["owed"], r["dead"], r["low"], r["sent_today"]
    top, top_floors = r["top"], r["top_floors"]

    k = {
        "sales": {"value": sales.get("net", 0), "gross": sales.get("gross", 0),
                  "bills": sales.get("bills", 0), "units": sales.get("units", 0),
                  "returns": sales.get("returns", 0), "available": bool(sales.get("available"))},
        "purchases": {"value": pur["value"], "grns": pur["grns"], "units": pur["units"],
                      "products": pur["products"]},
        "payments": {"value": pays["value"], "count": pays["count"]},
        "stock_value": {"value": round(sum(r["value"] for r in stock_rows), 2),
                        "qty": round(sum(r["qty"] for r in stock_rows), 3),
                        "items": sum(r["items"] for r in stock_rows)},
        "profit": {"value": prof.get("profit", 0), "margin_pct": prof.get("margin_pct"),
                   "revenue": prof.get("revenue", 0), "cost": prof.get("cost", 0),
                   "available": bool(prof.get("available"))},
        "discounts": {"value": sales.get("discount", 0)},
        "returns": {"value": round(float(sales.get("returns", 0) or 0) + dn["value"], 2),
                    "store": sales.get("returns", 0), "store_notes": sales.get("return_notes", 0),
                    "debit_notes": dn["count"], "debit_value": dn["value"]},
        "low_stock": {"count": low.get("count", 0), "available": bool(low.get("available"))},
        "dead_stock": {"count": dead["count"], "value": dead["value"],
                       "critical": dead["critical"], "days": dead["days"]},
        "pending_grns": {"count": pend["count"], "value": pend["value"],
                         "documents": pend["documents"]},
        "pending_payments": {"value": round(sum(b["outstanding"] for b in owed), 2),
                             "bills": len(owed),
                             "overdue": round(sum(b["outstanding"] for b in owed
                                                  if (b["days"] or 0) > 30), 2)},
        "movement": {"inward": moves["inward"], "outward": moves["outward"],
                     "in_transit": transit, "dispatched": sent_today},
        "transactions": {"count": int(sales.get("bills", 0) or 0) + pur["grns"] + pays["count"]
                                  + dn["count"] + int(sent_today or 0),
                         "bills": sales.get("bills", 0), "grns": pur["grns"],
                         "payments": pays["count"], "debit_notes": dn["count"],
                         "dispatches": sent_today},
    }

    return {
        "day": day.isoformat(), "label": business_day.label(day, day),
        "generated_at": business_day.now_local().isoformat(timespec="minutes"),
        "scope": {"restricted": bool(allowed),
                  "warehouse_id": int(warehouse_id) if warehouse_id else None,
                  "warehouse": next((r["name"] for r in stock_rows
                                     if warehouse_id and r["warehouse_id"] == int(warehouse_id)), None),
                  "warehouses": [r["name"] for r in stock_rows] if allowed else None,
                  "company_wide": ["dead_stock", "low_stock", "pending_payments", "payments"]},
        "pos": {"available": pos_ok},
        "counts": r["counts"],
        "kpis": k,
        "series": r["series"],
        "top_products": top,
        "top_floors": top_floors,
        # One row per warehouse, per store and per till — the drill-down the
        # tiles above always provoke. See `places`.
        "places": r["places"],
        "low_stock": low.get("rows", []),
        "dead_stock": [{"sku": x["sku"], "name": x["name"], "category": x["category"],
                        "qty": x["qty"], "value": x["stock_value"], "days": x["days_idle"]}
                       for x in sorted(dead["rows"], key=lambda x: -x["stock_value"])[:6]],
        # after the rest: its last line reads the dead-stock KPI above
        "alerts": _safe(lambda: alerts(db, day, allowed, at, k, notices=r["notices"]), []),
        "activity": r["activity"],
        "headline": (f"{business_day.label(day, day)}: {inr(k['sales']['value'])} store sales, "
                     f"{inr_short(k['purchases']['value'])} received on {k['purchases']['grns']} GRN(s), "
                     f"{fmt_qty(k['transactions']['count'])} transactions")
                    if pos_ok else None,
    }
