"""Aggregated series behind the graphical dashboard."""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from ..database import get_db
from .. import models
from ..security import may_read
from ..services import charts as svc
from ..services import scope

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/charts")
def charts(months: int = svc.MONTHS_BACK, db: Session = Depends(get_db)):
    """Every series the graphical dashboard draws, in one call.

    One call rather than five: the charts are read together and a dashboard that
    fills in piecemeal reads as five things loading badly rather than one thing
    loading. `months` is clamped — the axis stops being readable long before the
    query stops being answerable."""
    return svc.dashboard_charts(db, months=max(3, min(months, 24)))


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """Every figure the warehouse dashboard's tiles show, in one call.

    The screen used to fetch ten whole lists — every GRN, every unpaid bill,
    every debit note — and count them in the browser. Most of the wait was
    building and shipping those lists (~20 MB), and the notification feed and the
    dead-stock summary each ran the till's sales aggregation in parallel. Here
    they share one request, so that read happens once (see request_memo).

    Each part is what the screen computed from the list it used to fetch, by the
    same function and in the same order, so every figure is the same number. A
    part this account could not have fetched on its own — Payments for a floor
    user — or one that fails comes back null, which the screen reports exactly
    as it did a refused call: "Some figures did not load"."""
    from . import purchases as purchases_r, outward as outward_r, lr as lr_r
    from . import inventory as inventory_r
    from ..services import payments as pay_svc
    from ..services import notifications as notif_svc
    from ..services import dead_stock as ds_svc
    from .purchases import js_sum

    from ..services import command_center as cc

    def transit(s):
        rows = outward_r.list_outwards(status="posted", kind="all", db=s, warehouse_id=wid)
        return {"count": len(rows), "qty": js_sum(o["total_qty"] for o in rows)}

    def bills(s):
        rows = pay_svc.pending_bills(s, None)
        return {"count": len(rows), "payable": js_sum(b["outstanding"] for b in rows),
                "overdue": sum(1 for b in rows if (b.get("days") or 0) > 30)}

    def lr(s):
        rows = lr_r.list_lr(received="all", limit=500, db=s, wid=wid)
        return {"count": len(rows),
                "pending": sum(1 for e in rows if not e["received_by"]),
                "unlinked": sum(1 for e in rows if not e["matched"])}

    parts = [
        # (key, the path the screen used to read it from, how to compute it)
        ("grns", "/api/purchases", lambda s: purchases_r.dashboard_figures(s, wid)),
        ("stock", "/api/inventory/summary", lambda s: inventory_r.summary(db=s, wid=wid)),
        ("transit", "/api/outward", transit),
        ("out_drafts", "/api/outward", lambda s: {"count": len(outward_r.list_outwards(
            status="draft", kind="all", db=s, warehouse_id=wid))}),
        ("bills", "/api/payments/pending", bills),
        ("returns", "/api/returns", lambda s: {"draft_count": scope.returns(
            s.query(models.PurchaseReturn), wid).filter(
            models.PurchaseReturn.status == "draft").count()}),
        ("lr", "/api/lr", lr),
        ("suppliers", "/api/suppliers", lambda s: {"count": s.query(models.Supplier).count()}),
        # the feed and the summary both read the dead-stock bands; built side by
        # side, the second waits for the first's read (the request memo)
        ("notifs", "/api/notifications", lambda s: notif_svc.feed(s)),
        ("dead", "/api/dead-stock/summary", lambda s: ds_svc.summary(s)),
    ]
    out = {key: None for key, path, _ in parts if not may_read(request, path)}
    # Every part at once, each on its own session — the same functions, so the
    # same figures; the slowest part decides the wait instead of all of them in
    # turn. A part that fails is null, as before ("Some figures did not load").
    quick = [(key, compute, None) for key, path, compute in parts
             if key not in out and key not in ("notifs", "dead", "stock", "bills")]
    groups = cc.warm_groups() + [quick] + [
        [(key, compute, None)] for key, path, compute in parts
        if key not in out and key in ("notifs", "dead", "stock", "bills")]
    got = cc._run_groups(groups, db)
    for key, path, _ in parts:
        if key not in out:
            out[key] = got.get(key)
    return out
