"""Price changer — set what things sell for, one item or in bulk.

The rules are in services/pricing.py; nothing is decided here. In particular
`/preview` and `/apply` call the same function with the same arguments, so what
somebody is shown is what happens — a preview computed a second way is a preview
that can lie.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..models import PRICE_FIELDS, PRICE_OPERATIONS
from ..services import pricing as svc

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


class ChangeIn(BaseModel):
    #: mrp | sale_price | sale_discount_pct
    field: str
    #: set | percent | amount | discount_off_mrp
    operation: str
    value: float
    #: Round the result to the nearest this many rupees. 0/None leaves it exact.
    round_to: Optional[float] = None
    #: How the products were picked: by attribute, by text, or by hand. The
    #: three are combined — a list of ids wins, because picking by hand is the
    #: most specific thing somebody can do.
    filters: Optional[dict] = None
    text: Optional[str] = ""
    product_ids: Optional[List[int]] = None
    note: Optional[str] = None


def _who(request: Request):
    """Whoever is signed in, for the record.

    `request.state.user` is a DICT — see security.auth_middleware, which sets
    {"username", "role", …}. Reading it with getattr silently returned nothing
    and every price change was filed against a dash, which is the one thing a
    price change must not be: nobody's.
    """
    user = getattr(request.state, "user", None) or {}
    if isinstance(user, dict):
        return user.get("username") or user.get("name") or "—"
    return getattr(user, "username", None) or "—"


@router.get("/options")
def options(db: Session = Depends(get_db)):
    """What the screen's pickers offer, built from the stock itself.

    From the catalogue rather than a fixed list, so a filter can only ever be
    offered when there is something behind it — and so a brand added yesterday
    is aimable at today.
    """
    def values(column):
        return [v for (v,) in db.query(column).filter(
            column.isnot(None), column != "").distinct().order_by(column).all()]

    return {
        "fields": [{"key": "sale_price", "label": "Selling price"},
                   {"key": "mrp", "label": "MRP"},
                   {"key": "sale_discount_pct", "label": "Discount off MRP (%)"}],
        "operations": [
            {"key": "set", "label": "Set to"},
            {"key": "percent", "label": "Change by %"},
            {"key": "amount", "label": "Change by ₹"},
            {"key": "discount_off_mrp", "label": "Set to MRP less %"}],
        "category": values(models.Product.category),
        "category_section": values(models.Product.category_section),
        "brand": values(models.Product.brand),
        "color": values(models.Product.color),
        "size": values(models.Product.size),
        "material": values(models.Product.material),
        "product_type": values(models.Product.product_type),
        "suppliers": [{"id": s.id, "name": s.name} for s in
                      db.query(models.Supplier).order_by(models.Supplier.name).all()],
    }


@router.get("/products")
def products(category: Optional[str] = None, brand: Optional[str] = None,
             category_section: Optional[str] = None,
             supplier_id: Optional[int] = None,
             color: Optional[str] = None, size: Optional[str] = None,
             material: Optional[str] = None, q: str = "",
             limit: int = 300, db: Session = Depends(get_db)):
    """The products a selection covers, with every price on them."""
    filters = {"category": category, "brand": brand,
               "category_section": category_section, "supplier_id": supplier_id,
               "color": color, "size": size,
               "material": material}
    try:
        rows = svc.find(db, filters, q, limit=limit)
    except svc.PricingError as exc:
        raise HTTPException(400, str(exc))
    total = svc.count(db, filters, q)
    return {"total": total, "shown": len(rows),
            "products": [{"id": p.id, "sku": p.sku, "description": p.description,
                          "category": p.category, "brand": p.brand,
                          "size": p.size, "color": p.color,
                          "material": p.material, "design_no": p.design_no,
                          "uom": p.uom, "stock_qty": p.stock_qty,
                          "avg_cost": p.avg_cost, "mrp": p.mrp,
                          "sale_price": p.sale_price,
                          "sale_discount_pct": p.sale_discount_pct}
                         for p in rows]}


@router.post("/preview")
def preview(body: ChangeIn, db: Session = Depends(get_db)):
    """What the change would do. Writes nothing."""
    _check(body)
    try:
        return svc.preview(db, body.field, body.operation, body.value,
                           filters=body.filters, text=body.text or "",
                           ids=body.product_ids, round_to=body.round_to,
                           limit=500)
    except svc.PricingError as exc:
        raise HTTPException(400, str(exc))


@router.post("/apply")
def apply(body: ChangeIn, request: Request, db: Session = Depends(get_db)):
    """Do it, and write down that it was done."""
    _check(body)
    try:
        rev = svc.apply(db, body.field, body.operation, body.value,
                        filters=body.filters, text=body.text or "",
                        ids=body.product_ids, round_to=body.round_to,
                        note=body.note, user=_who(request))
    except svc.PricingError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return svc.revision_out(rev)


def _check(body: ChangeIn):
    if body.field not in PRICE_FIELDS:
        raise HTTPException(400, f"“{body.field}” is not a price this can change. "
                                 f"Cost comes from the GRNs and is not editable.")
    if body.operation not in PRICE_OPERATIONS:
        raise HTTPException(400, f"“{body.operation}” is not something this can do.")
    if body.operation == "set" and body.value < 0:
        raise HTTPException(400, "A price cannot be negative.")


@router.get("/revisions")
def revisions(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(models.PriceRevision)
            .order_by(models.PriceRevision.id.desc()).limit(limit).all())
    return {"revisions": [svc.revision_out(r) for r in rows]}


@router.get("/revisions/{rid}")
def revision(rid: int, db: Session = Depends(get_db)):
    rev = db.get(models.PriceRevision, rid)
    if not rev:
        raise HTTPException(404, "no such price revision")
    return svc.revision_out(rev, with_changes=True)


@router.post("/revisions/{rid}/revert")
def revert(rid: int, request: Request, db: Session = Depends(get_db)):
    rev = db.get(models.PriceRevision, rid)
    if not rev:
        raise HTTPException(404, "no such price revision")
    try:
        svc.revert(db, rev, user=_who(request))
    except svc.PricingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    return svc.revision_out(rev)


@router.get("/products/{pid}/history")
def history(pid: int, db: Session = Depends(get_db)):
    """Every price this one item has been given. Answers “why is this 400?”."""
    if not db.get(models.Product, pid):
        raise HTTPException(404, "product not found")
    return {"history": svc.history_for(db, pid)}
