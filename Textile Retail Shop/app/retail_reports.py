"""The Sales / Retail Reports catalogue — every register, grouped as the menu is.

The list mirrors the reference ERP's report menu group by group, in its order and
under its names, so somebody who knows where a report lived there finds it in the
same place here. Each entry is a function returning the one shape every report in
this shop returns — {columns, rows, totals, note} — so one page draws all of them
and one button exports any of them.

WHAT THIS DOES NOT DO IS INVENT. A report the shop has no record for (the stock
marker) is still listed, so the menu is complete, but it is marked `unavailable`
with the reason — a table of zeros for a thing that is never recorded would read
as "nothing happened", which is a different and false answer.

These are kept apart from `reports_lib.REPORTS` on purpose. That registry is also
the ask bar's routing table, and seventy more keyword lists would change which
report a plain-English question lands on. Where a report here is the same
question one of those already answers, it calls it rather than copying it.
"""
from collections import OrderedDict
from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import selectinload

# Everything this module needs is imported HERE, never inside a function. Inside
# the warehouse the shop is mounted with its package name swapped out, and an
# `from app import …` run at request time would reach the warehouse's own `app`
# (test_mounted.py holds this).
from app import db, drawer, reports_lib, vouchers
from app.datefilter import day_from, day_to
from app import warehouse_items as wi
from app.models import (Attendance, Category, Company, Counter, Coupon,
                        CouponRedemption, CreditNote, CreditNoteItem, Customer, CustomerAdvance,
                        CustomerFeedback, Delivery, DrawerSession, Invoice,
                        InvoiceItem, InvoicePayment, Location, LocationStock,
                        LoyaltyTxn, Product, PromotionApplication, PromotionScheme,
                        SaleSession, ScheduledMessage, StaffAdvance, StockMovement,
                        TransferReceipt, User)

METHODS = ("cash", "card", "upi")


# ---------------------------------------------------------------------------
#  what a report is asked about
# ---------------------------------------------------------------------------
class Ctx:
    """The period, the place, and any report-specific knob, in one object.

    `b2b` narrows every bill query to customers registered for GST — the B2B
    vertical is the same reports asked of those bills only, not a second set.
    """

    def __init__(self, start, end, company=None, location=None, counter=None,
                 params=None, b2b=False):
        self.start, self.end = start, end
        self.company, self.location, self.counter = company, location, counter
        self.params = params or {}
        self.b2b = b2b

    def param(self, key, default, cast=int):
        try:
            return cast(self.params.get(key, default))
        except (TypeError, ValueError):
            return default

    def _place(self, q, model):
        if self.company:
            q = q.filter(model.company_id == self.company)
        if self.location:
            q = q.filter(model.location_id == self.location)
        if self.counter:
            q = q.filter(model.counter_id == self.counter)
        return q

    def bills(self, *options):
        q = Invoice.query.filter(Invoice.live(),
                                 day_from(Invoice.invoice_date, self.start),
                                 day_to(Invoice.invoice_date, self.end))
        q = self._place(q, Invoice)
        if self.b2b:
            q = q.filter(Invoice.customer_id.in_(_registered_ids()))
        if options:
            q = q.options(*options)
        return q.order_by(Invoice.invoice_date, Invoice.id)

    def lines(self):
        """(InvoiceItem, Invoice) for every line billed in the period and place."""
        q = (db.session.query(InvoiceItem, Invoice)
             .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
             .filter(Invoice.live(),
                     day_from(Invoice.invoice_date, self.start),
                     day_to(Invoice.invoice_date, self.end)))
        q = self._place(q, Invoice)
        if self.b2b:
            q = q.filter(Invoice.customer_id.in_(_registered_ids()))
        return q.order_by(Invoice.invoice_date, Invoice.id, InvoiceItem.id)

    def credit_notes(self):
        """Credit notes RAISED in the period, placed by the bill they reverse."""
        q = (CreditNote.query.join(Invoice, Invoice.id == CreditNote.invoice_id)
             .filter(day_from(CreditNote.created_at, self.start),
                     day_to(CreditNote.created_at, self.end)))
        q = self._place(q, Invoice)
        if self.b2b:
            q = q.filter(Invoice.customer_id.in_(_registered_ids()))
        return q.order_by(CreditNote.created_at, CreditNote.id)


def _registered_ids():
    return db.session.query(Customer.id).filter(Customer.gstin.isnot(None),
                                                func.trim(Customer.gstin) != "")


# ---------------------------------------------------------------------------
#  small shared pieces
# ---------------------------------------------------------------------------
def _m(v):
    return round(float(v or 0), 2)


def _q(v):
    """A quantity: 12, not 12.0 — and 12.5 kept when a half really is a half."""
    v = round(float(v or 0), 3)
    return int(v) if v.is_integer() else v


def _d(value):
    if not value:
        return "—"
    return value.strftime("%d-%m-%Y")


def _dt(value):
    return value.strftime("%d-%m-%Y %H:%M") if value else "—"


def _day(value):
    """A day key from whatever func.date / a datetime handed back."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _pct(part, whole):
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _name(user):
    return user.full_name if user else "—"


def _served(inv):
    """Who the sale belongs to: the staff member who served it, else the till login."""
    return inv.staff or inv.cashier


def _tax(inv):
    return _m((inv.cgst or 0) + (inv.sgst or 0) + (inv.igst or 0))


def _collected(inv):
    """What was actually taken against a bill.

    The settlement rows when there are any. Without them a bill is taken as paid
    in full under its one method — every bill raised before split tenders was —
    unless it was explicitly left pending.
    """
    if inv.payments:
        return _m(sum(p.amount or 0 for p in inv.payments))
    return 0.0 if (inv.payment_status or "") == "pending" else _m(inv.total)


def _tenders(inv):
    """{method: amount} — what the drawer, the card machine and UPI took."""
    if inv.payments:
        out = {}
        for p in inv.payments:
            out[p.method] = _m(out.get(p.method, 0) + (p.amount or 0))
        return out
    if (inv.payment_status or "") == "pending":
        return {}
    return {(inv.payment_method or "cash"): _m(inv.total)}


def _tender_cols(tenders, into):
    for k, v in tenders.items():
        key = k if k in METHODS else "other"
        into[key] = _m(into.get(key, 0) + v)
    return into


def _sum(rows, i):
    return _m(sum((r[i] or 0) for r in rows if isinstance(r[i], (int, float))))


def _qsum(rows, i):
    return _q(sum((r[i] or 0) for r in rows if isinstance(r[i], (int, float))))


def _arrivals():
    """{product_id: first day it reached the shop}.

    The earliest transfer taken in from the warehouse, else the day the product
    row was created — which for anything the shop added itself is the day it
    was stocked. Used by every report that asks how long goods have stood.
    """
    first = dict(db.session.query(TransferReceipt.product_id,
                                  func.min(TransferReceipt.applied_at))
                 .group_by(TransferReceipt.product_id).all())
    out = {}
    for pid, created in db.session.query(Product.id, Product.created_at).all():
        when = first.get(pid) or created
        out[pid] = _day(when) if when else None
    return out


AGE_BANDS = ((0, 30, "0–30 days"), (31, 60, "31–60 days"), (61, 90, "61–90 days"),
             (91, 180, "91–180 days"), (181, 10 ** 6, "Over 180 days"))


def _band(days):
    if days is None:
        return "Unknown"
    for lo, hi, label in AGE_BANDS:
        if lo <= days <= hi:
            return label
    return "Unknown"


def _result(columns, rows, totals=None, note="", **extra):
    out = {"columns": columns, "rows": rows, "totals": totals or {}, "note": note}
    out.update(extra)
    return out


def _from_lib(key, ctx, note_extra=""):
    """A report reports_lib already answers, run unchanged."""
    out = dict(reports_lib.run(key, ctx.start, ctx.end))
    if note_extra:
        out["note"] = (out.get("note", "") + " " + note_extra).strip()
    return out


TAX_NOTE = ("Taxable value is each line before the bill-level discount, which is "
            "how the till works out tax — the discount is taken off the bill after "
            "tax. Intra-state tax is split equally into CGST and SGST.")


# ===========================================================================
#  SALES REPORTS
# ===========================================================================
def sales_report(ctx):
    bills = ctx.bills(selectinload(Invoice.items), selectinload(Invoice.payments)).all()
    rows = []
    for inv in bills:
        rows.append([inv.invoice_number, _dt(inv.invoice_date),
                     inv.customer.name if inv.customer else "Walk-in",
                     _name(_served(inv)), _q(inv.total_qty), _m(inv.subtotal),
                     _m(inv.discount), _tax(inv), _m(inv.total),
                     (inv.payment_method or "").upper()])
    return _result(["Bill", "Date", "Customer", "Salesperson", "Qty", "Gross",
                    "Discount", "Tax", "Net", "Payment"], rows,
                   {"Bills": len(rows), "Qty": _qsum(rows, 4), "Gross": _sum(rows, 5),
                    "Discount": _sum(rows, 6), "Tax": _sum(rows, 7), "Net": _sum(rows, 8)},
                   "Every bill raised in the period. Returns are not deducted here — "
                   "see the Day Summary for sales net of returns.")


def unsettled_bills(ctx):
    rows = []
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        got = _collected(inv)
        balance = _m((inv.total or 0) - got)
        if balance > 0.009:
            rows.append([inv.invoice_number, _d(inv.invoice_date),
                         inv.customer.name if inv.customer else "Walk-in",
                         (inv.customer.phone if inv.customer else "") or "—",
                         _m(inv.total), got, balance,
                         (date.today() - inv.invoice_date.date()).days])
    return _result(["Bill", "Date", "Customer", "Phone", "Bill total", "Collected",
                    "Balance", "Days old"], rows,
                   {"Bills": len(rows), "Balance": _sum(rows, 6)},
                   "Bills whose recorded payments add up to less than the bill. The "
                   "counter settles a bill in full before it prints, so this is "
                   "normally empty — a row here is a bill worth looking at.")


def _by_person(ctx, who):
    agg = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.items)).all():
        person = who(inv)
        row = agg.setdefault(person.id if person else 0,
                             {"user": person, "bills": 0, "qty": 0.0, "sales": 0.0,
                              "disc": 0.0, "disc_bills": 0, "free": 0.0, "gross": 0.0})
        row["bills"] += 1
        row["qty"] += inv.total_qty
        row["sales"] += inv.total or 0
        row["gross"] += inv.subtotal or 0
        row["disc"] += inv.discount or 0
        row["disc_bills"] += 1 if (inv.discount or 0) > 0 else 0
        row["free"] += sum((i.promo_value or 0) for i in inv.items if i.promo_role == "reward")
    return agg


def sales_salesman(ctx):
    agg = _by_person(ctx, _served)
    back = {}
    for note in ctx.credit_notes().all():
        person = _served(note.invoice)
        key = person.id if person else 0
        back[key] = back.get(key, 0) + (note.total or 0)
    rows = []
    for key, r in agg.items():
        u = r["user"]
        net = _m(r["sales"] - back.get(key, 0))
        rows.append([_name(u), u.staff_code if u else "—", r["bills"], _q(r["qty"]),
                     _m(r["sales"]), _m(back.get(key, 0)), net,
                     _m(r["sales"] / r["bills"]) if r["bills"] else 0,
                     _m(net * ((u.commission_pct or 0) if u else 0) / 100.0)])
    rows.sort(key=lambda r: -r[6])
    return _result(["Salesperson", "Code", "Bills", "Qty", "Sales", "Returns", "Net sales",
                    "Avg bill", "Commission"], rows,
                   {"Sales": _sum(rows, 4), "Returns": _sum(rows, 5),
                    "Net sales": _sum(rows, 6), "Commission": _sum(rows, 8)},
                   "Credited to whoever served the sale (the till login when nobody was "
                   "named). Returns come off that same person, dated by the credit note.")


def sales_salesman_detail(ctx):
    rows = []
    for item, inv in ctx.lines().all():
        p = item.product
        rows.append([_name(_served(inv)), inv.invoice_number, _d(inv.invoice_date),
                     p.sku if p else "—", p.name if p else "—", _q(item.quantity),
                     _m(item.unit_price), _m((item.line_total or 0) + (item.tax_amount or 0))])
    rows.sort(key=lambda r: (r[0], r[1]))
    return _result(["Salesperson", "Bill", "Date", "SKU", "Item", "Qty", "Rate", "Amount"],
                   rows, {"Lines": len(rows), "Qty": _qsum(rows, 5), "Amount": _sum(rows, 7)},
                   "Every line each person sold, bill by bill. Amount includes tax.")


def sales_margin(ctx):
    agg = OrderedDict()
    for item, inv in ctx.lines().all():
        p = item.product
        if not p:
            continue
        r = agg.setdefault(p.id, [p.sku, p.name, p.category.name if p.category else "—",
                                  0.0, 0.0, 0.0])
        r[3] += item.quantity or 0
        r[4] += item.line_total or 0
        r[5] += (item.quantity or 0) * (p.cost_price or 0)
    rows = []
    for sku, name, cat, qty, sales, cost in agg.values():
        margin = _m(sales - cost)
        rows.append([sku, name, cat, _q(qty), _m(sales), _m(cost), margin, _pct(margin, sales)])
    rows.sort(key=lambda r: -r[6])
    sales, cost = _sum(rows, 4), _sum(rows, 5)
    return _result(["SKU", "Item", "Category", "Qty", "Sales (taxable)", "Cost", "Margin",
                    "Margin %"], rows,
                   {"Sales": sales, "Cost": cost, "Margin": _m(sales - cost),
                    "Margin %": _pct(sales - cost, sales)},
                   "Cost is each product's CURRENT cost price — the till does not store "
                   "the cost at the moment of sale. Free promotional lines count their "
                   "cost with no sales value, which is what they cost the shop.")


def sales_invoice_wise(ctx):
    rows = []
    for inv in ctx.bills(selectinload(Invoice.items)).all():
        c = inv.customer
        rows.append([inv.invoice_number, _d(inv.invoice_date), c.name if c else "Walk-in",
                     (c.gstin if c else "") or "—", len(inv.items), _q(inv.total_qty),
                     _m(inv.subtotal), _m(inv.discount), _m(inv.cgst), _m(inv.sgst),
                     _m(inv.igst), _m(inv.total)])
    return _result(["Bill", "Date", "Customer", "GSTIN", "Lines", "Qty", "Taxable",
                    "Discount", "CGST", "SGST", "IGST", "Total"], rows,
                   {"Bills": len(rows), "Taxable": _sum(rows, 6), "Discount": _sum(rows, 7),
                    "CGST": _sum(rows, 8), "SGST": _sum(rows, 9), "IGST": _sum(rows, 10),
                    "Total": _sum(rows, 11)},
                   "One row per bill with its tax split. " + TAX_NOTE)


def sales_barcode(ctx):
    agg = OrderedDict()
    for item, inv in ctx.lines().all():
        p = item.product
        if not p:
            continue
        r = agg.setdefault(p.id, [p.barcode or p.sku, p.sku, p.name, p.size or "—",
                                  p.color or "—", 0.0, 0.0])
        r[5] += item.quantity or 0
        r[6] += (item.line_total or 0) + (item.tax_amount or 0)
    rows = [[bc, sku, n, s, c, _q(q), _m(a / q) if q else 0, _m(a)]
            for bc, sku, n, s, c, q, a in agg.values()]
    rows.sort(key=lambda r: -r[7])
    return _result(["Barcode", "SKU", "Item", "Size", "Colour", "Qty", "Avg rate", "Amount"],
                   rows, {"Items": len(rows), "Qty": _qsum(rows, 5), "Amount": _sum(rows, 7)},
                   "What sold, tag by tag. The SKU stands in where no barcode is printed. "
                   "Amount includes tax.")


def day_summary(ctx):
    days = OrderedDict()

    def day(k):
        return days.setdefault(k, {"bills": 0, "qty": 0.0, "gross": 0.0, "disc": 0.0,
                                   "tax": 0.0, "sales": 0.0, "back": 0.0, "tend": {}})
    for inv in ctx.bills(selectinload(Invoice.items), selectinload(Invoice.payments)).all():
        d = day(inv.invoice_date.date())
        d["bills"] += 1
        d["qty"] += inv.total_qty
        d["gross"] += inv.subtotal or 0
        d["disc"] += inv.discount or 0
        d["tax"] += _tax(inv)
        d["sales"] += inv.total or 0
        _tender_cols(_tenders(inv), d["tend"])
    for note in ctx.credit_notes().all():
        day(note.created_at.date())["back"] += note.total or 0
    rows = []
    for k in sorted(days):
        d = days[k]
        rows.append([_d(k), d["bills"], _q(d["qty"]), _m(d["gross"]), _m(d["disc"]),
                     _m(d["tax"]), _m(d["sales"]), _m(d["back"]), _m(d["sales"] - d["back"]),
                     _m(d["tend"].get("cash")), _m(d["tend"].get("card")),
                     _m(d["tend"].get("upi"))])
    tot = {h: _sum(rows, i) for i, h in ((3, "Gross"), (4, "Discount"), (5, "Tax"),
                                          (6, "Sales"), (7, "Returns"), (8, "Net sales"))}
    tot = {"Bills": sum(r[1] for r in rows), **tot}
    return _result(["Date", "Bills", "Qty", "Gross", "Discount", "Tax", "Sales", "Returns",
                    "Net sales", "Cash", "Card", "UPI"], rows, tot,
                   "One line per day. Returns are dated by the credit note, so a return "
                   "today of last week's bill comes off today. The shop has no offset-bill "
                   "configuration, so every bill is counted as raised.")


def sales_age_wise(ctx):
    arrived = _arrivals()
    agg = OrderedDict((label, [label, 0, 0.0, 0.0]) for _, _, label in AGE_BANDS)
    agg["Unknown"] = ["Unknown", 0, 0.0, 0.0]
    for item, inv in ctx.lines().all():
        since = arrived.get(item.product_id)
        days = (inv.invoice_date.date() - since).days if since else None
        r = agg[_band(max(days, 0) if days is not None else None)]
        r[1] += 1
        r[2] += item.quantity or 0
        r[3] += (item.line_total or 0) + (item.tax_amount or 0)
    rows = [[b, n, _q(q), _m(a)] for b, n, q, a in agg.values() if n]
    total = _sum(rows, 3)
    rows = [r + [_pct(r[3], total)] for r in rows]
    return _result(["Stock age when sold", "Lines", "Qty", "Amount", "Share %"], rows,
                   {"Qty": _qsum(rows, 2), "Amount": total},
                   "How long each garment had been in the shop on the day it sold — from "
                   "the first transfer that brought that item in, or the day the item was "
                   "added when it never came by transfer.")


def _session_rows(sessions):
    rows = []
    for s in sessions:
        t = s.totals()
        rows.append([s.code, _dt(s.created_at), _name(s.salesperson),
                     s.customer.name if s.customer else "Walk-in", len(s.items),
                     _q(sum(i.quantity for i in s.items)), _m(t["total"]),
                     s.status.replace("_", " "),
                     (db.session.get(Invoice, s.invoice_id).invoice_number
                      if s.invoice_id and db.session.get(Invoice, s.invoice_id) else "—")])
    return rows


SESSION_COLS = ["Cart", "Created", "Salesperson", "Customer", "Lines", "Qty",
                "Estimate total", "Status", "Bill"]


def _sessions(ctx, statuses, billed=None):
    q = SaleSession.query.filter(day_from(SaleSession.created_at, ctx.start),
                                 day_to(SaleSession.created_at, ctx.end),
                                 SaleSession.status.in_(statuses))
    if billed is True:
        q = q.filter(SaleSession.invoice_id.isnot(None))
    elif billed is False:
        q = q.filter(SaleSession.invoice_id.is_(None))
    return q.options(selectinload(SaleSession.items)).order_by(SaleSession.created_at).all()


def approved_bills(ctx):
    rows = _session_rows(_sessions(ctx, ("approved", "completed")))
    return _result(SESSION_COLS, rows, {"Carts": len(rows), "Estimate total": _sum(rows, 6)},
                   "Floor-sale carts the customer approved on their phone — those still "
                   "waiting at the counter and those already billed. Dated by when the "
                   "cart was started.")


def estimate_bills(ctx):
    rows = _session_rows(_sessions(ctx, ("open", "awaiting_approval", "approved",
                                         "rejected", "cancelled"), billed=False))
    return _result(SESSION_COLS, rows, {"Estimates": len(rows), "Estimate total": _sum(rows, 6)},
                   "Carts built on the floor that never became a bill — still open, "
                   "waiting for the customer, rejected or cancelled. Prices are what the "
                   "cart showed, not a bill.")


def delivery_report(ctx):
    q = Delivery.query.filter(day_from(Delivery.created_at, ctx.start),
                              day_to(Delivery.created_at, ctx.end))
    q = ctx._place(q, Delivery)
    rows = []
    for dlv in q.options(selectinload(Delivery.lines), selectinload(Delivery.bills))\
            .order_by(Delivery.created_at).all():
        rows.append([dlv.number, _dt(dlv.created_at),
                     dlv.customer.name if dlv.customer else "Walk-in",
                     ", ".join(b.invoice.invoice_number for b in dlv.bills if b.invoice) or "—",
                     _q(dlv.total_qty), _q(dlv.scanned_qty), _q(dlv.overridden_qty),
                     dlv.total_amount, _name(dlv.staff or dlv.cashier),
                     dlv.location.name if dlv.location else "—"])
    return _result(["Delivery", "When", "Customer", "Bills", "Pieces", "Scanned",
                    "Passed unscanned", "Value", "Handed over by", "Store"], rows,
                   {"Deliveries": len(rows), "Pieces": _qsum(rows, 4), "Value": _sum(rows, 7)},
                   "Goods physically handed to the customer. Pieces passed without a "
                   "scan were allowed by a manager and carry their reason on the delivery.")


def sales_snapshot(ctx):
    bills = ctx.bills(selectinload(Invoice.items)).all()
    count = len(bills)
    sales = _m(sum(i.total or 0 for i in bills))
    qty = sum(i.total_qty for i in bills)
    back = _m(sum(n.total or 0 for n in ctx.credit_notes().all()))
    walkins = sum(1 for i in bills if not i.customer_id)
    new_customers = Customer.query.filter(day_from(Customer.created_at, ctx.start),
                                          day_to(Customer.created_at, ctx.end)).count()
    cats, items, days = {}, {}, {}
    for inv in bills:
        days[inv.invoice_date.date()] = days.get(inv.invoice_date.date(), 0) + (inv.total or 0)
        for it in inv.items:
            p = it.product
            val = (it.line_total or 0) + (it.tax_amount or 0)
            if p:
                items[p.name] = items.get(p.name, 0) + val
                cname = p.category.name if p.category else "—"
                cats[cname] = cats.get(cname, 0) + val
    top = lambda d: (max(d.items(), key=lambda kv: kv[1]) if d else None)
    tc, ti, td = top(cats), top(items), top(days)
    rows = [["Bills", count], ["Sales value", sales],
            ["Average bill", _m(sales / count) if count else 0],
            ["Pieces sold", _q(qty)], ["Pieces per bill", _q(qty / count) if count else 0],
            ["Discount given", _m(sum(i.discount or 0 for i in bills))],
            ["Tax collected", _m(sum(_tax(i) for i in bills))],
            ["Returns", back], ["Net sales", _m(sales - back)],
            ["Walk-in bills", walkins], ["New customers", new_customers],
            ["Top category", f"{tc[0]} ({_m(tc[1]):,.2f})" if tc else "—"],
            ["Top item", f"{ti[0]} ({_m(ti[1]):,.2f})" if ti else "—"],
            ["Best day", f"{_d(td[0])} ({_m(td[1]):,.2f})" if td else "—"]]
    return _result(["Measure", "Value"], rows, {}, "The period on one card.")


def text_day_summary(ctx):
    days = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments), selectinload(Invoice.items)).all():
        d = days.setdefault(inv.invoice_date.date(), {"bills": 0, "sales": 0.0, "qty": 0.0,
                                                      "tend": {}, "back": 0.0})
        d["bills"] += 1
        d["sales"] += inv.total or 0
        d["qty"] += inv.total_qty
        _tender_cols(_tenders(inv), d["tend"])
    for note in ctx.credit_notes().all():
        days.setdefault(note.created_at.date(), {"bills": 0, "sales": 0.0, "qty": 0.0,
                                                 "tend": {}, "back": 0.0})["back"] += note.total or 0
    shop = current_app.config.get("SHOP_NAME", "Store")
    lines, rows = [], []
    for k in sorted(days):
        d = days[k]
        tend = " · ".join(f"{m.upper()} {d['tend'][m]:,.2f}" for m in d["tend"]) or "—"
        text = (f"{shop} — {_d(k)}\n"
                f"Bills: {d['bills']}  |  Pieces: {_q(d['qty'])}\n"
                f"Sales: {d['sales']:,.2f}  |  Returns: {d['back']:,.2f}  |  "
                f"Net: {d['sales'] - d['back']:,.2f}\n"
                f"Collected: {tend}")
        lines.append(text)
        rows.append([_d(k), d["bills"], _m(d["sales"]), _m(d["back"]),
                     _m(d["sales"] - d["back"])])
    return _result(["Date", "Bills", "Sales", "Returns", "Net"], rows,
                   {"Net": _sum(rows, 4)},
                   "A plain-text summary for each day, ready to copy into a message.",
                   text="\n\n".join(lines))


# ===========================================================================
#  ANALYSIS REPORTS
# ===========================================================================
def sales_analysis(ctx):
    agg, bills_by = OrderedDict(), {}
    for item, inv in ctx.lines().all():
        p = item.product
        cat = p.category if p else None
        key = cat.id if cat else 0
        r = agg.setdefault(key, [(cat.section if cat else None) or "—",
                                 cat.name if cat else "Uncategorised", 0.0, 0.0])
        r[2] += item.quantity or 0
        r[3] += (item.line_total or 0) + (item.tax_amount or 0)
        bills_by.setdefault(key, set()).add(inv.id)
    total = sum(r[3] for r in agg.values())
    rows = [[sec, name, len(bills_by.get(k, ())), _q(q), _m(a), _pct(a, total),
             _m(a / q) if q else 0] for k, (sec, name, q, a) in agg.items()]
    rows.sort(key=lambda r: -r[4])
    return _result(["Section", "Category", "Bills", "Qty", "Amount", "Share %", "Avg rate"],
                   rows, {"Qty": _qsum(rows, 3), "Amount": _sum(rows, 4)},
                   "Where the money came from, category by category. Amount includes tax.")


def sales_vs_settlement(ctx):
    days = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        d = days.setdefault(inv.invoice_date.date(), {"bills": 0, "sales": 0.0, "tend": {}})
        d["bills"] += 1
        d["sales"] += inv.total or 0
        _tender_cols(_tenders(inv), d["tend"])
    rows = []
    for k in sorted(days):
        d, t = days[k], days[k]["tend"]
        got = _m(sum(t.values()))
        rows.append([_d(k), d["bills"], _m(d["sales"]), _m(t.get("cash")), _m(t.get("card")),
                     _m(t.get("upi")), _m(t.get("other")), got, _m(d["sales"] - got)])
    return _result(["Date", "Bills", "Sales", "Cash", "Card", "UPI", "Other", "Collected",
                    "Difference"], rows,
                   {"Sales": _sum(rows, 2), "Collected": _sum(rows, 7),
                    "Difference": _sum(rows, 8)},
                   "What was billed against what was taken, day by day. A difference is "
                   "money billed and not recorded as received.")


def sales_vs_stock(ctx):
    sold = {}
    for item, inv in ctx.lines().all():
        sold[item.product_id] = sold.get(item.product_id, 0) + (item.quantity or 0)
    span = max(1, (ctx.end - ctx.start).days + 1)
    rows = []
    for p in Product.query.filter(Product.active.is_(True)).order_by(Product.name).all():
        s, stock = sold.get(p.id, 0), p.stock_qty or 0
        if not s and not stock:
            continue
        per_day = s / span
        rows.append([p.sku, p.name, p.category.name if p.category else "—", _q(s), _q(stock),
                     _pct(s, s + stock) if (s + stock) > 0 else 0,
                     int(stock / per_day) if per_day else "—"])
    rows.sort(key=lambda r: -(r[3] if isinstance(r[3], (int, float)) else 0))
    return _result(["SKU", "Item", "Category", "Sold in period", "In stock now",
                    "Sell-through %", "Days of cover"], rows,
                   {"Sold": _qsum(rows, 3), "In stock": _qsum(rows, 4)},
                   "Sell-through is sold ÷ (sold + what is left). Days of cover is how long "
                   "the stock on hand lasts at this period's daily rate; a dash means it did "
                   "not sell at all.")


def sales_purchase_stock(ctx):
    agg = {}

    def row(p):
        cat = p.category.name if p and p.category else "Uncategorised"
        return agg.setdefault(cat, [cat, 0.0, 0.0, 0.0, 0.0])
    # each row's product and its category are read below; loaded with the rows
    # rather than one query per row
    _cat = lambda rel: selectinload(rel).selectinload(Product.category)  # noqa: E731
    received = (db.session.query(TransferReceipt).options(_cat(TransferReceipt.product))
                .filter(day_from(TransferReceipt.applied_at, ctx.start),
                        day_to(TransferReceipt.applied_at, ctx.end)))
    if ctx.location:
        received = received.filter(TransferReceipt.location_id == ctx.location)
    for r in received.all():
        row(r.product)[1] += r.qty or 0
    for m in (StockMovement.query.options(_cat(StockMovement.product))
              .filter(StockMovement.change > 0,
                                         StockMovement.reason.in_(("opening", "purchase")),
                                         day_from(StockMovement.created_at, ctx.start),
                                         day_to(StockMovement.created_at, ctx.end)).all()):
        row(m.product)[1] += m.change or 0
    for item, inv in ctx.lines().options(_cat(InvoiceItem.product)).all():
        row(item.product)[2] += item.quantity or 0
    for note in ctx.credit_notes().options(
            selectinload(CreditNote.items).selectinload(CreditNoteItem.product)
            .selectinload(Product.category)).all():
        for ci in note.items:
            row(ci.product)[3] += ci.quantity or 0
    for p in Product.query.options(selectinload(Product.category)).filter(Product.active.is_(True)).all():
        row(p)[4] += p.stock_qty or 0
    rows = [[c, _q(r), _q(s), _q(b), _q(st)] for c, r, s, b, st in agg.values()
            if r or s or b or st]
    rows.sort(key=lambda r: -r[2])
    return _result(["Category", "Received", "Sold", "Returned", "In stock now"], rows,
                   {"Received": _qsum(rows, 1), "Sold": _qsum(rows, 2),
                    "In stock now": _qsum(rows, 4)},
                   "The shop does not buy from suppliers — the warehouse does — so "
                   "\"purchase\" here is stock RECEIVED into the shop: transfers taken in "
                   "from the warehouse plus opening entries in the period.")


# ===========================================================================
#  STOCK REPORTS
# ===========================================================================
def _stock_qty(ctx):
    """{product_id: qty} — the whole shop, or one branch when a branch is chosen."""
    if ctx.location:
        return dict(db.session.query(LocationStock.product_id, LocationStock.qty)
                    .filter(LocationStock.location_id == ctx.location).all())
    return None


def stock_detail(ctx):
    split = _stock_qty(ctx)
    rows = []
    for p in Product.query.filter(Product.active.is_(True)).order_by(Product.name).all():
        qty = (split.get(p.id, 0) if split is not None else p.stock_qty) or 0
        if split is not None and not qty:
            continue
        rows.append([p.sku, p.barcode or "—", p.name, p.category.name if p.category else "—",
                     p.size or "—", p.color or "—", p.fabric or "—",
                     p.floor.name if p.floor else "—", _q(qty), _m(p.cost_price),
                     _m(p.mrp) if p.mrp else "—", _m(p.selling_price),
                     _m(qty * (p.cost_price or 0)), _m(qty * (p.selling_price or 0))])
    return _result(["SKU", "Barcode", "Item", "Category", "Size", "Colour", "Fabric", "Floor",
                    "Qty", "Cost", "MRP", "Price", "Value at cost", "Value at price"], rows,
                   {"Items": len(rows), "Qty": _qsum(rows, 8), "Value at cost": _sum(rows, 12),
                    "Value at price": _sum(rows, 13)},
                   "Stock as it stands now — the date range does not apply. Choosing a "
                   "branch shows that branch's share of the stock; items the shop holds "
                   "with no branch split recorded are left out then.")


def stock_shelf_period(ctx):
    arrived = _arrivals()
    last_sold = dict(db.session.query(InvoiceItem.product_id, func.max(Invoice.invoice_date))
                     .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
                     .filter(Invoice.live())
                     .group_by(InvoiceItem.product_id).all())
    today = date.today()
    rows = []
    for p in Product.query.filter(Product.active.is_(True), Product.stock_qty > 0)\
            .order_by(Product.name).all():
        since = arrived.get(p.id)
        sold = last_sold.get(p.id)
        rows.append([p.sku, p.name, p.category.name if p.category else "—", _q(p.stock_qty),
                     _d(since), (today - since).days if since else "—",
                     _d(_day(sold)) if sold else "never",
                     (today - _day(sold)).days if sold else "—",
                     _m((p.stock_qty or 0) * (p.cost_price or 0))])
    rows.sort(key=lambda r: -(r[5] if isinstance(r[5], int) else -1))
    return _result(["SKU", "Item", "Category", "Qty", "In shop since", "Days on shelf",
                    "Last sold", "Days since sale", "Value at cost"], rows,
                   {"Items": len(rows), "Value at cost": _sum(rows, 8)},
                   "How long what is on the shelf has been there. 'In shop since' is the "
                   "first transfer that brought the item in. Current stock only.")


def stock_aging(ctx):
    arrived = _arrivals()
    today = date.today()
    rows, bands = [], OrderedDict((label, [0.0, 0.0]) for _, _, label in AGE_BANDS)
    for p in Product.query.filter(Product.active.is_(True), Product.stock_qty > 0).all():
        since = arrived.get(p.id)
        days = (today - since).days if since else None
        band = _band(days)
        value = _m((p.stock_qty or 0) * (p.cost_price or 0))
        rows.append([band, p.sku, p.name, p.category.name if p.category else "—",
                     _q(p.stock_qty), days if days is not None else "—", value])
        b = bands.setdefault(band, [0.0, 0.0])
        b[0] += p.stock_qty or 0
        b[1] += value
    order = {label: i for i, (_, _, label) in enumerate(AGE_BANDS)}
    rows.sort(key=lambda r: (order.get(r[0], 99), -(r[5] if isinstance(r[5], int) else 0)))
    totals = {f"{k} value": _m(v[1]) for k, v in bands.items() if v[0]}
    totals["Total value"] = _sum(rows, 6)
    return _result(["Age band", "SKU", "Item", "Category", "Qty", "Days", "Value at cost"],
                   rows, totals,
                   "Every item in stock, banded by how long it has been in the shop. "
                   "Current stock only — the date range does not apply.")


def direct_stock(ctx):
    rows = []
    # the product of every movement is read below — loaded with the rows, not
    # one query per movement (a month of opening stock is tens of thousands)
    for m in (StockMovement.query.options(selectinload(StockMovement.product))
              .filter(StockMovement.reason.in_(("opening", "adjustment", "purchase")),
                                         day_from(StockMovement.created_at, ctx.start),
                                         day_to(StockMovement.created_at, ctx.end))
              .order_by(StockMovement.created_at).all()):
        p = m.product
        rows.append([_dt(m.created_at), p.sku if p else "—", p.name if p else "—",
                     (m.reason or "").capitalize(), _q(m.change), m.reference or "—",
                     _m((m.change or 0) * ((p.cost_price or 0) if p else 0))])
    return _result(["When", "SKU", "Item", "Entry", "Qty", "Reference", "Value at cost"], rows,
                   {"Entries": len(rows), "Qty": _qsum(rows, 4), "Value": _sum(rows, 6)},
                   "Stock put in or taken out by hand — opening stock and adjustments — "
                   "rather than arriving on a warehouse transfer or leaving on a bill.")


def price_changer(ctx):
    con = wi._connect()
    if con is None:
        return _result(["When", "Revision"], [], {},
                       "The warehouse database cannot be read from here, and price changes "
                       "are made in the warehouse's Price Changer — so there is nothing to show.")
    held = {p.sku: p for p in Product.query.all()}
    rows = []
    try:
        start = datetime.combine(ctx.start, datetime.min.time())
        end = datetime.combine(ctx.end + timedelta(days=1), datetime.min.time())
        found = con.execute(
            "SELECT r.number, r.created_at, r.created_by, r.field, r.operation, r.value, "
            "r.reverted_at, c.sku, c.description, c.old_value, c.new_value "
            "FROM price_changes c JOIN price_revisions r ON r.id = c.revision_id "
            "ORDER BY r.created_at, c.id").fetchall()
    except Exception:                                   # noqa: BLE001
        found = []
    finally:
        con.close()
    labels = {"sale_price": "Selling price", "mrp": "MRP", "sale_discount_pct": "Discount %"}
    for r in found:
        r = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
        when = r.get("created_at")
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when[:19])
            except ValueError:
                when = None
        if not when or not (start <= when < end):
            continue
        if r.get("sku") not in held:
            continue
        old, new = r.get("old_value"), r.get("new_value")
        rows.append([_dt(when), r.get("number") or "—", labels.get(r.get("field"), r.get("field")),
                     r.get("sku"), r.get("description") or held[r["sku"]].name,
                     _m(old), _m(new), _m((new or 0) - (old or 0)), r.get("created_by") or "—",
                     "reverted" if r.get("reverted_at") else "in force"])
    return _result(["When", "Revision", "Price", "SKU", "Item", "Old", "New", "Change", "By",
                    "Status"], rows, {"Changes": len(rows)},
                   "Price changes made in the warehouse's Price Changer to items this shop "
                   "holds. A reverted revision is listed with its lines, because what was "
                   "done and undone is still a record.")


def stock_audit(ctx):
    return _from_lib("stock_audits", ctx)


def stock_split(ctx):
    places = Location.query.order_by(Location.name).all()
    split = {}
    for pid, lid, qty in db.session.query(LocationStock.product_id, LocationStock.location_id,
                                          LocationStock.qty).all():
        split.setdefault(pid, {})[lid] = qty or 0
    used = sorted({lid for d in split.values() for lid, q in d.items() if q},
                  key=lambda lid: next((l.name for l in places if l.id == lid), ""))
    names = {l.id: l.name for l in places}
    rows = []
    for p in Product.query.filter(Product.active.is_(True), Product.stock_qty > 0)\
            .order_by(Product.name).all():
        mine = split.get(p.id, {})
        parts = [_q(mine.get(lid, 0)) for lid in used]
        rows.append([p.sku, p.name, _q(p.stock_qty), *parts,
                     _q((p.stock_qty or 0) - sum(mine.values()))])
    cols = ["SKU", "Item", "Shop total", *[names.get(lid, f"#{lid}") for lid in used], "Not split"]
    totals = {"Shop total": _qsum(rows, 2)}
    for i, lid in enumerate(used):
        totals[names.get(lid, f"#{lid}")] = _qsum(rows, 3 + i)
    return _result(cols, rows, totals,
                   "The shop's stock split branch by branch. 'Not split' is stock held from "
                   "before branches were tracked — it is in the shop total and in no branch, "
                   "rather than divided up by guesswork. Current stock only.")


# ===========================================================================
#  DISCOUNT REPORTS
# ===========================================================================
def _discount_rows(agg):
    rows = []
    for r in agg.values():
        u = r["user"]
        rows.append([_name(u), r["bills"], r["disc_bills"], _m(r["gross"]), _m(r["disc"]),
                     _pct(r["disc"], r["gross"]), _m(r["free"])])
    rows.sort(key=lambda r: -r[4])
    return rows


DISCOUNT_COLS = ["{who}", "Bills", "Bills with discount", "Gross", "Discount", "Discount %",
                 "Free goods value"]


def discount_biller(ctx):
    rows = _discount_rows(_by_person(ctx, _served))
    return _result([c.format(who="Biller (served by)") for c in DISCOUNT_COLS], rows,
                   {"Discount": _sum(rows, 4), "Free goods value": _sum(rows, 6)},
                   "Discount by whoever served the sale. 'Free goods value' is what "
                   "promotional free items would have sold for — given away, not discounted.")


def discount_cashier(ctx):
    rows = _discount_rows(_by_person(ctx, lambda inv: inv.cashier))
    return _result([c.format(who="Cashier (till login)") for c in DISCOUNT_COLS], rows,
                   {"Discount": _sum(rows, 4), "Free goods value": _sum(rows, 6)},
                   "Discount by the login that rang the bill up at the till.")


# ===========================================================================
#  SETTLEMENT REPORTS
# ===========================================================================
def settlement_day_end(ctx):
    days = OrderedDict()

    def day(k):
        return days.setdefault(k, {"bills": 0, "tend": {}, "refund": {}})
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        d = day(inv.invoice_date.date())
        d["bills"] += 1
        _tender_cols(_tenders(inv), d["tend"])
    for note in ctx.credit_notes().all():
        m = (note.refund_method or "cash").lower()
        if m == "store_credit":
            continue            # kept as credit, not handed back — no money left the till
        r = day(note.created_at.date())["refund"]
        r[m] = r.get(m, 0) + (note.total or 0)
    rows = []
    for k in sorted(days):
        d, t, r = days[k], days[k]["tend"], days[k]["refund"]
        total, refunds = _m(sum(t.values())), _m(sum(r.values()))
        rows.append([_d(k), d["bills"], _m(t.get("cash")), _m(t.get("card")), _m(t.get("upi")),
                     _m(t.get("other")), total, refunds,
                     _m((t.get("cash") or 0) - (r.get("cash") or 0)), _m(total - refunds)])
    return _result(["Date", "Bills", "Cash", "Card", "UPI", "Other", "Collected", "Refunds",
                    "Cash in drawer", "Net"], rows,
                   {"Collected": _sum(rows, 6), "Refunds": _sum(rows, 7),
                    "Cash in drawer": _sum(rows, 8), "Net": _sum(rows, 9)},
                   "What each day's tills took, less what was refunded. 'Other' is store credit "
                   "and advances spent on bills. 'Cash in drawer' is cash taken less cash "
                   "refunded, without the float — the Opening/Closing report has the counts.")


def settlement_cashier(ctx):
    agg = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        u = inv.cashier
        r = agg.setdefault(u.id if u else 0, {"user": u, "bills": 0, "tend": {}, "refund": 0.0})
        r["bills"] += 1
        _tender_cols(_tenders(inv), r["tend"])
    for note in ctx.credit_notes().all():
        if (note.refund_method or "") == "store_credit":
            continue
        u = note.cashier
        agg.setdefault(u.id if u else 0, {"user": u, "bills": 0, "tend": {}, "refund": 0.0})[
            "refund"] += note.total or 0
    rows = []
    for r in agg.values():
        t = r["tend"]
        total = _m(sum(t.values()))
        rows.append([_name(r["user"]), r["bills"], _m(t.get("cash")), _m(t.get("card")),
                     _m(t.get("upi")), total, _m(r["refund"]), _m(total - r["refund"])])
    rows.sort(key=lambda r: -r[5])
    return _result(["Cashier", "Bills", "Cash", "Card", "UPI", "Collected", "Refunds handled",
                    "Net"], rows,
                   {"Collected": _sum(rows, 5), "Refunds": _sum(rows, 6), "Net": _sum(rows, 7)},
                   "By the login on the till — the person who answers for the drawer.")


def settlement_detail(ctx):
    rows = []
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        if inv.payments:
            for p in inv.payments:
                rows.append([_dt(p.created_at or inv.invoice_date), inv.invoice_number,
                             _name(inv.cashier), (p.method or "").upper(), _m(p.amount),
                             _m(p.tendered if p.tendered is not None else p.amount),
                             p.change, p.reference or "—"])
        elif (inv.payment_status or "") != "pending":
            rows.append([_dt(inv.invoice_date), inv.invoice_number, _name(inv.cashier),
                         (inv.payment_method or "cash").upper(), _m(inv.total), _m(inv.total),
                         0.0, "(single tender)"])
    return _result(["When", "Bill", "Cashier", "Method", "Amount", "Tendered", "Change",
                    "Reference"], rows,
                   {"Tenders": len(rows), "Amount": _sum(rows, 4), "Change given": _sum(rows, 6)},
                   "Every tender against every bill — a split bill has a row per tender. "
                   "Bills from before split tenders were recorded show one row for the "
                   "whole amount.")


def company_collection(ctx):
    agg = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        c = inv.company
        r = agg.setdefault(c.id if c else 0, {"c": c, "bills": 0, "sales": 0.0, "tend": {}})
        r["bills"] += 1
        r["sales"] += inv.total or 0
        _tender_cols(_tenders(inv), r["tend"])
    rows = []
    for r in agg.values():
        c, t = r["c"], r["tend"]
        rows.append([c.name if c else "(not recorded)", (c.gstin if c else "") or "—", r["bills"],
                     _m(r["sales"]), _m(t.get("cash")), _m(t.get("card")), _m(t.get("upi")),
                     _m(sum(t.values()))])
    rows.sort(key=lambda r: -r[3])
    return _result(["Company", "GSTIN", "Bills", "Sales", "Cash", "Card", "UPI", "Collected"],
                   rows, {"Sales": _sum(rows, 3), "Collected": _sum(rows, 7)},
                   "Split by the company whose GSTIN went on the bill.")


def credit_collection(ctx):
    rows = []
    q = (db.session.query(InvoicePayment, Invoice)
         .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
         .filter(Invoice.live(),
                 day_from(InvoicePayment.created_at, ctx.start),
                 day_to(InvoicePayment.created_at, ctx.end),
                 func.date(InvoicePayment.created_at) > func.date(Invoice.invoice_date)))
    q = ctx._place(q, Invoice)
    for pay, inv in q.order_by(InvoicePayment.created_at).all():
        rows.append([_d(pay.created_at), inv.invoice_number, _d(inv.invoice_date),
                     inv.customer.name if inv.customer else "Walk-in", (pay.method or "").upper(),
                     _m(pay.amount), (pay.created_at.date() - inv.invoice_date.date()).days])
    return _result(["Collected on", "Bill", "Bill date", "Customer", "Method", "Amount",
                    "Days after sale"], rows, {"Collected": _sum(rows, 5)},
                   "Money received against a bill on a later day than it was raised. The "
                   "counter settles every bill at the time of sale today, so this is empty "
                   "until credit sales are taken.")


def settlement_reconciliation(ctx):
    days = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        d = days.setdefault(inv.invoice_date.date(), [0, 0.0, 0.0, 0, 0.0])
        got = _collected(inv)
        d[0] += 1
        d[1] += inv.total or 0
        d[2] += got
        if abs((inv.total or 0) - got) > 0.009:
            d[3] += 1
        d[4] += inv.change_given
    rows = [[_d(k), v[0], _m(v[1]), _m(v[2]), _m(v[1] - v[2]), v[3], _m(v[4])]
            for k, v in sorted(days.items())]
    return _result(["Date", "Bills", "Billed", "Collected", "Difference", "Bills not matching",
                    "Change given"], rows,
                   {"Billed": _sum(rows, 2), "Collected": _sum(rows, 3),
                    "Difference": _sum(rows, 4), "Bills not matching": sum(r[5] for r in rows)},
                   "Bill totals against the tenders recorded on them. Change handed back is "
                   "shown separately: a customer paying 2,000 against 1,860 settles 1,860.")


def settlement_daywise(ctx):
    agg = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.payments)).all():
        key = (inv.invoice_date.date(), inv.location.name if inv.location else "—",
               inv.counter.name if inv.counter else "—")
        r = agg.setdefault(key, {"bills": 0, "tend": {}})
        r["bills"] += 1
        _tender_cols(_tenders(inv), r["tend"])
    rows = []
    for (d, store, till), r in sorted(agg.items()):
        t = r["tend"]
        rows.append([_d(d), store, till, r["bills"], _m(t.get("cash")), _m(t.get("card")),
                     _m(t.get("upi")), _m(sum(t.values()))])
    return _result(["Date", "Store", "Till", "Bills", "Cash", "Card", "UPI", "Total"], rows,
                   {"Cash": _sum(rows, 4), "Card": _sum(rows, 5), "UPI": _sum(rows, 6),
                    "Total": _sum(rows, 7)},
                   "Each day, each till. A dash is a bill raised before the till recorded "
                   "which branch and counter it stood at.")


# ===========================================================================
#  TAX REPORTS
# ===========================================================================
def _split_tax(inv, tax):
    if inv.is_interstate or ((inv.igst or 0) > 0 and not (inv.cgst or 0)):
        return 0.0, 0.0, _m(tax)
    return _m(tax / 2), _m(tax / 2), 0.0


def tax_summary(ctx):
    agg = {}
    for item, inv in ctx.lines().all():
        rate = float(item.gst_rate or 0)
        r = agg.setdefault(rate, [0, 0.0, 0.0, 0.0, 0.0, 0.0])
        c, s, i = _split_tax(inv, item.tax_amount or 0)
        r[0] += 1
        r[1] += item.quantity or 0
        r[2] += item.line_total or 0
        r[3] += c
        r[4] += s
        r[5] += i
    rows = [[f"{_q(rate)}%", n, _q(q), _m(tx), _m(c), _m(s), _m(i), _m(c + s + i),
             _m(tx + c + s + i)] for rate, (n, q, tx, c, s, i) in sorted(agg.items())]
    return _result(["GST rate", "Lines", "Qty", "Taxable", "CGST", "SGST", "IGST", "Tax",
                    "Value with tax"], rows,
                   {"Taxable": _sum(rows, 3), "CGST": _sum(rows, 4), "SGST": _sum(rows, 5),
                    "IGST": _sum(rows, 6), "Tax": _sum(rows, 7)}, TAX_NOTE)


def _per_bill_rates(ctx):
    bills = OrderedDict()
    for item, inv in ctx.lines().all():
        b = bills.setdefault(inv.id, {"inv": inv, "rates": {}})
        r = b["rates"].setdefault(float(item.gst_rate or 0), [0.0, 0.0])
        r[0] += item.line_total or 0
        r[1] += item.tax_amount or 0
    return bills


def tax_column(ctx):
    bills = _per_bill_rates(ctx)
    rates = sorted({rate for b in bills.values() for rate in b["rates"]})
    rows = []
    for b in bills.values():
        inv, c = b["inv"], b["inv"].customer
        cells = []
        for rate in rates:
            tx, tax = b["rates"].get(rate, [0.0, 0.0])
            cells += [_m(tx), _m(tax)]
        taxable = sum(v[0] for v in b["rates"].values())
        tax = sum(v[1] for v in b["rates"].values())
        rows.append([inv.invoice_number, _d(inv.invoice_date), c.name if c else "Walk-in",
                     (c.gstin if c else "") or "—", *cells, _m(taxable), _m(tax), _m(inv.total)])
    cols = ["Bill", "Date", "Customer", "GSTIN"]
    for rate in rates:
        cols += [f"Taxable {_q(rate)}%", f"Tax {_q(rate)}%"]
    cols += ["Total taxable", "Total tax", "Bill total"]
    n = len(cols)
    return _result(cols, rows, {"Bills": len(rows), "Total taxable": _sum(rows, n - 3),
                                "Total tax": _sum(rows, n - 2), "Bill total": _sum(rows, n - 1)},
                   "One row per bill, a pair of columns per GST rate. " + TAX_NOTE)


def tax_row(ctx):
    rows = []
    for b in _per_bill_rates(ctx).values():
        inv, c = b["inv"], b["inv"].customer
        for rate, (tx, tax) in sorted(b["rates"].items()):
            cg, sg, ig = _split_tax(inv, tax)
            rows.append([inv.invoice_number, _d(inv.invoice_date), c.name if c else "Walk-in",
                         (c.gstin if c else "") or "—", f"{_q(rate)}%", _m(tx), cg, sg, ig,
                         _m(tx + tax)])
    return _result(["Bill", "Date", "Customer", "GSTIN", "Rate", "Taxable", "CGST", "SGST",
                    "IGST", "Value"], rows,
                   {"Taxable": _sum(rows, 5), "CGST": _sum(rows, 6), "SGST": _sum(rows, 7),
                    "IGST": _sum(rows, 8)},
                   "One row per bill per GST rate. " + TAX_NOTE)


def hsn_report(ctx):
    agg = {}
    for item, inv in ctx.lines().all():
        p = item.product
        hsn = (p.hsn_code if p else None) or "—"
        rate = float(item.gst_rate or 0)
        r = agg.setdefault((hsn, rate), [(p.unit if p else "pcs") or "pcs", 0.0, 0.0, 0.0, 0.0, 0.0])
        c, s, i = _split_tax(inv, item.tax_amount or 0)
        r[1] += item.quantity or 0
        r[2] += item.line_total or 0
        r[3] += c
        r[4] += s
        r[5] += i
    rows = [[hsn, unit.upper(), f"{_q(rate)}%", _q(q), _m(tx), _m(c), _m(s), _m(i),
             _m(tx + c + s + i)] for (hsn, rate), (unit, q, tx, c, s, i) in sorted(agg.items())]
    return _result(["HSN", "UQC", "Rate", "Qty", "Taxable", "CGST", "SGST", "IGST",
                    "Total value"], rows,
                   {"Taxable": _sum(rows, 4), "CGST": _sum(rows, 5), "SGST": _sum(rows, 6),
                    "IGST": _sum(rows, 7), "Total value": _sum(rows, 8)},
                   "HSN-wise summary of outward supplies, as the GST return asks for it. "
                   + TAX_NOTE)


def tax_collection(ctx):
    days = OrderedDict()
    for inv in ctx.bills(selectinload(Invoice.items), selectinload(Invoice.payments)).all():
        d = days.setdefault(inv.invoice_date.date(), [0.0, 0.0, 0.0, 0.0, 0.0, {}])
        d[0] += inv.subtotal or 0
        d[1] += inv.cgst or 0
        d[2] += inv.sgst or 0
        d[3] += inv.igst or 0
        d[4] += inv.total or 0
        _tender_cols(_tenders(inv), d[5])
    rows = []
    for k in sorted(days):
        tx, c, s, i, total, t = days[k]
        rows.append([_d(k), _m(tx), _m(c), _m(s), _m(i), _m(c + s + i), _m(total),
                     _m(t.get("cash")), _m(t.get("card")), _m(t.get("upi"))])
    return _result(["Date", "Taxable", "CGST", "SGST", "IGST", "Tax", "Bill value", "Cash",
                    "Card", "UPI"], rows,
                   {"Taxable": _sum(rows, 1), "Tax": _sum(rows, 5), "Bill value": _sum(rows, 6)},
                   "Each day's tax split beside how the money came in. " + TAX_NOTE)


def gstr_report(ctx):
    agg = OrderedDict()

    def add(section, rate, taxable, tax, inv, sign=1):
        r = agg.setdefault((section, rate), [set(), 0.0, 0.0, 0.0, 0.0])
        cg, sg, ig = _split_tax(inv, tax)
        r[0].add(inv.id)
        r[1] += sign * taxable
        r[2] += sign * ig
        r[3] += sign * cg
        r[4] += sign * sg
    for item, inv in ctx.lines().all():
        c = inv.customer
        registered = bool(c and (c.gstin or "").strip())
        section = ("B2B — registered customers" if registered
                   else "B2C — inter-state" if inv.is_interstate else "B2C — intra-state")
        add(section, float(item.gst_rate or 0), item.line_total or 0, item.tax_amount or 0, inv)
    for note in ctx.credit_notes().options(selectinload(CreditNote.items)).all():
        c = note.invoice.customer
        section = ("Credit notes — registered (CDNR)" if c and (c.gstin or "").strip()
                   else "Credit notes — unregistered (CDNUR)")
        for ci in note.items:
            add(section, float(ci.gst_rate or 0), ci.line_total or 0, ci.tax_amount or 0,
                note.invoice, sign=-1)
    rows = [[section, f"{_q(rate)}%", len(v[0]), _m(v[1]), _m(v[2]), _m(v[3]), _m(v[4])]
            for (section, rate), v in agg.items()]
    rows.sort(key=lambda r: (r[0], r[1]))
    return _result(["Section", "Rate", "Documents", "Taxable", "IGST", "CGST", "SGST"], rows,
                   {"Taxable": _sum(rows, 3), "IGST": _sum(rows, 4), "CGST": _sum(rows, 5),
                    "SGST": _sum(rows, 6)},
                   "A GSTR-1 style summary to prepare the return from. Credit notes are "
                   "negative. B2C large invoices and place of supply are not separated here — "
                   "check those with your accountant before filing. " + TAX_NOTE)


# ===========================================================================
#  HR REPORTS
# ===========================================================================
def incentive_section(ctx):
    agg = OrderedDict()
    for item, inv in ctx.lines().all():
        u = _served(inv)
        p = item.product
        section = ((p.category.section if p and p.category else None) or "—")
        r = agg.setdefault((u.id if u else 0, section), [u, section, 0.0, 0.0])
        r[2] += item.quantity or 0
        r[3] += item.line_total or 0
    rows = []
    for u, section, qty, sales in agg.values():
        pct = (u.commission_pct or 0) if u else 0
        rows.append([_name(u), section, _q(qty), _m(sales), pct, _m(sales * pct / 100.0)])
    rows.sort(key=lambda r: (r[0], -r[3]))
    return _result(["Staff", "Section", "Qty", "Sales (taxable)", "Commission %", "Incentive"],
                   rows, {"Sales": _sum(rows, 3), "Incentive": _sum(rows, 5)},
                   "Each person's sales split by the category section (Ladies, Mens, Kids…). "
                   "The staff master holds one commission rate per person, so the same rate "
                   "is applied in every section. Returns are not deducted here — see the "
                   "employee incentive report for net figures.")


def incentive_employees(ctx):
    base = sales_salesman(ctx)
    rows = []
    users = {u.full_name: u for u in User.query.all()}
    for r in base["rows"]:
        u = users.get(r[0])
        rows.append([r[0], r[1], r[2], r[4], r[5], r[6], (u.commission_pct or 0) if u else 0,
                     r[8]])
    return _result(["Staff", "Code", "Bills", "Sales", "Returns", "Net sales", "Commission %",
                    "Incentive"], rows,
                   {"Net sales": _sum(rows, 5), "Incentive": _sum(rows, 7)},
                   "Incentive is net sales × the person's commission rate from the staff "
                   "master. Returns come off the person who made the original sale.")


def employee_detail(ctx):
    present = dict(db.session.query(Attendance.user_id,
                                    func.count(func.distinct(func.date(Attendance.check_in))))
                   .filter(day_from(Attendance.check_in, ctx.start),
                           day_to(Attendance.check_in, ctx.end))
                   .group_by(Attendance.user_id).all())
    rows = []
    for u in User.query.order_by(User.full_name).all():
        rows.append([u.staff_code, u.full_name, u.username, (u.role or "").capitalize(),
                     u.phone or "—", u.email or "—", _m(u.salary), u.commission_pct or 0,
                     "Active" if u.active else "Inactive", _d(u.created_at),
                     present.get(u.id, 0)])
    return _result(["Code", "Name", "Login", "Role", "Phone", "Email", "Salary", "Commission %",
                    "Status", "Joined", "Days present"], rows,
                   {"Staff": len(rows), "Monthly salary": _sum(rows, 6)},
                   "The staff master. 'Days present' counts the days in the period with a "
                   "check-in recorded.")


# ===========================================================================
#  CUSTOMER REPORTS
# ===========================================================================
def credit_outstanding(ctx):
    agg = OrderedDict()
    q = Invoice.query.filter(Invoice.live(), day_to(Invoice.invoice_date, ctx.end),
                             Invoice.customer_id.isnot(None))
    q = ctx._place(q, Invoice).options(selectinload(Invoice.payments))
    for inv in q.order_by(Invoice.invoice_date).all():
        balance = (inv.total or 0) - _collected(inv)
        if balance <= 0.009:
            continue
        c = inv.customer
        r = agg.setdefault(c.id, [c.name, c.phone or "—", 0, 0.0, 0.0, inv.invoice_date])
        r[2] += 1
        r[3] += inv.total or 0
        r[4] += balance
    rows = [[n, p, b, _m(t), _m(t - o), _m(o), _d(oldest), (date.today() - oldest.date()).days]
            for n, p, b, t, o, oldest in agg.values()]
    rows.sort(key=lambda r: -r[5])
    return _result(["Customer", "Phone", "Bills pending", "Billed", "Collected", "Outstanding",
                    "Oldest bill", "Days"], rows, {"Outstanding": _sum(rows, 5)},
                   "What customers owe as at the end date, from bills whose recorded payments "
                   "fall short. The counter settles in full today, so this stays empty until "
                   "credit sales are taken.")


def cn_customer(ctx):
    rows = []
    for n in ctx.credit_notes().all():
        c = n.invoice.customer
        rows.append([n.number, _d(n.created_at), c.name if c else "Walk-in",
                     (c.phone if c else "") or "—", n.invoice.invoice_number,
                     (n.refund_method or "").replace("_", " ").capitalize(), n.reason or "—",
                     _m(n.total)])
    return _result(["Credit note", "Date", "Customer", "Phone", "Against bill", "Refunded as",
                    "Reason", "Amount"], rows,
                   {"Notes": len(rows), "Amount": _sum(rows, 7)},
                   "Credit notes raised to customers, refunded as money or kept as store "
                   "credit. The shop issues no gift vouchers, so this lists credit notes only.")


def loyalty_consumption(ctx):
    value = float(current_app.config.get("LOYALTY_POINT_VALUE", 1.0) or 1.0)
    rows = []
    for t in (LoyaltyTxn.query.filter(LoyaltyTxn.points < 0,
                                      day_from(LoyaltyTxn.created_at, ctx.start),
                                      day_to(LoyaltyTxn.created_at, ctx.end))
              .order_by(LoyaltyTxn.created_at).all()):
        c = db.session.get(Customer, t.customer_id)
        inv = db.session.get(Invoice, t.invoice_id) if t.invoice_id else None
        pts = -(t.points or 0)
        rows.append([_dt(t.created_at), c.name if c else "—", (c.phone if c else "") or "—",
                     inv.invoice_number if inv else "—", (t.reason or "").capitalize(),
                     _q(pts), _m(pts * value)])
    return _result(["When", "Customer", "Phone", "Bill", "Entry", "Points used", "Value"], rows,
                   {"Points used": _qsum(rows, 5), "Value": _sum(rows, 6)},
                   f"Loyalty points redeemed on a bill, or reversed by a return. One point is "
                   f"worth {value:g} rupee(s).")


def gift_issue(ctx):
    return _from_lib("promotion_items", ctx,
                     "Gifts here are the items given free under a promotion scheme — the shop "
                     "keeps no separate gift register.")


def my_customers(ctx):
    period = dict((cid, (n, t)) for cid, n, t in
                  db.session.query(Invoice.customer_id, func.count(Invoice.id),
                                   func.sum(Invoice.total))
                  .filter(Invoice.live(), day_from(Invoice.invoice_date, ctx.start),
                          day_to(Invoice.invoice_date, ctx.end),
                          Invoice.customer_id.isnot(None))
                  .group_by(Invoice.customer_id).all())
    life = dict((cid, (n, last)) for cid, n, last in
                db.session.query(Invoice.customer_id, func.count(Invoice.id),
                                 func.max(Invoice.invoice_date))
                .filter(Invoice.live(), Invoice.customer_id.isnot(None))
                .group_by(Invoice.customer_id).all())
    rows = []
    for c in Customer.query.order_by(Customer.name).all():
        n, spent = period.get(c.id, (0, 0))
        ln, last = life.get(c.id, (0, None))
        rows.append([c.card_code, c.name, c.phone or "—", c.email or "—", c.gstin or "—",
                     _d(c.created_at), n, _m(spent), ln, _m(c.total_spent), _q(c.loyalty_points),
                     _d(_day(last)) if last else "never"])
    return _result(["Card", "Customer", "Phone", "Email", "GSTIN", "Joined", "Bills in period",
                    "Spent in period", "Lifetime bills", "Lifetime spend", "Points", "Last visit"],
                   rows, {"Customers": len(rows), "Spent in period": _sum(rows, 7),
                          "Points held": _qsum(rows, 10)},
                   "The customer master, with what each customer bought in the period.")


def inactive_customers(ctx):
    days = ctx.param("days", 90)
    cutoff = date.today() - timedelta(days=days)
    last = dict(db.session.query(Invoice.customer_id, func.max(Invoice.invoice_date))
                .filter(Invoice.live(), Invoice.customer_id.isnot(None))
                .group_by(Invoice.customer_id).all())
    count = dict(db.session.query(Invoice.customer_id, func.count(Invoice.id))
                 .filter(Invoice.live(), Invoice.customer_id.isnot(None))
                 .group_by(Invoice.customer_id).all())
    rows = []
    for c in Customer.query.order_by(Customer.name).all():
        seen = _day(last[c.id]) if last.get(c.id) else None
        if seen and seen > cutoff:
            continue
        rows.append([c.name, c.phone or "—", _d(seen) if seen else "never bought",
                     (date.today() - seen).days if seen else "—", count.get(c.id, 0),
                     _m(c.total_spent), _q(c.loyalty_points)])
    rows.sort(key=lambda r: -(r[3] if isinstance(r[3], int) else 10 ** 6))
    return _result(["Customer", "Phone", "Last visit", "Days since", "Lifetime bills",
                    "Lifetime spend", "Points"], rows, {"Customers": len(rows)},
                   f"Customers with no bill in the last {days} days, as at today — the "
                   f"date range does not apply. Change the window above.")


# ===========================================================================
#  MOBILE VERTICAL · ADD-ONS
# ===========================================================================
def scheme_details(ctx):
    used = dict((sid, (n, b)) for sid, n, b in
                db.session.query(PromotionApplication.scheme_id,
                                 func.count(func.distinct(PromotionApplication.invoice_id)),
                                 func.sum(PromotionApplication.benefit_value))
                .join(Invoice, Invoice.id == PromotionApplication.invoice_id)
                .filter(Invoice.live(), day_from(Invoice.invoice_date, ctx.start),
                        day_to(Invoice.invoice_date, ctx.end))
                .group_by(PromotionApplication.scheme_id).all())
    rows = []
    for s in PromotionScheme.query.order_by(PromotionScheme.priority, PromotionScheme.code).all():
        buy = " + ".join(f"{_q(c.min_qty)} × " + (c.label or " / ".join(i.label for i in c.items))
                         for c in s.conditions) or "—"
        get = " + ".join(f"{_q(r.qty)} × " + " / ".join(i.label for i in r.items)
                         for r in s.rewards) or "—"
        bills, benefit = used.get(s.id, (0, 0))
        rows.append([s.code, s.name, (s.scheme_type or "").replace("_", " "), _d(s.start_date),
                     _d(s.end_date), s.status(), " · ".join(p.label for p in s.places) or "everywhere",
                     buy, get, s.priority, bills, _m(benefit)])
    return _result(["Code", "Scheme", "Type", "From", "To", "Status", "Runs at", "Buy", "Get",
                    "Priority", "Bills in period", "Benefit in period"], rows,
                   {"Schemes": len(rows), "Benefit in period": _sum(rows, 11)},
                   "Every promotion scheme as it is set up, with how often it paid out in "
                   "the period.")


def margin_target(ctx):
    target = ctx.param("target", 30, float)
    agg = OrderedDict()
    for item, inv in ctx.lines().all():
        p = item.product
        cat = p.category.name if p and p.category else "Uncategorised"
        r = agg.setdefault(cat, [0.0, 0.0])
        r[0] += item.line_total or 0
        r[1] += (item.quantity or 0) * ((p.cost_price or 0) if p else 0)
    rows = []
    for cat, (sales, cost) in agg.items():
        m = sales - cost
        pct = _pct(m, sales)
        rows.append([cat, _m(sales), _m(cost), _m(m), pct, target, round(pct - target, 1),
                     "Met" if pct >= target else "Below"])
    rows.sort(key=lambda r: r[6])
    return _result(["Category", "Sales (taxable)", "Cost", "Margin", "Margin %", "Target %",
                    "Gap", "Result"], rows,
                   {"Below target": sum(1 for r in rows if r[7] == "Below"),
                    "Overall margin %": _pct(_sum(rows, 3), _sum(rows, 1))},
                   "Margin per category against the target entered above — the shop stores "
                   "no targets of its own. Cost is each product's current cost price.")


# ===========================================================================
#  CANCELLED BILLS · ADVANCES · THE DRAWER · COUPONS · STORE CREDIT
#  · WISHES · FEEDBACK · MESSAGES
#  Records kept by the screens added for them — see app/cancellation.py,
#  app/vouchers.py, app/drawer.py and app/messaging.py.
# ===========================================================================
def sales_cancelled(ctx):
    q = Invoice.query.filter(Invoice.payment_status == "cancelled",
                             day_from(Invoice.invoice_date, ctx.start),
                             day_to(Invoice.invoice_date, ctx.end))
    q = ctx._place(q, Invoice).options(selectinload(Invoice.items))
    rows = []
    for inv in q.order_by(Invoice.invoice_date, Invoice.id).all():
        rows.append([inv.invoice_number, _dt(inv.invoice_date), _dt(inv.cancelled_at),
                     _name(inv.cancelled_by), inv.cancel_reason or "—",
                     inv.customer.name if inv.customer else "Walk-in", _name(_served(inv)),
                     _q(sum(i.quantity or 0 for i in inv.items)), _m(inv.total),
                     (inv.payment_method or "").replace("_", " ").upper()])
    return _result(["Bill", "Billed", "Cancelled", "Cancelled by", "Reason", "Customer",
                    "Salesperson", "Qty", "Amount", "Paid by"], rows,
                   {"Bills": len(rows), "Qty": _qsum(rows, 7), "Amount": _sum(rows, 8)},
                   "Bills cancelled after they were raised. Their numbers stay in the series; "
                   "their stock, points, promotions and coupons were reversed, and they are "
                   "left out of every other sales, tax and settlement report.")


def customer_advance(ctx):
    q = CustomerAdvance.query.filter(day_from(CustomerAdvance.created_at, ctx.start),
                                     day_to(CustomerAdvance.created_at, ctx.end))
    rows = []
    for a in ctx._place(q, CustomerAdvance).order_by(CustomerAdvance.created_at).all():
        used = vouchers.advance_used(a)
        rows.append([a.number, _dt(a.created_at), a.customer.name if a.customer else "—",
                     (a.customer.phone if a.customer else "") or "—", (a.method or "").upper(),
                     a.reference or "—", _name(a.cashier), a.note or "—", _m(a.amount), used,
                     _m(a.refunded), vouchers.advance_balance(a)])
    return _result(["Advance", "Taken", "Customer", "Phone", "Paid by", "Reference", "Taken by",
                    "Note", "Amount", "Spent on bills", "Refunded", "Balance now"], rows,
                   {"Advances": len(rows), "Amount": _sum(rows, 8), "Spent on bills": _sum(rows, 9),
                    "Refunded": _sum(rows, 10), "Balance now": _sum(rows, 11)},
                   "Advances taken from customers in the period, with what has been spent from "
                   "each on bills (bills since cancelled do not count) and what was handed back.")


def opening_closing(ctx):
    q = DrawerSession.query.filter(day_from(DrawerSession.opened_at, ctx.start),
                                   day_to(DrawerSession.opened_at, ctx.end))
    rows = []
    for s in ctx._place(q, DrawerSession).order_by(DrawerSession.opened_at).all():
        expected = s.expected_cash if s.closed_at else drawer.breakdown(s)[0]
        diff = s.difference
        rows.append([s.counter.name if s.counter else "—", s.location.name if s.location else "—",
                     _dt(s.opened_at), _name(s.opened_by), _m(s.opening_float),
                     _dt(s.closed_at) if s.closed_at else "still open", _name(s.closed_by),
                     _m(expected), _m(s.counted_cash) if s.counted_cash is not None else "—",
                     diff if diff is not None else "—",
                     "—" if diff is None else "Tallies" if abs(diff) < 0.01
                     else "Over" if diff > 0 else "Short", s.notes or "—"])
    return _result(["Till", "Location", "Opened", "Opened by", "Float", "Closed", "Closed by",
                    "Expected cash", "Counted", "Difference", "Result", "Notes"], rows,
                   {"Sessions": len(rows), "Float": _sum(rows, 4), "Counted": _sum(rows, 8),
                    "Difference": _sum(rows, 9)},
                   "Each drawer from the float put in to the count taken out. Expected cash is "
                   "the float plus cash on bills and advances at that till, less cash refunds — "
                   "fixed when the drawer is closed; for a drawer still open it is as of now.")


def employee_advance(ctx):
    rows = []
    for a in (StaffAdvance.query.filter(StaffAdvance.given_on <= ctx.end)
              .order_by(StaffAdvance.given_on, StaffAdvance.id).all()):
        back = sum(r.amount or 0 for r in a.recoveries
                   if not r.recovered_on or r.recovered_on <= ctx.end)
        left = _m((a.amount or 0) - back)
        if left <= 0.009:
            continue
        u = a.user
        rows.append([_name(u), (u.staff_code if u else None) or "—", _d(a.given_on),
                     (a.method or "").upper(), a.note or "—", _m(a.amount), _m(back), left,
                     (ctx.end - a.given_on).days])
    return _result(["Staff", "Code", "Given on", "Paid by", "Note", "Advance", "Recovered",
                    "Pending", "Days pending"], rows,
                   {"Advances": len(rows), "Advance": _sum(rows, 5), "Pending": _sum(rows, 7)},
                   "Salary advances still not fully recovered as at the end date — the start "
                   "date does not apply.")


def _coupon_row_status(cp):
    return cp.state(date.today()).capitalize()


def settlement_coupon_issue(ctx):
    q = (db.session.query(Coupon, Invoice).join(Invoice, Invoice.id == Coupon.issued_invoice_id)
         .filter(Coupon.issued_via == "settlement",
                 day_from(Invoice.invoice_date, ctx.start),
                 day_to(Invoice.invoice_date, ctx.end)))
    rows = []
    for cp, inv in ctx._place(q, Invoice).order_by(Invoice.invoice_date, Coupon.id).all():
        rows.append([cp.code, cp.campaign.name, cp.campaign.describe, inv.invoice_number,
                     _d(inv.invoice_date), _m(inv.total),
                     inv.customer.name if inv.customer else "Walk-in", _d(cp.valid_to),
                     cp.uses, _coupon_row_status(cp)])
    return _result(["Coupon", "Campaign", "Offer", "Earned on bill", "Bill date", "Bill amount",
                    "Customer", "Valid to", "Times used", "Status"], rows,
                   {"Coupons": len(rows), "Used": sum(1 for r in rows if r[8])},
                   "Coupons the till handed out by itself on bills in the period. A coupon from "
                   "a bill later cancelled shows as withdrawn.")


def coupon_issue(ctx):
    rows = []
    for cp in (Coupon.query.filter(day_from(Coupon.created_at, ctx.start),
                                   day_to(Coupon.created_at, ctx.end))
               .order_by(Coupon.created_at, Coupon.id).all()):
        rows.append([cp.code, cp.campaign.name, cp.campaign.describe, _d(cp.created_at),
                     "At settlement" if cp.issued_via == "settlement" else "By hand",
                     cp.customer.name if cp.customer else "Anyone", _d(cp.valid_from),
                     _d(cp.valid_to), cp.uses_allowed if cp.uses_allowed is not None else "Unlimited",
                     cp.uses, _coupon_row_status(cp)])
    return _result(["Coupon", "Campaign", "Offer", "Issued", "How", "For", "Valid from",
                    "Valid to", "Uses allowed", "Times used", "Status"], rows,
                   {"Coupons": len(rows), "Used": sum(1 for r in rows if r[9])},
                   "Every coupon issued in the period, by hand or at settlement, and where it "
                   "stands today.")


def coupon_consumption(ctx):
    q = (db.session.query(CouponRedemption, Invoice)
         .join(Invoice, Invoice.id == CouponRedemption.invoice_id)
         .filter(CouponRedemption.voided_at.is_(None), Invoice.live(),
                 day_from(Invoice.invoice_date, ctx.start),
                 day_to(Invoice.invoice_date, ctx.end)))
    rows = []
    for r, inv in ctx._place(q, Invoice).order_by(Invoice.invoice_date, Invoice.id).all():
        cp = r.coupon
        rows.append([inv.invoice_number, _dt(inv.invoice_date),
                     inv.customer.name if inv.customer else "Walk-in", cp.code, cp.campaign.name,
                     _m((inv.subtotal or 0) + _tax(inv)), _m(r.amount), _m(inv.total)])
    return _result(["Bill", "Date", "Customer", "Coupon", "Campaign", "Bill before discounts",
                    "Coupon discount", "Bill total"], rows,
                   {"Redemptions": len(rows), "Coupon discount": _sum(rows, 6)},
                   "Coupons spent on bills in the period. A coupon comes off the bill as a "
                   "discount, so it is inside the Discount column of the sales reports too. "
                   "Bills since cancelled are left out, and their coupons can be used again.")


def gv_cn_consumption(ctx):
    q = (db.session.query(InvoicePayment, Invoice)
         .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
         .filter(InvoicePayment.method == "credit_note", Invoice.live(),
                 day_from(Invoice.invoice_date, ctx.start),
                 day_to(Invoice.invoice_date, ctx.end)))
    pairs = ctx._place(q, Invoice).order_by(Invoice.invoice_date, InvoicePayment.id).all()
    numbers = {p.reference for p, _ in pairs if p.reference}
    notes = ({n.number: n for n in CreditNote.query.filter(CreditNote.number.in_(numbers)).all()}
             if numbers else {})
    rows = []
    for pay, inv in pairs:
        n = notes.get(pay.reference)
        rows.append([inv.invoice_number, _dt(inv.invoice_date),
                     inv.customer.name if inv.customer else "Walk-in", pay.reference or "—",
                     _d(n.created_at) if n else "—", n.invoice.invoice_number if n else "—",
                     _m(n.total) if n else 0.0, _m(pay.amount),
                     vouchers.credit_note_balance(n) if n else 0.0])
    return _result(["Bill", "Date", "Customer", "Credit note", "Note raised", "Note against bill",
                    "Note value", "Used on this bill", "Left on note now"], rows,
                   {"Redemptions": len(rows), "Used": _sum(rows, 7)},
                   "Credit notes kept as store credit and spent against later bills. No gift "
                   "vouchers are issued, so this lists credit notes only.")


def _falls_in(day, start, end):
    """The date in [start, end] on `day`'s day and month (29 Feb → 28 Feb), or None."""
    for year in range(start.year, end.year + 1):
        try:
            d = date(year, day.month, day.day)
        except ValueError:
            d = date(year, 2, 28)
        if start <= d <= end:
            return d
    return None


def _wishes(ctx, kind):
    field = "dob" if kind == "birthday" else "anniversary"
    queued = {}
    for m in (ScheduledMessage.query.filter(ScheduledMessage.kind == kind,
                                            day_from(ScheduledMessage.scheduled_for, ctx.start),
                                            day_to(ScheduledMessage.scheduled_for, ctx.end))
              .order_by(ScheduledMessage.id).all()):
        queued[(m.customer_id, m.scheduled_for.date())] = m.status
    found = []
    for c in Customer.query.filter(getattr(Customer, field).isnot(None)).all():
        on = _falls_in(getattr(c, field), ctx.start, ctx.end)
        if on:
            found.append((on, c.name, c))
    found.sort(key=lambda t: (t[0], t[1]))
    return [[c.name, c.phone or "—", _d(getattr(c, field)), _d(on),
             on.year - getattr(c, field).year, (on - date.today()).days,
             (queued.get((c.id, on)) or "not queued").capitalize(), _m(c.total_spent)]
            for on, _, c in found]


def birthday(ctx):
    rows = _wishes(ctx, "birthday")
    return _result(["Customer", "Phone", "Date of birth", "Birthday", "Turning", "Days from today",
                    "Wish", "Lifetime spend"], rows,
                   {"Customers": len(rows), "No phone": sum(1 for r in rows if r[1] == "—")},
                   "Customers whose birthday falls in the period. 'Wish' is the message queued "
                   "for that day on the Messages screen, if any.")


def anniversary(ctx):
    rows = _wishes(ctx, "anniversary")
    return _result(["Customer", "Phone", "Anniversary date", "Anniversary", "Years",
                    "Days from today", "Wish", "Lifetime spend"], rows,
                   {"Customers": len(rows), "No phone": sum(1 for r in rows if r[1] == "—")},
                   "Customers whose wedding anniversary falls in the period. 'Wish' is the "
                   "message queued for that day on the Messages screen, if any.")


def feedback(ctx):
    rows = []
    for f in (CustomerFeedback.query.filter(day_from(CustomerFeedback.created_at, ctx.start),
                                            day_to(CustomerFeedback.created_at, ctx.end))
              .order_by(CustomerFeedback.created_at).all()):
        rows.append([_dt(f.created_at), f.customer.name if f.customer else "—",
                     (f.customer.phone if f.customer else "") or "—",
                     f.invoice.invoice_number if f.invoice else "—", f.rating,
                     "★" * (f.rating or 0), f.comments or "—",
                     "Bill QR" if f.source == "link" else "At the counter", _name(f.recorded_by)])
    ratings = [r[4] for r in rows]
    return _result(["When", "Customer", "Phone", "Bill", "Rating", "Stars", "Comments", "From",
                    "Recorded by"], rows,
                   {"Responses": len(rows),
                    "Average rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
                    "Low (1–2 stars)": sum(1 for x in ratings if x <= 2)},
                   "Ratings customers gave, typed in at the counter or sent from the QR code "
                   "on their bill.")


def scheduled_messages(ctx):
    rows = []
    for m in (ScheduledMessage.query.filter(day_from(ScheduledMessage.scheduled_for, ctx.start),
                                            day_to(ScheduledMessage.scheduled_for, ctx.end))
              .order_by(ScheduledMessage.scheduled_for, ScheduledMessage.id).all()):
        rows.append([_dt(m.scheduled_for), m.customer.name if m.customer else "—", m.phone or "—",
                     (m.kind or "").capitalize(), m.body, (m.status or "").capitalize(),
                     m.attempts or 0, _dt(m.sent_at), m.error or "—"])
    count = lambda s: sum(1 for r in rows if r[5].lower() == s)  # noqa: E731
    return _result(["Due", "Customer", "Phone", "Kind", "Message", "Status", "Attempts",
                    "Sent", "Note"], rows,
                   {"Messages": len(rows), "Sent": count("sent"), "Logged": count("logged"),
                    "Failed": count("failed"), "Queued": count("queued")},
                   "Messages due in the period and what became of each. 'Logged' means it came "
                   "due with no SMS/WhatsApp provider set up, so it was not sent.")


# ===========================================================================
#  the catalogue
# ===========================================================================
def _na(why, see=None):
    return {"unavailable": why, "see": see}


#: Report-specific knobs the page draws as extra inputs.
PARAMS = {
    "inactive_customers": [{"key": "days", "label": "No bill for", "type": "select",
                            "options": [(30, "30 days"), (60, "60 days"), (90, "90 days"),
                                        (180, "180 days"), (365, "1 year")], "default": 90}],
    "margin_target": [{"key": "target", "label": "Target margin %", "type": "number",
                       "default": 30}],
}

# (key, label, run-or-unavailable, flags)
# flags: dated (uses the period), places (honours company/branch/till), b2b
GROUPS = [
    ("sales", "Sales Reports", [
        ("sales_report", "Sales Report", sales_report, {}),
        ("unsettled_bills", "Unsettled Bill Report", unsettled_bills, {}),
        ("sales_salesman", "Sales Report - Salesman Wise", sales_salesman, {}),
        ("sales_salesman_detail", "Sales Report - Salesman Wise Detail", sales_salesman_detail, {}),
        ("sales_margin", "Sales Report - Margin", sales_margin, {}),
        ("sales_invoice_wise", "Sales Report - Invoice wise", sales_invoice_wise, {}),
        ("sales_barcode", "Sales Report - Barcode wise", sales_barcode, {}),
        ("sales_cancelled", "Sales Report - Cancelled", sales_cancelled, {}),
        ("day_summary", "Sales Report - Day Summary (Without OffSet Bill Configuration)",
         day_summary, {}),
        ("sales_age_wise", "Sales Report - Age Wise", sales_age_wise, {}),
        ("approved_bills", "Approved Bill Report", approved_bills, {"places": False}),
        ("estimate_bills", "Estimate Bill Report", estimate_bills, {"places": False}),
        ("delivery_report", "Delivery Report", delivery_report, {}),
        ("alteration_report", "Alteration Report",
         lambda ctx: _from_lib("alterations", ctx), {"places": False}),
        ("promotion_details", "Promotion Details Report",
         lambda ctx: _from_lib("promotions", ctx), {"places": False}),
        ("sales_snapshot", "Sales Snapshot", sales_snapshot, {}),
        ("text_day_summary", "Text Day Summary", text_day_summary, {}),
    ]),
    ("analysis", "Analysis Reports", [
        ("sales_analysis", "Sales Analysis Report", sales_analysis, {}),
        ("sales_vs_settlement", "Sales Vs Settlement Report", sales_vs_settlement, {}),
        ("sales_vs_stock", "Sales Vs Stock Report", sales_vs_stock, {}),
        ("sales_purchase_stock", "Sales Vs Purchase Vs Stock", sales_purchase_stock, {}),
    ]),
    ("stock", "Stock Reports", [
        ("stock_detail", "Stock Detail Report", stock_detail,
         {"dated": False, "places": "location"}),
        ("stock_shelf_period", "Stock Report With Shelf Period", stock_shelf_period,
         {"dated": False, "places": False}),
        ("stock_aging", "Stock Aging Detail Report", stock_aging,
         {"dated": False, "places": False}),
        ("direct_stock", "Direct Stock Report", direct_stock, {"places": False}),
        ("stock_marker", "Stock Marker Report",
         _na("The shop keeps no stock-marker record. Tell us what this report should show "
             "and it can be added."), {}),
        ("price_changer", "Price Changer Report", price_changer, {"places": False}),
        ("stock_audit", "Stock Audit Report", stock_audit, {"places": False}),
        ("stock_split", "Stock Split Report", stock_split, {"dated": False, "places": False}),
    ]),
    ("discount", "Discount Reports", [
        ("discount_biller", "Biller Wise Report", discount_biller, {}),
        ("discount_cashier", "Cashier Wise Report", discount_cashier, {}),
    ]),
    ("settlement", "Settlement Reports", [
        ("settlement_day_end", "Day End Settlement Summary", settlement_day_end, {}),
        ("settlement_cashier", "Cashier Wise Settlement Summary", settlement_cashier, {}),
        ("settlement_detail", "Settlement Detail Report", settlement_detail, {}),
        ("company_collection", "Company wise Sale Collection Report", company_collection, {}),
        ("credit_collection", "Credit Sale Collection Report", credit_collection, {}),
        ("customer_advance", "Customer Advance Collection Report", customer_advance, {}),
        ("settlement_reconciliation", "Settlement Reconciliation", settlement_reconciliation, {}),
        ("settlement_daywise", "Daywise Settlement", settlement_daywise, {}),
        ("opening_closing", "Opening/Closing Report", opening_closing, {}),
    ]),
    ("tax", "Tax Reports", [
        ("tax_summary", "Sales Tax Report (Summary)", tax_summary, {}),
        ("tax_column", "Sales Tax Report (Column wise)", tax_column, {}),
        ("tax_row", "Sales Tax Report (Row wise)", tax_row, {}),
        ("hsn", "Sales HSN Report", hsn_report, {}),
        ("tax_collection", "Sales Tax Splitup with Collection Report", tax_collection, {}),
        ("gstr", "GSTR Report", gstr_report, {}),
    ]),
    ("hr", "HR Reports", [
        ("incentive_section", "Incentive Report - Section", incentive_section, {}),
        ("incentive_employees", "Incentive Report - Employees", incentive_employees, {}),
        ("employee_detail", "Employee Detail Report", employee_detail, {"places": False}),
        ("employee_advance", "Employee Advance Pending Report", employee_advance,
         {"places": False}),
    ]),
    ("b2b", "Reports - B2B Vertical", [
        ("b2b_sales", "Sales Report", sales_invoice_wise, {"b2b": True}),
        ("b2b_tax_column", "Sales Tax Report (Column wise)", tax_column, {"b2b": True}),
        ("b2b_tax_row", "Sales Tax Report (Row wise)", tax_row, {"b2b": True}),
        ("b2b_hsn", "Sales HSN Report", hsn_report, {"b2b": True}),
    ]),
    ("customer", "Customer Reports", [
        ("credit_outstanding", "Credit Customer Outstanding Report", credit_outstanding, {}),
        ("cn_customer", "Customer Gift Voucher/Credit Note Report", cn_customer, {}),
        ("settlement_coupon_issue", "Settlement Coupon Issue Report", settlement_coupon_issue, {}),
        ("coupon_issue", "Coupon Issue Report", coupon_issue, {"places": False}),
        ("coupon_consumption", "Coupon Consumption Report", coupon_consumption, {}),
        ("gv_cn_consumption", "Gift Voucher/Credit Note Consumption Report", gv_cn_consumption, {}),
        ("loyalty_consumption", "Loyalty Reward Consumption Report", loyalty_consumption,
         {"places": False}),
        ("gift_issue", "Gift Issue Report", gift_issue, {"places": False}),
        ("birthday", "Birthday Wishes", birthday, {"places": False}),
        ("anniversary", "Anniversary Wishes", anniversary, {"places": False}),
        ("my_customers", "My Customers Report", my_customers, {"places": False}),
        ("feedback", "Customer Feedback Report", feedback, {"places": False}),
        ("inactive_customers", "Inactive Customer Report", inactive_customers,
         {"dated": False, "places": False}),
        ("scheduled_messages", "Scheduled Message Log", scheduled_messages, {"places": False}),
    ]),
    ("mobile", "Reports - Mobile Vertical", [
        ("scheme_details", "Scheme Details Report", scheme_details, {"places": False}),
    ]),
    ("addons", "Add-ons", [
        ("margin_target", "Product Margin Against Target", margin_target, {}),
    ]),
]

REPORTS = OrderedDict()
for _gkey, _glabel, _items in GROUPS:
    for _key, _label, _run, _flags in _items:
        spec = {"key": _key, "label": _label, "group": _gkey, "group_label": _glabel,
                "dated": _flags.get("dated", True), "places": _flags.get("places", True),
                "b2b": _flags.get("b2b", False), "params": PARAMS.get(_key, [])}
        if isinstance(_run, dict):
            spec.update(_run)
            spec["run"] = None
        else:
            spec.update({"run": _run, "unavailable": None, "see": None})
        REPORTS[_key] = spec


def catalogue():
    """The groups, in menu order, with their reports."""
    return [{"key": g, "label": label,
             "reports": [REPORTS[k] for k, *_ in items],
             "available": sum(1 for k, *_ in items if not REPORTS[k]["unavailable"])}
            for g, label, items in GROUPS]


def run(key, start, end, company=None, location=None, counter=None, params=None):
    spec = REPORTS[key]
    if spec["unavailable"]:
        return None
    places = spec["places"]
    ctx = Ctx(start, end,
              company=company if places is True else None,
              location=location if places in (True, "location") else None,
              counter=counter if places is True else None,
              params=params, b2b=spec["b2b"])
    out = spec["run"](ctx)
    out.setdefault("totals", {})
    out.setdefault("note", "")
    return out
