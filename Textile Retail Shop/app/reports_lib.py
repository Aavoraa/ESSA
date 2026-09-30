"""The shop's reports, as data rather than as pages.

Each report knows what it counts and returns the same shape —
{columns, rows, totals, note} — so one renderer draws all of them and a question
asked in English or Tamil can be answered by picking one and filling in its
dates. `note` is where a report says the thing that would otherwise be assumed
wrong: that returns are netted off the seller, that stock value is at cost, that
a walk-in has no customer to name.

Adding a report here makes it answerable by the ask bar automatically — the
router's list of keys is built from REPORTS, so the two cannot drift apart.
"""
from datetime import date, timedelta

from sqlalchemy import func

from app import db
from app.datefilter import day_from, day_to
from app.models import (Alteration, CreditNote, Category, Counter, Customer,
                        Invoice, InvoiceItem, Location, LoyaltyTxn, Product,
                        PromotionApplication, PromotionAudit, StockAudit,
                        StockAuditLine, User)


def _inv_in(start, end):
    return db.session.query(Invoice).filter(
        Invoice.live(), day_from(Invoice.invoice_date, start),
        day_to(Invoice.invoice_date, end))


def _money(v):
    return round(float(v or 0), 2)


# ---- the reports ----------------------------------------------------------

def sales_summary(start, end):
    rows, total, count = [], 0.0, 0
    for d, n, amt, tax in db.session.query(
            func.date(Invoice.invoice_date), func.count(Invoice.id),
            func.sum(Invoice.total), func.sum(Invoice.cgst + Invoice.sgst + Invoice.igst)
    ).filter(Invoice.live(), day_from(Invoice.invoice_date, start),
             day_to(Invoice.invoice_date, end)
             ).group_by(func.date(Invoice.invoice_date)).order_by(func.date(Invoice.invoice_date)).all():
        rows.append([d, n, _money(amt), _money(tax)])
        total += _money(amt)
        count += n
    return {"columns": ["Date", "Bills", "Sales", "Tax"], "rows": rows,
            "totals": {"Bills": count, "Sales": _money(total)},
            "note": "Every bill raised in the period, including any later returned."}


def sales_by_product(start, end):
    rows = db.session.query(
        Product.sku, Product.name, func.sum(InvoiceItem.quantity),
        func.sum(InvoiceItem.line_total + InvoiceItem.tax_amount)
    ).join(InvoiceItem, InvoiceItem.product_id == Product.id
           ).join(Invoice, Invoice.id == InvoiceItem.invoice_id
                  ).filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                           day_to(Invoice.invoice_date, end)
                           ).group_by(Product.id).order_by(func.sum(
                               InvoiceItem.line_total + InvoiceItem.tax_amount).desc()).all()
    out = [[s, n, round(float(q), 2), _money(r)] for s, n, q, r in rows]
    return {"columns": ["SKU", "Item", "Qty sold", "Revenue"], "rows": out,
            "totals": {"Revenue": _money(sum(r[3] for r in out))},
            "note": "Revenue includes tax. Returns are not deducted here."}


def sales_by_category(start, end):
    rows = db.session.query(
        Category.name, func.sum(InvoiceItem.quantity),
        func.sum(InvoiceItem.line_total + InvoiceItem.tax_amount)
    ).join(Product, Product.category_id == Category.id
           ).join(InvoiceItem, InvoiceItem.product_id == Product.id
                  ).join(Invoice, Invoice.id == InvoiceItem.invoice_id
                         ).filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                                  day_to(Invoice.invoice_date, end)
                                  ).group_by(Category.id).order_by(func.sum(
                                      InvoiceItem.line_total + InvoiceItem.tax_amount).desc()).all()
    out = [[c, round(float(q), 2), _money(r)] for c, q, r in rows]
    return {"columns": ["Category", "Qty sold", "Revenue"], "rows": out,
            "totals": {"Revenue": _money(sum(r[2] for r in out))},
            "note": "Categories are the warehouse's master codes."}


def sales_by_staff(start, end):
    out = []
    for u in User.query.order_by(User.full_name).all():
        served = db.or_(Invoice.staff_id == u.id,
                        db.and_(Invoice.staff_id.is_(None), Invoice.cashier_id == u.id))
        sold = db.session.query(func.coalesce(func.sum(Invoice.total), 0)).filter(
            served, Invoice.live(), day_from(Invoice.invoice_date, start),
            day_to(Invoice.invoice_date, end)).scalar() or 0
        back = db.session.query(func.coalesce(func.sum(CreditNote.total), 0)).join(
            Invoice, CreditNote.invoice_id == Invoice.id).filter(
            served, day_from(CreditNote.created_at, start),
            day_to(CreditNote.created_at, end)).scalar() or 0
        if not sold and not back:
            continue
        net = _money(sold - back)
        out.append([u.full_name, u.staff_code, _money(sold), _money(back), net,
                    _money(net * (u.commission_pct or 0) / 100.0)])
    out.sort(key=lambda r: r[4], reverse=True)
    return {"columns": ["Staff", "Code", "Sold", "Returned", "Net sales", "Commission"],
            "rows": out, "totals": {"Net sales": _money(sum(r[4] for r in out)),
                                    "Commission": _money(sum(r[5] for r in out))},
            "note": "Credited to whoever served the sale; returns come off that "
                    "same person, not whoever handled the refund."}


def sales_by_customer(start, end):
    rows = db.session.query(
        Customer.name, Customer.phone, func.count(Invoice.id), func.sum(Invoice.total)
    ).join(Invoice, Invoice.customer_id == Customer.id
           ).filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                    day_to(Invoice.invoice_date, end)
                    ).group_by(Customer.id).order_by(func.sum(Invoice.total).desc()).all()
    out = [[n, p or "—", c, _money(t)] for n, p, c, t in rows]
    walkins = _inv_in(start, end).filter(Invoice.customer_id.is_(None)).count()
    return {"columns": ["Customer", "Phone", "Bills", "Spent"], "rows": out,
            "totals": {"Spent": _money(sum(r[3] for r in out))},
            "note": f"{walkins} bill(s) were walk-ins with no customer attached, "
                    "so they are not in this list."}


def sales_by_payment(start, end):
    """What was taken, by tender.

    Summed from the settlement rows rather than grouped on the invoice's
    one-word label: a bill paid half in cash and half on a card is labelled
    "mixed", and grouping on that would drop its cash out of the cash line — so
    the drawer would stop reconciling to this report the day split payments
    arrived. `Invoice.settled` falls back to the single method for every bill
    raised before settlements were recorded.
    """
    totals, counts = {}, {}
    for inv in _inv_in(start, end).all():
        for method, amount in inv.settled.items():
            totals[method] = round(totals.get(method, 0.0) + amount, 2)
            counts[method] = counts.get(method, 0) + 1
    out = [[(m or "—").capitalize(), counts[m], totals[m]]
           for m in sorted(totals, key=lambda k: -totals[k])]
    return {"columns": ["Payment", "Bills", "Amount"],
            "rows": [[m, c, _money(t)] for m, c, t in out],
            "totals": {"Amount": _money(sum(r[2] for r in out))},
            "note": "How the money came in, before any refunds. A split bill "
                    "appears under each tender it used, so Bills can exceed the "
                    "number of sales."}


def gst_summary(start, end):
    invoices = _inv_in(start, end).all()
    rows = [["CGST", _money(sum(i.cgst for i in invoices))],
            ["SGST", _money(sum(i.sgst for i in invoices))],
            ["IGST", _money(sum(i.igst for i in invoices))]]
    taxable = _money(sum(i.subtotal - (i.discount or 0) for i in invoices))
    return {"columns": ["Head", "Amount"], "rows": rows,
            "totals": {"Taxable value": taxable,
                       "Tax": _money(sum(r[1] for r in rows))},
            "note": "Tax on bills raised. Credit notes reverse tax separately — "
                    "see the returns report."}


def invoice_list(start, end):
    out = []
    for i in _inv_in(start, end).order_by(Invoice.invoice_date.desc()).all():
        who = i.staff.full_name if i.staff else i.cashier.full_name
        out.append([i.invoice_number, i.invoice_date.strftime("%d %b %Y %H:%M"),
                    i.customer.name if i.customer else "Walk-in",
                    who, (i.payment_method or "").capitalize(), _money(i.total)])
    return {"columns": ["Bill", "When", "Customer", "Served by", "Payment", "Total"],
            "rows": out, "totals": {"Total": _money(sum(r[5] for r in out))},
            "note": "Every bill in the period, newest first."}


def returns_report(start, end):
    out = []
    for n in CreditNote.query.filter(day_from(CreditNote.created_at, start),
                                     day_to(CreditNote.created_at, end)
                                     ).order_by(CreditNote.id.desc()).all():
        out.append([n.number, n.invoice.invoice_number,
                    n.created_at.strftime("%d %b %Y"),
                    n.invoice.customer.name if n.invoice.customer else "Walk-in",
                    (n.refund_method or "").replace("_", " ").capitalize(),
                    n.reason or "—", _money(n.total)])
    return {"columns": ["Credit note", "Against", "When", "Customer", "Refund", "Reason", "Amount"],
            "rows": out, "totals": {"Refunded": _money(sum(r[6] for r in out))},
            "note": "Goods that came back. Stock was restored unless the line was "
                    "marked damaged."}


def alterations_report(start, end):
    out = []
    for a in Alteration.query.filter(day_from(Alteration.created_at, start),
                                     day_to(Alteration.created_at, end)
                                     ).order_by(Alteration.id.desc()).all():
        out.append([a.number, a.invoice.invoice_number,
                    a.created_at.strftime("%d %b %Y"),
                    a.tailor.name if a.tailor else "—",
                    a.promised_date.strftime("%d %b") if a.promised_date else "—",
                    "OVERDUE" if a.is_overdue else a.status, a.total_qty, _money(a.charge)])
    return {"columns": ["Job", "Against", "Taken in", "Tailor", "Promised", "Status", "Pieces", "Charge"],
            "rows": out, "totals": {"Charges": _money(sum(r[7] for r in out))},
            "note": "Alterations move no stock — the garment already belongs to "
                    "the customer."}


def low_stock(start, end):
    items = Product.query.filter(Product.stock_qty <= Product.reorder_level,
                                 Product.active.is_(True)).order_by(Product.stock_qty).all()
    out = [[p.sku, p.name, p.stock_qty, p.reorder_level, _money(p.selling_price)] for p in items]
    return {"columns": ["SKU", "Item", "In stock", "Reorder at", "Price"], "rows": out,
            "totals": {"Items": len(out)},
            "note": "Current position — not affected by the date range."}


def stock_on_hand(start, end):
    items = Product.query.filter(Product.active.is_(True)).order_by(Product.name).all()
    out = [[p.sku, p.name, p.category.name if p.category else "—", p.stock_qty,
            _money(p.cost_price), _money((p.stock_qty or 0) * (p.cost_price or 0))]
           for p in items]
    return {"columns": ["SKU", "Item", "Category", "Qty", "Cost", "Value"], "rows": out,
            "totals": {"Value": _money(sum(r[5] for r in out))},
            "note": "Shop stock only, valued at cost. The warehouse holds its own "
                    "separately. Not affected by the date range."}


def never_sold(start, end):
    sold = db.session.query(InvoiceItem.product_id).distinct()
    items = Product.query.filter(Product.active.is_(True),
                                 ~Product.id.in_(sold)).order_by(Product.name).all()
    out = [[p.sku, p.name, p.stock_qty, _money(p.selling_price)] for p in items]
    return {"columns": ["SKU", "Item", "In stock", "Price"], "rows": out,
            "totals": {"Items": len(out)},
            "note": "Never sold at all, ever — not just in the period."}


def loyalty_report(start, end):
    rows = db.session.query(
        Customer.name, func.sum(db.case((LoyaltyTxn.points > 0, LoyaltyTxn.points), else_=0)),
        func.sum(db.case((LoyaltyTxn.points < 0, -LoyaltyTxn.points), else_=0))
    ).join(LoyaltyTxn, LoyaltyTxn.customer_id == Customer.id
           ).filter(day_from(LoyaltyTxn.created_at, start),
                    day_to(LoyaltyTxn.created_at, end)
                    ).group_by(Customer.id).all()
    out = [[n, round(float(e or 0), 2), round(float(r or 0), 2)] for n, e, r in rows]
    held = _money(db.session.query(func.coalesce(func.sum(Customer.loyalty_points), 0)).scalar())
    return {"columns": ["Customer", "Earned", "Redeemed/reversed"], "rows": out,
            "totals": {"Points outstanding (all time)": held},
            "note": "Points reversed by a return appear under redeemed."}


def commission_report(start, end):
    r = sales_by_staff(start, end)
    return {"columns": r["columns"], "rows": r["rows"], "totals": r["totals"],
            "note": r["note"]}


# ---- promotions -----------------------------------------------------------
#
# Every one of these is dated by the BILL, not by when the promotion row was
# written. A credit note raised in April against a March bill reduces March's
# application, which is where the goods actually went out — filing the reduction
# under April would leave two months both wrong.

def _promo_apps(start, end):
    return (db.session.query(PromotionApplication)
            .join(Invoice, Invoice.id == PromotionApplication.invoice_id)
            .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                    day_to(Invoice.invoice_date, end)))


def _reward_lines(start, end):
    """Every free / discounted line billed in the period, with its scheme."""
    return (db.session.query(InvoiceItem, PromotionApplication)
            .join(PromotionApplication,
                  PromotionApplication.id == InvoiceItem.promo_application_id)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .filter(InvoiceItem.promo_role == "reward",
                    Invoice.live(), day_from(Invoice.invoice_date, start),
                    day_to(Invoice.invoice_date, end)))


def promotion_schemes(start, end):
    """Scheme performance — the register a manager asks for by name."""
    # What each scheme actually handed over, so the report can name the free
    # item rather than making somebody open a bill to find out.
    given = {}
    for item, app_row in _reward_lines(start, end).all():
        given.setdefault(app_row.scheme_code, {}).setdefault(
            item.product.name if item.product else "—", 0)
        given[app_row.scheme_code][item.product.name if item.product else "—"] += \
            item.quantity

    rows = (db.session.query(
        PromotionApplication.scheme_code, PromotionApplication.scheme_name,
        func.count(func.distinct(PromotionApplication.invoice_id)),
        func.sum(PromotionApplication.times_applied),
        func.sum(PromotionApplication.qualifying_qty),
        func.sum(PromotionApplication.reward_qty),
        func.sum(PromotionApplication.benefit_value))
        .join(Invoice, Invoice.id == PromotionApplication.invoice_id)
        .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                day_to(Invoice.invoice_date, end))
        .group_by(PromotionApplication.scheme_code,
                  PromotionApplication.scheme_name)
        .order_by(func.sum(PromotionApplication.benefit_value).desc()).all())

    out = []
    for code, name, bills, times, qual, free, value in rows:
        items = given.get(code) or {}
        top = sorted(items.items(), key=lambda kv: -kv[1])
        label = ", ".join(n for n, _ in top[:2]) or "—"
        if len(top) > 2:
            label += f" +{len(top) - 2}"
        out.append([code, name, bills, int(times or 0),
                    round(float(qual or 0), 2), round(float(free or 0), 2),
                    label, _money(value)])
    return {"columns": ["Code", "Scheme", "Bills", "Times applied",
                        "Qualifying qty", "Free qty", "Free item", "Benefit"],
            "rows": out,
            "totals": {"Free qty": round(sum(r[5] for r in out), 2),
                       "Benefit": _money(sum(r[7] for r in out))},
            "note": "Benefit is what the free goods would have sold for. A "
                    "return that breaks a promotion reduces the bill it was "
                    "earned on, so these figures net down rather than double-"
                    "counting a reward the customer no longer qualifies for."}


def promotion_items(start, end):
    """Stock issued as free items — product by product.

    The one promotion report that is also a STOCK report: these garments left
    the building and nobody paid for them, so this is what a stock question ends
    up asking of promotions.
    """
    agg = {}
    for item, app_row in _reward_lines(start, end).all():
        p = item.product
        key = (p.sku if p else "—", p.name if p else "—", app_row.scheme_name)
        row = agg.setdefault(key, [0.0, 0.0])
        row[0] += item.quantity or 0
        row[1] += item.promo_value or 0
    out = [[sku, name, scheme, round(qty, 2), _money(value)]
           for (sku, name, scheme), (qty, value) in
           sorted(agg.items(), key=lambda kv: -kv[1][1])]
    return {"columns": ["SKU", "Item", "Scheme", "Free qty", "Value given"],
            "rows": out,
            "totals": {"Free qty": round(sum(r[3] for r in out), 2),
                       "Value given": _money(sum(r[4] for r in out))},
            "note": "Every line billed under a promotion, at what it would "
                    "otherwise have sold for. Each one moved stock — look for "
                    "them in the ledger under “promo”."}


def promotion_places(start, end):
    """Store-wise and POS-wise performance, in one table.

    Both in one because they are the same question at two depths, and a shop
    with one till at each branch would otherwise get the identical report twice.
    """
    # Started from the applications explicitly. Selecting Location.name first and
    # letting SQLAlchemy infer the FROM would hang the joins off `locations`,
    # which is not what any of the join conditions are about — and the answer
    # would be a cross product that looks plausible until a second branch exists.
    rows = (db.session.query(
        Location.name, Counter.name,
        func.count(func.distinct(PromotionApplication.invoice_id)),
        func.sum(PromotionApplication.times_applied),
        func.sum(PromotionApplication.reward_qty),
        func.sum(PromotionApplication.benefit_value))
        .select_from(PromotionApplication)
        .join(Invoice, Invoice.id == PromotionApplication.invoice_id)
        .outerjoin(Location, Location.id == Invoice.location_id)
        .outerjoin(Counter, Counter.id == Invoice.counter_id)
        .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                day_to(Invoice.invoice_date, end))
        .group_by(Location.name, Counter.name)
        .order_by(func.sum(PromotionApplication.benefit_value).desc()).all())
    out = [[loc or "—", till or "—", bills, int(times or 0),
            round(float(free or 0), 2), _money(value)]
           for loc, till, bills, times, free, value in rows]
    return {"columns": ["Store", "Counter", "Bills", "Times applied",
                        "Free qty", "Benefit"], "rows": out,
            "totals": {"Benefit": _money(sum(r[5] for r in out))},
            "note": "A dash means the bill was raised before the till recorded "
                    "which branch and counter it came from."}


def promotion_daily(start, end):
    """Date-wise promotion performance."""
    rows = (db.session.query(
        func.date(Invoice.invoice_date),
        func.count(func.distinct(PromotionApplication.invoice_id)),
        func.sum(PromotionApplication.times_applied),
        func.sum(PromotionApplication.qualifying_qty),
        func.sum(PromotionApplication.reward_qty),
        func.sum(PromotionApplication.benefit_value))
        .join(PromotionApplication,
              PromotionApplication.invoice_id == Invoice.id)
        .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                day_to(Invoice.invoice_date, end))
        .group_by(func.date(Invoice.invoice_date))
        .order_by(func.date(Invoice.invoice_date)).all())
    out = [[d, bills, int(times or 0), round(float(qual or 0), 2),
            round(float(free or 0), 2), _money(value)]
           for d, bills, times, qual, free, value in rows]
    return {"columns": ["Date", "Bills", "Times applied", "Qualifying qty",
                        "Free qty", "Benefit"], "rows": out,
            "totals": {"Free qty": round(sum(r[4] for r in out), 2),
                       "Benefit": _money(sum(r[5] for r in out))},
            "note": "Days with no promotion on any bill are left out rather "
                    "than shown as zero."}


def stock_audits(start, end):
    """Physical counts taken in the period, and what each one found.

    Dated by when the count STARTED — that is when the shelf was looked at, and
    an audit approved a fortnight later still describes the day it was walked.
    """
    rows = (StockAudit.query
            .filter(day_from(StockAudit.started_at, start),
                    day_to(StockAudit.started_at, end))
            .order_by(StockAudit.id.desc()).all())
    out = []
    for a in rows:
        s = a.summary
        out.append([a.number,
                    a.floor.name if a.floor else "—",
                    a.location.name if a.location else "—",
                    a.started_at.strftime("%d-%m-%Y"),
                    a.started_by.full_name if a.started_by else "—",
                    f"{s['counted']}/{s['products']}",
                    s["system_qty"], s["physical_qty"],
                    s["shortage"], s["excess"], s["matched"],
                    a.status.replace("_", " "),
                    a.adjusted_at.strftime("%d-%m-%Y") if a.adjusted_at else "—"])
    return {"columns": ["Audit", "Floor", "Store", "Started", "By",
                        "Counted", "System qty", "Physical qty",
                        "Shortage", "Excess", "Matched", "Status", "Applied"],
            "rows": out,
            "totals": {"Shortage": round(sum(r[8] for r in out), 3),
                       "Excess": round(sum(r[9] for r in out), 3)},
            "note": "A count changes no stock on its own. “Applied” is the date "
                    "an approved count corrected the books — a blank there means "
                    "the variance was found and the figures were left alone."}


def stock_audit_lines(start, end):
    """Every counted line, product by product — the detail behind the summary."""
    rows = (db.session.query(StockAuditLine, StockAudit)
            .join(StockAudit, StockAudit.id == StockAuditLine.audit_id)
            .filter(day_from(StockAudit.started_at, start),
                    day_to(StockAudit.started_at, end),
                    StockAuditLine.physical_qty.isnot(None))
            .order_by(StockAudit.id.desc(), StockAuditLine.id).all())
    out = []
    for line, audit in rows:
        p = line.product
        out.append([audit.number, audit.floor.name if audit.floor else "—",
                    line.sku, line.name,
                    p.category.name if p and p.category else "—",
                    " · ".join(x for x in ((p.size if p else None),
                                           (p.color if p else None),
                                           (p.fabric if p else None)) if x) or "—",
                    line.purchase_qty, line.sales_qty, line.system_qty,
                    line.physical_qty, line.difference,
                    line.line_status.replace("_", " "),
                    _money(line.value_variance)])
    return {"columns": ["Audit", "Floor", "SKU", "Item", "Category",
                        "Attributes", "Purchase", "Sales", "System",
                        "Physical", "Diff", "Status", "Value"],
            "rows": out,
            "totals": {"Lines": len(out),
                       "Value of the gap": _money(sum(r[12] for r in out))},
            "note": "Only lines somebody actually counted. Purchase − Sales is "
                    "the system figure: that is how the books got there, off the "
                    "movement ledger. The gap is priced at the selling price."}


def promotion_audit(start, end):
    """Every promotion event, including the ones that gave nothing away.

    The refusals are the point. A scheme that qualifies on twenty bills and
    hands over nothing because its reward has been out of stock all week looks
    like a scheme nobody is using, until this report says otherwise.
    """
    rows = (PromotionAudit.query
            .filter(day_from(PromotionAudit.created_at, start),
                    day_to(PromotionAudit.created_at, end))
            .order_by(PromotionAudit.id.desc()).limit(1000).all())
    labels = {"applied": "Applied", "blocked_no_stock": "BLOCKED — no stock",
              "substituted": "Substituted", "reduced": "Reduced by a return",
              "reversed": "Reversed by a return"}
    out = [[a.created_at.strftime("%d-%m-%Y %H:%M"), a.scheme_code or "—",
            a.scheme_name or "—",
            a.invoice.invoice_number if a.invoice else "—",
            labels.get(a.event, a.event),
            a.location.name if a.location else "—",
            a.user.full_name if a.user else "—",
            round(float(a.reward_qty or 0), 2), _money(a.benefit_value)]
           for a in rows]
    blocked = sum(1 for a in rows if a.event == "blocked_no_stock")
    return {"columns": ["When", "Code", "Scheme", "Bill", "Event", "Store",
                        "By", "Free qty", "Benefit"], "rows": out,
            "totals": {"Events": len(out), "Refused for want of stock": blocked},
            "note": "Dated by when the event happened, not by the bill — a "
                    "return in April reversing a March promotion belongs to "
                    "April here. Capped at the most recent 1,000 events."}


REPORTS = {
    "sales_summary": {
        "label": "Sales summary", "run": sales_summary, "dated": True,
        "blurb": "Day-by-day bills, sales and tax",
        "keywords": ["sales", "summary", "total", "revenue", "turnover", "how much",
                     "daily", "விற்பனை", "மொத்தம்", "வருமானம்"]},
    "sales_by_product": {
        "label": "Sales by item", "run": sales_by_product, "dated": True,
        "blurb": "What sold, how many and for how much",
        # Topic words only. "which item" was here once and beat "running low" on
        # a count of matches, sending a low-stock question to sales — a question's
        # phrasing is not evidence of its subject.
        "keywords": ["product", "item", "best seller", "top selling", "best selling",
                     "பொருள்", "விற்ற"]},
    "sales_by_category": {
        "label": "Sales by category", "run": sales_by_category, "dated": True,
        "blurb": "Revenue per category code",
        "keywords": ["category", "categories", "department", "வகை"]},
    "sales_by_staff": {
        "label": "Sales by staff", "run": sales_by_staff, "dated": True,
        "blurb": "Who sold what, net of returns, with commission",
        "keywords": ["staff", "salesperson", "who sold", "employee", "cashier",
                     "ஊழியர்", "விற்பனையாளர்"]},
    "commission": {
        "label": "Staff commission", "run": commission_report, "dated": True,
        "blurb": "Commission earned per staff member",
        "keywords": ["commission", "incentive", "கமிஷன்"]},
    "sales_by_customer": {
        "label": "Sales by customer", "run": sales_by_customer, "dated": True,
        "blurb": "Who bought, how often and how much",
        "keywords": ["customer", "buyer", "client", "வாடிக்கையாளர்"]},
    "sales_by_payment": {
        "label": "Payment breakdown", "run": sales_by_payment, "dated": True,
        "blurb": "Cash, card and UPI split",
        "keywords": ["payment", "cash", "card", "upi", "paid", "பணம்"]},
    "gst_summary": {
        "label": "GST summary", "run": gst_summary, "dated": True,
        "blurb": "CGST, SGST and IGST on bills raised",
        "keywords": ["gst", "tax", "cgst", "sgst", "igst", "வரி"]},
    "invoice_list": {
        "label": "Bill register", "run": invoice_list, "dated": True,
        "blurb": "Every bill raised, with who served it",
        "keywords": ["invoice", "bill", "register", "list of bills", "ரசீது", "பில்"]},
    "returns": {
        "label": "Returns", "run": returns_report, "dated": True,
        "blurb": "Credit notes raised and money refunded",
        "keywords": ["return", "refund", "credit note", "returned", "திரும்ப", "ரிட்டர்ன்"]},
    "alterations": {
        "label": "Alterations", "run": alterations_report, "dated": True,
        "blurb": "Tailoring jobs, tailors and what is overdue",
        "keywords": ["alteration", "tailor", "stitching", "overdue", "தையல்", "ஆல்டரேஷன்"]},
    "low_stock": {
        "label": "Low stock", "run": low_stock, "dated": False,
        "blurb": "At or below reorder level right now",
        "keywords": ["low stock", "running low", "reorder", "running out",
                     "out of stock", "restock", "short", "இருப்பு குறைவு",
                     "குறைவான"]},
    "stock_on_hand": {
        "label": "Stock on hand", "run": stock_on_hand, "dated": False,
        "blurb": "Everything the shop holds, valued at cost",
        "keywords": ["stock", "inventory", "on hand", "holding", "stock value",
                     "இருப்பு", "சரக்கு"]},
    "never_sold": {
        "label": "Never sold", "run": never_sold, "dated": False,
        "blurb": "Items that have never sold at all",
        "keywords": ["never sold", "dead stock", "not selling", "slow moving"]},
    "loyalty": {
        "label": "Loyalty points", "run": loyalty_report, "dated": True,
        "blurb": "Points earned and redeemed per customer",
        # "reward" was here and had to go: it is the word the promotion reports
        # are about, and a question asking what the shop gave away was being
        # answered with a table of loyalty points.
        "keywords": ["loyalty", "points", "புள்ளி"]},
    "promotions": {
        "label": "Promotion schemes", "run": promotion_schemes, "dated": True,
        "blurb": "Each offer: bills, qualifying qty, free qty and what it cost",
        "keywords": ["promotion", "scheme", "offer", "buy get", "free item",
                     "சலுகை", "இலவசம்"]},
    "promotion_items": {
        "label": "Free items issued", "run": promotion_items, "dated": True,
        "blurb": "Stock given away under a promotion, product by product",
        "keywords": ["free items", "free goods", "giveaway", "gift",
                     "stock issued free", "இலவச பொருள்"]},
    "promotion_places": {
        # Two-word keys, and every one of them carries "promotion" or "offer".
        # A bare "store" here would win every question that mentions a branch,
        # including the ones about sales — the router scores on how many keywords
        # a question contains, so a keyword broader than its report is how a
        # question ends up at the wrong table.
        "label": "Promotions by store", "run": promotion_places, "dated": True,
        "blurb": "Which branch and counter the offers ran at",
        "keywords": ["promotion store", "promotion branch", "offer store",
                     "promotion counter", "promotion pos", "promotions by store",
                     "promotion store wise", "offers by store",
                     "promotion by branch", "offer branch"]},
    "promotion_daily": {
        "label": "Promotions by date", "run": promotion_daily, "dated": True,
        "blurb": "Day by day: free items given and what they were worth",
        "keywords": ["promotion daily", "offer daily", "promotion by date",
                     "promotions by date", "promotion date wise",
                     "promotion trend", "offers by date"]},
    "stock_audits": {
        "label": "Physical stock audits", "run": stock_audits, "dated": True,
        "blurb": "Counts taken per floor, with shortage and excess",
        "keywords": ["stock audit", "physical stock", "physical count",
                     "stock count", "audit floor", "floor count",
                     "இருப்பு சரிபார்ப்பு"]},
    "stock_audit_lines": {
        "label": "Stock audit detail", "run": stock_audit_lines, "dated": True,
        "blurb": "Every counted item: purchase, sales, system, physical, gap",
        "keywords": ["audit detail", "count detail", "stock variance",
                     "shortage detail", "excess detail", "stock difference"]},
    "promotion_audit": {
        "label": "Promotion audit", "run": promotion_audit, "dated": True,
        "blurb": "Every promotion event, including offers refused for no stock",
        "keywords": ["promotion audit", "promotion history", "why no free",
                     "promotion blocked", "offer refused"]},
}


def run(key, start, end):
    """Run one report. Undated ones ignore the range but still receive it."""
    spec = REPORTS[key]
    out = spec["run"](start, end)
    out["key"] = key
    out["label"] = spec["label"]
    out["dated"] = spec["dated"]
    return out


def catalogue():
    return [{"key": k, "label": v["label"], "blurb": v["blurb"], "dated": v["dated"]}
            for k, v in REPORTS.items()]
