import csv
import io

from flask import Blueprint, Response, abort, render_template, request, jsonify
from flask_login import login_required
from datetime import datetime, date, timedelta
from sqlalchemy import func
from app import db
from app.paging import paged
from app.datefilter import day_from, day_to
from app.models import (Company, Counter, Customer, Invoice, InvoiceItem,
                        Location, Product)
from app.utils import role_required
from app import nlq, reports_lib, retail_reports

reports_bp = Blueprint("reports", __name__)


def parse_places():
    """The company / branch / till this report is being asked about.

    Every figure on the page is filtered by the same three, so a page that says
    "Tirupur" says it about the total, the trend, the top products and the GST
    alike. A report where the heading and the numbers disagree about what is
    being counted is worse than no filter at all.

    Returns (filters, chosen) — the SQL conditions, and the rows for the form.
    """
    ids = {}
    for key in ("company", "location", "counter"):
        raw = request.args.get(key, "")
        ids[key] = int(raw) if str(raw).strip().isdigit() else None
    conds = []
    if ids["company"]:
        conds.append(Invoice.company_id == ids["company"])
    if ids["location"]:
        conds.append(Invoice.location_id == ids["location"])
    if ids["counter"]:
        conds.append(Invoice.counter_id == ids["counter"])
    return conds, ids


def place_lists():
    """What the report's three dropdowns offer."""
    return {
        "companies": Company.query.order_by(Company.name).all(),
        "locations": Location.query.order_by(Location.name).all(),
        "counters": (db.session.query(Counter, Location.name)
                     .join(Location, Location.id == Counter.location_id)
                     .order_by(Location.name, Counter.name).all()),
    }


def parse_range():
    end = date.today()
    start = end - timedelta(days=30)
    if request.args.get("from"):
        try:
            start = datetime.strptime(request.args["from"], "%Y-%m-%d").date()
        except ValueError:
            pass
    if request.args.get("to"):
        try:
            end = datetime.strptime(request.args["to"], "%Y-%m-%d").date()
        except ValueError:
            pass
    return start, end


@reports_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    start, end = parse_range()
    where, chosen = parse_places()
    q = db.session.query(Invoice).filter(
        Invoice.live(), day_from(Invoice.invoice_date, start),
        day_to(Invoice.invoice_date, end),
        *where,
    )
    invoices = q.all()

    total_sales = sum(i.total for i in invoices)
    total_tax = sum(i.cgst + i.sgst + i.igst for i in invoices)
    total_discount = sum(i.discount for i in invoices)
    invoice_count = len(invoices)
    avg_bill = total_sales / invoice_count if invoice_count else 0

    # Daily trend
    trend_map = {}
    d = start
    while d <= end:
        trend_map[d.isoformat()] = 0.0
        d += timedelta(days=1)
    for inv in invoices:
        key = inv.invoice_date.date().isoformat()
        if key in trend_map:
            trend_map[key] += inv.total
    # The map is keyed ISO because that is what sorts as a calendar does; the
    # axis is read by a person, so it is labelled the way the shop writes a date.
    trend_labels = [d.strftime("%d-%m-%Y") for d in
                    (date.fromisoformat(k) for k in trend_map)]
    trend_values = [round(v, 2) for v in trend_map.values()]

    # Top products
    top = db.session.query(
        Product.name,
        func.sum(InvoiceItem.quantity).label("qty"),
        func.sum(InvoiceItem.line_total + InvoiceItem.tax_amount).label("revenue")
    ).join(InvoiceItem, InvoiceItem.product_id == Product.id
    ).join(Invoice, Invoice.id == InvoiceItem.invoice_id
    ).filter(
        Invoice.live(), day_from(Invoice.invoice_date, start),
        day_to(Invoice.invoice_date, end),
        *where,
    ).group_by(Product.id).order_by(func.sum(InvoiceItem.line_total + InvoiceItem.tax_amount).desc()).limit(10).all()

    # Payment breakdown, by TENDER rather than by the invoice's one-word label.
    #
    # Grouping on Invoice.payment_method was right while a bill had exactly one.
    # It is not any more: a bill settled half on a card is labelled "mixed", and
    # that put its whole value in a bucket of its own — the cash line understated
    # by the cash actually taken, and the drawer no longer reconciling to the
    # report. So the split comes off the settlement rows, and `Invoice.settled`
    # falls back to the single method for every bill raised before them.
    pay_totals, pay_counts = {}, {}
    for inv in invoices:
        for method, amount in inv.settled.items():
            pay_totals[method] = round(pay_totals.get(method, 0.0) + amount, 2)
            # Counted as a bill this tender appeared on. A split bill therefore
            # counts once under cash and once under card, which is the honest
            # answer to "how many sales took a card" — not to "how many sales",
            # which is `invoice_count` above.
            pay_counts[method] = pay_counts.get(method, 0) + 1
    pay_rows = [(m, pay_counts[m], pay_totals[m])
                for m in sorted(pay_totals, key=lambda k: -pay_totals[k])]

    # GST summary
    gst_summary = {
        "cgst": sum(i.cgst for i in invoices),
        "sgst": sum(i.sgst for i in invoices),
        "igst": sum(i.igst for i in invoices),
    }

    # Top customers
    top_customers = db.session.query(
        Customer.name, func.count(Invoice.id), func.sum(Invoice.total)
    ).join(Invoice, Invoice.customer_id == Customer.id
    ).filter(
        Invoice.live(), day_from(Invoice.invoice_date, start),
        day_to(Invoice.invoice_date, end),
        *where,
    ).group_by(Customer.id).order_by(func.sum(Invoice.total).desc()).limit(10).all()

    # Where the day's money came from. Grouped in the database rather than in
    # Python because a year of bills is a lot of rows to carry up here to add up,
    # and this is the query somebody runs every evening.
    def by(label_col, join_model, join_on):
        return (db.session.query(label_col,
                                 func.count(Invoice.id),
                                 func.coalesce(func.sum(Invoice.total), 0))
                .join(join_model, join_on)
                .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                        day_to(Invoice.invoice_date, end), *where)
                .group_by(label_col)
                .order_by(func.coalesce(func.sum(Invoice.total), 0).desc()).all())

    by_location = by(Location.name, Location, Location.id == Invoice.location_id)
    by_counter = by(Counter.name, Counter, Counter.id == Invoice.counter_id)
    by_company = by(Company.name, Company, Company.id == Invoice.company_id)

    # …and the same thing a day at a time, which is what "daily sales, location
    # wise" means when somebody says it: one row per branch per day.
    daily_places = (db.session.query(func.date(Invoice.invoice_date),
                                     Location.name,
                                     func.count(Invoice.id),
                                     func.coalesce(func.sum(Invoice.total), 0))
                    .join(Location, Location.id == Invoice.location_id)
                    .filter(Invoice.live(), day_from(Invoice.invoice_date, start),
                            day_to(Invoice.invoice_date, end), *where)
                    .group_by(func.date(Invoice.invoice_date), Location.name)
                    .order_by(func.date(Invoice.invoice_date).desc(),
                              func.coalesce(func.sum(Invoice.total), 0).desc()).all())

    # Bills raised before any of this existed carry no branch, so they appear in
    # none of the three splits above. Said out loud rather than left as a gap
    # between two totals that do not match.
    unplaced = sum(1 for i in invoices if not i.location_id)

    # Two views of one page: the report catalogue, which is what somebody opens
    # Reports for, and the sales overview beneath it. Both are drawn every time
    # and one is shown; a filter applied on the overview comes back to the
    # overview, so pressing Apply never throws somebody back to the catalogue.
    filtered = any(request.args.get(k) for k in ("from", "to", "company", "location", "counter"))
    view = request.args.get("view") or ("overview" if filtered else "all")

    return render_template(
        "reports/index.html",
        view=view, groups=retail_reports.catalogue(),
        start=start, end=end,
        chosen=chosen, places=place_lists(),
        by_location=by_location, by_counter=by_counter, by_company=by_company,
        daily_places=daily_places, unplaced=unplaced,
        total_sales=total_sales, total_tax=total_tax, total_discount=total_discount,
        invoice_count=invoice_count, avg_bill=avg_bill,
        trend_labels=trend_labels, trend_values=trend_values,
        top_products=top, pay_rows=pay_rows,
        gst_summary=gst_summary, top_customers=top_customers,
    )


@reports_bp.route("/r/<key>")
@login_required
@role_required("admin", "manager")
def report(key):
    """One report from the catalogue: its filters, its table, and its export.

    `?export=csv` hands back the same rows as a spreadsheet — the figures a
    manager downloads must be the figures on the screen, so both come from one
    run of the one function.
    """
    spec = retail_reports.REPORTS.get(key)
    if not spec:
        abort(404)
    start, end = parse_range()
    _, chosen = parse_places()
    params = {p["key"]: request.args.get(p["key"], p["default"]) for p in spec["params"]}
    result = None
    if not spec["unavailable"]:
        result = retail_reports.run(key, start, end, chosen["company"], chosen["location"],
                                    chosen["counter"], params)

    if request.args.get("export") == "csv" and result is not None:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([spec["group_label"], spec["label"]])
        if spec["dated"]:
            w.writerow(["From", start.strftime("%d-%m-%Y"), "To", end.strftime("%d-%m-%Y")])
        w.writerow([])
        w.writerow(result["columns"])
        w.writerows(result["rows"])
        if result["totals"]:
            w.writerow([])
            for k, v in result["totals"].items():
                w.writerow([k, v])
        if result["note"]:
            w.writerow([])
            w.writerow([result["note"]])
        name = f"{key}_{start.isoformat()}_{end.isoformat()}.csv" if spec["dated"] else f"{key}.csv"
        # The byte-order mark is what makes Excel read the rupee sign and Tamil
        # names as UTF-8 rather than as mojibake.
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    numeric = set()
    if result:
        for i in range(len(result["columns"])):
            if any(isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
                   for r in result["rows"] if i < len(r)):
                numeric.add(i)
    return render_template(
        "reports/view.html", spec=spec, result=result, numeric=numeric,
        start=start, end=end, chosen=chosen, places=place_lists(), params=params,
        see=retail_reports.REPORTS.get(spec.get("see")) if spec.get("see") else None)


@reports_bp.route("/low-stock")
@login_required
@role_required("admin", "manager")
def low_stock():
    # a page at a time — after the migration nearly the whole catalogue sits at
    # or under its reorder level (411K rows, 114 MB of page)
    page = paged(Product.query.filter(
        Product.stock_qty <= Product.reorder_level, Product.active == True
    ).order_by(Product.stock_qty, Product.id))
    return render_template("reports/low_stock.html", items=page.items, page=page)


# ---------- Ask a question ----------
#
# The question is routed to one of the reports in reports_lib rather than turned
# into a query — see app/nlq.py for why. The answer comes back in the same
# {columns, rows, totals, note} shape every report uses, so one renderer draws it.
@reports_bp.route("/ask", methods=["POST"])
@login_required
@role_required("admin", "manager")
def ask():
    data = request.get_json(silent=True) or {}
    return jsonify(nlq.ask(data.get("q", "")))


@reports_bp.route("/catalogue")
@login_required
@role_required("admin", "manager")
def catalogue():
    """What can be asked about at all — used to show suggestions."""
    return jsonify({"engine": "model" if nlq.available() else "keywords",
                    "reports": reports_lib.catalogue(),
                    "examples": nlq.EXAMPLES})
