from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import current_user, login_required
from sqlalchemy import func

from app import db, messaging, places, vouchers
from app.paging import paged
from app.models import (Customer, CustomerAdvance, CustomerFeedback, Invoice,
                        LoyaltyTxn, MESSAGE_KINDS, PAYMENT_METHODS, ScheduledMessage)
from app.utils import generate_number, role_required

customers_bp = Blueprint("customers", __name__)


def _date(name):
    """A date field from the form, or None when it is blank or not a date."""
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _fill(c):
    c.name = request.form["name"].strip()
    c.phone = request.form.get("phone", "").strip()
    c.email = request.form.get("email", "").strip()
    c.address = request.form.get("address", "")
    c.gstin = request.form.get("gstin", "").strip()
    c.state_code = request.form.get("state_code", "33").strip()
    c.dob = _date("dob")
    c.anniversary = _date("anniversary")


@customers_bp.route("/")
@login_required
def list_customers():
    q = request.args.get("q", "").strip()
    query = Customer.query
    if q:
        query = query.filter(
            (Customer.name.ilike(f"%{q}%")) | (Customer.phone.ilike(f"%{q}%")) | (Customer.email.ilike(f"%{q}%"))
        )
    page = paged(query.order_by(Customer.name, Customer.id))     # 150K customers
    return render_template("customers/list.html", customers=page.items, page=page, q=q)


@customers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_customer():
    if request.method == "POST":
        c = Customer()
        _fill(c)
        db.session.add(c)
        db.session.commit()
        flash("Customer added.", "success")
        return redirect(url_for("customers.list_customers"))
    return render_template("customers/form.html", customer=None)


@customers_bp.route("/<int:cid>")
@login_required
def view_customer(cid):
    c = Customer.query.get_or_404(cid)
    invoices = Invoice.query.filter_by(customer_id=cid).order_by(Invoice.invoice_date.desc()).all()
    txns = LoyaltyTxn.query.filter_by(customer_id=cid).order_by(LoyaltyTxn.created_at.desc()).all()
    advances = [(a, vouchers.advance_balance(a))
                for a in CustomerAdvance.query.filter_by(customer_id=cid)
                .order_by(CustomerAdvance.created_at.desc()).all()]
    feedback = (CustomerFeedback.query.filter_by(customer_id=cid)
                .order_by(CustomerFeedback.created_at.desc()).limit(20).all())
    return render_template("customers/detail.html", customer=c, invoices=invoices, txns=txns,
                           advances=advances, advance_balance=round(sum(b for _, b in advances), 2),
                           feedback=feedback, methods=PAYMENT_METHODS)


@customers_bp.route("/<int:cid>/card")
@login_required
def customer_card(cid):
    """Printable membership card with a scannable barcode for the POS."""
    c = Customer.query.get_or_404(cid)
    return render_template("customers/card.html", customer=c)


@customers_bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(cid):
    c = Customer.query.get_or_404(cid)
    if request.method == "POST":
        _fill(c)
        db.session.commit()
        flash("Customer updated.", "success")
        return redirect(url_for("customers.view_customer", cid=cid))
    return render_template("customers/form.html", customer=c)


# ---------------------------------------------------------------------------
#  advances — money left with the shop before a bill exists
# ---------------------------------------------------------------------------
def _place():
    return places.resolve(*(session.get(k) for k in
                            ("company_id", "location_id", "floor_id", "counter_id")))


@customers_bp.route("/<int:cid>/advance", methods=["POST"])
@login_required
def take_advance(cid):
    c = Customer.query.get_or_404(cid)
    amount = request.form.get("amount", type=float) or 0
    method = request.form.get("method") if request.form.get("method") in PAYMENT_METHODS else "cash"
    if amount <= 0:
        flash("Enter the amount the customer is paying in advance.", "danger")
        return redirect(url_for("customers.view_customer", cid=cid))
    company, location, _floor, counter = _place()
    adv = CustomerAdvance(number=generate_number("ADV", CustomerAdvance, "number"),
                          customer_id=c.id, amount=round(amount, 2), method=method,
                          reference=(request.form.get("reference") or "").strip()[:64] or None,
                          note=(request.form.get("note") or "").strip()[:256] or None,
                          cashier_id=current_user.id,
                          company_id=company.id if company else None,
                          location_id=location.id if location else None,
                          counter_id=counter.id if counter else None)
    db.session.add(adv)
    db.session.commit()
    flash(f"Advance {adv.number} of ₹{adv.amount:,.2f} taken from {c.name}. "
          "It can be spent at the counter under Advance.", "success")
    return redirect(url_for("customers.advance_receipt", aid=adv.id))


@customers_bp.route("/advances")
@login_required
def advances():
    rows = [(a, vouchers.advance_used(a), vouchers.advance_balance(a))
            for a in CustomerAdvance.query.order_by(CustomerAdvance.created_at.desc()).limit(300).all()]
    return render_template("customers/advances.html", rows=rows,
                           outstanding=round(sum(b for _, _, b in rows), 2))


@customers_bp.route("/advances/<int:aid>")
@login_required
def advance_receipt(aid):
    adv = CustomerAdvance.query.get_or_404(aid)
    return render_template("customers/advance_receipt.html", adv=adv,
                           used=vouchers.advance_used(adv), balance=vouchers.advance_balance(adv))


@customers_bp.route("/advances/<int:aid>/refund", methods=["POST"])
@login_required
@role_required("admin", "manager")
def refund_advance(aid):
    adv = CustomerAdvance.query.get_or_404(aid)
    balance = vouchers.advance_balance(adv)
    amount = request.form.get("amount", type=float) or balance
    if amount <= 0 or amount > balance + 0.01:
        flash(f"{adv.number} has ₹{balance:,.2f} left to refund.", "danger")
        return redirect(url_for("customers.advance_receipt", aid=aid))
    adv.refunded = round((adv.refunded or 0) + amount, 2)
    adv.refund_method = request.form.get("method") if request.form.get("method") in PAYMENT_METHODS else "cash"
    adv.refunded_at = datetime.utcnow()
    adv.refunded_by_id = current_user.id
    db.session.commit()
    flash(f"₹{amount:,.2f} of {adv.number} refunded by {adv.refund_method.upper()}.", "success")
    return redirect(url_for("customers.advance_receipt", aid=aid))


# ---------------------------------------------------------------------------
#  feedback
# ---------------------------------------------------------------------------
@customers_bp.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    if request.method == "POST":
        rating = request.form.get("rating", type=int)
        if rating not in (1, 2, 3, 4, 5):
            flash("Pick a rating from 1 to 5.", "danger")
            return redirect(request.referrer or url_for("customers.feedback"))
        invoice = None
        bill = (request.form.get("invoice") or "").strip()
        if request.form.get("invoice_id", type=int):
            invoice = db.session.get(Invoice, request.form.get("invoice_id", type=int))
        elif bill:
            invoice = Invoice.query.filter_by(invoice_number=bill).first()
            if invoice is None:
                flash(f"No bill numbered {bill}.", "danger")
                return redirect(url_for("customers.feedback"))
        customer_id = request.form.get("customer_id", type=int) or (invoice.customer_id if invoice else None)
        phone = (request.form.get("phone") or "").strip()
        if not customer_id and phone:
            c = Customer.query.filter_by(phone=phone).first()
            customer_id = c.id if c else None
        db.session.add(CustomerFeedback(customer_id=customer_id,
                                        invoice_id=invoice.id if invoice else None,
                                        rating=rating,
                                        comments=(request.form.get("comments") or "").strip()[:2000] or None,
                                        source="counter", recorded_by_id=current_user.id))
        db.session.commit()
        flash("Feedback recorded — thank you.", "success")
        return redirect(request.form.get("next") or url_for("customers.feedback"))
    rows = CustomerFeedback.query.order_by(CustomerFeedback.created_at.desc()).limit(300).all()
    avg = db.session.query(func.avg(CustomerFeedback.rating)).scalar()
    return render_template("customers/feedback.html", rows=rows,
                           average=round(float(avg), 2) if avg else None)


# ---------------------------------------------------------------------------
#  scheduled messages
# ---------------------------------------------------------------------------
@customers_bp.route("/messages")
@login_required
@role_required("admin", "manager")
def messages():
    status = request.args.get("status") or ""
    q = ScheduledMessage.query
    if status:
        q = q.filter(ScheduledMessage.status == status)
    rows = q.order_by(ScheduledMessage.scheduled_for.desc()).limit(300).all()
    url, _ = messaging.provider()
    counts = dict(db.session.query(ScheduledMessage.status, func.count(ScheduledMessage.id))
                  .group_by(ScheduledMessage.status).all())
    return render_template("customers/messages.html", rows=rows, status=status, counts=counts,
                           provider=bool(url), kinds=MESSAGE_KINDS,
                           birthdays=messaging.due_wishes("birthday", 7),
                           anniversaries=messaging.due_wishes("anniversary", 7))


@customers_bp.route("/messages/wishes", methods=["POST"])
@login_required
@role_required("admin", "manager")
def queue_wishes():
    days = max(0, min(request.form.get("days", type=int) or 7, 60))
    made = sum(messaging.queue_wishes(kind, days, user_id=current_user.id)
               for kind in ("birthday", "anniversary"))
    db.session.commit()
    flash(f"{made} birthday/anniversary wish(es) queued for the next {days} day(s)."
          if made else "Nothing new to queue — every wish in that window is already queued.",
          "success" if made else "info")
    return redirect(url_for("customers.messages"))


@customers_bp.route("/messages/new", methods=["POST"])
@login_required
@role_required("admin", "manager")
def new_message():
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Write the message first.", "danger")
        return redirect(url_for("customers.messages"))
    who = request.form.get("to") or "one"
    if who == "all":
        customers = Customer.query.filter(Customer.phone.isnot(None), Customer.phone != "").all()
    else:
        phone = (request.form.get("phone") or "").strip()
        customers = Customer.query.filter_by(phone=phone).all() if phone else []
        if not customers:
            flash(f"No customer with the phone number “{phone}”.", "danger")
            return redirect(url_for("customers.messages"))
    when = None
    raw = (request.form.get("when") or "").strip()
    if raw:
        try:
            when = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            when = None
    kind = request.form.get("kind") if request.form.get("kind") in MESSAGE_KINDS else "custom"
    made = messaging.queue_message(customers, body, when=when, kind=kind, user_id=current_user.id)
    db.session.commit()
    flash(f"{made} message(s) queued.", "success")
    return redirect(url_for("customers.messages"))


@customers_bp.route("/messages/send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def send_messages():
    counts = messaging.send_due()
    db.session.commit()
    total = sum(counts.values())
    if not total:
        flash("No messages are due yet.", "info")
    else:
        flash(f"{counts.get('sent', 0)} sent · {counts.get('logged', 0)} logged without a "
              f"provider · {counts.get('failed', 0)} failed.",
              "success" if counts.get("sent") else "warning")
    return redirect(url_for("customers.messages"))


@customers_bp.route("/messages/<int:mid>/cancel", methods=["POST"])
@login_required
@role_required("admin", "manager")
def cancel_message(mid):
    m = ScheduledMessage.query.get_or_404(mid)
    if m.status == "queued":
        m.status = "cancelled"
        db.session.commit()
        flash("Message cancelled.", "success")
    return redirect(url_for("customers.messages"))
