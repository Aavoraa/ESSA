"""Purchase Orders — the buying desk's register.

The order is the first document in the chain and, until now, the one the system
did not have. See services/purchase_orders for the lifecycle and why editing
stops at `confirmed`.

Shaped like the LR router next door on purpose: a list narrowed by the warehouse
the call is made inside, a manual create, a PATCH that refuses once the document
is committed, and status moves as their own routes rather than a writable
`status` field. A status that can be PATCHed is a status whose rules can be
skipped by anyone who sends the right JSON.
"""
import hashlib
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import COMPANY_NAME
from ..database import get_db
from ..services import (dates, po_extract, purchase_orders as po_svc, scope,
                        storage)

router = APIRouter(prefix="/api/purchase-orders", tags=["purchase-orders"])


class POLineIn(BaseModel):
    """One row of the order grid. Everything optional — a line is often written
    before its rate is agreed, and refusing to hold it until then is how the
    working copy ends up on paper beside the screen."""
    particulars: Optional[str] = None
    size: Optional[str] = None
    qty: Optional[float] = None
    uom: Optional[str] = None
    rate: Optional[float] = None
    amount: Optional[float] = None
    brand: Optional[str] = None
    design_no: Optional[str] = None
    hsn: Optional[str] = None
    notes: Optional[str] = None


class POIn(BaseModel):
    """A new order, or an amendment to one still open.

    `lines` absent means "leave the grid alone"; `lines: []` means "clear it".
    The two are different intentions and a screen sending a partial form must be
    able to express the first without performing the second.
    """
    po_no: Optional[str] = None
    po_date: Optional[str] = None
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    company: Optional[str] = None
    brand: Optional[str] = None
    item: Optional[str] = None
    place: Optional[str] = None
    transport: Optional[str] = None
    agent: Optional[str] = None
    purchaser: Optional[str] = None
    discount_pct: Optional[float] = None
    notes: Optional[str] = None
    entry_source: Optional[str] = None
    document_id: Optional[int] = None
    lines: Optional[List[POLineIn]] = None


class StatusIn(BaseModel):
    status: str
    by: Optional[str] = None
    reason: Optional[str] = None


def _payload(body: POIn) -> Dict[str, Any]:
    """The body as a plain dict, with unset keys genuinely absent.

    `exclude_unset` is the load-bearing part: without it every optional field
    arrives as None and a PATCH of one box wipes the rest of the form.
    """
    data = body.model_dump(exclude_unset=True)
    if data.get("lines") is not None:
        data["lines"] = [dict(l) for l in data["lines"]]
    return data


# ---------------------------------------------------------------------------
#  reading
# ---------------------------------------------------------------------------
@router.get("")
def list_orders(status: str = "all", q: str = "", supplier: str = "",
                date_from: str = "", date_to: str = "", limit: int = 500,
                db: Session = Depends(get_db),
                wid: Optional[int] = Depends(scope.current)):
    """The order book, newest first, for the warehouse this call is inside.

    Dates compare as strings because every date here is stored ISO, where lexical
    order is chronological order — the same reason the LR search does it.
    """
    query = db.query(models.PurchaseOrder)
    if status and status != "all":
        if status not in po_svc.STATUSES:
            raise HTTPException(400, f"'{status}' is not a purchase order status")
        query = query.filter(models.PurchaseOrder.status == status)
    if supplier:
        query = query.filter(models.PurchaseOrder.supplier_name.ilike(f"%{supplier}%"))
    if dates.to_iso(date_from):
        query = query.filter(models.PurchaseOrder.po_date >= dates.to_iso(date_from))
    if dates.to_iso(date_to):
        query = query.filter(models.PurchaseOrder.po_date <= dates.to_iso(date_to))
    if q:
        like = f"%{q}%"
        query = query.filter(
            models.PurchaseOrder.po_no.ilike(like)
            | models.PurchaseOrder.supplier_name.ilike(like)
            | models.PurchaseOrder.item.ilike(like)
            | models.PurchaseOrder.brand.ilike(like)
            | models.PurchaseOrder.purchaser.ilike(like))
    rows = (scope.purchase_orders(query, wid)
            .order_by(models.PurchaseOrder.id.desc())
            .limit(max(1, min(limit, 2000))).all())
    # How many consignments cite each order, in ONE query rather than one per
    # row. The list needs it so the Cancel button can be offered only where it
    # would actually work — a button that is drawn and then refused teaches
    # people to expect errors, which is exactly what the per-status actions were
    # written to avoid. Passing `db` to `out()` per row would have answered the
    # same question with N queries.
    ids = [r.id for r in rows]
    linked = {}
    if ids:
        linked = dict(
            db.query(models.LREntry.purchase_order_id,
                     func.count(models.LREntry.id))
              .filter(models.LREntry.purchase_order_id.in_(ids))
              .group_by(models.LREntry.purchase_order_id).all())
    out = []
    for r in rows:
        d = po_svc.out(r, with_lines=False)
        d["linked_lr_count"] = linked.get(r.id, 0)
        out.append(d)
    return out


@router.get("/open")
def list_open(supplier_name: str = "", db: Session = Depends(get_db),
              wid: Optional[int] = Depends(scope.current)):
    """Confirmed orders — what the LR Entry form's PO picker offers.

    Its own route rather than `?status=confirmed`, because "which orders may I
    book goods in against" is a question with one right answer and the transport
    desk should not be able to get a different one by changing a filter.
    """
    rows = po_svc.open_orders(db, warehouse_id=wid, supplier_name=supplier_name)
    return [po_svc.out(r, with_lines=False) for r in rows]


@router.get("/statuses")
def statuses():
    """The lifecycle, for the filter chips and the status control."""
    return {"statuses": po_svc.STATUSES,
            "transitions": {k: sorted(v) for k, v in po_svc.TRANSITIONS.items()},
            "editable": sorted(po_svc.EDITABLE)}


@router.get("/extract/status")
def extract_status():
    """Whether a photographed order can be read at all.

    Asked before the button is offered, so an unconfigured deployment says so on
    a settings page instead of failing after somebody has taken the photograph —
    the same courtesy /api/voice/status pays the Tamil microphone.
    """
    return {"available": po_extract.available()}


@router.post("/extract")
async def extract_order(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Photograph or upload an order → read it → hand back a DRAFT for review.

    NOTHING IS SAVED HERE, and that is the design rather than an omission. The
    reply is a draft the form fills itself from; the person who photographed the
    page corrects it and presses Save, which is the ordinary
    `POST /api/purchase-orders` every hand-keyed order goes through. An OCR pass
    that wrote straight to the register would be a system that files its own
    mistakes, and no amount of accuracy makes that the right shape.

    The page itself IS stored — as a `Document` of type `purchase_order`, the
    third the app reads after invoices and LR registers — so the order can always
    be checked against the paper it came off. `document_id` comes back and the
    save carries it, which is what pins the two together.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "that file is empty")
    h = hashlib.sha256(raw).hexdigest()
    ext = os.path.splitext(file.filename or "")[1] or ".bin"
    stored = storage.save(raw, f"{h[:16]}{ext}")
    doc = models.Document(filename=file.filename, stored_path=stored, content_hash=h,
                          mime=file.content_type, status="uploaded",
                          document_type="purchase_order")
    db.add(doc)
    db.commit()
    db.refresh(doc)

    res = po_extract.extract_po(storage.materialise(stored) or stored)
    return {"document_id": doc.id, "entry_source": "import", **res}


#: The header boxes a supplier pick can fill, in the order they are looked for.
_SUPPLIER_FILLS = ("company", "brand", "item", "place", "transport", "agent",
                   "purchaser", "discount_pct")


def _first(values):
    """The first value that says something — None, '' and whitespace do not."""
    for v in values:
        if v is not None and str(v).strip():
            return v
    return None


def _place_from(address: Optional[str], state: Optional[str]) -> Optional[str]:
    """The town off a supplier's address: its last part that is a word rather
    than a pincode or the state. 'No 4, Market St, Tirupur - 641604, Tamil Nadu'
    reads as Tirupur."""
    if not address:
        return None
    st = (state or "").strip().lower()
    for part in reversed(address.replace("\n", ",").split(",")):
        word = "".join(c for c in part if not c.isdigit()).strip(" -.")
        if word and word.lower() != st and not word.lower().startswith(("pin", "india")):
            return word
    return None


@router.get("/supplier-defaults")
def supplier_defaults(name: str = "", db: Session = Depends(get_db),
                      wid: Optional[int] = Depends(scope.current)):
    """What the order form can fill in the moment a supplier is picked.

    Read from what this supplier's own paperwork already says, newest first:
    their last purchase orders, then the consignments booked in from them (LR
    entries carry the transporter, agent and purchase manager), then the
    supplier master (the town off its address) and the products they supply
    (the brand). Nothing is invented — a box nothing on record speaks for is
    left out, and the form leaves it as it is.
    """
    name = (name or "").strip()
    if not name:
        return {"fills": {}, "source": {}}
    PO, LR = models.PurchaseOrder, models.LREntry
    sup = (db.query(models.Supplier).filter(func.lower(models.Supplier.name) == name.lower())
           .first())
    po_q = db.query(PO).filter(func.lower(PO.supplier_name) == name.lower())
    if sup:
        po_q = db.query(PO).filter((PO.supplier_id == sup.id)
                                   | (func.lower(PO.supplier_name) == name.lower()))
    pos = (scope.purchase_orders(po_q, wid)
           .filter(PO.status != "cancelled").order_by(PO.id.desc()).limit(20).all())
    lrs = (db.query(LR).filter(func.lower(LR.supplier_name) == name.lower())
           .order_by(LR.id.desc()).limit(20).all())

    fills, source = {}, {}

    def take(key, value, where):
        if key not in fills and value is not None and str(value).strip():
            fills[key] = value
            source[key] = where

    for k in _SUPPLIER_FILLS:
        take(k, _first(getattr(p, k) for p in pos), "last order")
    take("transport", _first(l.transport for l in lrs), "last consignment")
    take("agent", _first(l.agent for l in lrs), "last consignment")
    take("purchaser", _first(l.purchase_manager for l in lrs), "last consignment")
    take("item", _first(l.item for l in lrs), "last consignment")
    if sup:
        take("place", _place_from(sup.address, sup.state), "supplier master")
        brand = (db.query(models.Product.brand)
                 .filter(models.Product.primary_supplier_id == sup.id,
                         models.Product.brand.isnot(None), models.Product.brand != "")
                 .group_by(models.Product.brand)
                 .order_by(func.count(models.Product.id).desc()).first())
        if brand:
            take("brand", brand[0], "supplier's products")
        PL, P = models.PurchaseLine, models.Purchase
        billed = (db.query(PL.brand).join(P, P.id == PL.purchase_id)
                  .filter(P.supplier_id == sup.id, PL.brand.isnot(None), PL.brand != "")
                  .group_by(PL.brand).order_by(func.max(P.id).desc()).first())
        if billed:
            take("brand", billed[0], "supplier's invoices")
    take("company", COMPANY_NAME, "company default")
    return {"supplier_id": sup.id if sup else None, "fills": fills, "source": source}


@router.get("/{po_id}")
def get_order(po_id: int, db: Session = Depends(get_db)):
    po = db.get(models.PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "purchase order not found")
    return po_svc.out(po, db)


# ---------------------------------------------------------------------------
#  writing
# ---------------------------------------------------------------------------
@router.post("")
def create_order(body: POIn, db: Session = Depends(get_db),
                 wid: Optional[int] = Depends(scope.current)):
    """Raise an order. Starts as a draft — confirming it is a separate act."""
    po = po_svc.create(db, _payload(body), warehouse_id=wid)

    # A supplier typed on an order is a supplier this company buys from, so it
    # joins the master exactly as one typed on an LR entry does. Without this the
    # first order to a new vendor leaves their name on the document and nowhere
    # else, and the invoice that follows cannot be matched to them.
    #
    # Matched case-INSENSITIVELY, which an exact comparison would not do. The
    # dictation path makes this concrete: voicefill hands back what was heard in
    # lower case, so "supplier matoshree" spoken into the form would otherwise
    # file a second Matoshree beside the one already there — and two spellings of
    # one vendor is exactly what a supplier master exists to prevent. The name is
    # stored as it was given; only the LOOKUP ignores case.
    name = (po.supplier_name or "").strip()
    if name:
        known = db.query(models.Supplier).filter(
            func.lower(models.Supplier.name) == name.lower()).first()
        if not known:
            db.add(models.Supplier(name=name))

    db.commit()
    db.refresh(po)
    return po_svc.out(po, db)


@router.patch("/{po_id}")
def update_order(po_id: int, body: POIn, db: Session = Depends(get_db)):
    po = db.get(models.PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "purchase order not found")
    try:
        po_svc.update(db, po, _payload(body))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(po)
    return po_svc.out(po, db)


@router.post("/{po_id}/status")
def move_status(po_id: int, body: StatusIn, db: Session = Depends(get_db)):
    """Confirm, send, or cancel an order.

    One route for every move rather than a `/confirm` and a `/cancel`, so the
    transition table in the service is the only place the rules live.
    """
    po = db.get(models.PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "purchase order not found")
    try:
        po_svc.set_status(db, po, body.status, by=body.by, reason=body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(po)
    return po_svc.out(po, db)


@router.delete("/{po_id}")
def delete_order(po_id: int, db: Session = Depends(get_db)):
    """Remove an order outright.

    A draft, or an order already cancelled. A pending or confirmed order is a
    document the supplier also holds, and the honest way to withdraw it is
    `cancelled` first — which records that it was called off — instead of
    leaving the other side holding a document this system has no record of.
    Once cancelled, clearing it off the register is the business's call.

    Never an order a consignment cites, whatever its state: the transport row
    would be left pointing at nothing. Cancelling already refuses such an order,
    so this only guards against a link made some other way.
    """
    po = db.get(models.PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404, "purchase order not found")
    if po.status not in po_svc.DELETABLE:
        raise HTTPException(
            400, f"a {po.status} order cannot be deleted — cancel it first, so "
                 f"it is on record that it was called off")
    linked = db.query(models.LREntry).filter(
        models.LREntry.purchase_order_id == po.id).count()
    if linked:
        raise HTTPException(
            400, f"{linked} transport entr{'y' if linked == 1 else 'ies'} "
                 f"reference this order — it cannot be deleted")
    db.delete(po)
    db.commit()
    return {"ok": True, "deleted": po_id}
