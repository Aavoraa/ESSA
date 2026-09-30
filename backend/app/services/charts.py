"""
Aggregated series for the graphical dashboard.

Every series here is a *shape of data*, not a picture: `{labels, values}` or a
list of `{label, value}`. The screen decides what to draw. That split matters
because the same numbers appear on the static dashboard as tiles, and if the two
disagree the dashboard has told someone two different things about one warehouse.

WHY THIS AGGREGATES SERVER-SIDE
-------------------------------
The alternative is to run the reports and total their rows in the browser. That
would double the definition of every figure — the report's and the chart's — and
they drift. Worse, the definitions that matter here are exactly the subtle ones:
stock is only stock if a posted GRN put it there (`integrity`), and a purchase
counts on its invoice date rather than the day it was keyed. Both live in Python
already, so the charts are computed next to them.

CONSISTENCY WITH THE TILES
--------------------------
`stock_by_category` filters products through `integrity.Context` — the same rule
`inventory_summary` uses for the Stock value tile. Without that the donut would
total a different, larger number than the tile directly above it, and the honest
reading of that is that one of them is wrong.

WHAT IS NOT HERE
----------------
No sales series. This system records purchases in and dispatches out; it has no
customer, price or sale. "Stock outward" is goods leaving for a shop or another
godown, which is a transfer, not a sale — charting it as revenue would invent a
figure the database does not contain.
"""
import datetime as dt
from collections import defaultdict

from .. import models
from . import dates as date_svc
from . import payments as pay_svc

#: Trailing months every time-series covers. Twelve so a year-on-year shape is
#: visible; the axis stays readable at this width.
MONTHS_BACK = 12


def _month_key(iso):
    """'2026-05-14' -> '2026-05'. None when the date cannot be read."""
    d = date_svc.to_iso(iso)
    return d[:7] if d else None


def _month_axis(months=MONTHS_BACK, today=None):
    """The last `months` months as ['2025-09', … '2026-08'], oldest first.

    Built from the calendar rather than from the data, so a month with nothing in
    it is a gap in the line instead of being silently skipped — a series that
    closes up its empty months reads as continuous activity that did not happen."""
    today = today or dt.date.today()
    out, y, m = [], today.year, today.month
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _label(month_key):
    """'2026-05' -> 'May 26', for an axis tick."""
    y, m = month_key.split("-")
    return f"{dt.date(int(y), int(m), 1).strftime('%b')} {y[2:]}"


# ---------------------------------------------------------------------------
#  Series
# ---------------------------------------------------------------------------
def purchases_by_month(db, months=MONTHS_BACK):
    """Posted purchase value per month, on the invoice date.

    Invoice date, not created_at: a bill keyed three weeks late belongs to the
    month the goods were billed, which is the month someone is asking about."""
    axis = _month_axis(months)
    seen = dict.fromkeys(axis, 0.0)
    # the two columns read, not every GRN as a full object
    for invoice_date, grand_total in (db.query(models.Purchase.invoice_date,
                                               models.Purchase.grand_total)
                                        .filter(models.Purchase.status == "posted")):
        k = _month_key(invoice_date)
        if k in seen:
            seen[k] += float(grand_total or 0)
    return {"labels": [_label(k) for k in axis],
            "values": [round(seen[k], 2) for k in axis]}


def movement_by_month(db, months=MONTHS_BACK):
    """Pieces in and pieces out per month, as two series.

    Both are reported positive — outward is stored as a negative delta, and a
    chart with one series below the axis would read as a diverging measure rather
    than two quantities being compared."""
    axis = _month_axis(months)
    inward = dict.fromkeys(axis, 0.0)
    outward = dict.fromkeys(axis, 0.0)
    # Only the two columns this reads, and only the months on the axis: the
    # whole ledger as ORM rows was a million objects to draw twelve bars.
    start = dt.datetime.strptime(axis[0] + "-01", "%Y-%m-%d") if axis else None
    rows = db.query(models.StockMovement.created_at, models.StockMovement.qty_delta)
    if start is not None:
        rows = rows.filter(models.StockMovement.created_at >= start)
    for created_at, qty_delta in rows:
        if not created_at:
            continue
        k = created_at.strftime("%Y-%m")
        if k not in inward:
            continue
        q = float(qty_delta or 0)
        if q >= 0:
            inward[k] += q
        else:
            outward[k] += -q
    return {"labels": [_label(k) for k in axis],
            "series": [{"name": "Received", "values": [round(inward[k], 2) for k in axis]},
                       {"name": "Dispatched", "values": [round(outward[k], 2) for k in axis]}]}


def stock_by_category(db, top=5):
    """Stock value split by category master, biggest first, tail folded to Other.

    Only products a posted GRN created are counted — the rule `inventory_summary`
    uses — so this totals to the Stock value tile rather than to a larger number
    that includes debris and records kept at zero after an unpost.

    Folded rather than truncated: a top-5 that quietly drops the rest would show a
    ring that does not add up to the stock value stated beside it."""
    from . import integrity
    from sqlalchemy import select
    # provenance read for exactly the products this ring can include
    ctx = integrity.Context(db, product_ids=select(models.Product.id)
                            .where(models.Product.stock_qty != 0))
    buckets = defaultdict(float)
    # A product with no stock has no stock value, so it cannot reach a bucket;
    # asking only the ones that hold stock gives the same ring for a fraction of
    # the provenance checks.
    for p in db.query(models.Product).filter(models.Product.stock_qty != 0):
        if ctx.product_state(p) != integrity.POSTED:
            continue
        val = float(p.stock_value or 0)
        if val <= 0:
            continue
        buckets[p.category or "Unmapped"] += val
    ranked = sorted(buckets.items(), key=lambda kv: -kv[1])
    head = [{"label": k, "value": round(v, 2)} for k, v in ranked[:top]]
    tail = ranked[top:]
    if tail:
        head.append({"label": f"Other ({len(tail)})",
                     "value": round(sum(v for _, v in tail), 2), "other": True})
    return head


def top_suppliers(db, top=6):
    """Suppliers by posted purchase value, biggest first."""
    buckets = defaultdict(float)
    # the supplier's name joined in, rather than one lazy load per supplier
    for sid, name, grand_total in (db.query(models.Supplier.id, models.Supplier.name,
                                            models.Purchase.grand_total)
                                .select_from(models.Purchase)
                                .outerjoin(models.Supplier,
                                           models.Supplier.id == models.Purchase.supplier_id)
                                .filter(models.Purchase.status == "posted")):
        buckets[name if sid is not None else "—"] += float(grand_total or 0)
    ranked = sorted(buckets.items(), key=lambda kv: -kv[1])[:top]
    return [{"label": k, "value": round(v, 2)} for k, v in ranked]


#: Ageing buckets, in order. An ordered scale, which is why the screen draws it
#: with one hue getting darker rather than with a colour per bucket.
AGEING_BANDS = ((0, 30, "0–30 days"), (31, 60, "31–60"), (61, 90, "61–90"),
                (91, None, "90+ days"))


def payables_ageing(db):
    """What is owed to suppliers, split by how long it has been owed."""
    out = [{"label": lbl, "value": 0.0, "bills": 0} for _, _, lbl in AGEING_BANDS]
    for b in pay_svc.pending_bills(db, None):
        days = b.get("days")
        # A bill whose invoice date could not be read has no age. It is put in the
        # oldest band rather than dropped: an unknown-age debt is a chasing problem,
        # and silently omitting it makes the total disagree with the payables tile.
        idx = len(AGEING_BANDS) - 1
        if days is not None:
            for i, (lo, hi, _lbl) in enumerate(AGEING_BANDS):
                if days >= lo and (hi is None or days <= hi):
                    idx = i
                    break
        out[idx]["value"] += float(b.get("outstanding") or 0)
        out[idx]["bills"] += 1
    for row in out:
        row["value"] = round(row["value"], 2)
    return out


def dashboard_charts(db, months=MONTHS_BACK):
    """Every series the graphical dashboard draws, in one call."""
    return {
        "months": months,
        "purchases": purchases_by_month(db, months),
        "movement": movement_by_month(db, months),
        "stock_by_category": stock_by_category(db),
        "top_suppliers": top_suppliers(db),
        "payables_ageing": payables_ageing(db),
    }
