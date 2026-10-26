"""Stock the warehouse sent here, taken into the branch it was sent to.

The warehouse dispatches goods to a place by name — Stock Outward, to "TAQUA
SILKS, TIRUPUR" — and posting that reduces the warehouse's own stock. Until now
nothing picked it up at the other end: the pieces left one system and arrived in
none. A shop's count was whatever it had when its items were first imported, plus
whatever somebody typed since.

So the transfers are read, and what was ACCEPTED at the far end becomes stock at
that branch. Accepted, not sent: the warehouse already records the difference as
a transfer discrepancy, and a shop that took in the sent figure would be holding
pieces that never came off the lorry.

WHAT MAKES THIS SAFE TO RUN AGAIN. Every applied line is written down by the
warehouse's own line id (models.TransferReceipt, unique), and a line already
written down is skipped. Without that, every restart would add the whole delivery
again — and a stock figure that grows when you restart the till is the kind of
bug that is believed for weeks, because nobody watches a number that only moves
upward when they are not looking.

TWO FIGURES, KEPT IN STEP. `Product.stock_qty` stays what the shop holds
altogether — it is what the till sells against and what every existing screen
reads. `LocationStock` is the split of it: what is at each branch. Both are moved
together here. They are not derived from each other, because a shop whose
branches were stocked before any of this existed has a total and no split, and
dividing one to invent the other would be making up numbers about real goods.

Read-only towards the warehouse, like everything else the shop reads of it.
"""
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from app import db
# At module level, not inside the functions: once the shop is mounted in the
# warehouse app, `app` names the WAREHOUSE's package at request time, and a
# function-level `from app import …` reaches into the wrong one. Store → Stock
# Inward calls into here per request, so this has to be bound now.
from app import warehouse_items as wh
from app.models import Location, LocationStock, Product, StockMovement, TransferReceipt

#: Statuses of a warehouse dispatch that mean the goods are in the branch.
#: Only `received`: somebody counted it in — on the store's own Stock Inward
#: (app/store_stock.py) or the warehouse's. A `posted` dispatch is on the road;
#: taking it in unseen is what the store's Stock Inward replaced, since the
#: count there is what the branch actually got.
ARRIVED = ("received",)


def at(location_id, product_id):
    """The LocationStock row for a place and a product, created at zero if new."""
    row = LocationStock.query.filter_by(location_id=location_id,
                                        product_id=product_id).first()
    if row is None:
        row = LocationStock(location_id=location_id, product_id=product_id, qty=0.0)
        db.session.add(row)
    return row


def move(location_id, product_id, change):
    """Move a branch's count, and return the row. Nothing else is touched."""
    if not location_id or not product_id or not change:
        return None
    row = at(location_id, product_id)
    row.qty = round((row.qty or 0.0) + float(change), 3)
    return row


def _pending_lines(con, known_ids):
    """Warehouse dispatch lines that have arrived and are not yet taken in."""
    # every column aliased, because rows come back as dicts keyed by name and
    # `l.id` would arrive as "id" beside the outward's own
    sql = (
        "SELECT l.id AS line_id, l.outward_id AS outward_id, "
        "       l.product_id AS product_id, l.qty AS sent, "
        "       l.accepted_qty AS accepted, o.code AS code, "
        "       o.to_destination AS dest, o.status AS status, "
        "       o.received_date AS received_date, o.date AS sent_date "
        "FROM stock_outward_lines l "
        "JOIN stock_outwards o ON o.id = l.outward_id "
        "WHERE o.status IN (" + ", ".join("'%s'" % s for s in ARRIVED) + ")"
    )
    # a store's own return to the warehouse is not stock arriving here
    try:
        rows = con.execute(sql + " AND o.from_store_id IS NULL").fetchall()
    except SQLAlchemyError:
        con.rollback()
        try:
            rows = con.execute(sql).fetchall()      # a warehouse from before returns
        except SQLAlchemyError:
            return []
    return [r for r in rows if int(r["line_id"]) not in known_ids]


def sync_transfers():
    """Take in every dispatch that has arrived and is not already in.

    Returns (lines, pieces) — how many dispatch lines were applied and how many
    pieces they brought — so a caller can report it or ignore it.
    """

    con = wh._connect()
    if con is None:
        return 0, 0

    known = {r[0] for r in db.session.query(TransferReceipt.wh_line_id).all()}
    try:
        pending = _pending_lines(con, known)
    finally:
        con.close()
    if not pending:
        return 0, 0

    places = {loc.name.strip().lower(): loc for loc in Location.query.all()}
    lines = pieces = 0
    for row in pending:
        lid, oid, wh_pid = row["line_id"], row["outward_id"], row["product_id"]
        sent, accepted, code = row["sent"], row["accepted"], row["code"]
        recv_date, sent_date = row["received_date"], row["sent_date"]
        place = places.get(" ".join(str(row["dest"] or "").split()).lower())
        if place is None:
            continue          # dispatched somewhere this shop does not know
        product = Product.query.filter_by(warehouse_id=wh_pid).first() if wh_pid else None
        if product is None:
            # The item has never been scanned here. Bring it in the same way a
            # scan would, so a delivery of something new is not silently dropped.
            product = _import(wh_pid)
            if product is None:
                continue
        qty = float(accepted if accepted is not None else sent or 0)
        if qty <= 0:
            continue

        move(place.id, product.id, qty)
        product.stock_qty = round(float(product.stock_qty or 0) + qty, 3)
        db.session.add(TransferReceipt(
            wh_line_id=int(lid), wh_outward_id=oid, code=code,
            location_id=place.id, product_id=product.id, qty=qty,
            received_on=(str(recv_date or sent_date or ""))[:10] or None,
            applied_at=datetime.utcnow()))
        db.session.flush()
        db.session.add(StockMovement(
            product_id=product.id, change=qty, reason="transfer-in",
            reference=f"{code or 'transfer'} → {place.name}"))
        lines += 1
        pieces += qty

    db.session.commit()
    return lines, round(pieces, 3)


def _import(wh_product_id):
    """Pull one warehouse item in by its id, or None if it cannot be read."""
    con = wh._connect()
    if con is None:
        return None
    try:
        rows = con.execute("SELECT * FROM products WHERE id = ?",
                           (int(wh_product_id),)).fetchall()
    except SQLAlchemyError:
        return None
    finally:
        con.close()
    if not rows:
        return None
    try:
        return wh.import_item(rows[0])
    except SQLAlchemyError:
        return None


def stock_by_location(product_id):
    """[(location name, qty)] for one product, biggest first, zeros dropped."""
    rows = (db.session.query(Location.name, LocationStock.qty)
            .join(LocationStock, LocationStock.location_id == Location.id)
            .filter(LocationStock.product_id == product_id,
                    LocationStock.qty != 0)
            .order_by(LocationStock.qty.desc()).all())
    return [(name, qty) for name, qty in rows]
