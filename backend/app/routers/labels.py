"""
Label Designer + Label Printing.

Two audiences, deliberately two screens, and the split is the whole point of the
module:

  * A manager DESIGNS a template — occasionally, carefully, once per label
    stock they buy. That is /templates below.
  * The warehouse PRINTS from it — daily, in a hurry, choosing stock and a
    quantity and nothing else. That is /print.

Neither endpoint stores a product value in a template or a layout on a product.
A template holds field REFERENCES; printing resolves them against the live
database at the moment the sheet is rendered. That is what makes one template
print the whole warehouse, and what makes a corrected price show up on the next
label without anyone re-opening the designer.

Printing goes through the same guards Inventory's own label endpoints do
(services/integrity): a label is a physical thing stuck on a garment, so a
sheet is refused for stock that no posted GRN created, and for a SKU whose piece
codes and stock figure disagree. A guard the client can skip is not a guard, so
it lives here rather than only in the UI.
"""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models
from ..services import barcode_svc
from ..services import label_designer as ld

router = APIRouter(prefix="/api/labels", tags=["labels"])


# ---------------------------------------------------------------------------
#  The palette
# ---------------------------------------------------------------------------
@router.get("/fields")
def fields():
    """What a label may show, and the fonts it may show it in — the designer's
    left-hand panel, served rather than duplicated in the UI so a field added to
    the catalogue appears there without a frontend change."""
    groups = []
    for f in ld.FIELDS:
        if f["group"] not in groups:
            groups.append(f["group"])
    return {"fields": ld.FIELDS, "groups": groups, "fonts": ld.FONTS,
            "sample": ld.SAMPLE, "aligns": list(ld.ALIGNS), "sizes": ld.SIZES,
            "min_mm": ld.MIN_MM, "max_mm": ld.MAX_MM}


# ---------------------------------------------------------------------------
#  Templates
# ---------------------------------------------------------------------------
class TemplateIn(BaseModel):
    name: str
    description: Optional[str] = ""
    width_mm: Optional[float] = 50.0
    height_mm: Optional[float] = 35.0
    padding_mm: Optional[float] = 2.0
    border: Optional[bool] = True
    target: Optional[str] = "product"       # product | unit
    font: Optional[str] = "Arial, Helvetica, sans-serif"
    elements: Optional[list] = None
    active: Optional[bool] = True
    is_default: Optional[bool] = False
    created_by: Optional[str] = None


def _get(db, tid) -> models.LabelTemplate:
    t = db.get(models.LabelTemplate, tid)
    if not t:
        raise HTTPException(404, "template not found")
    return t


def _apply(t, body: TemplateIn):
    t.name = (body.name or "").strip() or "Untitled label"
    t.description = body.description or ""
    t.width_mm = max(ld.MIN_MM, min(float(body.width_mm or 50), ld.MAX_MM))
    t.height_mm = max(ld.MIN_MM, min(float(body.height_mm or 35), ld.MAX_MM))
    t.padding_mm = max(0.0, min(float(body.padding_mm or 0), 20.0))
    t.border = bool(body.border)
    t.target = body.target if body.target in ("product", "unit") else "product"
    t.font = body.font or "Arial, Helvetica, sans-serif"
    t.active = bool(body.active)
    t.elements = ld.normalise_elements(body.elements, t.width_mm, t.height_mm)
    return t


def _make_default(db, t):
    """One default at a time. Clearing the others here rather than trusting the
    caller is what stops two templates both claiming to be the one the printing
    screen opens on."""
    db.query(models.LabelTemplate).filter(
        models.LabelTemplate.id != t.id).update({"is_default": False})
    t.is_default = True


@router.get("/templates")
def list_templates(include_inactive: bool = True, db: Session = Depends(get_db)):
    """Every template. Inactive ones are included by default and flagged, because
    this list IS the designer's own screen — hiding them there would leave a
    deactivated template with no way back."""
    ld.ensure_default(db)
    q = db.query(models.LabelTemplate)
    if not include_inactive:
        q = q.filter(models.LabelTemplate.active == True)  # noqa: E712
    rows = q.order_by(models.LabelTemplate.is_default.desc(),
                      models.LabelTemplate.name).all()
    return [ld.to_dict(t) for t in rows]


@router.get("/templates/{tid}")
def get_template(tid: int, db: Session = Depends(get_db)):
    return ld.to_dict(_get(db, tid))


@router.post("/templates")
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    t = models.LabelTemplate(created_by=body.created_by, created_at=dt.datetime.utcnow())
    _apply(t, body)
    db.add(t)
    db.flush()
    # the first template anyone creates is the default — there is nothing else
    # for the printing screen to open on
    if body.is_default or db.query(models.LabelTemplate).count() == 1:
        _make_default(db, t)
    db.commit()
    db.refresh(t)
    return ld.to_dict(t)


@router.put("/templates/{tid}")
def update_template(tid: int, body: TemplateIn, db: Session = Depends(get_db)):
    t = _get(db, tid)
    _apply(t, body)
    if body.is_default:
        _make_default(db, t)
    db.commit()
    db.refresh(t)
    return ld.to_dict(t)


@router.post("/templates/{tid}/duplicate")
def duplicate_template(tid: int, db: Session = Depends(get_db)):
    """Copy a template to work on. The copy is never the default and never
    inherits it — duplicating the label the warehouse is printing from must not
    change what the warehouse prints."""
    src = _get(db, tid)
    copy = models.LabelTemplate(
        name=f"{src.name} (copy)", description=src.description,
        width_mm=src.width_mm, height_mm=src.height_mm, padding_mm=src.padding_mm,
        border=src.border, target=src.target, font=src.font,
        elements=list(src.elements or []), is_default=False, active=True,
        created_by=src.created_by, created_at=dt.datetime.utcnow())
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return ld.to_dict(copy)


@router.post("/templates/{tid}/default")
def set_default(tid: int, db: Session = Depends(get_db)):
    t = _get(db, tid)
    if not t.active:
        raise HTTPException(400, "activate this template before making it the default — "
                                 "the printing screen would open on a template nobody may use")
    _make_default(db, t)
    db.commit()
    return {"ok": True, "default_id": t.id}


class ActiveIn(BaseModel):
    active: bool


@router.post("/templates/{tid}/active")
def set_active(tid: int, body: ActiveIn, db: Session = Depends(get_db)):
    """Deactivate a template rather than delete it: labels printed from it are on
    garments in the building, and the design that produced them is worth keeping
    readable. The default cannot be deactivated — that would leave the printing
    screen pointing at nothing."""
    t = _get(db, tid)
    if not body.active and t.is_default:
        raise HTTPException(400, "this is the default template — make another one the "
                                 "default first, then deactivate this one")
    t.active = bool(body.active)
    db.commit()
    return {"ok": True, "active": t.active}


@router.delete("/templates/{tid}")
def delete_template(tid: int, db: Session = Depends(get_db)):
    t = _get(db, tid)
    if t.is_default:
        raise HTTPException(400, "this is the default template — make another one the "
                                 "default first, then delete this one")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Preview — the design against real data
# ---------------------------------------------------------------------------
def _product(db, pid) -> models.Product:
    p = db.get(models.Product, pid)
    if not p:
        raise HTTPException(404, "product not found")
    return p


@router.get("/preview-values")
def preview_values(product_id: int = 0, unit_id: int = 0, db: Session = Depends(get_db)):
    """The values one label would carry, plus the symbols already rendered.

    The canvas draws these itself rather than embedding a rendered sheet, so
    dragging a field is instant and what moves under the cursor is the real
    string — a 40-character supplier description overflowing its box is
    something the designer must see while designing, not discover on a sticker.

    With no product it answers with the sample record, which is what the canvas
    shows before anyone picks one."""
    if unit_id:
        u = db.get(models.ProductUnit, unit_id)
        if not u:
            raise HTTPException(404, "piece not found")
        p, unit = u.product, u
    elif product_id:
        p, unit = _product(db, product_id), None
    else:
        vals = dict(ld.SAMPLE)
        return {"source": "sample", "values": vals,
                "qr_svg": barcode_svc.qr_svg(vals["qr_code"], scale=3),
                "barcode_svg": barcode_svc.barcode_svg(vals["barcode"]),
                "qr_payload_bytes": len(vals["qr_code"].encode())}
    receipts = ld.receipt_index(db, [p.id])
    vals = ld.context(p, unit, receipts.get(p.id))
    return {"source": "unit" if unit else "product",
            "product_id": p.id, "unit_id": unit.id if unit else None,
            "values": vals,
            "qr_svg": barcode_svc.qr_svg(vals["qr_code"], scale=3),
            "barcode_svg": barcode_svc.barcode_svg(vals["barcode"]),
            "qr_payload_bytes": len(vals["qr_code"].encode())}


@router.get("/qr-check")
def qr_check(box_mm: float, product_id: int = 0, db: Session = Depends(get_db)):
    """Is a QR drawn this big still scannable? See label_designer.qr_warning —
    the one property on the canvas whose mistake is invisible until the labels
    are on garments."""
    payload = None
    if product_id:
        payload = barcode_svc.qr_payload(_product(db, product_id))
    return {"box_mm": box_mm,
            "module_mm": barcode_svc.qr_module_size_mm(payload or ld.SAMPLE["qr_code"], box_mm),
            "warning": ld.qr_warning(box_mm, payload)}


@router.get("/templates/{tid}/preview", response_class=HTMLResponse)
def preview_sheet(tid: int, product_id: int = 0, copies: int = 1,
                  db: Session = Depends(get_db)):
    """The finished article, as the printer will produce it — one product, N
    copies. Unlike /print this asks no questions about stock: it is a proof of
    the DESIGN, so it renders whatever product is named (or the sample) without
    refusing anything."""
    t = _get(db, tid)
    if product_id:
        p = _product(db, product_id)
        receipts = ld.receipt_index(db, [p.id])
        vals = ld.context(p, None, receipts.get(p.id))
    else:
        vals = dict(ld.SAMPLE)
    n = max(1, min(int(copies or 1), 200))
    return HTMLResponse(ld.render_sheet(t, [vals] * n, title=f"Preview — {t.name}"))


# ---------------------------------------------------------------------------
#  Printing
# ---------------------------------------------------------------------------
def _parse_items(items: str):
    """"12:8,15:5" -> [(12, 8), (15, 5)]. A bare id means one label."""
    out = []
    for chunk in (items or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        pid, _, qty = chunk.partition(":")
        if not pid.strip().isdigit():
            continue
        n = int(qty) if qty.strip().isdigit() else 1
        out.append((int(pid), max(1, min(n, 2000))))
    return out


@router.get("/print", response_class=HTMLResponse)
def print_labels(template_id: int = 0, items: str = "", units: str = "",
                 unit_products: str = "", db: Session = Depends(get_db)):
    """The sheet the warehouse prints.

      items=12:8,15:5   → 8 labels for product 12 and 5 for product 15
      units=101,102     → those individual pieces, each carrying its own code
      unit_products=12  → every live piece of product 12

    `template_id` is optional: without one the default template is used, which
    is what the phone and any bookmarked URL should get.

    Every route through here is guarded the same way Inventory's own label
    endpoints are — see the module docstring. Opening a piece-label sheet counts
    as printing those pieces, which is what lets the Inventory screen offer
    Reprint afterwards rather than pretending nothing was printed.
    """
    from ..services import integrity
    from ..services import units as unit_svc

    ld.ensure_default(db)
    t = _get(db, template_id) if template_id else db.query(
        models.LabelTemplate).filter(models.LabelTemplate.is_default == True).first()  # noqa: E712
    if not t:
        raise HTTPException(400, "no label template — create one in Label Designer first")

    # Provenance for the products on this sheet only — every check below asks
    # about those and no others, and the whole catalogue's took ~6 s to read
    # for a sheet of a few labels. (Purchase statuses are read in full either
    # way; everything else a Context holds is per product.)
    unit_ids = [int(x) for x in units.split(",") if x.strip().isdigit()]
    scope_ids = {pid for (pid,) in db.query(models.ProductUnit.product_id).filter(
        models.ProductUnit.id.in_(unit_ids))} if unit_ids else set()
    scope_ids |= {int(x) for x in unit_products.split(",") if x.strip().isdigit()}
    scope_ids |= {pid for pid, _n in _parse_items(items)}
    ctx = integrity.Context(db, product_ids=sorted(scope_ids))
    rows = []            # [(product, unit_or_None, copies)]

    if units.strip() or unit_products.strip():
        picked = []
        if units.strip():
            want = [int(x) for x in units.split(",") if x.strip().isdigit()]
            found = db.query(models.ProductUnit).filter(
                models.ProductUnit.id.in_(want)).all()
            found.sort(key=lambda u: want.index(u.id))
            dead = [u.code for u in found if ctx.unit_state(u) != integrity.POSTED]
            if dead:
                raise HTTPException(400, f"{len(dead)} of these piece codes belong to no "
                                         f"posted GRN ({', '.join(dead[:4])}"
                                         f"{'…' if len(dead) > 4 else ''}) — run Inventory "
                                         f"Repair before printing")
            picked += found
        for pid in [int(x) for x in unit_products.split(",") if x.strip().isdigit()]:
            p = _product(db, pid)
            ok, why = integrity.can_print(db, p, ctx)
            if not ok:
                raise HTTPException(400, why)
            picked += [u for u in db.query(models.ProductUnit).filter(
                models.ProductUnit.product_id == pid).order_by(
                models.ProductUnit.seq).all()
                if ctx.unit_state(u) == integrity.POSTED]
        rows = [(u.product, u, 1) for u in picked if u.product]
        if picked:
            unit_svc.mark_printed(db, picked)
            db.commit()
    else:
        for pid, copies in _parse_items(items):
            p = _product(db, pid)
            ok, why = integrity.can_print(db, p, ctx)
            if not ok:
                raise HTTPException(400, f"{p.sku or p.description}: {why}")
            if not p.sku:
                barcode_svc.assign_identifiers(db, p)
                db.commit()
            rows.append((p, None, copies))

    if not rows:
        raise HTTPException(400, "nothing selected to print — pass items=, units= or "
                                 "unit_products=")

    receipts = ld.receipt_index(db, [p.id for p, _u, _n in rows])
    values = []
    for p, unit, copies in rows:
        values += [ld.context(p, unit, receipts.get(p.id))] * copies
    return HTMLResponse(ld.render_sheet(t, values))
