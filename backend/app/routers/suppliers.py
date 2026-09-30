from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from ..database import get_db
from ..fastjson import returns_json
from .. import models

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
