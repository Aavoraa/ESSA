from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   jsonify, current_app, session)
from flask_login import login_required, current_user
from datetime import datetime
from itsdangerous import URLSafeSerializer
from app import db
from app.datefilter import day_from, day_to
from app import (billing_numbers, cancellation, drawer, places, promotions, schemes,
                 transfers, vouchers, warehouse_items)
from app import modules as modules_mod
import re
from urllib.parse import urlsplit
from app.utils import role_required
# Every name this file needs is imported HERE, at module level, and never inside
# a view. This package is loaded as `app`, and when the shop is served inside the
# Essa backend that name belongs to the backend by the time a request arrives —
# so a late `from app…` reaches into the wrong package and raises ImportError at
# the worst moment, on a screen that worked in every test run standalone. See
# backend/app/pos_mount.py, and the same note in app/places.py.
from app.models import (Product, Coupon, Customer, Floor, Invoice, InvoiceItem,
                        InvoicePayment, StockMovement, LoyaltyTxn, User, SalesSchemeUse,
                        PAYMENT_METHODS, VOUCHER_METHODS)

pos_bp = Blueprint("pos", __name__)

#: Money is compared to the paisa. Floats do not land exactly on a total built
#: from percentages, so "did this balance" needs a tolerance rather than ==.
SETTLE_TOLERANCE = 0.01


def parse_payments(data):
    """The tenders offered for this bill, cleaned. Raises ValueError on nonsense.

    Accepts the new `payments` list — [{method, amount, tendered, reference}] —
    and falls back to the single `payment_method` this route has always taken, so
    anything still posting the old shape keeps working. The fallback carries no
    amount: it means "settle the whole bill this way", and the amount is filled
    in below once the total is known.
    """
    # Whether a settlement was SENT, not whether it has anything in it. An empty
    # list is a settlement with no money in it and has to be refused; a missing
    # key is the old single-method shape and falls back. Reading both as "no
    # payments" let a till bill an empty settlement as cash for the full amount.
    if "payments" not in data:
        method = (data.get("payment_method") or "cash").strip().lower()
        if method not in PAYMENT_METHODS:
            raise ValueError(f"“{method}” is not a payment method")
        return None, method              # None = settle the whole total this way

    raw = data.get("payments")
    if isinstance(raw, str):
        import json
        raw = json.loads(raw or "[]")
    if not raw:
        raise ValueError("no payment was entered")

    out = []
    for row in raw:
        method = (row.get("method") or "").strip().lower()
        if method not in PAYMENT_METHODS and method not in VOUCHER_METHODS:
            raise ValueError(f"“{method}” is not a payment method")
        try:
            amount = round(float(row.get("amount") or 0), 2)
            tendered = row.get("tendered")
            tendered = round(float(tendered), 2) if tendered not in (None, "") else amount
        except (TypeError, ValueError):
            raise ValueError("payment amounts must be numbers")
        if amount <= 0:
            continue                     # a blank row is not a payment
        if tendered < amount - SETTLE_TOLERANCE:
            raise ValueError(f"{method}: {tendered:g} tendered against {amount:g} — "
                             f"less was handed over than is being settled")
        if method == "credit_note" and not (row.get("reference") or "").strip():
            raise ValueError("Enter the credit note's number to spend it")
        if method != "cash" and tendered > amount + SETTLE_TOLERANCE:
            # Only a drawer gives change. A card or a UPI transfer is for an
            # exact amount, and recording an over-tender on one would invent
            # change that nobody handed back.
            raise ValueError(f"{method} cannot be over-tendered — "
                             f"it is settled for an exact amount")
        out.append({"method": method, "amount": amount, "tendered": tendered,
                    "reference": (row.get("reference") or "").strip() or None})
    if not out:
        raise ValueError("no payment was entered")
    return out, ("mixed" if len({p["method"] for p in out}) > 1
                 else out[0]["method"])

#: Where the till's Company / Location / Counter choice is kept.
#:
#: In the SESSION, deliberately. It is a property of the machine somebody is
#: standing at, not of the person signed into it and not of the shop: two tills
#: at one branch are two counters, and the same cashier moving between them must
#: not carry the first one's drawer to the second. It also has to outlast a
#: reload — a picker that forgets on every refresh gets set wrong, or ignored.
POST_KEYS = ("company_id", "location_id", "floor_id", "counter_id")


def _chosen():
    """(company, location, floor, counter) — each None until picked or mapped."""
    return places.resolve(*(session.get(k) for k in POST_KEYS))


def _place():
    """Where this till is, in the shape the promotion engine reads.

    Includes the warehouse the frame was opened from, so an offer configured for
    one warehouse's branches does not run at another's — the same narrowing
    places.picker_options already does to the branch list.
    """
    company, location, _floor, counter = _chosen()
    return promotions.Place(warehouse_id=places.current_scope(), company=company,
                            location=location, counter=counter)


def _cart_lines(data):
    """The paid lines a request is offering, as the engine wants them.

    Reward lines are dropped if a caller sends any: what the customer gets free
    is decided here, from the products they are paying for, and a page that
    could add its own free lines could bill the shop's stock away.
    """
    items = data.get("items")
    if isinstance(items, str):
        import json
        items = json.loads(items or "[]")
    out = []
    for it in items or []:
        if (it.get("promo_role") or it.get("free")):
            continue
        out.append({"product_id": it.get("product_id"),
                    "quantity": it.get("quantity")})
    return out


@pos_bp.route("/api/promotions", methods=["POST"])
@login_required
def api_promotions():
    """What this cart has earned, as the cashier builds it.

    Called on every cart change so a free item appears while the customer is
    still at the counter, and so an offer that cannot be honoured says why
    there and then. Advisory only — `checkout` runs the same evaluation again
    against its own reading of the cart, and that is the one that bills.
    """
    data = request.get_json(silent=True) or request.form
    choices = data.get("choices") or {}
    if isinstance(choices, str):
        import json
        choices = json.loads(choices or "{}")
    try:
        outcome = promotions.evaluate(_cart_lines(data), place=_place(),
                                      choices=choices)
    except Exception as exc:                        # noqa: BLE001
        # A promotion is a bonus; a till that cannot bill because an offer is
        # misconfigured is worse than one that quietly runs no offers. The
        # counter shows nothing and the sale goes through.
        current_app.logger.warning("promotion evaluation failed", exc_info=True)
        return jsonify({"awards": [], "notices": [], "error": str(exc),
                        "schemes": _scheme_preview(data)})
    return jsonify(dict(outcome.to_json(), schemes=_scheme_preview(data)))


def _scheme_preview(data):
    """The sales schemes this cart earns, for the counter to show: a % per line
    now (`lines`, keyed by the line's position in `items`), and what is coming
    when the bill is paid (`later`). Advisory — checkout works it out again."""
    try:
        items = data.get("items")
        if isinstance(items, str):
            import json
            items = json.loads(items or "[]")
        company, location, _storey, _till = _chosen()
        company = company or places.default_company()
        lines = []
        for n, it in enumerate(items or []):
            prod = Product.query.get(int(it.get("product_id") or 0))
            if prod is not None:
                lines.append(schemes.Line(n, prod, it.get("quantity"),
                                          it.get("unit_price", prod.selling_price),
                                          prod.gst_rate, it.get("discount_pct") or 0))
        agreement = (data.get("agreement_code") or "").strip().upper() or None
        by_line, _awards = schemes.evaluate_bill(
            lines, company.id if company else None, location.id if location else None, agreement)
        later = schemes.preview_later(
            lines, company.id if company else None, location.id if location else None, agreement)
        return {"lines": {str(k): v for k, v in by_line.items()},
                "later": [a.to_json() for a in later]}
    except Exception:                                # noqa: BLE001
        current_app.logger.warning("scheme preview failed", exc_info=True)
        return {"lines": {}, "later": []}


@pos_bp.route("/api/agreement")
@login_required
def api_agreement():
    """Is this an agreement code a running scheme asks for?"""
    company, location, _storey, _till = _chosen()
    company = company or places.default_company()
    found = schemes.agreement_schemes(request.args.get("code"),
                                      company.id if company else None,
                                      location.id if location else None)
    if not found:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "code": (request.args.get("code") or "").strip().upper(),
                    "schemes": [s.name for s in found]})


@pos_bp.route("/")
@login_required
def counter():
    """The billing counter. Scan-driven: no product list.

    The grid used to render every in-stock product as a tile. On a shop with a
    few hundred items that is a wall of near-identical names — five tiles reading
    PILLOW COVER at ₹0.00 tell a cashier nothing about which one is in their hand
    — and picking off it is how the wrong variant gets billed. The tag on the
    garment is unambiguous, so the counter takes the scan and nothing else.

    Every product still resolves: `api_product` looks up a SKU, a barcode or a
    warehouse QR against the whole catalogue, and pulls the item in from the
    warehouse if the shop has not seen it yet. Dropping the grid removed a
    listing, not a capability — and it removed a query that loaded the entire
    catalogue on every page load.
    """
    # No customer list: counter.html never reads one (a customer is found by
    # phone at the counter), and loading all 150K of them on every open of the
    # till was most of its load time.
    staff = User.query.filter(User.active.is_(True)).order_by(User.full_name).all()
    company, location, storey, till = _chosen()
    return render_template("pos/counter.html",
                           staff=staff,
                           places=places.picker_options(),
                           chosen_company=company, chosen_location=location,
                           chosen_floor=storey, chosen_counter=till,
                           next_bill=billing_numbers.peek(storey),
                           default_company=places.default_company(),
                           drawer_open=drawer.current(till.id if till else None),
                           exchange=_exchange_from(request.args.get("credit_note")))


def _customer_json(c):
    """A customer in the shape the counter's customer card paints."""
    return {"id": c.id, "name": c.name, "phone": c.phone or "",
            "loyalty_points": round(c.loyalty_points or 0, 2),
            "total_spent": round(c.total_spent or 0, 2),
            "invoice_count": len(c.invoices)}


def _exchange_from(number):
    """The credit note a return has just raised, for the bill that replaces it.

    A return kept as store credit hands the customer straight to a new bill: the
    counter opens with the customer attached and the note waiting on the
    settlement page as a tender, so the exchange is one sale rather than a slip
    of paper somebody has to remember to type in.
    """
    note = vouchers.find_credit_note(number) if number else None
    if note is None or (note.refund_method or "") != "store_credit":
        return None
    balance = vouchers.credit_note_balance(note)
    if balance <= SETTLE_TOLERANCE:
        return None
    cust = note.invoice.customer if note.invoice else None
    return {"number": note.number, "balance": balance,
            "customer": _customer_json(cust) if cust else None}


@pos_bp.route("/settle")
@login_required
def settle():
    """Settlement — its own tab. Every bill the counter has raised and nobody
    has paid for yet, and the money for the one chosen.

    The counter lands here straight after printing the delivery copy, with
    `?id=` naming the bill it has just raised (and `?credit_note=` when it is an
    exchange). Opened from the menu, it picks the newest bill waiting.
    """
    pending = (Invoice.query.filter(Invoice.payment_status == "pending")
               .order_by(Invoice.id.desc()).limit(60).all())
    iid = request.args.get("id", type=int)
    inv = Invoice.query.get(iid) if iid else (pending[0] if pending else None)
    settled_already = inv is not None and (inv.payment_status or "") != "pending"
    # What the bill's settlement-stage schemes will do when it is paid — shown
    # now so the amount due on screen is the amount the server will ask for.
    scheme_awards, scheme_off = [], 0.0
    if inv is not None and not settled_already and not inv.is_cancelled:
        try:
            scheme_awards = schemes.settlement_awards(inv)
            scheme_off = schemes.settlement_discount(scheme_awards, inv.total)
        except Exception:                            # noqa: BLE001
            current_app.logger.warning("settlement schemes preview failed", exc_info=True)
            scheme_awards, scheme_off = [], 0.0
    return render_template(
        "pos/settle.html", pending=pending, inv=inv, settled_already=settled_already,
        rcpt=_receipt(inv) if inv is not None else None,
        scheme_awards=scheme_awards, scheme_off=scheme_off,
        due=round((inv.total or 0) - scheme_off, 2) if inv is not None else 0,
        exchange=_exchange_from(request.args.get("credit_note")))


@pos_bp.route("/api/offers")
@login_required
def api_offers():
    """The promotion schemes running at this till today — the salesman's list."""
    try:
        schemes = promotions.active_schemes(place=_place())
    except Exception as exc:                        # noqa: BLE001
        current_app.logger.warning("could not list offers", exc_info=True)
        return jsonify({"offers": [], "error": str(exc)})
    return jsonify({"offers": [{
        "code": s.code, "name": s.name, "describe": promotions.describe(s),
        "until": s.end_date.strftime("%d-%m-%Y") if s.end_date else None,
        "terms": s.terms or ""} for s in schemes]})


def _recent_bills_query():
    """Bills raised on this till — or by this login when no till is set."""
    _company, _location, _storey, till = _chosen()
    q = Invoice.query
    q = q.filter(Invoice.counter_id == till.id) if till else \
        q.filter(Invoice.cashier_id == current_user.id)
    return q.order_by(Invoice.id.desc())


@pos_bp.route("/api/recent-bills")
@login_required
def api_recent_bills():
    """The last few bills on this till, for reprinting or downloading."""
    return jsonify({"bills": [{
        "id": i.id, "number": i.invoice_number, "total": i.total,
        "when": i.invoice_date.strftime("%d-%m %H:%M") if i.invoice_date else "",
        "customer": i.customer.name if i.customer else "Walk-in",
        "cancelled": i.is_cancelled} for i in _recent_bills_query().limit(8)]})


@pos_bp.route("/invoice/last")
@login_required
def last_invoice():
    """The previous bill on this till, straight to its print view."""
    inv = _recent_bills_query().first()
    if inv is None:
        flash("No bill has been raised on this till yet.", "info")
        return redirect(url_for("pos.counter"))
    return redirect(url_for("pos.print_invoice", iid=inv.id))


def _place_json(company, location, storey, till):
    """What the till is billing as, and what its next bill will be called.

    The bill number travels with the place because it is DECIDED by it — a
    cashier moving a till from Ground to First has to see TF appear, there and
    then, or the first they know of a wrong series is a printed bill.
    """
    return {
        "company": {"id": company.id, "name": company.name,
                    "gstin": company.gstin or ""} if company else None,
        "location": {"id": location.id, "name": location.name} if location else None,
        "floor": {"id": storey.id, "name": storey.name,
                  "prefix": storey.prefix} if storey else None,
        "counter": {"id": till.id, "name": till.name} if till else None,
        "next_bill": billing_numbers.peek(storey),
        "options": places.picker_options(),
    }


@pos_bp.route("/place", methods=["GET", "POST"])
@login_required
def place():
    """Read or set which company, location, floor and counter this till bills as.

    POST takes the four ids and answers with what it actually settled on, which
    is not always what was sent: a counter belonging to another branch is dropped
    rather than stored, and so is a floor the chosen till is not on. The till
    redraws from the answer, so what it shows is what the next bill will carry —
    never what was merely asked for.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        for key in POST_KEYS:
            raw = data.get(key)
            session[key] = int(raw) if str(raw or "").strip().isdigit() else None
        company, location, storey, till = places.resolve(
            *(session.get(k) for k in POST_KEYS))
        # store back what survived, so the session never holds a pairing the
        # screen has already been told is impossible
        session["company_id"] = company.id if company else None
        session["location_id"] = location.id if location else None
        session["floor_id"] = storey.id if storey else None
        session["counter_id"] = till.id if till else None
    else:
        company, location, storey, till = _chosen()
    return jsonify(_place_json(company, location, storey, till))


@pos_bp.route("/api/next-bill")
@login_required
def api_next_bill():
    """The number the next bill on this till would carry.

    A look, not a reservation — see billing_numbers.peek. The till shows it so
    the cashier can read the series off the screen before taking money; the
    number that counts is the one on the committed bill, which the checkout
    returns.
    """
    _company, _location, storey, _till = _chosen()
    return jsonify(billing_numbers.peek(storey))


def resolve_staff(value):
    """The staff member a counter identified, or None.

    Takes what the ID card carries (`STF000003`), a bare staff number, or the id
    the picker sends. Only active staff resolve — someone who has left should not
    be collecting commission on today's sales.
    """
    text = str(value or "").strip()
    if not text:
        return None
    q = User.query.filter(User.active.is_(True))
    digits = text.upper()[3:] if text.upper().startswith("STF") else text
    if digits.isdigit():
        u = q.filter(User.id == int(digits)).first()
        if u:
            return u
    # A username typed in full is the other thing people reach for.
    return q.filter(User.username.ilike(text)).first()


@pos_bp.route("/api/staff")
@login_required
def api_staff():
    """Resolve a scanned ID card or a typed staff code to the person."""
    u = resolve_staff(request.args.get("code", ""))
    if not u:
        return jsonify({"error": "No active staff member for that code"}), 404
    return jsonify({"id": u.id, "code": u.staff_code,
                    "name": u.full_name, "role": u.role})


@pos_bp.route("/api/product")
@pos_bp.route("/api/product/<path:code>")
@login_required
def api_product(code=None):
    """Resolve anything scannable at the counter to a cart line.

    Takes a SKU, a printed barcode, a warehouse QR (`E1|…` / `EU1|…`) or a bare
    per-piece code — resolve_scan sorts out which, and pulls the item in from the
    warehouse if the shop hasn't got it yet. So any tag that exists scans here,
    including items not on the grid because they're out of stock.

    `?code=` is the form the counter uses: a QR payload can carry '/', which a
    path segment mangles. The path route stays for anything still calling it.
    """
    code = (code if code is not None else request.args.get("code", "")).strip()
    p = warehouse_items.resolve_scan(code)
    if not p:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": p.id, "sku": p.sku, "barcode": p.barcode, "name": p.name,
        "price": p.selling_price, "gst": p.gst_rate,
        "stock": p.stock_qty, "unit": p.unit, "hsn": p.hsn_code,
    })


@pos_bp.route("/api/product-search")
@login_required
def api_product_search():
    """Products by name, SKU or barcode, for the bill's entry row — typed into
    its Product box when there is no tag to scan. In-stock items only."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = (Product.query
            .filter(Product.active.is_(True), Product.stock_qty > 0,
                    Product.name.ilike(like) | Product.sku.ilike(like)
                    | Product.barcode.ilike(like))
            .order_by(Product.name).limit(15).all())
    return jsonify([{"id": p.id, "sku": p.sku, "barcode": p.barcode, "name": p.name,
                     "price": p.selling_price, "gst": p.gst_rate,
                     "stock": p.stock_qty, "unit": p.unit} for p in rows])


@pos_bp.route("/checkout", methods=["POST"])
@login_required
def checkout():
    data = request.get_json() or request.form
    try:
        items_json = data.get("items")
        if isinstance(items_json, str):
            import json
            items = json.loads(items_json)
        else:
            items = items_json or []
        if not items:
            return jsonify({"error": "Cart is empty"}), 400

        customer_id = data.get("customer_id") or None
        customer_id = int(customer_id) if customer_id else None
        customer = Customer.query.get(customer_id) if customer_id else None

        # `settle_later`: the counter raises the bill now — stock comes off, the
        # number is taken, the delivery copy prints — and the money is taken
        # afterwards at Settlement (`settle_bill`). Without it the sale is billed
        # and paid in one go, which is how Floor Sales and older callers work.
        settle_later = bool(data.get("settle_later"))
        if settle_later:
            payments, payment_method = None, "pending"
        else:
            # Parsed before anything is written, so a malformed settlement is
            # refused while the cart is still a cart — not after stock has come
            # off the shelf.
            try:
                payments, payment_method = parse_payments(data)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        discount = float(data.get("discount") or 0)
        redeem_points = float(data.get("redeem_points") or 0)

        # Interstate = customer state code != shop state code
        shop_state = current_app.config["SHOP_STATE_CODE"]
        is_interstate = False
        if customer and customer.state_code and customer.state_code != shop_state:
            is_interstate = True

        # Who sold each line. The counter scans a staff ID after every product,
        # because one bill can carry garments sold by different people; each paid
        # line names its own. A caller that names one person for the whole sale
        # (floor sales, older pages) still works — that person covers every line
        # that names nobody. Refused if any paid line ends up with nobody: the
        # commission is worked out from this, and a line with nobody against it
        # is a sale nobody gets paid for.
        default_staff = resolve_staff(data.get("staff_code") or data.get("staff_id"))
        line_staff = []
        for it in items:
            if it.get("promo_role") or it.get("free"):
                line_staff.append(None)
                continue
            who = resolve_staff(it.get("staff_code") or it.get("staff_id")) or default_staff
            if who is None:
                return jsonify({"error": "Scan the staff ID for every item before "
                                         "billing — one line has nobody against it."}), 400
            line_staff.append(who)
        sellers = [s for s in line_staff if s is not None]
        if not sellers:
            return jsonify({"error": "Identify the staff member before billing "
                                     "— scan the ID card or enter the staff code."}), 400
        # The bill's own `staff_id` — what the salesman reports and the receipt
        # header read — is whoever sold the most lines, the first of them on a tie.
        staff = max(sellers, key=lambda s: sum(1 for x in sellers if x.id == s.id))

        # Where this sale happened, and under whose registration. The company
        # falls back to the default rather than being left blank: a tax invoice
        # with no entity on it is not a tax invoice, and a till nobody has
        # configured is the normal state of a shop that only has one company.
        company, location, storey, till = _chosen()
        if company is None:
            company = places.default_company()

        # ---- what this cart has earned ---------------------------------------
        # Worked out BEFORE anything is written, and from the products the
        # customer is paying for rather than from anything the page sent about
        # free items. Two reasons it has to be here and not further down:
        #
        #   * it reads stock, and every line below reduces it. Asked after the
        #     sale had come off the shelf it would see the shop as three garments
        #     poorer and could refuse a reward the customer had plainly earned.
        #   * it is the same reading the counter was shown while the cart was
        #     being built, so the till and the bill agree.
        #
        # A misconfigured offer must never cost the shop a sale, so the whole
        # evaluation is allowed to fail and the bill goes through without it.
        try:
            outcome = promotions.evaluate(_cart_lines(data), place=_place(),
                                          choices=(data.get("promotion_choices") or {}))
        except Exception:                            # noqa: BLE001
            current_app.logger.warning("promotions skipped for this bill",
                                       exc_info=True)
            outcome = promotions.Outcome([], [])

        # ---- the bill number -------------------------------------------------
        # Taken from the series of the FLOOR this sale is being rung on, at the
        # backend, inside this transaction — see app/billing_numbers.py. Never
        # sent by the page and never typed: a bill number is the shop's
        # statutory record of the sale, and a cashier who could choose one could
        # raise two bills with the same number or skip a number nobody can then
        # account for.
        #
        # `storey` and nothing else, because it is also what the invoice records
        # three lines below. Deriving the number from the TILL's mapping while
        # the bill recorded the resolved floor is exactly how a bill came to
        # print "Ground Floor" at its head and INV-000013 beside it.
        #
        # Allocating HERE, before the lines are written, is what makes two tills
        # on one floor safe: the series' row is locked for the rest of this
        # transaction, so the second till waits and gets the next number rather
        # than the same one. A till on another floor is on another row and never
        # waits at all.
        number, prefix, fin_year, seq = billing_numbers.allocate(storey)

        inv = Invoice(
            invoice_number=number,
            bill_prefix=prefix,
            fin_year=fin_year,
            bill_seq=seq,
            customer_id=customer_id,
            cashier_id=current_user.id,
            staff_id=staff.id,
            payment_method=payment_method,
            discount=discount,
            is_interstate=is_interstate,
            company_id=company.id if company else None,
            location_id=location.id if location else None,
            floor_id=storey.id if storey else None,
            counter_id=till.id if till else None,
        )
        # Who approved the discounts — the discount card's E.Code. Refused if it
        # names nobody, so the bill never records an approver that isn't one.
        if (data.get("discount_by") or "").strip():
            approver = resolve_staff(data.get("discount_by"))
            if approver is None:
                return jsonify({"error": "The discount approver's code (E.Code) "
                                         "is not an active staff member."}), 400
            inv.discount_by_id = approver.id

        # The agreement code the bill was raised under (the discount card's
        # P.Code) — a scheme's Agreement Code filter asks for it.
        agreement = (data.get("agreement_code") or "").strip().upper() or None
        inv.agreement_code = agreement

        db.session.add(inv)
        db.session.flush()

        subtotal = 0.0
        total_tax = 0.0

        # ---- sales schemes at the counter (On Same Bill / On Bill Addon) ------
        # Worked out HERE, from the lines about to be written — the counter's
        # own reading is only what it showed. A misconfigured scheme must not
        # cost the shop the sale, so a failure bills without schemes.
        scheme_by_line, scheme_awards = {}, []
        try:
            engine_lines = []
            for n, it in enumerate(items):
                if it.get("promo_role") or it.get("free"):
                    continue
                prod = Product.query.get(int(it["product_id"]))
                if prod is not None:
                    engine_lines.append(schemes.Line(
                        n, prod, it.get("quantity"), it.get("unit_price", prod.selling_price),
                        prod.gst_rate, it.get("discount_pct") or 0))
            scheme_by_line, scheme_awards = schemes.evaluate_bill(
                engine_lines, company.id if company else None,
                location.id if location else None, agreement)
        except Exception:                            # noqa: BLE001
            current_app.logger.warning("sales schemes skipped for this bill", exc_info=True)
            scheme_by_line, scheme_awards = {}, []

        # Stock leaves the building in exactly one place, whether it was sold or
        # given away. `where` is the branch it comes off, and `why` is what the
        # ledger will say about it — a free garment under a promotion is not a
        # sale, and a movement that called it one would make the promotion
        # invisible to every stock question afterwards.
        def take_stock(product, qty, why):
            product.stock_qty -= qty
            # …and out of the branch it was rung at. The shop's total above is
            # what the till sells against and what every screen reads; this is
            # the split of it, so a sale at Tirupur comes off Tirupur's shelf and
            # not off the pieces sitting at another branch.
            #
            # Not a second check on whether the sale may happen. A shop whose
            # branches were stocked before any of this existed has a total and no
            # split, and refusing to sell what is plainly on the counter because
            # a table added last week says zero would be the software arguing
            # with the room.
            if location is not None:
                transfers.move(location.id, product.id, -qty)
            db.session.add(StockMovement(
                product_id=product.id, change=-qty, reason=why,
                reference=inv.invoice_number
                          + (f" @ {location.name}" if location is not None else "")
            ))

        # Every paid line, by product, so the promotion can mark which of them
        # earned it. A list per product because a till may bill the same garment
        # on two lines at two prices.
        sold_items = {}

        for n, (it, seller) in enumerate(zip(items, line_staff)):
            if it.get("promo_role") or it.get("free"):
                # Free lines are not accepted from the page. They are worked out
                # below, from the products actually being paid for.
                continue
            pid = int(it["product_id"])
            qty = float(it["quantity"])
            product = Product.query.get(pid)
            if not product or product.stock_qty < qty:
                db.session.rollback()
                return jsonify({"error": f"Insufficient stock for {product.name if product else 'product'}"}), 400
            unit_price = float(it.get("unit_price", product.selling_price))
            # This line's own discount, as a percentage. It lowers the price the
            # line is charged at, so GST is on the discounted amount and a return
            # refunds what was actually paid; the price before it is kept to print.
            try:
                line_pct = min(100.0, max(0.0, float(it.get("discount_pct") or 0)))
            except (TypeError, ValueError):
                line_pct = 0.0
            # …and on top of it, whatever a sales scheme gives this line.
            sch = scheme_by_line.get(n)
            eff = schemes.combine(line_pct, sch["pct"]) if sch else line_pct
            list_price = None
            if eff > 0:
                list_price = unit_price
                unit_price = round(unit_price * (1 - eff / 100.0), 2)
            line_total = qty * unit_price  # taxable
            tax = round(line_total * product.gst_rate / 100.0, 2)

            line = InvoiceItem(
                invoice_id=inv.id, product_id=pid,
                quantity=qty, unit_price=unit_price, list_price=list_price,
                gst_rate=product.gst_rate,
                line_total=line_total, tax_amount=tax,
                staff_id=seller.id,
                scheme_id=sch["scheme_id"] if sch else None,
                scheme_pct=sch["pct"] if sch else None,
            )
            db.session.add(line)
            sold_items.setdefault(pid, []).append(line)
            take_stock(product, qty, "sale")
            subtotal += line_total
            total_tax += tax

        for a in scheme_awards:
            db.session.add(SalesSchemeUse(scheme_id=a.scheme.id, invoice_id=inv.id,
                                          stage="bill", benefit_value=round(a.amount, 2)))

        # ---- promotions, written onto the bill --------------------------------
        # A reward line is a real invoice line at a real quantity, so its stock
        # moves exactly as a sold one does — through the same `take_stock`,
        # against this same bill, under its own reason.
        #
        # Inside a SAVEPOINT so that an offer that blows up here undoes only
        # itself. Without one, a half-written application would ride along on the
        # commit below and the bill would carry a promotion nobody can account
        # for; with the outer rollback instead, a bad scheme would cost the shop
        # the sale. Neither is acceptable at a counter with a customer at it.
        promo_free_value = 0.0
        if outcome.awards or outcome.notices:
            mark = db.session.begin_nested()
            before = (subtotal, total_tax)
            try:
                for line, product, qty in promotions.apply_to_invoice(
                        inv, outcome, sold_items, place=_place(),
                        user_id=current_user.id):
                    take_stock(product, qty, promotions.MOVEMENT_REASON)
                    subtotal += line.line_total
                    total_tax += line.tax_amount
                    promo_free_value += line.promo_value or 0
                mark.commit()
            except Exception:                        # noqa: BLE001
                mark.rollback()
                subtotal, total_tax = before
                promo_free_value = 0.0
                current_app.logger.warning("promotions skipped for this bill",
                                           exc_info=True)

        # apply discount to subtotal proportionally to keep tax reasonable
        if discount > subtotal:
            discount = subtotal
        # The bill records the discount it was actually built on, not the figure
        # typed — a ₹5,000 discount on a ₹3,000 cart took ₹3,000 off.
        inv.discount = discount

        # ---- a coupon ----------------------------------------------------------
        # A reduction the shop gives, so it joins the discount rather than the
        # tenders. Valued against the bill as it stands after the typed discount,
        # and never more than the goods are worth, so it cannot eat into the tax.
        # Checked again here whatever the counter showed: a coupon used at another
        # till a minute ago must not be spent twice.
        coupon, coupon_amt = None, 0.0
        coupon_code = (data.get("coupon_code") or "").strip()
        if coupon_code:
            coupon = vouchers.find_coupon(coupon_code)
            try:
                coupon_amt = vouchers.coupon_amount(
                    coupon, round(subtotal - discount + total_tax, 2),
                    customer_id=customer_id, discount_room=subtotal - discount)
            except vouchers.CouponError as exc:
                db.session.rollback()
                return jsonify({"error": str(exc)}), 400
            discount = round(discount + coupon_amt, 2)
            inv.discount = discount
            inv.coupon_discount = coupon_amt

        if is_interstate:
            inv.igst = round(total_tax, 2)
        else:
            inv.cgst = round(total_tax / 2, 2)
            inv.sgst = round(total_tax / 2, 2)

        inv.subtotal = round(subtotal, 2)
        pre_loyalty_total = round(subtotal - discount + total_tax, 2)

        # Loyalty redemption
        loyalty_redeemed_value = 0.0
        if customer and redeem_points > 0:
            available = customer.loyalty_points
            use = min(redeem_points, available, pre_loyalty_total)
            loyalty_redeemed_value = use * current_app.config["LOYALTY_POINT_VALUE"]
            inv.loyalty_redeemed = use
            customer.loyalty_points -= use
            db.session.add(LoyaltyTxn(
                customer_id=customer.id, points=-use,
                reason="redeem", invoice_id=inv.id
            ))

        inv.total = round(pre_loyalty_total - loyalty_redeemed_value, 2)

        # The coupon is part of what the bill comes to, so it is spent with the
        # bill — not when the money arrives.
        if coupon is not None and coupon_amt > 0:
            vouchers.redeem(coupon, inv, coupon_amt)

        if settle_later:
            # Raised, not paid. "pending" rather than a method: the drawer reads a
            # bill with no settlement rows as paid in its one method, and a bill
            # nobody has paid for yet must not count as cash in it.
            inv.payment_status = "pending"
            inv.payment_method = "pending"
            db.session.commit()
            return jsonify(dict(_sale_json(inv, []), pending=True,
                                promo_benefit=round(promo_free_value, 2)))

        refused, issued = _take_payment(inv, payments, payment_method)
        if refused is not None:
            db.session.rollback()
            return refused
        db.session.commit()
        return jsonify(dict(_sale_json(inv, issued),
                            promo_benefit=round(promo_free_value, 2)))
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


def _take_payment(inv, payments, payment_method):
    """Settle `inv` with these tenders. Returns (refusal response or None, coupons issued).

    Checked against the bill's own total, which is the first moment the amount to
    settle exists: the discount and the points redeemed both came off before it.
    Writes nothing that survives a refusal — the caller rolls back.

    The bill's On Settlement schemes come off its total first — this is the
    moment they pay out — and its Next Bill / Next Day Bill schemes issue their
    coupons once the money balances.
    """
    try:
        scheme_awards = schemes.settlement_awards(inv)
    except Exception:                                # noqa: BLE001
        current_app.logger.warning("settlement schemes skipped", exc_info=True)
        scheme_awards = []
    off = schemes.settlement_discount(scheme_awards, inv.total)
    if off > 0:
        # Part of the bill's discount, like a coupon: it came off before the
        # money, and the bill names it apart.
        inv.discount = round((inv.discount or 0) + off, 2)
        inv.scheme_discount = off
        inv.total = round((inv.total or 0) - off, 2)
    if payments is None:
        # the old single-method shape — settle the whole bill that way
        payments = [{"method": payment_method, "amount": inv.total,
                     "tendered": inv.total, "reference": None}]
    # Store credit and advances are checked against what is left on them and
    # pinned to the documents they draw on — see app/vouchers.py.
    try:
        payments = vouchers.settle_vouchers(payments, inv.customer_id)
    except ValueError as exc:
        return (jsonify({"error": str(exc)}), 400), []
    settled = round(sum(p["amount"] for p in payments), 2)
    if abs(settled - inv.total) > SETTLE_TOLERANCE:
        short = round(inv.total - settled, 2)
        return (jsonify({
            "error": (f"Payment does not settle the bill — "
                      f"₹{abs(short):.2f} {'short' if short > 0 else 'over'}. "
                      f"Bill ₹{inv.total:.2f}, entered ₹{settled:.2f}."),
            "total": inv.total, "settled": settled, "balance": short}), 400), []
    for p in payments:
        db.session.add(InvoicePayment(
            invoice_id=inv.id, method=p["method"], amount=p["amount"],
            tendered=p["tendered"], reference=p["reference"]))
    inv.payment_method = ("mixed" if len({p["method"] for p in payments}) > 1
                          else payments[0]["method"])
    inv.payment_status = "paid"

    # Loyalty is earned on money paid, so it is earned here.
    customer = Customer.query.get(inv.customer_id) if inv.customer_id else None
    if customer and inv.total >= current_app.config["LOYALTY_MIN_BILL"]:
        earned = round(inv.total * current_app.config["LOYALTY_EARN_RATE"], 2)
        inv.loyalty_earned = earned
        customer.loyalty_points = (customer.loyalty_points or 0) + earned
        customer.total_spent = (customer.total_spent or 0) + inv.total
        db.session.add(LoyaltyTxn(customer_id=customer.id, points=earned,
                                  reason="earn", invoice_id=inv.id))
    elif customer:
        customer.total_spent = (customer.total_spent or 0) + inv.total

    # …and whatever coupon this bill earns for next time — the coupon
    # campaigns' own, and the sales schemes'.
    issued = vouchers.issue_at_settlement(inv, user_id=current_user.id)
    issued += schemes.pay_out(inv, scheme_awards, user_id=current_user.id)
    return None, issued


def _sale_json(inv, issued):
    """What the till is told about a bill it has just raised or settled."""
    return {"success": True, "invoice_id": inv.id,
            "coupons_issued": [{"code": c.code, "worth": c.campaign.describe,
                                "valid_to": c.valid_to.strftime("%d-%m-%Y")}
                               for c in issued],
            "invoice_number": inv.invoice_number,
            "total": inv.total, "payment_method": inv.payment_method,
            # what to hand back, so the till can say it out loud
            "change": inv.change_given,
            # …and what to put in the bag that nobody paid for — the other thing
            # that has to physically happen before the customer walks away.
            "free_items": [
                {"name": l.product.name, "sku": l.product.sku, "qty": l.quantity,
                 "scheme": l.promo_application.scheme_name if l.promo_application else ""}
                for l in inv.items if l.promo_role == "reward"]}


@pos_bp.route("/settle/<int:iid>", methods=["POST"])
@login_required
def settle_bill(iid):
    """Take the money for a bill the counter has already raised."""
    inv = Invoice.query.get_or_404(iid)
    if inv.is_cancelled:
        return jsonify({"error": f"{inv.invoice_number} was cancelled."}), 400
    if (inv.payment_status or "") != "pending":
        return jsonify({"error": f"{inv.invoice_number} is already settled."}), 400
    data = request.get_json(silent=True) or request.form
    if "payments" not in data:
        return jsonify({"error": "no payment was entered"}), 400
    try:
        payments, method = parse_payments(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        refused, issued = _take_payment(inv, payments, method)
        if refused is not None:
            db.session.rollback()
            return refused
        db.session.commit()
    except Exception as e:                           # noqa: BLE001
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
    return jsonify(_sale_json(inv, issued))


@pos_bp.route("/api/coupon")
@login_required
def api_coupon():
    """Is this coupon good for this bill, and what does it take off?

    The counter asks when the code is applied so the saving is on screen before
    the customer pays. Advisory — `checkout` values the coupon again itself.

    A bill still short of the coupon's minimum does not make the code bad: a
    cashier may apply it before the rest of the cart is rung up. So the code is
    checked as if the minimum were met, and `below_min` says it is not met yet.
    """
    code = (request.args.get("code") or "").strip()
    amount = request.args.get("amount", type=float) or 0.0
    room = request.args.get("room", type=float)
    customer_id = request.args.get("customer_id", type=int)
    coupon = vouchers.find_coupon(code)
    min_bill = (coupon.campaign.min_bill or 0) if coupon and coupon.campaign else 0
    below = amount + vouchers.TOL < min_bill
    try:
        value = vouchers.coupon_amount(coupon, max(amount, min_bill), customer_id=customer_id,
                                       discount_room=room)
    except vouchers.CouponError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    c = coupon.campaign
    return jsonify({"ok": True, "code": coupon.code, "amount": 0.0 if below else value,
                    "below_min": below,
                    "describe": c.describe, "kind": c.kind, "value": c.value,
                    "min_bill": min_bill, "max_discount": c.max_discount,
                    "customer_id": coupon.customer_id})


@pos_bp.route("/api/credit-note")
@login_required
def api_credit_note():
    """What is left on a credit note kept as store credit."""
    note = vouchers.find_credit_note(request.args.get("code"))
    if note is None:
        return jsonify({"ok": False, "error": "No credit note with that number"})
    if (note.refund_method or "") != "store_credit":
        return jsonify({"ok": False, "error": f"{note.number} was refunded as "
                        f"{(note.refund_method or 'cash').replace('_', ' ')}, not kept as store credit"})
    return jsonify({"ok": True, "number": note.number, "total": note.total,
                    "balance": vouchers.credit_note_balance(note),
                    "customer": note.invoice.customer.name if note.invoice.customer else None})


@pos_bp.route("/api/advance-balance")
@login_required
def api_advance_balance():
    """The customer's unspent advances, for the Advance tender."""
    cid = request.args.get("customer_id", type=int)
    return jsonify({"balance": vouchers.customer_advance_balance(cid),
                    "advances": [{"number": a.number, "balance": b}
                                 for a, b in vouchers.open_advances(cid)]})


def _feedback_link(inv):
    """The customer's own feedback page for this bill — see routes/feedback.py."""
    token = URLSafeSerializer(current_app.config["SECRET_KEY"], salt="feedback").dumps(inv.id)
    return url_for("feedback.give", token=token, _external=True)


def _invoice_back_url(inv, print_view):
    """Where the bill's ← Back goes: the screen it was opened FROM.

    It used to be the Billing Counter, always — right after ringing a bill up,
    and wrong from everywhere else a bill is opened (the Invoices list, a
    customer, a delivery, the dashboard…): the Invoices screen's Back dropped
    the person on the till.

    The referring page is used only when it is this shop's own page and not
    this bill itself (which is what it is after a cancel posts back here).
    Without one, a bill opened to print has just been rung up, so it goes back
    to the counter; any other goes to the Invoices list — or the counter for a
    Store user, who is not given the list.
    """
    ref = request.referrer or ""
    if ref:
        parts = urlsplit(ref)
        root = request.script_root or ""
        this_bill = re.search(rf"/invoice/{inv.id}(/[a-z]+)?/?$", parts.path)
        own = (parts.netloc == request.host and parts.path.startswith(root + "/")
               and not this_bill)
        if own:
            return parts.path + (f"?{parts.query}" if parts.query else "")
    if print_view or (modules_mod.store_user(request)
                      and not modules_mod.store_user_allows("pos.invoice_list")):
        return url_for("pos.counter")
    return url_for("pos.invoice_list")


def _receipt(inv):
    """The bill in the shape the shop's printed receipt reads it.

    Every figure the customer sees is tax-inclusive, as on the shop's own
    receipts: RATE is the unit price with GST, GROSS is what the goods come to at
    those rates, and DISCOUNT is everything that came off on the way to the net
    total — the typed discount, a coupon, points redeemed. Each line's AMT is its
    share of the net total, so the column adds up to the Net Total to the paisa
    (the last paid line takes the rounding).

    The tax block is the bill's stored taxable value and GST, by rate — those are
    the figures the bill was raised with and the returns are filed from.
    """
    rows = []
    for n, it in enumerate(inv.items, 1):
        p = it.product
        seller = it.staff or (inv.staff if it.promo_role != "reward" else None)
        metres = (p.unit or "").lower() == "mtr" if p else False
        gst = 1 + (it.gst_rate or 0) / 100.0
        # The rate before this line's own discount; the line as charged.
        before = it.list_price if it.list_price is not None else (it.unit_price or 0)
        rows.append({
            "sno": n, "emp": seller.id if seller else "",
            "barc": (p.barcode or p.sku) if p else "", "product": p.name if p else "—",
            "mtr": it.quantity if metres else None, "qty": None if metres else it.quantity,
            "rate": round(before * gst, 2),
            "gross": round((it.quantity or 0) * before * gst, 2),
            "charged": round((it.line_total or 0) + (it.tax_amount or 0), 2),
            "free": it.is_free_item, "dis": 0, "amt": 0.0,
        })
    gross = round(sum(r["gross"] for r in rows), 2)
    net = round(inv.total or 0, 2)
    discount = round(max(0.0, gross - net), 2)
    # Each line as charged, then its share of what came off the whole bill (the
    # bill discount, a coupon, points) — the last paid line takes the rounding.
    charged = round(sum(r["charged"] for r in rows), 2)
    paid = [r for r in rows if r["charged"] > 0]
    left = net
    for r in paid[:-1]:
        r["amt"] = round(r["charged"] * net / charged, 2) if charged else 0.0
        left -= r["amt"]
    if paid:
        paid[-1]["amt"] = round(left, 2)
    for r in rows:
        if r["gross"] > 0 and not r["free"]:
            r["dis"] = round(max(0.0, 1 - r["amt"] / r["gross"]) * 100)
    by_rate = {}
    for it in inv.items:
        b = by_rate.setdefault(it.gst_rate or 0, [0.0, 0.0])
        b[0] += it.line_total or 0
        b[1] += it.tax_amount or 0
    taxes = [{"rate": rate, "taxable": round(t, 2), "tax": round(x, 2),
              "half": round(x / 2, 2)} for rate, (t, x) in sorted(by_rate.items()) if t or x]
    return {"rows": rows, "gross": gross, "discount": discount, "net": net,
            "pcs": round(sum(r["qty"] or 0 for r in rows), 3),
            "mtr": round(sum(r["mtr"] or 0 for r in rows), 3),
            "taxes": taxes}


def _invoice_page(inv, print_view):
    return render_template(
        "pos/invoice.html", inv=inv, print_view=print_view, rcpt=_receipt(inv),
        # ?download=1 saves the bill as a PDF instead of sending it to the printer
        download=request.args.get("download") == "1",
        # ?fresh=1 is the hand-off right after settling: show what changed hands
        fresh=request.args.get("fresh") == "1",
        # ?copy=delivery — the slip printed when the bill is raised, for the
        # delivery person; ?copy=customer — the one printed once it is paid.
        # Neither: a reprint, both copies.
        only_copy=(request.args.get("copy") if request.args.get("copy") in
                   ("delivery", "customer") else None),
        # ?then=settle — once the delivery copy has printed, on to Settlement
        then_url=(url_for("pos.settle", id=inv.id,
                          credit_note=request.args.get("credit_note") or None)
                  if request.args.get("then") == "settle" else None),
        back_url=_invoice_back_url(inv, print_view),
        cancel_blocked=cancellation.why_not(inv, current_user),
        coupons_issued=Coupon.query.filter_by(issued_invoice_id=inv.id).all(),
        feedback_link=_feedback_link(inv) if not inv.is_cancelled else None)


@pos_bp.route("/invoice/<int:iid>")
@login_required
def view_invoice(iid):
    inv = Invoice.query.get_or_404(iid)
    return _invoice_page(inv, False)


@pos_bp.route("/invoice/<int:iid>/print")
@login_required
def print_invoice(iid):
    inv = Invoice.query.get_or_404(iid)
    return _invoice_page(inv, True)


@pos_bp.route("/invoice/<int:iid>/cancel", methods=["POST"])
@login_required
@role_required("admin", "manager")
def cancel_invoice(iid):
    """Cancel a bill — see app/cancellation.py for what that undoes."""
    inv = Invoice.query.get_or_404(iid)
    blocked = cancellation.why_not(inv, current_user)
    if blocked:
        flash(blocked, "danger")
        return redirect(url_for("pos.view_invoice", iid=inv.id))
    try:
        outcome = cancellation.cancel(inv, current_user, request.form.get("reason"))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("pos.view_invoice", iid=inv.id))
    db.session.commit()
    back = " · ".join(f"{m.upper()} ₹{a:,.2f}" for m, a in outcome["refund"].items() if a)
    flash(f"{inv.invoice_number} cancelled. Stock, points and promotions are back"
          + (f". Hand back {back}." if back else "."), "success")
    for method, ref, amount in outcome["vouchers"]:
        flash(f"₹{amount:,.2f} is available again on {ref} "
              f"({'store credit' if method == 'credit_note' else 'advance'}).", "info")
    return redirect(url_for("pos.view_invoice", iid=inv.id))


@pos_bp.route("/invoices")
@login_required
def invoice_list():
    from datetime import datetime, timedelta, date
    from sqlalchemy import func, or_

    q         = (request.args.get("q") or "").strip()
    date_from = request.args.get("from") or ""
    date_to   = request.args.get("to") or ""
    cashier   = request.args.get("cashier", type=int)
    payment   = request.args.get("payment") or ""
    series    = (request.args.get("series") or "").strip().upper()
    min_amt   = request.args.get("min", type=float)
    max_amt   = request.args.get("max", type=float)
    status    = (request.args.get("status") or "").strip()

    query = Invoice.query.outerjoin(Customer, Invoice.customer_id == Customer.id)
    # Cancelled bills stay in the register — their numbers are part of the series
    # — but they are shown only when asked for, and never in the totals.
    if status == "cancelled":
        query = query.filter(Invoice.payment_status == "cancelled")
    elif status != "all":
        query = query.filter(Invoice.live())

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Invoice.invoice_number.ilike(like),
            Customer.name.ilike(like),
            Customer.phone.ilike(like),
        ))

    def parse(d):
        try: return datetime.strptime(d, "%Y-%m-%d").date()
        except: return None
    df = parse(date_from); dt = parse(date_to)
    if df: query = query.filter(day_from(Invoice.invoice_date, df))
    if dt: query = query.filter(day_to(Invoice.invoice_date, dt))
    if cashier: query = query.filter(Invoice.cashier_id == cashier)
    if payment: query = query.filter(Invoice.payment_method == payment)
    # On the prefix the bill was BUILT with, not on the text of its number: a
    # floor whose prefix was changed later still returns the bills it raised, and
    # a LIKE on the number would quietly miss them.
    if series: query = query.filter(Invoice.bill_prefix == series)
    if min_amt is not None: query = query.filter(Invoice.total >= min_amt)
    if max_amt is not None: query = query.filter(Invoice.total <= max_amt)

    # Ordered by the SEQUENCE within a chosen series, because that is the whole
    # question somebody filtering to one floor is asking — "…001, …002, …003,
    # and is anything missing". Across all floors the sequences interleave and
    # mean nothing together, so those stay in date order.
    if series:
        invoices = query.order_by(Invoice.fin_year.desc(),
                                  Invoice.bill_seq.desc()).limit(500).all()
    else:
        invoices = query.order_by(Invoice.invoice_date.desc()).limit(500).all()
    counted = [i for i in invoices if not i.is_cancelled]
    summary = {
        "count": len(invoices),
        "total": sum(i.total for i in counted),
        "tax":   sum(i.cgst + i.sgst + i.igst for i in counted),
    }
    cashiers = User.query.filter_by(active=True).order_by(User.full_name).all()

    # What the series dropdown offers: every prefix that has actually been billed
    # on, named by the floor it belongs to. Built from the bills rather than from
    # the floor master so a series that predates a rename, or one whose floor has
    # since been removed, is still selectable — the bills exist either way.
    #
    # `Floor` is imported at the top of this file and NOT here. By the time a
    # request runs, `app` in sys.modules is the warehouse's package again — the
    # swap in backend/app/pos_mount is long over — so a `from app.models import`
    # at this point reaches into the wrong package and raises ImportError. It did
    # exactly that, on this line. See the note above the imports.
    named = {f.prefix: f"{f.location.name} · {f.name}"
             for f in Floor.query.all() if f.location}
    used = [p for (p,) in db.session.query(Invoice.bill_prefix)
            .filter(Invoice.bill_prefix.isnot(None)).distinct().all() if p]
    series_options = sorted(
        (p, f"{p} — {named.get(p, 'the shop’s own series' if p == billing_numbers.FALLBACK_PREFIX else 'no floor uses this now')}")
        for p in used)

    return render_template(
        "pos/invoices.html",
        invoices=invoices, summary=summary, cashiers=cashiers,
        q=q, date_from=date_from, date_to=date_to,
        cashier_id=cashier, payment=payment,
        series=series, series_options=series_options, status=status,
        min_amt=min_amt if min_amt is not None else "",
        max_amt=max_amt if max_amt is not None else "",
    )
