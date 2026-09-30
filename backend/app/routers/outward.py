from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..services import outward as svc
from ..services import stock_view
from ..services import scope

router = APIRouter(prefix="/api/outward", tags=["stock-outward"])


class OutwardLineIn(BaseModel):
    product_id: Optional[int] = None
    barcode: Optional[str] = None
    qty: float
    accepted_qty: Optional[float] = None


class OutwardIn(BaseModel):
    date: Optional[str] = None
    # Where it leaves from, and where it goes. `to_warehouse_id` makes this a
    # warehouse-to-warehouse transfer that ARRIVES when it is received;
    # `to_store_id` dispatches to a shop, whose own database owns it from there.
    # `to_destination` alone still works and is matched to a known place by name.
    from_warehouse_id: Optional[int] = None
    to_warehouse_id: Optional[int] = None
    to_store_id: Optional[int] = None
    to_destination: Optional[str] = None
    packed_by: Optional[str] = None
    received_by: Optional[str] = None
    from_location: Optional[str] = "WAREHOUSE"
    lines: List[OutwardLineIn] = []


class ReceiveIn(BaseModel):
    """The Stock Inward side: who took the goods in, and how many of each line
    they accepted. Lines left out are accepted in full."""
    received_by: Optional[str] = None
    date: Optional[str] = None
    accepted: Optional[Dict[int, float]] = None      # outward_line_id -> qty


def _line_out(l, db: Session):
    prod = l.product
    # What is on hand AT THE SOURCE — the only figure that answers "can I send
    # this". The company total is shown beside it so a picker who is short can
    # see the goods exist somewhere rather than concluding they are out of stock.
    from ..services import stock_locations as stock_loc
    src = l.outward.from_warehouse_id if l.outward else None
    here = stock_loc.qty_at(db, prod.id, src) if (prod and src) else None
    return {
        "id": l.id, "product_id": l.product_id, "barcode": l.barcode,
        "description": l.description, "qty": l.qty,
        "accepted_qty": l.accepted_qty, "short_qty": l.short_qty,
        "rate": l.rate,
        "stock_on_hand": here if here is not None else (prod.stock_qty if prod else None),
        "stock_company_wide": prod.stock_qty if prod else None,
        "stock_by_warehouse": stock_loc.balances_for(db, prod.id) if prod else [],
        "value": round(float(l.qty or 0) * float(l.rate or 0), 2),
        # The whole product record — QR, name, size, colour, batch and the rest.
        # A dispatch or an acceptance is someone matching a row against a garment
        # in their hand; a barcode and a description cannot settle that, because
        # four sizes of one style share both.
        "product": stock_view.product_card(db, prod) if prod else None,
    }


def _out(o, db: Session = None, with_lines=False, sums=None):
    """`sums`, when given, is (line_count, total_qty, accepted_qty) already
    totalled in SQL for this note — the list passes it so thousands of notes do
    not each load their lines just to be counted. Without it the model's own
    properties do the same arithmetic over `o.lines`."""
    if sums is not None:
        n, sent, acc = sums
        received = o.status == "received"
        total_qty = sent if n else 0
        accepted = (acc if n else 0) if received else 0.0
        shortfall = round(total_qty - accepted, 3) if received else 0.0
        line_count = n
    else:
        total_qty, accepted, shortfall = o.total_qty, o.total_accepted, o.shortfall
        line_count = len(o.lines)
    d = {"id": o.id, "code": o.code, "date": o.date, "to_destination": o.to_destination,
         "from_company": o.from_company, "from_location": o.from_location,
         "from_warehouse_id": o.from_warehouse_id,
         "from_warehouse": o.from_warehouse.name if o.from_warehouse else None,
         "to_warehouse_id": o.to_warehouse_id,
         "to_warehouse": o.to_warehouse.name if o.to_warehouse else None,
         "to_store_id": o.to_store_id,
         "to_store": o.to_store.name if o.to_store else None,
         # What KIND of movement this is, said once here rather than re-derived
         # on every screen that has to draw it differently.
         "kind": ("transfer" if o.to_warehouse_id else
                  "store" if o.to_store_id else "dispatch"),
         "is_transfer": o.is_transfer,
         "packed_by": o.packed_by, "received_by": o.received_by,
         "received_date": o.received_date, "status": o.status,
         "total_qty": total_qty, "accepted_qty": accepted,
         "shortfall": shortfall, "line_count": line_count,
         "created_at": o.created_at.isoformat() if o.created_at else None,
         "posted_at": o.posted_at.isoformat() if o.posted_at else None,
         "received_at": o.received_at.isoformat() if o.received_at else None}
    if with_lines:
        d["lines"] = [_line_out(l, db) for l in o.lines]
    return d


def _get(oid: int, db: Session):
    o = db.get(models.StockOutward, oid)
    if not o:
        raise HTTPException(404, "outward not found")
    return o


@returns_json(router.get(""))
def list_outwards(status: str = "all", kind: str = "all",
                  db: Session = Depends(get_db),
                  warehouse_id: Optional[int] = Depends(scope.current)):
    """`status` filters the list: draft | posted | received. 'posted' is what the
    Stock Inward screen wants — dispatched, not yet accepted anywhere.

    `kind` narrows it to transfer (warehouse → warehouse), store, or dispatch.
    `warehouse_id` returns the notes that concern one building — sent from it OR
    coming to it, because both are that warehouse's business and making the
    screen ask twice is how the inbound half gets forgotten."""
    q = db.query(models.StockOutward)
    if status and status != "all":
        q = q.filter(models.StockOutward.status == status)
    if kind == "transfer":
        q = q.filter(models.StockOutward.to_warehouse_id.isnot(None))
    elif kind == "store":
        q = q.filter(models.StockOutward.to_store_id.isnot(None))
    elif kind == "dispatch":
        q = q.filter(models.StockOutward.to_warehouse_id.is_(None),
                     models.StockOutward.to_store_id.is_(None))
    # Sent from here OR coming to here — both are this warehouse's business, and
    # filtering to the source alone is what makes an arriving transfer invisible
    # at the branch that has to count it in.
    q = scope.outwards(q, warehouse_id)
    O, L = models.StockOutward, models.StockOutwardLine
    notes = (q.options(joinedload(O.from_warehouse), joinedload(O.to_warehouse),
                       joinedload(O.to_store))
              .order_by(O.id.desc()).all())
    # every note's line totals in one grouped query, over the same filtered set
    sums = {oid: (int(n), float(sent or 0), float(acc or 0)) for oid, n, sent, acc in
            db.query(L.outward_id, func.count(L.id), func.sum(func.coalesce(L.qty, 0)),
                     func.sum(func.coalesce(L.accepted_qty, L.qty, 0)))
              .filter(L.outward_id.in_(q.with_entities(O.id).scalar_subquery()))
              .group_by(L.outward_id).all()}
    return [_out(o, sums=sums.get(o.id, (0, 0, 0))) for o in notes]


@router.post("")
def create_outward(body: OutwardIn, db: Session = Depends(get_db)):
    try:
        o = svc.create_outward(db, body.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.commit(); db.refresh(o)
    return _out(o, db, with_lines=True)


@router.get("/{oid}")
def get_outward(oid: int, db: Session = Depends(get_db)):
    return _out(_get(oid, db), db, with_lines=True)


@router.post("/{oid}/post")
def post_outward(oid: int, allow_negative: bool = False, db: Session = Depends(get_db)):
    o = _get(oid, db)
    res = svc.post_outward(db, o, allow_negative=allow_negative)
    if not res.get("ok"):
        db.rollback()
        raise HTTPException(400, res)
    db.commit()
    return res


@router.post("/{oid}/receive")
def receive_outward(oid: int, body: ReceiveIn, db: Session = Depends(get_db)):
    """Stock Inward — accept a dispatched transfer at the destination.

    Records the accepted quantity per line against the same document, so a short
    delivery is the difference between two columns rather than a second piece of
    paper nobody reconciles."""
    o = _get(oid, db)
    res = svc.receive_outward(db, o, accepted=body.accepted,
                              received_by=body.received_by, date=body.date)
    if not res.get("ok"):
        db.rollback()
        raise HTTPException(400, res.get("error"))
    db.commit()
    return res


@router.get("/{oid}/verify")
def verify_scan(oid: int, code: str, db: Session = Depends(get_db)):
    """Check a scanned garment against this transfer — is it on the note, and
    which line? Used by both ends: packing it out and counting it in."""
    o = _get(oid, db)
    res = svc.verify_code(db, o, code)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    product = db.get(models.Product, res["product_id"])
    res["product"] = stock_view.product_card(db, product)
    return res
