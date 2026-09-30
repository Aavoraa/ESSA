"""Floor sales — mobile salesperson + customer live-view module.

Flow:
1. Salesperson opens /floor -> starts a session (gets 6-char code + QR).
2. Salesperson scans/searches products on their phone; each add is saved.
3. Customer opens /floor/view/<code> on their own phone (or via QR).
4. Salesperson clicks "Request approval" -> status = awaiting_approval.
5. Customer sees big Approve / Reject buttons; taps Approve.
6. Salesperson taps Finalize -> real Invoice is created; session closed.
"""
import random, string
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, abort
)
from flask_login import login_required, current_user
from datetime import datetime
from sqlalchemy import func
from app import db
from app import billing_numbers, promotions, warehouse_items
from app.models import (
    SaleSession, SaleSessionItem, Product, Customer,
    Invoice, InvoiceItem, StockMovement, LoyaltyTxn
)
from app.utils import generate_number

floor_bp = Blueprint("floor", __name__)


def _new_code():
    for _ in range(10):
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not SaleSession.query.filter_by(code=code).first():
            return code
    raise RuntimeError("Could not generate unique code")


def _session_json(s):
    t = s.totals()
    return {
        "code": s.code, "status": s.status,
        "customer": {"id": s.customer.id, "name": s.customer.name,
                     "phone": s.customer.phone, "points": s.customer.loyalty_points}
                    if s.customer else None,
        "salesperson": s.salesperson.full_name,
        "discount": s.discount, "redeem_points": s.redeem_points,
        "payment_method": s.payment_method,
        "items": [{
            "id": it.id, "product_id": it.product_id, "name": it.product.name,
            "sku": it.product.sku, "unit": it.product.unit, "stock": it.product.stock_qty,
            "quantity": it.quantity, "unit_price": it.unit_price, "gst_rate": it.gst_rate,
            "line_total": it.line_total, "tax_amount": it.tax_amount,
        } for it in s.items],
        **t,
        "invoice_id": s.invoice_id,
        "updated_at": s.updated_at.isoformat(),
    }


# ---------- Customer lookup API (phone / invoice # / barcode) ----------
@floor_bp.route("/api/customer-lookup")
@login_required
def customer_lookup():
    """Find one customer by mobile number or any prior invoice number.
    Barcode scans just pass their decoded string as ?q= — we try phone then invoice."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Enter a phone or invoice number"}), 400

    c = None
    # 0) membership-card barcode, e.g. "CUST000042"
    import re
    m = re.fullmatch(r"CUST0*(\d+)", q, re.IGNORECASE)
    if m:
        c = Customer.query.get(int(m.group(1)))
    # 1) exact phone as typed
    if not c:
        c = Customer.query.filter_by(phone=q).first()
    # 2) digits-only match — normalizes "+91 99000 11111" vs "9900011111"
    digits = "".join(ch for ch in q if ch.isdigit())
    if not c and len(digits) >= 5:
        # The same test, narrowed in the database first on PostgreSQL: a phone
        # whose ASCII digits end in `digits` — or any phone holding a non-ASCII
        # character, whose digits only Python's isdigit() can read — then the
        # original check below, in the same scan order. It used to load every
        # customer with a phone (150K) on each lookup.
        cands = Customer.query.filter(Customer.phone.isnot(None))
        if db.engine.dialect.name == "postgresql":
            cands = cands.filter(db.or_(
                func.regexp_replace(Customer.phone, "[^0-9]", "", "g").like("%" + digits),
                Customer.phone.op("~")("[^[:ascii:]]"))).order_by(db.literal_column("ctid"))
            # ctid: the table's physical order — the order the unfiltered scan
            # it replaces returned rows in, so the FIRST match is the same one
            # when several customers' numbers end in the same digits
        for cand in cands.all():
            if "".join(ch for ch in (cand.phone or "") if ch.isdigit()).endswith(digits):
                c = cand; break
    # 3) invoice number
    if not c:
        inv = Invoice.query.filter_by(invoice_number=q).first()
        if inv and inv.customer_id:
            c = inv.customer
    # 4) name (last resort)
    if not c:
        c = Customer.query.filter(Customer.name.ilike(f"%{q}%")).first()

    if not c:
        return jsonify({"error": f"No customer matches '{q}'"}), 404
    return jsonify({
        "id": c.id, "name": c.name, "phone": c.phone or "",
        "email": c.email or "", "gstin": c.gstin or "",
        "loyalty_points": round(c.loyalty_points or 0, 2),
        "total_spent": round(c.total_spent or 0, 2),
        "invoice_count": len(c.invoices),
    })


@floor_bp.route("/api/customer/new", methods=["POST"])
@login_required
def customer_new():
    """Quick-add a new customer during the sale."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if phone and Customer.query.filter_by(phone=phone).first():
        return jsonify({"error": "Phone already exists"}), 400
    c = Customer(
        name=name, phone=phone,
        email=(data.get("email") or "").strip(),
        state_code=(data.get("state_code") or "33").strip(),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({
        "id": c.id, "name": c.name, "phone": c.phone or "",
        "loyalty_points": 0, "total_spent": 0, "invoice_count": 0,
    })


# ---------- Product picker (search API) ----------
@floor_bp.route("/api/products")
@login_required
def api_products():
    q = (request.args.get("q") or "").strip()
    query = Product.query.filter(Product.active == True, Product.stock_qty > 0)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Product.name.ilike(like)) | (Product.sku.ilike(like)) |
            (Product.fabric.ilike(like)) | (Product.color.ilike(like)) |
            (Product.barcode.ilike(like))
        )
    products = query.order_by(Product.name).limit(60).all()
    return jsonify([{
        "id": p.id, "sku": p.sku, "name": p.name,
        "price": p.selling_price, "gst": p.gst_rate,
        "stock": p.stock_qty, "unit": p.unit,
        "fabric": p.fabric, "color": p.color, "size": p.size,
        "category": p.category.name if p.category else "",
    } for p in products])


# ---------- Salesperson ----------
@floor_bp.route("/")
@login_required
def index():
    my_open = SaleSession.query.filter(
        SaleSession.salesperson_id == current_user.id,
        SaleSession.status.in_(["open", "awaiting_approval", "approved"])
    ).order_by(SaleSession.created_at.desc()).all()
    return render_template("floor/index.html", sessions=my_open)


@floor_bp.route("/new", methods=["POST"])
@login_required
def new_session():
    s = SaleSession(code=_new_code(), salesperson_id=current_user.id)
    db.session.add(s)
    db.session.commit()
    return redirect(url_for("floor.session_view", code=s.code))


@floor_bp.route("/s/<code>")
@login_required
def session_view(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.salesperson_id != current_user.id and not current_user.is_manager:
        abort(403)
    # no customer list: salesperson.html never reads one, and it was all 150K
    return render_template("floor/salesperson.html", s=s)


@floor_bp.route("/s/<code>/state")
@login_required
def session_state(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    return jsonify(_session_json(s))


@floor_bp.route("/s/<code>/add", methods=["POST"])
@login_required
def add_item(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status not in ("open", "awaiting_approval", "rejected"):
        return jsonify({"error": "Session is locked."}), 400

    data = request.get_json() or {}
    lookup = (data.get("code") or "").strip()
    product_id = data.get("product_id")

    p = None
    if product_id:
        p = Product.query.get(int(product_id))
    elif lookup:
        # Anything scannable first — SKU, barcode, or a warehouse QR off the tag,
        # pulling the item in from the warehouse if the shop hasn't got it yet.
        p = warehouse_items.resolve_scan(lookup)
        if not p:
            # Not a code at all: treat it as someone typing part of a name.
            p = Product.query.filter(Product.name.ilike(f"%{lookup}%"), Product.active == True).first()
    if not p:
        return jsonify({"error": f"No product matches '{lookup}'"}), 404
    if p.stock_qty <= 0:
        return jsonify({"error": f"{p.name} is out of stock"}), 400

    # merge with existing item if present
    existing = next((it for it in s.items if it.product_id == p.id), None)
    if existing:
        if existing.quantity + 1 > p.stock_qty:
            return jsonify({"error": f"Only {p.stock_qty} left in stock"}), 400
        existing.quantity += 1
        existing.recompute()
    else:
        item = SaleSessionItem(
            session_id=s.id, product_id=p.id, quantity=1,
            unit_price=p.selling_price, gst_rate=p.gst_rate,
        )
        item.recompute()
        db.session.add(item)

    # any change reverts an awaiting/rejected session to open
    if s.status in ("awaiting_approval", "rejected"):
        s.status = "open"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_session_json(s))


@floor_bp.route("/s/<code>/update", methods=["POST"])
@login_required
def update_session(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status == "completed":
        return jsonify({"error": "Already completed"}), 400
    data = request.get_json() or {}

    if "item_id" in data:
        it = SaleSessionItem.query.get(int(data["item_id"]))
        if not it or it.session_id != s.id:
            return jsonify({"error": "Item not found"}), 404
        if data.get("remove"):
            db.session.delete(it)
        else:
            q = float(data.get("quantity", it.quantity))
            if q <= 0:
                db.session.delete(it)
            else:
                if q > it.product.stock_qty:
                    return jsonify({"error": f"Only {it.product.stock_qty} left"}), 400
                it.quantity = q
                it.recompute()

    if "discount" in data:
        s.discount = max(0, float(data["discount"] or 0))
    if "redeem_points" in data:
        s.redeem_points = max(0, float(data["redeem_points"] or 0))
    if "payment_method" in data:
        s.payment_method = data["payment_method"]
    if "customer_id" in data:
        cid = data["customer_id"]
        s.customer_id = int(cid) if cid else None

    if s.status in ("awaiting_approval", "rejected"):
        s.status = "open"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_session_json(s))


@floor_bp.route("/s/<code>/request-approval", methods=["POST"])
@login_required
def request_approval(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if not s.items:
        return jsonify({"error": "Cart is empty"}), 400
    s.status = "awaiting_approval"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_session_json(s))


@floor_bp.route("/s/<code>/finalize", methods=["POST"])
@login_required
def finalize(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status != "approved":
        return jsonify({"error": "Customer has not approved yet."}), 400

    shop_state = current_app.config["SHOP_STATE_CODE"]
    customer = s.customer
    is_interstate = bool(customer and customer.state_code and customer.state_code != shop_state)

    # The same offers the billing counter applies. A customer who bought three
    # chudithars from someone walking the floor has bought three chudithars, and
    # a promotion that depends on which desk served them is a promotion the shop
    # cannot explain. Evaluated before any stock moves, for the reason set out in
    # pos.checkout: every line below reduces the figure this reads.
    try:
        outcome = promotions.evaluate(
            [{"product_id": i.product_id, "quantity": i.quantity} for i in s.items])
    except Exception:                                # noqa: BLE001
        current_app.logger.warning("promotions skipped for this sale", exc_info=True)
        outcome = promotions.Outcome([], [])

    # Floor SALES — a phone cart built while walking the shop — is not a till,
    # and it has no counter, so it has no storey mapped to it and bills on the
    # shop's plain INV- series exactly as it always has. That is the honest
    # answer rather than guessing a floor from the salesperson: the bill prefix
    # says which register a sale belongs in, and inventing one would file sales
    # under a floor nobody chose. (If these ever need their own series, the
    # answer is a floor picker on the phone, not a guess here.)
    #
    # What HAS changed is that the number now comes from the same backend
    # allocator the counter uses, so two salespeople finalising at once cannot
    # land on the same one — see app/billing_numbers.py.
    number, prefix, fin_year, seq = billing_numbers.allocate(None)
    inv = Invoice(
        invoice_number=number,
        bill_prefix=prefix,
        fin_year=fin_year,
        bill_seq=seq,
        customer_id=customer.id if customer else None,
        cashier_id=s.salesperson_id,
        staff_id=s.salesperson_id,
        payment_method=s.payment_method or "cash",
        discount=s.discount or 0,
        is_interstate=is_interstate,
    )
    db.session.add(inv)
    db.session.flush()

    subtotal, tax = 0.0, 0.0
    sold_items = {}
    for it in s.items:
        p = it.product
        if p.stock_qty < it.quantity:
            db.session.rollback()
            return jsonify({"error": f"Stock changed for {p.name}"}), 400
        line = InvoiceItem(
            invoice_id=inv.id, product_id=p.id,
            quantity=it.quantity, unit_price=it.unit_price,
            gst_rate=it.gst_rate, line_total=it.line_total, tax_amount=it.tax_amount,
        )
        db.session.add(line)
        sold_items.setdefault(p.id, []).append(line)
        p.stock_qty -= it.quantity
        db.session.add(StockMovement(
            product_id=p.id, change=-it.quantity, reason="sale",
            reference=inv.invoice_number
        ))
        subtotal += it.line_total
        tax += it.tax_amount

    # The reward lines, and the stock they take. In a savepoint so a
    # misconfigured offer costs the floor a promotion rather than a sale.
    free_items = []
    if outcome.awards or outcome.notices:
        mark = db.session.begin_nested()
        keep = (subtotal, tax)
        try:
            for line, product, qty in promotions.apply_to_invoice(
                    inv, outcome, sold_items, user_id=s.salesperson_id):
                product.stock_qty -= qty
                db.session.add(StockMovement(
                    product_id=product.id, change=-qty,
                    reason=promotions.MOVEMENT_REASON,
                    reference=inv.invoice_number))
                subtotal += line.line_total
                tax += line.tax_amount
                free_items.append({"name": product.name, "sku": product.sku,
                                   "qty": qty})
            mark.commit()
        except Exception:                            # noqa: BLE001
            mark.rollback()
            subtotal, tax = keep
            free_items = []
            current_app.logger.warning("promotions skipped for this sale",
                                       exc_info=True)

    if is_interstate:
        inv.igst = round(tax, 2)
    else:
        inv.cgst = round(tax / 2, 2)
        inv.sgst = round(tax / 2, 2)

    inv.subtotal = round(subtotal, 2)
    pre_loyalty = round(subtotal - (s.discount or 0) + tax, 2)

    loyalty_redeemed_value = 0.0
    if customer and (s.redeem_points or 0) > 0:
        use = min(s.redeem_points, customer.loyalty_points, pre_loyalty)
        loyalty_redeemed_value = use * current_app.config["LOYALTY_POINT_VALUE"]
        inv.loyalty_redeemed = use
        customer.loyalty_points -= use
        db.session.add(LoyaltyTxn(customer_id=customer.id, points=-use, reason="redeem", invoice_id=inv.id))

    inv.total = round(pre_loyalty - loyalty_redeemed_value, 2)

    if customer and inv.total >= current_app.config["LOYALTY_MIN_BILL"]:
        earned = round(inv.total * current_app.config["LOYALTY_EARN_RATE"], 2)
        inv.loyalty_earned = earned
        customer.loyalty_points = (customer.loyalty_points or 0) + earned
        customer.total_spent = (customer.total_spent or 0) + inv.total
        db.session.add(LoyaltyTxn(customer_id=customer.id, points=earned, reason="earn", invoice_id=inv.id))
    elif customer:
        customer.total_spent = (customer.total_spent or 0) + inv.total

    s.status = "completed"
    s.invoice_id = inv.id
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "invoice_id": inv.id,
                    "invoice_number": inv.invoice_number,
                    # …and what the salesperson has to fetch off the shelf before
                    # the customer leaves. A promotion nobody hands over is one
                    # the shop has paid for and the customer never received.
                    "free_items": free_items})


@floor_bp.route("/s/<code>/cancel", methods=["POST"])
@login_required
def cancel(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status == "completed":
        return jsonify({"error": "Already completed"}), 400
    s.status = "cancelled"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})


# ---------- Customer public view (no auth) ----------
@floor_bp.route("/view/<code>")
def customer_view(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    return render_template("floor/customer.html", s=s)


@floor_bp.route("/view/<code>/state")
def customer_state(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    return jsonify(_session_json(s))


@floor_bp.route("/view/<code>/approve", methods=["POST"])
def customer_approve(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status != "awaiting_approval":
        return jsonify({"error": "Not awaiting your approval"}), 400
    s.status = "approved"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_session_json(s))


@floor_bp.route("/view/<code>/reject", methods=["POST"])
def customer_reject(code):
    s = SaleSession.query.filter_by(code=code).first_or_404()
    if s.status != "awaiting_approval":
        return jsonify({"error": "Not awaiting your approval"}), 400
    s.status = "rejected"
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_session_json(s))
