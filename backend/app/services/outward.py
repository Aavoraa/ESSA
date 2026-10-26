"""
Stock Outward / Stock Inward service — the two ends of one transfer.

build:    create a draft outward, resolving each scanned code / product to an
          inventory product (so the screen can show the full record — QR, name,
          size, colour, batch — before anything is packed).
post:     append one negative StockMovement per line (kind='outward') and reduce
          each product's stock. Guards against dispatching more than on hand.
          Idempotent — an outward posts once.
receive:  the STOCK INWARD side. The destination counts what turned up and
          accepts it, line by line, against the same document. Accepted qty
          defaults to the sent qty; anything less is a transfer discrepancy,
          recorded rather than silently absorbed.

Mirrors the app's pair of screens (Warehouse / Stock Outward → Store / Stock
Inward, which prints a Goods Transfer note): From company/location, Packed By, a
To destination, Received By, and a sent vs accepted qty per line.
"""
import datetime as dt
from .. import models
from . import dates
from . import barcode_svc
from . import stock_locations as stock_loc


def _next_code(db, warehouse_id=None):
    from . import numbering
    return numbering.next_number(
        db, "transfer", warehouse_id=warehouse_id,
        is_taken=lambda code: db.query(models.StockOutward).filter(
            models.StockOutward.code == code).first() is not None)


# ---------------------------------------------------------------------------
#  the two ends
# ---------------------------------------------------------------------------
def resolve_endpoints(db, payload, existing=None):
    """Work out where this dispatch leaves from and where it goes.

    Returns (from_warehouse_id, to_warehouse_id, to_store_id, to_destination) or
    raises ValueError. The destination NAME is always produced, whichever kind of
    place was chosen, because the till matches branches by name and every row
    raised before these columns existed has only the string — see
    models.StockOutward.

    A destination that is neither a warehouse nor a store of ours is kept as free
    text, exactly as it always was. Not every dispatch goes to a place this
    company owns, and refusing to send goods to a customer would be a strange way
    to gain warehouses.
    """
    from_id = payload.get("from_warehouse_id",
                          getattr(existing, "from_warehouse_id", None))
    from_id = stock_loc.resolve_warehouse_id(db, from_id)

    to_wid = payload.get("to_warehouse_id", getattr(existing, "to_warehouse_id", None))
    to_sid = payload.get("to_store_id", getattr(existing, "to_store_id", None))
    name = payload.get("to_destination", getattr(existing, "to_destination", None))

    if to_wid:
        wh = db.get(models.Warehouse, int(to_wid))
        if not wh:
            raise ValueError("that destination warehouse doesn't exist")
        # Sending a building's stock to itself is a document that moves nothing
        # and reconciles to nothing. Caught here rather than at post, so it is
        # refused while the person is still looking at the form they got wrong.
        if wh.id == from_id:
            raise ValueError(f"“{wh.name}” is both ends of this transfer — "
                             f"pick a different destination")
        return from_id, wh.id, None, wh.name

    if to_sid:
        st = db.get(models.Store, int(to_sid))
        if not st:
            raise ValueError("that destination store doesn't exist")
        return from_id, None, st.id, st.name

    # Nothing chosen from the registry: match a typed name to a place we know, so
    # a branch selected the old way still lands as a real destination rather than
    # as a string that looks like one.
    typed = (name or "").strip()
    if typed:
        st = db.query(models.Store).filter(models.Store.name == typed).first()
        if st:
            return from_id, None, st.id, st.name
        wh = db.query(models.Warehouse).filter(models.Warehouse.name == typed).first()
        if wh and wh.id != from_id:
            return from_id, wh.id, None, wh.name
    return from_id, None, None, typed or None


def resolve_product(db, barcode=None, product_id=None, description=None):
    """Find the product a dispatch line refers to.

    `barcode` is whatever was scanned or typed — a product QR payload, a
    per-piece garment label, our SKU, or the supplier's printed code. All of them
    go through barcode_svc.resolve, so the picker can scan the tag on the item
    itself instead of looking up a code by hand."""
    if product_id:
        return db.get(models.Product, product_id)
    if barcode:
        p = barcode_svc.resolve(db, barcode)
        if p:
            return p
    if description:
        return db.query(models.Product).filter(models.Product.description == description).first()
    return None


def create_outward(db, payload):
    """payload: {date, from_warehouse_id, to_warehouse_id | to_store_id |
                 to_destination, packed_by, received_by, from_location,
                 lines:[{barcode|product_id, qty, accepted_qty}]}

    Raises ValueError when the two ends don't make sense — see resolve_endpoints.
    """
    from_id, to_wid, to_sid, to_name = resolve_endpoints(db, payload)
    o = models.StockOutward(
        code=_next_code(db, from_id), date=dates.normalise(payload.get("date")),
        from_warehouse_id=from_id, to_destination=to_name,
        to_warehouse_id=to_wid, to_store_id=to_sid,
        packed_by=payload.get("packed_by"), received_by=payload.get("received_by"),
        from_location=payload.get("from_location", "WAREHOUSE"),
        status="draft",
    )
    db.add(o)
    db.flush()
    for ln in payload.get("lines", []):
        prod = resolve_product(db, ln.get("barcode"), ln.get("product_id"), ln.get("description"))
        if not prod:
            continue
        qty = float(ln.get("qty") or 0)
        db.add(models.StockOutwardLine(
            outward_id=o.id, product_id=prod.id, barcode=prod.barcode,
            description=prod.description, qty=qty,
            accepted_qty=ln.get("accepted_qty"),
            # Valued at what it costs IN THE BUILDING IT IS LEAVING. The
            # destination receives it at this rate, so a transfer carries the
            # source's cost with the goods instead of restating them at a company
            # average neither warehouse paid.
            rate=stock_loc.cost_at(db, prod.id, from_id) or prod.last_rate or 0,
        ))
    db.flush()
    return o


def validate_stock(db, outward):
    """Lines that would take the SOURCE WAREHOUSE negative if this were posted.

    Checked at the source rather than against the company total, which is the
    whole point of the split: twenty shirts in Karur are not twenty shirts
    available to dispatch from Erode, and a company-level check would let Erode
    send goods that are standing four hundred kilometres away."""
    problems = []
    from_id = stock_loc.resolve_warehouse_id(db, outward.from_warehouse_id)
    src = db.get(models.Warehouse, from_id)
    for l in outward.lines:
        prod = db.get(models.Product, l.product_id)
        if not prod:
            continue
        on_hand = stock_loc.qty_at(db, prod.id, from_id)
        if (l.qty or 0) > on_hand:
            from . import stock_view
            problems.append({"product": stock_view.display_name(prod),
                             "requested": l.qty, "on_hand": on_hand,
                             "warehouse": src.name if src else None,
                             "elsewhere": round(float(prod.stock_qty or 0) - on_hand, 3)})
    return problems


def post_outward(db, outward, allow_negative=False):
    """Dispatch: the goods leave the source warehouse.

    They do NOT arrive anywhere yet. A warehouse-to-warehouse transfer lands when
    the far end counts it in (`receive_outward`), so stock is visibly in transit
    in between rather than existing in two buildings at once or in neither."""
    if outward.status != "draft":
        return {"ok": False, "error": "already posted"}
    outward.from_warehouse_id = stock_loc.resolve_warehouse_id(
        db, outward.from_warehouse_id)
    problems = validate_stock(db, outward)
    if problems and not allow_negative:
        return {"ok": False, "error": "insufficient_stock", "problems": problems}

    for l in outward.lines:
        prod = db.get(models.Product, l.product_id)
        if not prod:
            continue
        stock_loc.apply(db, prod, outward.from_warehouse_id, -float(l.qty or 0),
                        kind="outward", ref_type="outward", ref_id=outward.id,
                        rate=l.rate or prod.avg_cost or 0,
                        note=f"Outward {outward.code} → "
                             f"{outward.to_destination or ''}".strip())
    outward.status = "posted"
    outward.posted_at = dt.datetime.utcnow()
    db.flush()
    return {"ok": True, "outward_id": outward.id, "lines": len(outward.lines),
            "total_qty": outward.total_qty,
            "from_warehouse_id": outward.from_warehouse_id,
            "to_warehouse_id": outward.to_warehouse_id,
            "is_transfer": outward.is_transfer}


# ---------------------------------------------------------------------------
#  Stock Inward — the destination accepting a dispatch
# ---------------------------------------------------------------------------
def receive_outward(db, outward, accepted=None, received_by=None, date=None):
    """Record what the destination actually took in.

    `accepted`: {line_id: qty}. A line left out is accepted in full — the common
    case is that the whole box is right, and making someone re-key every line to
    say so invites the opposite error.

    Accepting fewer than were sent is recorded, NOT corrected: the stock has
    already left this warehouse, and where the missing pieces are (in transit,
    damaged, miscounted at one end) is a question for a human. The shortfall is
    reported so it can be settled deliberately — with a stock adjustment if the
    goods come back, or written off if they don't.

    WHEN THE DESTINATION IS A WAREHOUSE OF OURS, this is also the moment the
    goods ARRIVE: one inward movement per line, for the quantity ACCEPTED, at the
    rate the source valued them at. The pieces that did not turn up are therefore
    nowhere — which is the truthful position, and exactly what the shortfall
    figure is for.

    When the destination is a SHOP, nothing arrives here. The till's own database
    owns a store's stock and builds it from these dispatches (see pos_mount);
    receiving it into this ledger as well would put one garment on two systems'
    shelves."""
    if outward.status == "draft":
        return {"ok": False, "error": "this outward hasn't been dispatched yet — "
                                      "post it before receiving it"}
    if outward.status == "received":
        return {"ok": False, "error": "already received"}

    accepted = {int(k): v for k, v in (accepted or {}).items()}
    for l in outward.lines:
        sent = float(l.qty or 0)
        if l.id in accepted:
            raw = accepted[l.id]
            try:
                q = float(raw if raw not in (None, "") else 0)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"“{l.description}”: accepted quantity "
                                              f"must be a number"}
            if q < 0:
                return {"ok": False,
                        "error": f"“{l.description}”: accepted quantity can't be negative"}
            if q > sent:
                return {"ok": False,
                        "error": f"“{l.description}”: {q:g} accepted but only {sent:g} "
                                 f"were sent — a transfer can't grow in transit"}
            l.accepted_qty = q
        else:
            l.accepted_qty = sent

    outward.received_by = received_by or outward.received_by
    outward.received_date = dates.normalise(date) if date else outward.received_date
    outward.received_at = dt.datetime.utcnow()
    outward.status = "received"
    db.flush()

    landed = 0
    if outward.to_warehouse_id:
        for l in outward.lines:
            prod = db.get(models.Product, l.product_id)
            if not prod:
                continue
            mv = stock_loc.apply(
                db, prod, outward.to_warehouse_id, float(l.accepted_qty or 0),
                kind="inward", ref_type="transfer_in", ref_id=outward.id,
                rate=l.rate or prod.avg_cost or 0,
                note=(f"Store return {outward.code} from {outward.from_location or ''}".strip()
                      if outward.from_store_id else
                      f"Transfer {outward.code} from "
                      f"{outward.from_warehouse.name if outward.from_warehouse else ''}".strip()))
            if mv is not None:
                landed += 1
        db.flush()

    short = [{"line_id": l.id, "product_id": l.product_id,
              "description": l.description, "sent": l.qty,
              "accepted": l.accepted_qty, "short": l.short_qty}
             for l in outward.lines if l.short_qty > 0]
    return {"ok": True, "outward_id": outward.id, "lines": len(outward.lines),
            "total_qty": outward.total_qty, "accepted_qty": outward.total_accepted,
            "shortfall": outward.shortfall, "discrepancies": short,
            "is_transfer": outward.is_transfer,
            "to_warehouse_id": outward.to_warehouse_id,
            "lines_landed": landed}


def verify_code(db, outward, code):
    """Resolve a code scanned while checking a transfer in or out.

    Answers the question the person holding the garment is actually asking — "is
    this one of the items on this note?" — rather than just "what is this?". A
    scan that resolves to a product NOT on the document is the error worth
    catching, so it comes back as matched=False with the product still named."""
    product = barcode_svc.resolve(db, code)
    if not product:
        return {"ok": False, "error": f"nothing matches the code “{code}”"}
    line = next((l for l in outward.lines if l.product_id == product.id), None)
    return {"ok": True, "matched": line is not None,
            "line_id": line.id if line else None, "product_id": product.id}
