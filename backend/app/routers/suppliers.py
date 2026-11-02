from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..services import scope

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


@returns_json(router.get(""))
def list_suppliers(db: Session = Depends(get_db)):
    # Profiles loaded for every supplier in one IN query, and documents COUNTED in
    # one grouped query — the list only shows how many, and loading each
    # supplier's documents to measure len() was two queries per supplier.
    doc_counts = dict(db.query(models.Document.supplier_id, func.count(models.Document.id))
                        .filter(models.Document.supplier_id.isnot(None))
                        .group_by(models.Document.supplier_id).all())
    out = []
    for s in (db.query(models.Supplier).options(selectinload(models.Supplier.profiles))
                .order_by(models.Supplier.name).all()):
        p = s.active_profile
        out.append({
            "id": s.id, "name": s.name, "gstin": s.gstin, "state": s.state,
            "has_profile": p is not None,
            "profile_version": p.version if p else 0,
            "profile_samples": p.sample_count if p else 0,
            "tax_mode": p.tax_mode if p else None,
            "has_tds": p.has_tds if p else False,
            "document_count": int(doc_counts.get(s.id, 0)),
        })
    return out


def _business(db: Session, s):
    """What this supplier has done with us, for the supplier's page: invoices
    and what they came to, what is still owed, what has been paid and returned,
    how many of our products they supply, and the latest invoices. Every figure
    is read straight off its own table — the same ones the GRN list, Payments
    and Returns screens show."""
    from ..services import payments as pay
    from ..services import dates as date_svc
    P, R, Pay = models.Purchase, models.PurchaseReturn, models.Payment
    grns = (db.query(P.id, P.grn_no, P.invoice_number, P.invoice_date, P.grand_total, P.status)
              .filter(P.supplier_id == s.id).order_by(P.id.desc()).all())
    posted = [g for g in grns if g.status == "posted"]
    dated = sorted(d for d in (date_svc.to_iso(g.invoice_date) for g in grns) if d)
    bills = pay.pending_bills(db, s.id)
    pays = db.query(func.count(Pay.id), func.coalesce(func.sum(Pay.paid_amount), 0)) \
             .filter(Pay.supplier_id == s.id).one()
    rets = db.query(func.count(R.id), func.coalesce(func.sum(R.total), 0)) \
             .filter(R.supplier_id == s.id, R.status == "posted").one()
    products = db.query(func.count(models.Product.id)) \
                 .filter(models.Product.primary_supplier_id == s.id).scalar()
    return {
        "invoices": len(grns), "posted": len(posted),
        "purchased": round(sum(float(g.grand_total or 0) for g in posted), 2),
        "first_invoice": dated[0] if dated else None,
        "last_invoice": dated[-1] if dated else None,
        "pending_bills": len(bills),
        "outstanding": round(sum(b["outstanding"] for b in bills), 2),
        "payments": int(pays[0] or 0), "paid": round(float(pays[1] or 0), 2),
        "returns": int(rets[0] or 0), "returned": round(float(rets[1] or 0), 2),
        "products": int(products or 0),
        "recent": [{"id": g.id, "grn_no": g.grn_no, "invoice_number": g.invoice_number,
                    "invoice_date": g.invoice_date, "grand_total": g.grand_total,
                    "status": g.status} for g in grns[:10]],
    }


#: Invoice header fields that belong to the supplier's way of billing rather than
#: to one bill — the agent, the transporter, the destination — so the last bill
#: from them is a fair guess for the next. Numbers, dates, IRN and LR numbers are
#: per bill and never carried over.
_INVOICE_HABITS = ("terms", "agent", "broker", "transporter", "destination", "book_city")


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip()) or v == {}


def _merge(into: dict, src: dict):
    """Fill what `into` lacks from `src`, one level deep into dicts (the bank)."""
    for k, v in (src or {}).items():
        if isinstance(v, dict):
            sub = into.setdefault(k, {}) if isinstance(into.get(k), dict) or k not in into else None
            if sub is not None:
                _merge(sub, v)
        elif _blank(into.get(k)) and not _blank(v):
            into[k] = v


@router.get("/autofill")
def autofill(name: str = "", db: Session = Depends(get_db),
             wid: Optional[int] = Depends(scope.current)):
    """Everything on record about a supplier, shaped the way the invoice keeps it,
    for a form to fill itself from the moment the supplier is picked.

    The supplier master first, then the newest readings of their own invoices for
    what the master does not hold (legal name, CIN, manufacturer, bank). The buyer
    is who those invoices were billed to, else this warehouse's business. Only
    what is on record comes back — a form leaves anything missing as it is.
    """
    name = (name or "").strip()
    if not name:
        return {"found": False}
    low = name.lower()
    s = db.query(models.Supplier).filter(func.lower(models.Supplier.name) == low).first()
    if not s:
        for cand in db.query(models.Supplier).filter(models.Supplier.aliases.isnot(None)).all():
            if any(str(a).strip().lower() == low for a in (cand.aliases or [])):
                s = cand
                break
    if not s:
        return {"found": False}
    supplier = {k: getattr(s, k) for k in
                ("name", "gstin", "pan", "state", "state_code", "address", "phone", "email")}
    supplier = {k: v for k, v in supplier.items() if not _blank(v)}
    if s.bank:
        supplier["bank"] = {k: v for k, v in s.bank.items() if not _blank(v)}
    buyer, invoice = {}, {}
    docs = (db.query(models.Document).filter(models.Document.supplier_id == s.id)
            .order_by(models.Document.id.desc()).limit(10).all())
    for d in docs:
        x = d.latest_extraction
        data = (x.data if x else None) or {}
        sup = dict(data.get("supplier") or {})
        sup.pop("name", None)                  # the name picked is the name kept
        _merge(supplier, sup)
        _merge(buyer, data.get("buyer") or {})
        inv = data.get("invoice") or {}
        _merge(invoice, {k: inv.get(k) for k in _INVOICE_HABITS})
    if not buyer:
        from ..services import businesses
        b = businesses.for_warehouse(db, wid) or businesses.default_business(db)
        if b:
            buyer = {k: v for k, v in {
                "name": b.legal_name or b.name, "gstin": b.gstin,
                "state": getattr(b, "state", None),
                "address": ", ".join(p for p in (b.address, getattr(b, "city", None)) if p) or None,
            }.items() if not _blank(v)}
    return {"found": True, "id": s.id, "supplier": supplier, "buyer": buyer,
            "invoice": {k: v for k, v in invoice.items() if not _blank(v)},
            "lr": _lr_habits(db, s, name, invoice)}


#: What the LR Entry form can carry from one consignment of a supplier's to the
#: next. Docket numbers, dates, counts and freight are per lorry and never are.
_LR_HABITS = ("lr_mode", "agent", "agent_commission", "transport", "auto_transfer_location",
              "purchase_manager", "stock_holding_days", "additional_margin", "paid_topay", "item")


def _lr_habits(db: Session, s, name: str, invoice: dict) -> dict:
    """The supplier's last consignments first, then their last purchase order
    (agent, transport, purchaser, item), then their invoices (agent, transporter)."""
    LR, PO = models.LREntry, models.PurchaseOrder
    low = name.lower()
    out = {}
    for row in (db.query(LR).filter(func.lower(LR.supplier_name).in_({low, s.name.lower()}))
                .order_by(LR.id.desc()).limit(20).all()):
        _merge(out, {k: getattr(row, k, None) for k in _LR_HABITS})
    po = (db.query(PO).filter((PO.supplier_id == s.id) | (func.lower(PO.supplier_name) == low))
          .filter(PO.status != "cancelled").order_by(PO.id.desc()).first())
    if po:
        _merge(out, {"agent": po.agent, "transport": po.transport,
                     "purchase_manager": po.purchaser, "item": po.item})
    _merge(out, {"agent": invoice.get("agent"), "transport": invoice.get("transporter")})
    return {k: v for k, v in out.items() if not _blank(v)}


@router.get("/{supplier_id}")
def get_supplier(supplier_id: int, db: Session = Depends(get_db)):
    s = db.get(models.Supplier, supplier_id)
    if not s:
        raise HTTPException(404, "supplier not found")
    p = s.active_profile
    return {
        "id": s.id, "name": s.name, "gstin": s.gstin, "pan": s.pan,
        "state": s.state, "state_code": s.state_code, "address": s.address,
        "phone": s.phone, "email": s.email,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "bank": s.bank, "aliases": s.aliases,
        "business": _business(db, s),
        "profile": ({
            "version": p.version, "template_key": p.template_key,
            "tax_mode": p.tax_mode, "default_tax_rates": p.default_tax_rates,
            "has_tds": p.has_tds, "uom_default": p.uom_default,
            "sample_count": p.sample_count,
            "detect_gstin": p.detect_gstin, "detect_keywords": p.detect_keywords,
        } if p else None),
        "documents": [{"id": d.id, "filename": d.filename, "status": d.status}
                      for d in s.documents],
    }
