from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session, joinedload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..services import payments as svc

router = APIRouter(prefix="/api/payments", tags=["payments"])


class AllocationIn(BaseModel):
    purchase_id: int
    cash: float = 0.0
    discount: float = 0.0
    tds: float = 0.0
    debit_adjust: float = 0.0


class PaymentIn(BaseModel):
    supplier_id: int
    date: Optional[str] = None
    mode: str = "NEFT"
    bank: Optional[str] = None
    cheque_no: Optional[str] = None
    cheque_date: Optional[str] = None
    ref_no: Optional[str] = None
    remarks: Optional[str] = None
    allocations: List[AllocationIn] = []


def _pay_out(p, with_allocs=False):
    d = {"id": p.id, "receipt_no": p.receipt_no, "supplier_id": p.supplier_id,
         "supplier_name": p.supplier.name if p.supplier else None, "date": p.date,
         "mode": p.mode, "ref_no": p.ref_no, "gross_amount": p.gross_amount,
         "discount_total": p.discount_total, "tds_total": p.tds_total,
         "debit_adjust_total": p.debit_adjust_total, "paid_amount": p.paid_amount,
         "remarks": p.remarks,
         "created_at": p.created_at.isoformat() if p.created_at else None}
    if with_allocs:
        d["allocations"] = [{"purchase_id": a.purchase_id, "invoice_number": a.invoice_number,
                             "invoice_total": a.invoice_total, "discount": a.discount,
                             "tds": a.tds, "debit_adjust": a.debit_adjust,
                             "settled": a.settled} for a in p.allocations]
    return d


@returns_json(router.get("/pending"))
def pending(supplier_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Unpaid invoices (optionally for one supplier) — the 'Search Pendings'."""
    return svc.pending_bills(db, supplier_id)


@returns_json(router.get(""))
def list_payments(db: Session = Depends(get_db)):
    return [_pay_out(p) for p in db.query(models.Payment)
            .options(joinedload(models.Payment.supplier))
            .order_by(models.Payment.id.desc()).all()]


@router.post("")
def create_payment(body: PaymentIn, db: Session = Depends(get_db)):
    if not body.allocations:
        raise HTTPException(400, "no invoices selected")
    pay = svc.create_payment(db, body.model_dump())
    db.commit(); db.refresh(pay)
    return _pay_out(pay, with_allocs=True)


@router.get("/supplier/{supplier_id}/ledger")
def ledger(supplier_id: int, db: Session = Depends(get_db)):
    return {"outstanding": svc.supplier_outstanding(db, supplier_id),
            "rows": svc.supplier_ledger(db, supplier_id)}


@router.get("/{pid}")
def get_payment(pid: int, db: Session = Depends(get_db)):
    p = db.get(models.Payment, pid)
    if not p:
        raise HTTPException(404, "payment not found")
    return _pay_out(p, with_allocs=True)
