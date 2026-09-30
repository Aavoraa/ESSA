"""
Supplier Payments service (Finance / Supplier Payment) — accounts payable.

A posted purchase is a payable of its grand_total. A Payment settles one or more
purchases; each allocation can reduce its invoice by cash + discount + TDS +
debit-note adjustment (the four variants in the recordings). Outstanding for an
invoice = grand_total − Σ(settled across all allocations).
"""
import datetime as dt
from sqlalchemy import func, select
from .. import models
from ..request_memo import memo
from . import dates


def _today():
    # Date.now()-free: use the max invoice date seen, fallback fixed — but for
    # "days outstanding" we accept the DB clock via created_at of a probe.
    return dt.datetime.utcnow().date()


def _parse_date(s):
    """One parser for the whole system — see services/dates.py.

    It used to know three formats; anything else (a two-digit year, "31 Jul 2026"
    off an e-invoice) fell through to None and the bill silently lost its days
    outstanding, which is the kind of gap that shows up as an ageing report that
    almost adds up."""
    return dates.parse(s)


def invoice_settled(db, purchase_id):
    allocs = db.query(models.PaymentAllocation).filter(
        models.PaymentAllocation.purchase_id == purchase_id).all()
    return round(sum(a.settled or 0 for a in allocs), 2)


def invoice_returns(db, purchase_id):
    """Posted purchase-return (debit note) value against this invoice — reduces
    what we owe."""
    rows = db.query(models.PurchaseReturn).filter(
        models.PurchaseReturn.purchase_id == purchase_id,
        models.PurchaseReturn.status == "posted").all()
    return round(sum(r.total or 0 for r in rows), 2)


def invoice_outstanding(db, purchase):
    return round((purchase.grand_total or 0)
                 - invoice_settled(db, purchase.id)
                 - invoice_returns(db, purchase.id), 2)


def pending_bills(db, supplier_id):
    # Read once per request (request_memo — GET only, never beyond the request):
    # the warehouse dashboard's payables tile and its notification feed's overdue
    # rule both ask, in the same call.
    # plain dicts, so shared by every session of the request (see dead_stock)
    return memo(("pay.pending_bills", supplier_id),
                lambda: _pending_bills(db, supplier_id))


def pending_bills_by_supplier(db):
    """{supplier_id: pending_bills(db, supplier_id)} for every supplier at once —
    the Supplier Pending Bills report asked each of thousands of suppliers in
    turn, re-reading every settlement for each."""
    out = {}
    for b in _pending_bills(db, None, with_supplier=True):
        out.setdefault(b.pop("supplier_id"), []).append(b)
    return out


def _pending_bills(db, supplier_id, with_supplier=False):
    # Plain rows of the same statement the ORM query issued (every column, same
    # filter, same ORDER BY — so the same rows in the same order, ties included):
    # building an ORM object per posted GRN was half this function's time.
    P = models.Purchase
    q = select(*P.__table__.columns).where(P.status == "posted")
    if supplier_id:
        q = q.where(P.supplier_id == supplier_id)
    # The same figures invoice_settled / invoice_returns give, read for every
    # invoice in two grouped queries instead of two queries per invoice — with
    # tens of thousands of GRNs the per-invoice form took over a minute.
    settled = dict(db.query(models.PaymentAllocation.purchase_id,
                            func.sum(models.PaymentAllocation.settled))
                     .filter(models.PaymentAllocation.purchase_id.isnot(None))
                     .group_by(models.PaymentAllocation.purchase_id).all())
    returns = dict(db.query(models.PurchaseReturn.purchase_id,
                            func.sum(models.PurchaseReturn.total))
                     .filter(models.PurchaseReturn.purchase_id.isnot(None),
                             models.PurchaseReturn.status == "posted")
                     .group_by(models.PurchaseReturn.purchase_id).all())
    out = []
    today = _today()
    for p in db.execute(q.order_by(P.invoice_date)):
        s = round(float(settled.get(p.id) or 0), 2)
        r = round(float(returns.get(p.id) or 0), 2)
        outstanding = round((p.grand_total or 0) - s - r, 2)
        if outstanding <= 0.01:
            continue
        d = _parse_date(p.invoice_date)
        days = (today - d).days if d else None
        out.append({
            "purchase_id": p.id, "invoice_number": p.invoice_number,
            "invoice_date": p.invoice_date, "days": days,
            "grand_total": p.grand_total, "settled": s,
            "returns": r, "outstanding": outstanding,
        })
        if with_supplier:
            out[-1]["supplier_id"] = p.supplier_id
    return out


def _next_receipt(db):
    n = db.query(models.Payment).count() + 1
    return f"ESP{n:05d}"


def create_payment(db, payload):
    """payload: {supplier_id, date, mode, bank, cheque_no, cheque_date, ref_no,
                 remarks, allocations:[{purchase_id, cash, discount, tds, debit_adjust}]}"""
    allocs = payload.get("allocations", [])
    pay = models.Payment(
        receipt_no=_next_receipt(db), supplier_id=payload.get("supplier_id"),
        # ISO on the way in, so the register sorts and the ledger dates compare
        date=dates.normalise(payload.get("date")), mode=payload.get("mode", "NEFT"),
        bank=payload.get("bank"), cheque_no=payload.get("cheque_no"),
        cheque_date=dates.normalise(payload.get("cheque_date")),
        ref_no=payload.get("ref_no"), remarks=payload.get("remarks"),
    )
    db.add(pay)
    db.flush()

    gross = disc = tds = debit = paid = 0.0
    for a in allocs:
        p = db.get(models.Purchase, a.get("purchase_id"))
        if not p:
            continue
        cash = float(a.get("cash") or 0)
        d = float(a.get("discount") or 0)
        t = float(a.get("tds") or 0)
        dn = float(a.get("debit_adjust") or 0)
        settled = round(cash + d + t + dn, 2)
        db.add(models.PaymentAllocation(
            payment_id=pay.id, purchase_id=p.id, invoice_number=p.invoice_number,
            invoice_total=p.grand_total, discount=d, tds=t, debit_adjust=dn,
            settled=settled,
        ))
        gross += p.grand_total or 0
        disc += d; tds += t; debit += dn; paid += cash

    pay.gross_amount = round(gross, 2)
    pay.discount_total = round(disc, 2)
    pay.tds_total = round(tds, 2)
    pay.debit_adjust_total = round(debit, 2)
    pay.paid_amount = round(paid, 2)
    db.flush()
    return pay


def supplier_ledger(db, supplier_id):
    """Chronological debits (purchases) and credits (payments) with balance."""
    rows = []
    for p in db.query(models.Purchase).filter(
            models.Purchase.supplier_id == supplier_id,
            models.Purchase.status == "posted").all():
        rows.append({"date": p.invoice_date, "type": "purchase",
                     "ref": p.invoice_number, "debit": p.grand_total or 0,
                     "credit": 0.0})
    for r in db.query(models.PurchaseReturn).filter(
            models.PurchaseReturn.supplier_id == supplier_id,
            models.PurchaseReturn.status == "posted").all():
        rows.append({"date": r.date, "type": "purchase_return", "ref": r.code,
                     "debit": 0.0, "credit": r.total or 0,
                     "detail": f"debit note vs {r.invoice_number or ''}".strip()})
    for pay in db.query(models.Payment).filter(
            models.Payment.supplier_id == supplier_id).all():
        credit = (pay.paid_amount or 0) + (pay.discount_total or 0) + \
                 (pay.tds_total or 0) + (pay.debit_adjust_total or 0)
        rows.append({"date": pay.date, "type": "payment", "ref": pay.receipt_no,
                     "debit": 0.0, "credit": round(credit, 2),
                     "detail": f"{pay.mode} · cash {pay.paid_amount:.0f}"
                               + (f" · disc {pay.discount_total:.0f}" if pay.discount_total else "")
                               + (f" · TDS {pay.tds_total:.0f}" if pay.tds_total else "")
                               + (f" · debit {pay.debit_adjust_total:.0f}" if pay.debit_adjust_total else "")})
    rows.sort(key=lambda r: (_parse_date(r["date"]) or _today()))
    bal = 0.0
    for r in rows:
        bal += r["debit"] - r["credit"]
        r["balance"] = round(bal, 2)
    return rows


def supplier_outstanding(db, supplier_id):
    return round(sum(b["outstanding"] for b in pending_bills(db, supplier_id)), 2)
