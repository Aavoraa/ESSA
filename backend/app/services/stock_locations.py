"""Where the stock is — the ledger split by warehouse.

WHAT THIS CHANGES. Until now `Product.stock_qty` was one number for the whole
company and `StockMovement` had no idea which building it happened in. That was
the honest shape of a one-warehouse business, and it is the single thing standing
between this app and a second warehouse: with one figure, receiving in Erode and
dispatching from Karur both move the same pile, and "how much is in Karur" has no
answer to give.

So every movement now names a warehouse, and the balance at each one is kept in
`stock_balances`.

THE COMPANY TOTALS STAY WHERE THEY WERE. `Product.stock_qty` and
`Product.avg_cost` are still maintained, as the roll-up of the balances beneath
them. That is deliberate and it is what makes this change safe to land: a dozen
screens, the locator, dead stock, the reports and the shop sync all read those
two columns, and they keep reading the same numbers they always did. Nothing had
to be rewritten to keep working; things get rewritten to become warehouse-aware,
one at a time, because they want to be.

THE LEDGER IS STILL THE TRUTH. `stock_balances` is a cache of a sum — kept
because the picker's screen asks "what is on hand here" before every dispatch
line, and answering that with a GROUP BY over every movement ever written is a
table scan per keystroke. Every write goes through `apply` below, which appends
the movement and updates the balance together; `rebuild` recomputes the table
from the ledger whenever the two are doubted, and `backfill` is that same replay
run once over a database that predates any of this.

WHY AVERAGE COST IS PER WAREHOUSE. The same shirt bought at ₹180 into Erode and
₹200 into Karur is worth different money in each building. A warehouse-wise stock
valuation built on one blended figure would report, for both, a value neither of
them holds. So the weighted average rolls per warehouse, and the company figure
is the value-weighted roll-up of them — which, with one warehouse, is the number
this app has always shown.

STORES ARE NOT IN HERE. A store's stock lives in the till's own database, which
reads the dispatches raised against it (see pos_mount and the shop's
app/transfers). Stock dispatched to a shop therefore LEAVES this ledger and does
not arrive anywhere in it. Recording an arrival here as well would put the same
garment on two systems' shelves and double the company's stock on hand.
"""
import datetime as dt

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from .. import models

#: Movements that ADD at cost, and so roll the weighted average. Everything else
#: — a dispatch, an adjustment, an unpost's reversal — moves quantity and leaves
#: the valuation of what remains alone.
_VALUING_KINDS = ("inward",)

#: Quantities are floats; "is it zero" needs a tolerance rather than ==.
TOLERANCE = 0.001


# ---------------------------------------------------------------------------
#  which warehouse
# ---------------------------------------------------------------------------
def default_warehouse(db: Session):
    """The warehouse a movement belongs to when nothing said which.

    Every install that predates this module IS one warehouse — that is the
    assumption the whole app was built on — so resolving a blank to the first one
    is describing what is already true rather than picking a building at random.
    """
    from . import locations
    return locations.ensure_default_warehouse(db)


def default_warehouse_id(db: Session):
    wh = default_warehouse(db)
    return wh.id if wh else None


def resolve_warehouse_id(db: Session, warehouse_id=None):
    """A warehouse id that certainly exists. Falls back to the default."""
    if warehouse_id:
        wh = db.get(models.Warehouse, int(warehouse_id))
        if wh:
            return wh.id
    return default_warehouse_id(db)


# ---------------------------------------------------------------------------
#  reading a balance
# ---------------------------------------------------------------------------
def _row(db: Session, product_id: int, warehouse_id: int, create=False):
    row = (db.query(models.StockBalance)
             .filter(models.StockBalance.product_id == product_id,
                     models.StockBalance.warehouse_id == warehouse_id).first())
    if row is None and create:
        row = models.StockBalance(product_id=product_id, warehouse_id=warehouse_id,
                                  qty=0.0, avg_cost=0.0)
        db.add(row)
        db.flush()
    return row


def qty_at(db: Session, product_id: int, warehouse_id=None) -> float:
    """How many of this product stand in this warehouse.

    A blank warehouse means the company total, NOT the default warehouse's
    balance — a caller that has not been made warehouse-aware yet is asking the
    question it has always asked, and answering it with one building's figure
    would silently under-report the moment a second building has stock.
    """
    if not warehouse_id:
        p = db.get(models.Product, product_id)
        return float(p.stock_qty or 0) if p else 0.0
    row = _row(db, product_id, warehouse_id)
    return float(row.qty or 0) if row else 0.0


def cost_at(db: Session, product_id: int, warehouse_id=None) -> float:
    """The weighted-average cost this product carries at this warehouse."""
    if warehouse_id:
        row = _row(db, product_id, warehouse_id)
        if row and (row.qty or 0) > TOLERANCE:
            return float(row.avg_cost or 0)
    p = db.get(models.Product, product_id)
    return float(p.avg_cost or 0) if p else 0.0


def balances_for(db: Session, product_id: int) -> list:
    """Every warehouse this product stands in, most stock first.

    Zero rows are kept and returned. A warehouse that HAS held this item and now
    holds none is a different fact from one that never stocked it, and the
    dispatch screen needs to show the difference — "0 in Karur" is an answer,
    "Karur is not listed" is a question.
    """
    rows = (db.query(models.StockBalance)
              .filter(models.StockBalance.product_id == product_id).all())
    out = []
    for r in rows:
        wh = r.warehouse
        out.append({"warehouse_id": r.warehouse_id,
                    "warehouse_name": wh.name if wh else None,
                    "warehouse_code": wh.code if wh else None,
                    "qty": round(float(r.qty or 0), 3),
                    "avg_cost": round(float(r.avg_cost or 0), 4),
                    "value": r.value})
    out.sort(key=lambda x: (-x["qty"], x["warehouse_name"] or ""))
    return out


# ---------------------------------------------------------------------------
#  writing — the one way stock moves
# ---------------------------------------------------------------------------
def apply(db: Session, product, warehouse_id, qty_delta, *, kind, ref_type,
          ref_id=None, rate=None, note=None):
    """Move stock at one warehouse: append the movement, update the balance.

    This is the ONLY way stock changes. Everything else — a GRN posting, a
    dispatch, an unpost's reversal, a manual adjustment — comes through here, so
    there is exactly one place where the ledger and the balance can disagree, and
    it is a place that writes both.

    `qty_delta` is signed: positive receives, negative issues. An inward rolls the
    warehouse's weighted-average cost; nothing else does, because a dispatch does
    not change what the remaining goods cost.

    Returns the StockMovement it wrote, or None when the delta rounds to nothing.
    """
    qty_delta = round(float(qty_delta or 0), 3)
    if abs(qty_delta) < TOLERANCE:
        return None
    warehouse_id = resolve_warehouse_id(db, warehouse_id)
    rate = float(rate or 0)

    row = _row(db, product.id, warehouse_id, create=True)
    old_qty = float(row.qty or 0)
    old_avg = float(row.avg_cost or 0)
    new_qty = round(old_qty + qty_delta, 3)

    if kind in _VALUING_KINDS and qty_delta > 0:
        row.avg_cost = (round(((old_qty * old_avg) + (qty_delta * rate)) / new_qty, 4)
                        if new_qty > TOLERANCE else rate)
    row.qty = new_qty
    row.updated_at = dt.datetime.utcnow()

    mv = models.StockMovement(
        product_id=product.id, warehouse_id=warehouse_id, qty_delta=qty_delta,
        kind=kind, ref_type=ref_type, ref_id=ref_id, rate=rate,
        balance_after=row.qty, note=note,
    )
    db.add(mv)
    roll_up(db, product)
    db.flush()
    return mv


def roll_up(db: Session, product):
    """Restate the company figures from the balances beneath them.

    `Product.stock_qty` is the sum; `Product.avg_cost` is the value-weighted
    average across the warehouses holding it, which is the only definition under
    which the company's stock value equals the sum of the warehouses' — any other
    and the consolidated report would not add up to the warehouse-wise one.

    At zero stock the previous average is KEPT rather than zeroed. A product that
    has sold out still cost what it cost, and zeroing it would make the next
    receipt look like the first one this item ever had.
    """
    rows = (db.query(models.StockBalance)
              .filter(models.StockBalance.product_id == product.id).all())
    total_qty = round(sum(float(r.qty or 0) for r in rows), 3)
    product.stock_qty = total_qty
    if total_qty > TOLERANCE:
        value = sum(float(r.qty or 0) * float(r.avg_cost or 0) for r in rows)
        product.avg_cost = round(value / total_qty, 4)
    return total_qty


# ---------------------------------------------------------------------------
#  replay — the ledger recomputed, which is what makes the cache safe
# ---------------------------------------------------------------------------
def replay(db: Session, product, exclude_ids=()) -> dict:
    """{warehouse_id: (qty, avg_cost)} from this product's ledger.

    A weighted average cannot be un-mixed arithmetically — it depends on the
    order things arrived — so removing one receipt from it is only ever right
    when nothing happened since. Replaying the append-only ledger without those
    rows gives the exact figures the product would carry if they had never been
    written, which is what an append-only ledger is for. `exclude_ids` is how an
    unpost asks "what would this be if my rows were not there".

    Movements written before warehouses held stock carry no warehouse and are
    read as the default one — see `backfill`, which makes that permanent.

    The ledger is QUERIED rather than read off `product.movements`. That
    relationship is a session-cached collection, and a movement inserted through
    `apply` sets the foreign key without appending to it — so a collection loaded
    earlier in the same session does not contain the rows just written. Replaying
    from it therefore restated a product to what it was BEFORE the change that
    prompted the replay, which is exactly wrong during an unpost: the reversal is
    written and then immediately replayed away.
    """
    exclude_ids = set(exclude_ids or ())
    fallback = default_warehouse_id(db)
    out = {}
    rows = (db.query(models.StockMovement)
              .filter(models.StockMovement.product_id == product.id)
              .order_by(models.StockMovement.id).all())
    for mv in rows:
        if mv.id in exclude_ids:
            continue
        wid = mv.warehouse_id or fallback
        qty, avg = out.get(wid, (0.0, 0.0))
        delta = float(mv.qty_delta or 0)
        if delta > 0 and mv.kind in _VALUING_KINDS:
            new_qty = round(qty + delta, 3)
            rate = float(mv.rate or 0)
            avg = (round(((qty * avg) + (delta * rate)) / new_qty, 4)
                   if new_qty > TOLERANCE else rate)
            qty = new_qty
        else:
            qty = round(qty + delta, 3)
        out[wid] = (qty, avg)
    return out


def rebuild(db: Session, product) -> float:
    """Recompute this product's balances and company totals from its ledger.

    Used wherever movements were written or removed outside `apply` — an unpost
    is the case that matters, because it appends compensating rows and then has
    to restate a weighted average that cannot be arithmetically undone. Returns
    the company quantity.
    """
    replayed = replay(db, product)
    have = {r.warehouse_id: r for r in
            db.query(models.StockBalance).filter(
                models.StockBalance.product_id == product.id).all()}

    for wid, (qty, avg) in replayed.items():
        row = have.pop(wid, None)
        if row is None:
            row = models.StockBalance(product_id=product.id, warehouse_id=wid)
            db.add(row)
        row.qty = qty
        row.avg_cost = avg
        row.updated_at = dt.datetime.utcnow()

    # A warehouse the ledger no longer mentions holds nothing. The row is zeroed
    # rather than deleted: it is the record that this building did once stock
    # this item, which is what makes "0 in Karur" distinguishable from "Karur
    # never had it" on the dispatch screen.
    for row in have.values():
        row.qty = 0.0
        row.updated_at = dt.datetime.utcnow()

    db.flush()
    return roll_up(db, product)


def rebuild_all(db: Session, product_ids=None) -> int:
    """Replay the ledger for every product (or the ones named). Returns the count."""
    q = db.query(models.Product)
    if product_ids:
        q = q.filter(models.Product.id.in_(list(product_ids)))
    n = 0
    for product in q.all():
        rebuild(db, product)
        n += 1
    db.commit()
    return n


# ---------------------------------------------------------------------------
#  backfill — one database that predates all of this
# ---------------------------------------------------------------------------
def backfill(db: Session) -> dict:
    """Give every locationless movement a warehouse, and build the balance table.

    Safe to run on every start and a no-op once done. The assignment is not a
    guess: an install with movements but no warehouse column was a ONE-warehouse
    business — that is the assumption the app was written under — so every one of
    those rows happened at the one warehouse, and `balance_after` on them is
    already that warehouse's balance as well as the company's.

    Only runs the replay when something actually needed filling, or when the
    balance table is empty against a ledger that is not. Replaying every product
    on every boot would be a table scan of the whole history each morning.
    """
    if not db.query(models.StockMovement).first():
        return {"movements_located": 0, "products_rebuilt": 0}

    wid = default_warehouse_id(db)
    if not wid:
        return {"movements_located": 0, "products_rebuilt": 0}

    located = (db.query(models.StockMovement)
                 .filter(models.StockMovement.warehouse_id.is_(None))
                 .update({"warehouse_id": wid}, synchronize_session=False))
    if located:
        db.commit()

    empty = not db.query(models.StockBalance).first()
    if not (located or empty):
        return {"movements_located": 0, "products_rebuilt": 0}

    rebuilt = rebuild_all(db)
    return {"movements_located": int(located or 0), "products_rebuilt": rebuilt}


# ---------------------------------------------------------------------------
#  aggregates — what the central dashboard and the warehouse-wise reports read
# ---------------------------------------------------------------------------
def warehouse_totals(db: Session, warehouse_ids=None) -> list:
    """Stock quantity, value and distinct items, per warehouse.

    Every ACTIVE warehouse appears, including the ones holding nothing. A new
    building that has not received yet is a row reading zero, not a row missing
    from the dashboard — missing is how a warehouse nobody has stocked stays
    invisible until someone wonders where it went.
    """
    wanted = set(warehouse_ids) if warehouse_ids else None
    # Summed per warehouse by the database: one row per building instead of
    # every balance row in the company loaded as an object (hundreds of
    # thousands of them on a real catalogue) to be added up here.
    B = models.StockBalance
    q = db.query(B.warehouse_id,
                 func.sum(func.coalesce(B.qty, 0)),
                 func.sum(func.coalesce(B.qty, 0) * func.coalesce(B.avg_cost, 0)),
                 func.sum(case((func.coalesce(B.qty, 0) > TOLERANCE, 1), else_=0)))
    if wanted is not None:
        q = q.filter(B.warehouse_id.in_(list(wanted)))
    agg = {wid: {"qty": float(qty or 0), "value": float(value or 0), "items": int(items or 0)}
           for wid, qty, value, items in q.group_by(B.warehouse_id).all()}

    # Built from the SAME serialiser every other warehouse payload uses, rather
    # than hand-rolled here. Hand-rolling is what made the dashboard's "Trades
    # in" column permanently blank: the screen reads `catalogue`, and this
    # function had never emitted it. Two dict literals describing one row is two
    # things to keep in step, and they had already drifted.
    from . import locations as loc_svc

    out = []
    q = db.query(models.Warehouse).filter(models.Warehouse.active.is_(True))
    if wanted is not None:
        q = q.filter(models.Warehouse.id.in_(list(wanted)))
    for wh in q.order_by(models.Warehouse.name).all():
        a = agg.pop(wh.id, {"qty": 0.0, "value": 0.0, "items": 0})
        out.append({**loc_svc.warehouse_out(wh, counts=True),
                    # `warehouse_id` as well as `id`: the dashboard and the
                    # enter-warehouse button both key on it.
                    "warehouse_id": wh.id,
                    "qty": round(a["qty"], 3), "value": round(a["value"], 2),
                    "items": a["items"]})
    # Stock standing in a warehouse that has been switched off is still stock,
    # and a valuation that quietly dropped it would not reconcile to the ledger.
    for wid, a in agg.items():
        if a["qty"] <= TOLERANCE:
            continue
        wh = db.get(models.Warehouse, wid)
        row = (loc_svc.warehouse_out(wh, counts=True) if wh else
               {"id": wid, "name": f"#{wid}", "code": None, "address": None,
                "catalogue": None, "catalogue_id": None, "catalogue_code": None,
                "store_count": 0})
        out.append({**row, "warehouse_id": wid, "active": False,
                    "qty": round(a["qty"], 3), "value": round(a["value"], 2),
                    "items": a["items"]})
    return out


def stock_at(db: Session, warehouse_id, limit=None) -> list:
    """Every product standing in one warehouse, most valuable first."""
    # each balance's product loaded with it, not one query per row
    rows = (db.query(models.StockBalance)
              .options(joinedload(models.StockBalance.product))
              .filter(models.StockBalance.warehouse_id == warehouse_id,
                      models.StockBalance.qty > TOLERANCE).all())
    out = []
    for r in rows:
        p = r.product
        if not p:
            continue
        out.append({"product_id": p.id, "sku": p.sku, "barcode": p.barcode,
                    "description": p.description, "category": p.category,
                    "uom": p.uom, "qty": round(float(r.qty or 0), 3),
                    "avg_cost": round(float(r.avg_cost or 0), 4),
                    "value": r.value})
    out.sort(key=lambda x: -x["value"])
    return out[:limit] if limit else out


def movement_series(db: Session, days=14, warehouse_id=None, today=None) -> dict:
    """Quantity in and out per DAY — the dashboard's Inward vs Outward chart.

    Every day in the window is returned, including the ones nothing happened on.
    A chart drawn only from days that have rows compresses a quiet week into a
    single bar and makes the gap invisible, which is the opposite of what a
    movement chart is read for.
    """
    import datetime as dt

    days = max(1, min(int(days or 14), 90))
    end = (today or dt.datetime.utcnow()).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=days - 1)

    buckets = {(start + dt.timedelta(days=i)).date(): [0.0, 0.0]
               for i in range(days)}

    q = db.query(models.StockMovement).filter(
        models.StockMovement.created_at >= start)
    if warehouse_id:
        q = q.filter(models.StockMovement.warehouse_id == warehouse_id)
    for mv in q.all():
        if not mv.created_at:
            continue
        slot = buckets.get(mv.created_at.date())
        if slot is None:
            continue
        d = float(mv.qty_delta or 0)
        if d > 0:
            slot[0] += d
        else:
            slot[1] += -d

    keys = sorted(buckets)
    return {
        # Day and month only. The year is the same across a two-week window and
        # spelling it out on every tick crowds the axis off the card.
        "labels": [k.strftime("%d %b") for k in keys],
        "inward": [round(buckets[k][0], 3) for k in keys],
        "outward": [round(buckets[k][1], 3) for k in keys],
        "days": days,
    }


def transfer_summary(db: Session, since=None, warehouse_id=None) -> dict:
    """Movements BETWEEN this company's own places — the transfer register,
    summarised for the dashboard.

    Counted per warehouse in two directions, because "what left here" and "what
    came here" are different questions asked by different people, and a single
    net figure answers neither. In-transit is called out separately: a transfer
    that has been dispatched and not yet counted in belongs to no warehouse's
    shelf, and it is the number worth chasing.
    """
    q = db.query(models.StockOutward)
    if since:
        q = q.filter(models.StockOutward.created_at >= since)
    rows = q.all()
    if warehouse_id:
        rows = [o for o in rows
                if warehouse_id in (o.from_warehouse_id, o.to_warehouse_id)]

    # The line totals every note needs, in ONE grouped query. Reading
    # `o.total_qty` / `o.total_accepted` loads each note's lines separately —
    # one query per note, 17,000 of them on a real register — for two sums the
    # database can give for all of them at once. Same arithmetic as the model's
    # properties: sent = Σ qty; accepted = Σ (accepted_qty, else qty).
    L = models.StockOutwardLine
    line_sums = {oid: (float(sent or 0), float(acc or 0)) for oid, sent, acc in
                 db.query(L.outward_id, func.sum(L.qty),
                          func.sum(func.coalesce(L.accepted_qty, L.qty)))
                   .group_by(L.outward_id).all()}

    def sent_of(o):
        return line_sums.get(o.id, (0.0, 0.0))[0]

    def accepted_of(o):
        # StockOutward.total_accepted: nothing is accepted until it is received
        return line_sums.get(o.id, (0.0, 0.0))[1] if o.status == "received" else 0.0

    per = {}

    def slot(wid):
        return per.setdefault(wid, {"warehouse_id": wid, "name": None,
                                    "sent": 0.0, "received": 0.0,
                                    "in_transit": 0.0, "to_stores": 0.0,
                                    "documents": 0})

    totals = {"transfers": 0, "to_store": 0, "dispatch": 0,
              "qty_moved": 0.0, "in_transit": 0.0}
    for o in rows:
        qty = sent_of(o)
        kind = ("transfer" if o.to_warehouse_id
                else "store" if o.to_store_id else "dispatch")
        totals[{"transfer": "transfers", "store": "to_store",
                "dispatch": "dispatch"}[kind]] += 1
        if o.status == "draft":
            continue                       # nothing has moved yet
        totals["qty_moved"] += qty
        if o.from_warehouse_id:
            a = slot(o.from_warehouse_id)
            a["documents"] += 1
            if kind == "store":
                a["to_stores"] += qty
            else:
                a["sent"] += qty
        if kind == "transfer":
            b = slot(o.to_warehouse_id)
            if o.status == "received":
                b["received"] += accepted_of(o)
            else:
                # dispatched and not yet accepted — standing in neither building
                b["in_transit"] += qty
                totals["in_transit"] += qty

    for wid, a in per.items():
        wh = db.get(models.Warehouse, wid)
        a["name"] = wh.name if wh else f"#{wid}"
        for k in ("sent", "received", "in_transit", "to_stores"):
            a[k] = round(a[k], 3)
    totals["qty_moved"] = round(totals["qty_moved"], 3)
    totals["in_transit"] = round(totals["in_transit"], 3)
    return {"totals": totals,
            "warehouses": sorted(per.values(),
                                 key=lambda r: -(r["sent"] + r["received"]))}


def movement_flow(db: Session, since=None, until=None, warehouse_id=None) -> dict:
    """Quantity in and quantity out over a window, for the dashboard tiles.

    Inward is everything that added; outward is the absolute value of everything
    that removed. Reversals count on whichever side they landed — an unposted
    receipt genuinely did take stock back out of the building.
    """
    q = db.query(models.StockMovement)
    if warehouse_id:
        q = q.filter(models.StockMovement.warehouse_id == warehouse_id)
    if since:
        q = q.filter(models.StockMovement.created_at >= since)
    if until:
        q = q.filter(models.StockMovement.created_at <= until)
    inward = outward = 0.0
    for mv in q.all():
        d = float(mv.qty_delta or 0)
        if d > 0:
            inward += d
        else:
            outward += -d
    return {"inward": round(inward, 3), "outward": round(outward, 3)}
