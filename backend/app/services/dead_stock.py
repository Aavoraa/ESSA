"""Dead stock detection, the discount ladder, and what clearing it is worth.

The question this answers is "what have we been holding that nobody is buying,
and what is it worth to move it out". It is a READ over the records that already
exist — products, the stock-movement ledger, and the till's sales — and it
creates no stock of its own. A clearance line points at the product it came
from; when that product sells at the counter, the campaign's realisation moves
because the sale moved, not because anyone re-keyed it here.

**What counts as movement.** A garment is alive if it has left the warehouse in
either of the two ways stock leaves: sold at the till (services/pos_sales.py) or
dispatched to a store on a Stock Outward. The later of those two dates is what
the age is measured from — an item dispatched last week is not dead stock
whatever the till has done, and vice versa.

**Stock that has never moved at all.** Most of the register is this, and it is
the case the Excel had no answer for: with no sale to date from, "days since
sale" is blank and the row disappears out of the report that exists to find it.
Here the clock runs from the day the stock last CAME IN, and the row says so
(`basis`). Last inward rather than first: a SKU that was restocked a fortnight
ago is being bought, and dating it from a receipt two years old would fill the
register with lines nobody needs to act on — which is how a report like this
stops being read.

**Purchase returns are not movement.** Goods sent back to a supplier leave the
building without anybody buying them; counting that as life would hide a line
that was rejected precisely because it wasn't selling.
"""
import datetime as dt
import json

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .. import models, runtime
from ..request_memo import memo
from . import pos_sales, stock_view

# The ladder from the clearance worksheet, and the assumptions the cash impact
# is worked out on. Every one of these is editable from the screen; this is what
# an install starts with and what it falls back to for anything left unset.
#
# `to` is exclusive and the last band is open-ended. The bands are contiguous by
# construction — a gap between them would be an age with no discount, which is
# not a policy anybody means to write.
DEFAULT_RULES = {
    "buckets": [
        {"from": 0,   "to": 90,   "label": "Under 90 days", "discount": 0},
        {"from": 90,  "to": 120,  "label": "90–119 days",   "discount": 20},
        {"from": 120, "to": 180,  "label": "120–179 days",  "discount": 30},
        {"from": 180, "to": 270,  "label": "180–269 days",  "discount": 40},
        {"from": 270, "to": 365,  "label": "270–364 days",  "discount": 50},
        {"from": 365, "to": None, "label": "365+ days",     "discount": 60},
    ],
    # the three lines the alerts are drawn at
    "approaching_days": 60,
    "dead_after_days": 90,
    "critical_days": 180,
    # what freed-up capital is assumed to do in a year, and what it earns —
    # the two figures behind "clearing this is worth ₹X of annual revenue"
    "stock_turns": 4.0,
    "gross_margin_pct": 35.0,
}

#: what a clearance line can be marked for doing — the Excel's Action column
ACTIONS = ["Clear Now", "Markdown", "Bundle", "Promotional Sale",
           "Transfer to Store", "Return to Supplier", "Hold", "Review"]

#: a campaign's life. `draft` is being built, `active` is running (and its
#: realisation is being read off the till), `closed` is history and stops moving.
CAMPAIGN_STATUSES = ["draft", "active", "closed"]


# ---------------------------------------------------------------- rules ------

def get_rules():
    """The ladder in force. Stored settings are merged OVER the defaults, so an
    install that only changed one percentage still picks up every field added
    since, instead of being frozen at the shape it was first saved in."""
    rules = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_RULES.items()}
    saved = runtime.get("dead_stock_rules") or {}
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in rules and v not in (None, ""):
                rules[k] = v
    rules["buckets"] = _clean_buckets(rules.get("buckets"))
    return rules


def _clean_buckets(buckets):
    """Sorted, numeric, and never empty — whatever arrived from the client.

    A ladder is read by walking it in order, so an unsorted one silently applies
    the wrong discount rather than failing; sorting here is what makes the read
    below a simple walk."""
    out = []
    for b in (buckets or []):
        if not isinstance(b, dict):
            continue
        try:
            frm = int(float(b.get("from") or 0))
        except (TypeError, ValueError):
            continue
        to = b.get("to")
        try:
            to = None if to in (None, "", "∞") else int(float(to))
        except (TypeError, ValueError):
            to = None
        try:
            disc = float(b.get("discount") or 0)
        except (TypeError, ValueError):
            disc = 0.0
        out.append({"from": frm, "to": to, "discount": max(0.0, min(100.0, disc)),
                    "label": str(b.get("label") or "").strip() or _band_label(frm, to)})
    out.sort(key=lambda b: b["from"])
    return out or [dict(b) for b in DEFAULT_RULES["buckets"]]


def _band_label(frm, to):
    return f"{frm}+ days" if to is None else f"{frm}–{to - 1} days"


def save_rules(patch: dict):
    """Store the changed fields and return the ladder as it now stands."""
    current = get_rules()
    for k, v in (patch or {}).items():
        if k in DEFAULT_RULES and v is not None:
            current[k] = v
    current["buckets"] = _clean_buckets(current.get("buckets"))
    runtime.set_many(dead_stock_rules=current)
    return current


def bucket_for(days, rules=None):
    """The band an age falls in. Ages past the last band's start stay in it —
    a 900-day line is not off the end of the ladder, it is deep in the last rung."""
    rules = rules or get_rules()
    chosen = None
    for b in rules["buckets"]:
        if days >= b["from"] and (b["to"] is None or days < b["to"]):
            return b
        if days >= b["from"]:
            chosen = b
    return chosen or rules["buckets"][0]


# ------------------------------------------------------------- the read ------

def _iso(value):
    """A date column, a datetime column or None → 'YYYY-MM-DD' or None."""
    if not value:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.date().isoformat() if isinstance(value, dt.datetime) else value.isoformat()
    return str(value)[:10] or None


def _days_since(iso, today):
    if not iso:
        return None
    try:
        return max(0, (today - dt.date.fromisoformat(iso)).days)
    except ValueError:
        return None


def _movement_index(db, stocked_only=False):
    """Per product: when stock last went OUT on a dispatch, and last came IN.

    Two grouped queries rather than a walk of the ledger — the ledger is
    append-only and grows without limit, and this runs on every open of the
    register.

    `stocked_only` limits both to products holding stock — the only ones the
    register reads. Every product that ever moved is hundreds of thousands of
    groups shipped back to Python to be looked up a few thousand times; the
    stocked ones are found through the product_id index instead."""
    out = {}
    M = models.StockMovement
    stocked = (db.query(models.Product.id).filter(models.Product.stock_qty > 0)
               if stocked_only else None)

    def latest(cond):
        q = db.query(M.product_id, func.max(M.created_at)).filter(cond)
        if stocked is not None:
            q = q.filter(M.product_id.in_(stocked.scalar_subquery()))
        return q.group_by(M.product_id).all()

    dispatched = latest(M.ref_type == "outward")
    for pid, when in dispatched:
        out.setdefault(pid, {})["dispatched"] = _iso(when)
    received = latest(M.kind == "inward")
    for pid, when in received:
        out.setdefault(pid, {})["received"] = _iso(when)
    return out


def _price_base(p):
    """What a clearance price is worked out FROM, and where that came from.

    MRP is the honest base — a discount is off the tag, and that is the number
    the customer sees struck through. Failing that the sale price, and failing
    both, cost: a row priced off cost says so, because "40% off cost" is not a
    markdown, it is a loss, and nobody should read that figure without knowing."""
    if p.mrp:
        return float(p.mrp), "mrp"
    if p.sale_price:
        return float(p.sale_price), "sale_price"
    return float(p.avg_cost or 0), "cost"


def product_rows(db, rules=None, today=None, include_healthy=True):
    """Every stocked product with its age, its band and what clearing it realises.

    One pass, no per-product queries: the till's sales and the movement ledger
    are each read once and indexed, because this is the read behind the
    register, the dashboard, the alerts and the worksheet, and all four are
    opened at a desk while somebody waits.
    """
    rules = rules or get_rules()
    today = today or dt.date.today()
    # the Command Centre asks for this twice in one load (the dead-stock count
    # and the notification rules); same inputs, same request, same answer
    # Not keyed on the session: the rows are plain dicts, and the dashboards
    # build their sections on separate sessions of the same request.
    key = ("dead_stock.product_rows", today.isoformat(), bool(include_healthy),
           json.dumps(rules, sort_keys=True, default=str))
    return memo(key, lambda: _product_rows(db, rules, today, include_healthy))


class _Supplier:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class _ProductRow:
    """A product read as a plain row, answering `p.<column>` and
    `p.primary_supplier.name` as the ORM object did."""
    __slots__ = ("_row", "primary_supplier")

    def __init__(self, row, supplier_names):
        self._row = row
        sid = row.primary_supplier_id
        self.primary_supplier = _Supplier(supplier_names[sid]) if sid in supplier_names else None

    def __getattr__(self, name):
        return getattr(self._row, name)


def _product_rows(db, rules, today, include_healthy):
    # One read of the till. last_sold is taken from it rather than from
    # pos_sales.last_sold_index(), which is this same aggregation run again.
    # Only the stocked products' sales where the till shares our database — the
    # only ones read below — and the whole aggregation otherwise.
    # Our own two reads first, the till's after: on a dashboard the till's is
    # already running on another thread (command_center.warm_groups), so by the
    # time these are done it is usually there to be picked up — rather than
    # these waiting behind it. Reads only, so the order changes no figure.
    moves = _movement_index(db, stocked_only=True)
    # The stocked products as plain rows (the same statement's columns, the same
    # order) and the suppliers' names from one small read, rather than 360K
    # full objects with a supplier each: this was half of every dashboard's wait.
    P = models.Product
    names = dict(db.query(models.Supplier.id, models.Supplier.name).all())
    products = [_ProductRow(r, names) for r in db.execute(
        select(*P.__table__.columns).where(P.stock_qty > 0).order_by(P.id))]
    scoped = pos_sales.sales_of_stocked()
    sales = pos_sales.sales_by_product() if scoped is None else scoped[0]

    rows = []
    if scoped is not None:
        # stocked since the till was read: fetched rather than read as unsold
        missing = [p.id for p in products if p.id not in scoped[1]]
        if missing:
            sales = {**sales, **pos_sales.sales_of(missing)}
    for p in products:
        last_sold = (sales.get(p.id) or {}).get("last_sold")
        mv = moves.get(p.id, {})
        last_dispatched = mv.get("dispatched")
        last_received = mv.get("received") or _iso(p.created_at)

        # the later of the two ways stock leaves; only when it has never left at
        # all does the clock run from the day it last came in
        moved_on, basis = None, "never"
        for when, kind in ((last_sold, "sale"), (last_dispatched, "dispatch")):
            if when and (moved_on is None or when > moved_on):
                moved_on, basis = when, kind
        if moved_on is None:
            moved_on, basis = last_received, "received"
        days = _days_since(moved_on, today)
        if days is None:
            # no sale, no dispatch, no receipt and no created_at — nothing to
            # date it from, so it is not evidence of anything
            continue

        band = bucket_for(days, rules)
        qty = float(p.stock_qty or 0)
        cost = float(p.avg_cost or 0)
        base, price_source = _price_base(p)
        discount = float(band["discount"])
        clearance_price = round(base * (1 - discount / 100.0), 2)
        status = ("critical" if days >= rules["critical_days"]
                  else "dead" if days >= rules["dead_after_days"]
                  else "approaching" if days >= rules["approaching_days"]
                  else "healthy")
        if not include_healthy and status == "healthy":
            continue
        sale = sales.get(p.id) or {}
        rows.append({
            "product_id": p.id, "sku": p.sku, "barcode": p.barcode,
            "name": stock_view.display_name(p), "description": p.description,
            "category": p.category, "category_section": p.category_section,
            "supplier": p.primary_supplier.name if p.primary_supplier else None,
            "size": p.size, "color": p.color, "brand": p.brand,
            "uom": p.uom,
            "qty": round(qty, 3),
            "cost_price": round(cost, 2),
            "stock_value": round(qty * cost, 2),
            "mrp": p.mrp, "sale_price": p.sale_price,
            "price_base": round(base, 2), "price_source": price_source,
            "last_sold": last_sold, "last_dispatched": last_dispatched,
            "last_received": last_received,
            "basis": basis, "moved_on": moved_on, "days_idle": days,
            "bucket": band["label"], "bucket_from": band["from"],
            "discount_pct": discount,
            "clearance_price": clearance_price,
            "expected_realisation": round(clearance_price * qty, 2),
            "status": status,
            # what the till has ever done with it — the answer to "is this
            # genuinely unsold, or unsold SINCE a run that emptied the shelf"
            "sold_ever_qty": sale.get("qty"),
            "sold_ever_amount": sale.get("amount"),
        })
    rows.sort(key=lambda r: (-r["days_idle"], -r["stock_value"]))
    return rows


# --------------------------------------------------------------- filters -----

def _matches(row, q="", bucket="", category="", supplier="", size="",
             status="dead", min_value=None, min_qty=None):
    if status and status != "all" and row["status"] != status:
        if not (status == "dead" and row["status"] == "critical"):
            return False
    if bucket and row["bucket"] != bucket:
        return False
    for value, key in ((category, "category"), (supplier, "supplier"), (size, "size")):
        if value and (row.get(key) or "").strip().lower() != value.strip().lower():
            return False
    if min_value is not None and row["stock_value"] < min_value:
        return False
    if min_qty is not None and row["qty"] < min_qty:
        return False
    if q:
        hay = " ".join(str(row.get(k) or "") for k in
                       ("sku", "barcode", "name", "description", "category",
                        "supplier", "size", "color", "brand")).lower()
        if q.strip().lower() not in hay:
            return False
    return True


def register(db, **filters):
    """The Dead Stock Register: the rows, their totals, and the filter lists.

    `status` defaults to `dead`, which includes critical — the register exists to
    show what has crossed the line, and a critical row is a dead row that has
    been dead longer."""
    rules = get_rules()
    rows = product_rows(db, rules)
    shown = [r for r in rows if _matches(r, **filters)]
    return {
        "rows": shown,
        "totals": _totals(shown),
        "rules": rules,
        "pos": pos_sales.status(),
        # every value present in the FULL read, so a filter that empties the
        # list can still be changed to one that doesn't
        "options": {
            "buckets": [b["label"] for b in rules["buckets"]],
            "categories": sorted({r["category"] for r in rows if r["category"]}),
            "suppliers": sorted({r["supplier"] for r in rows if r["supplier"]}),
            "sizes": sorted({r["size"] for r in rows if r["size"]}),
        },
    }


def _totals(rows):
    return {
        "lines": len(rows),
        "qty": round(sum(r["qty"] for r in rows), 3),
        "stock_value": round(sum(r["stock_value"] for r in rows), 2),
        "expected_realisation": round(sum(r["expected_realisation"] for r in rows), 2),
    }


# --------------------------------------------------------------- summary -----

def summary(db):
    """The dashboard: the four figures, the ladder's own breakdown, and what the
    categories look like — all off ONE read of the products."""
    rules = get_rules()
    rows = product_rows(db, rules)
    dead = [r for r in rows if r["status"] in ("dead", "critical")]
    totals = _totals(dead)
    locked = totals["stock_value"]
    expected = totals["expected_realisation"]

    by_category = {}
    for r in dead:
        key = r["category"] or "Uncategorised"
        g = by_category.setdefault(key, {"category": key, "lines": 0, "qty": 0.0,
                                         "stock_value": 0.0, "expected_realisation": 0.0})
        g["lines"] += 1
        g["qty"] += r["qty"]
        g["stock_value"] += r["stock_value"]
        g["expected_realisation"] += r["expected_realisation"]
    for g in by_category.values():
        g["qty"] = round(g["qty"], 3)
        g["stock_value"] = round(g["stock_value"], 2)
        g["expected_realisation"] = round(g["expected_realisation"], 2)
        g["recovery_pct"] = _pct(g["expected_realisation"], g["stock_value"])

    # only the bands that actually hold something. The full ladder is a policy
    # and it is shown, editable, on the Rules screen; a summary padded with empty
    # rungs is a table someone has to read past to find the two that matter
    by_bucket = []
    for b in rules["buckets"]:
        band = [r for r in dead if r["bucket"] == b["label"]]
        if not band:
            continue
        by_bucket.append({"bucket": b["label"], "discount_pct": b["discount"], **_totals(band)})

    return {
        "totals": {**totals, "recovery_pct": _pct(expected, locked),
                   # SKUs as well as pieces: "24 products" is the number somebody
                   # can act on, "186 pcs" is the number that sounds urgent, and
                   # a dashboard tile needs both
                   "skus": len(dead)},
        "counts": _counts(rows, rules),
        "by_category": sorted(by_category.values(), key=lambda g: -g["stock_value"]),
        "by_bucket": by_bucket,
        "trend": clearance_trend(db),
        "cash_impact": cash_impact(locked, expected, rules),
        "rules": rules,
        "pos": pos_sales.status(),
        "oldest": dead[:10],
    }


def _pct(part, whole):
    return round(part / whole * 100.0, 1) if whole else None


def clearance_trend(db, months=6):
    """Expected against actually realised, by the month a campaign started.

    The one figure management asks for that a register cannot answer: not "how
    much dead stock is there" but "did the last clearance work". Expected is what
    the worksheet promised; realised is what the till has since taken for those
    products inside those dates — so the gap between the two bars IS the
    shortfall, and it moves on its own as the goods sell.

    Grouped by start month rather than by campaign so two runs in one month read
    as one month's effort, which is how a month is judged.
    """
    campaigns = db.query(models.ClearanceCampaign).order_by(
        models.ClearanceCampaign.starts_on).all()
    by_month = {}
    for c in campaigns:
        month = (c.starts_on or _iso(c.created_at) or "")[:7]
        if not month:
            continue
        out = campaign_out(db, c)
        t = out["totals"]
        row = by_month.setdefault(month, {"month": month, "campaigns": 0, "qty": 0.0,
                                          "sold_qty": 0.0, "expected": 0.0, "actual": 0.0,
                                          "cost": 0.0})
        row["campaigns"] += 1
        row["qty"] += t["qty"] or 0
        row["sold_qty"] += t["sold_qty"] or 0
        row["expected"] += t["expected_realisation"] or 0
        row["actual"] += t["actual_realisation"] or 0
        row["cost"] += t["stock_cost"] or 0
    rows = sorted(by_month.values(), key=lambda r: r["month"])[-max(1, months):]
    for r in rows:
        r["qty"] = round(r["qty"], 3)
        r["sold_qty"] = round(r["sold_qty"], 3)
        r["remaining_qty"] = round(max(0.0, r["qty"] - r["sold_qty"]), 3)
        for k in ("expected", "actual", "cost"):
            r[k] = round(r[k], 2)
        r["realisation_pct"] = _pct(r["actual"], r["expected"])
        r["sell_through_pct"] = _pct(r["sold_qty"], r["qty"])
    return rows


def _counts(rows, rules):
    out = {}
    for name in ("healthy", "approaching", "dead", "critical"):
        band = [r for r in rows if r["status"] == name]
        out[name] = {"lines": len(band),
                     "qty": round(sum(r["qty"] for r in band), 3),
                     "stock_value": round(sum(r["stock_value"] for r in band), 2)}
    # "dead" on the dashboard means everything past the line, critical included —
    # the two are shown separately below it, and a total that excluded the worst
    # rows would be the one number nobody could reconcile
    out["dead_total"] = {
        "lines": out["dead"]["lines"] + out["critical"]["lines"],
        "qty": round(out["dead"]["qty"] + out["critical"]["qty"], 3),
        "stock_value": round(out["dead"]["stock_value"] + out["critical"]["stock_value"], 2),
    }
    out["thresholds"] = {"approaching": rules["approaching_days"],
                         "dead": rules["dead_after_days"],
                         "critical": rules["critical_days"]}
    return out


def cash_impact(locked, expected, rules=None):
    """What clearing the dead stock is worth, stated with its assumptions.

    The annual figures are what the freed capital would do if it turned over at
    the configured rate — a projection, and labelled as one. It is kept because
    "₹1.4 lakh is asleep on a shelf" is an argument nobody acts on, and "that
    capital would turn four times and earn ₹3 lakh of margin" is one that gets
    the markdown approved."""
    rules = rules or get_rules()
    turns = float(rules.get("stock_turns") or 0)
    margin = float(rules.get("gross_margin_pct") or 0)
    annual_revenue = round(expected * turns, 2)
    return {
        "capital_locked": round(locked, 2),
        "expected_cash": round(expected, 2),
        "recovery_pct": _pct(expected, locked),
        "stock_turns": turns,
        "gross_margin_pct": margin,
        "annual_revenue_potential": annual_revenue,
        "annual_gross_profit": round(annual_revenue * margin / 100.0, 2),
    }


# ---------------------------------------------------------------- alerts -----

def alerts(db):
    """The three warnings, in the order they should be acted on.

    Three levels rather than one 90-day event, because the useful moment is
    before the line goes dead: at 60 days a markdown of 10% might still move it,
    and at 180 the question has changed from "what discount" to "who will take
    the lot"."""
    return {"alerts": alert_levels(db), "checked_on": dt.date.today().isoformat(),
            "pos": pos_sales.status()}


def alert_levels(db):
    """`alerts()["alerts"]` alone — for the notification rules, which read the
    bands and not where the till's figures came from (`status()`, a query of
    its own)."""
    rules = get_rules()
    rows = product_rows(db, rules)
    levels = []
    for key, tone, title, note in (
        ("critical", "critical", "Critical dead stock",
         "unsold for %(days)s+ days — immediate clearance recommended"),
        ("dead", "dead", "Dead stock", "no sale for %(days)s+ days"),
        ("approaching", "warn", "Approaching dead stock",
         "no sale for %(days)s+ days — dead in under a month"),
    ):
        band = [r for r in rows if r["status"] == key]
        if not band:
            continue
        days = {"critical": rules["critical_days"], "dead": rules["dead_after_days"],
                "approaching": rules["approaching_days"]}[key]
        levels.append({
            "level": key, "tone": tone, "title": title,
            "note": note % {"days": days},
            "lines": len(band),
            "qty": round(sum(r["qty"] for r in band), 3),
            "stock_value": round(sum(r["stock_value"] for r in band), 2),
            "expected_realisation": round(sum(r["expected_realisation"] for r in band), 2),
        })
    return levels


# ------------------------------------------------------------- campaigns -----

def campaign_lines(db, campaign):
    """A campaign's lines with what has ACTUALLY happened to them since it opened.

    Sold and realised are read from the till inside the campaign's own dates —
    never stored on the line. Storing them would make this a second stock record
    that has to be kept in step with the first, and the first is the one the
    business runs on."""
    window = pos_sales.sales_by_product(campaign.starts_on, campaign.ends_on)
    out = []
    for l in campaign.lines:
        p = l.product
        sold = window.get(l.product_id) or {}
        sold_qty = round(float(sold.get("qty") or 0), 3)
        planned = float(l.qty or 0)
        out.append({
            "id": l.id, "product_id": l.product_id,
            "sku": p.sku if p else None,
            "name": stock_view.display_name(p) if p else l.note,
            "size": p.size if p else None, "category": p.category if p else None,
            "qty": planned,
            "stock_qty": float(p.stock_qty or 0) if p else None,
            "days_idle": l.days_idle, "bucket": l.bucket,
            "cost_price": l.cost_price, "mrp": l.mrp,
            "discount_pct": l.discount_pct,
            "clearance_price": l.clearance_price,
            "expected_realisation": l.expected_realisation,
            "action": l.action, "note": l.note,
            "sold_qty": sold_qty,
            "remaining_qty": round(max(0.0, planned - sold_qty), 3),
            "actual_realisation": round(float(sold.get("amount") or 0), 2),
            "bills": sold.get("bills") or 0,
        })
    return out


def campaign_out(db, campaign, with_lines=True):
    lines = campaign_lines(db, campaign) if with_lines else []
    planned = round(sum(l["qty"] for l in lines), 3)
    sold = round(sum(l["sold_qty"] for l in lines), 3)
    expected = round(sum(l["expected_realisation"] or 0 for l in lines), 2)
    actual = round(sum(l["actual_realisation"] for l in lines), 2)
    cost = round(sum((l["cost_price"] or 0) * l["qty"] for l in lines), 2)
    out = {
        "id": campaign.id, "name": campaign.name, "status": campaign.status,
        "starts_on": campaign.starts_on, "ends_on": campaign.ends_on,
        "note": campaign.note, "created_by": campaign.created_by,
        "created_at": _iso(campaign.created_at), "closed_at": _iso(campaign.closed_at),
        "line_count": len(lines),
        "totals": {
            "qty": planned, "sold_qty": sold,
            "remaining_qty": round(max(0.0, planned - sold), 3),
            "stock_cost": cost,
            "expected_realisation": expected,
            "actual_realisation": actual,
            "sell_through_pct": _pct(sold, planned),
            "realisation_pct": _pct(actual, expected),
        },
    }
    if with_lines:
        out["lines"] = lines
    return out


def line_from_row(row, action="Review"):
    """A register row, frozen onto a clearance line.

    The age, the band, the discount and the price are COPIED rather than looked
    up later on purpose: they are what the campaign was approved on. Left live,
    every line would silently re-price itself as the stock aged, and a campaign
    would no longer be able to say what it had promised."""
    return dict(
        product_id=row["product_id"], qty=row["qty"], days_idle=row["days_idle"],
        bucket=row["bucket"], discount_pct=row["discount_pct"],
        cost_price=row["cost_price"], mrp=row["mrp"],
        clearance_price=row["clearance_price"],
        expected_realisation=row["expected_realisation"],
        action=action if action in ACTIONS else "Review",
    )
