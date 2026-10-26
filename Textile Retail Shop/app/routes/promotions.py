"""Sales promotion schemes — the master the admin keeps, and what it did.

The screens here write scheme rows and nothing else. No billing logic lives in
this file: the engine in app/promotions.py reads what is saved and the till
applies it, so an offer nobody has coded for is an offer somebody typed in.

CONDITIONS AND REWARDS ARRIVE AS JSON, not as `condition_1_qty` form fields.
They are lists of unknown length holding lists of unknown length — a scheme can
name three categories to qualify and four products to reward — and the indexed
form-field encoding for that is a parser nobody can read against a page nobody
can debug. The rows are built in the browser, posted as one field, and validated
here on the way in; anything malformed is refused with the reason rather than
half-saved.
"""
import json
from datetime import date, datetime

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func

from app import db, promotions, schemes, warehouse_items
from app.master_categories import grouped_categories
from app.models import (Category, Company, Counter, Location, Product,
                        PromotionApplication, PromotionAudit,
                        PromotionCondition, PromotionConditionItem,
                        PromotionPlace, PromotionReward, PromotionRewardItem,
                        PromotionScheme, OUT_OF_STOCK_ACTIONS, RETURN_POLICIES,
                        REWARD_SELECTION, SCHEME_TYPES, SalesScheme,
                        SalesSchemeBenefit, SalesSchemeFilter, SalesSchemeUse,
                        SCHEME_APPLICABLE, SCHEME_BENEFIT_LEGACY, SCHEME_BENEFIT_MODES,
                        SCHEME_FILTER_MODES, SCHEME_MODES)
from app.utils import generate_number, role_required

promotions_bp = Blueprint("promotions", __name__)


class Invalid(ValueError):
    """A scheme that would not mean anything, with the reason it wouldn't."""


# --------------------------------------------------------------------------
# reading the form
# --------------------------------------------------------------------------

def _date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise Invalid(f"“{raw}” is not a date the form can read (YYYY-MM-DD).")


def _rows(raw, what):
    """Parse one of the JSON fields into a list of dicts, or say why not."""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        raise Invalid(f"The {what} could not be read.")
    if not isinstance(rows, list):
        raise Invalid(f"The {what} could not be read.")
    return rows


def _targets(rows, what):
    """[(product_id, category_id), …] from the picker's rows.

    Each row names EITHER a product or a category. Both, or neither, is a row
    that cannot be matched against, so it is refused rather than stored as
    something the engine will silently skip.
    """
    out = []
    for r in rows or []:
        pid = r.get("product_id") or None
        cid = r.get("category_id") or None
        pid = int(pid) if pid else None
        cid = int(cid) if cid else None
        if bool(pid) == bool(cid):
            raise Invalid(f"Each {what} row must name one product or one "
                          f"category — not both, and not neither.")
        if pid and not db.session.get(Product, pid):
            raise Invalid(f"No product #{pid} — it may have been archived.")
        if cid and not db.session.get(Category, cid):
            raise Invalid(f"No category #{cid}.")
        out.append((pid, cid))
    if not out:
        raise Invalid(f"A scheme needs at least one {what}.")
    return out


def _read_form(form):
    """Everything the form said, validated. Raises Invalid with the reason."""
    name = (form.get("name") or "").strip()
    if not name:
        raise Invalid("The scheme needs a name.")

    scheme_type = (form.get("scheme_type") or "buy_get_free").strip()
    if scheme_type not in SCHEME_TYPES:
        raise Invalid(f"“{scheme_type}” is not a scheme type.")

    start = _date(form.get("start_date"))
    end = _date(form.get("end_date"))
    if start and end and end < start:
        raise Invalid("The scheme ends before it starts.")

    conditions = _rows(form.get("conditions_json"), "buy rules")
    if not conditions:
        raise Invalid("A scheme needs at least one thing the customer has to buy.")
    parsed_conditions = []
    for c in conditions:
        try:
            qty = float(c.get("min_qty") or 0)
        except (TypeError, ValueError):
            raise Invalid("A buy quantity must be a number.")
        if qty <= 0:
            raise Invalid("A buy quantity must be more than zero.")
        parsed_conditions.append({
            "min_qty": qty,
            "label": (c.get("label") or "").strip()[:128] or None,
            "targets": _targets(c.get("items"), "qualifying product"),
        })

    rewards = _rows(form.get("rewards_json"), "reward rules")
    if not rewards:
        raise Invalid("A scheme needs at least one reward.")
    parsed_rewards = []
    for r in rewards:
        try:
            qty = float(r.get("qty") or 0)
            value = float(r.get("value") or 0)
        except (TypeError, ValueError):
            raise Invalid("A reward quantity must be a number.")
        if qty <= 0:
            raise Invalid("A reward quantity must be more than zero.")
        if scheme_type == "buy_get_percent" and not 0 < value <= 100:
            raise Invalid("A percentage reward needs a discount between 0 and 100.")
        if scheme_type == "buy_get_price" and value < 0:
            raise Invalid("A fixed-price reward cannot be negative.")
        parsed_rewards.append({
            "qty": qty, "value": value,
            "targets": _targets(r.get("items"), "reward product"),
        })

    places = []
    for p in _rows(form.get("places_json"), "locations"):
        row = {k: (int(p[k]) if p.get(k) else None)
               for k in ("warehouse_id", "company_id", "location_id", "counter_id")}
        if any(row.values()):
            places.append(row)

    max_apps = (form.get("max_applications") or "").strip()
    try:
        max_apps = int(max_apps) if max_apps else None
    except ValueError:
        raise Invalid("The maximum number of applications must be a whole number.")
    if max_apps is not None and max_apps < 1:
        raise Invalid("A maximum of zero applications would switch the scheme off "
                      "— untick Active instead, so it is clear what happened.")

    try:
        priority = int((form.get("priority") or "100").strip() or 100)
    except ValueError:
        raise Invalid("Priority must be a whole number.")

    selection = (form.get("reward_selection") or "cheapest").strip()
    oos = (form.get("out_of_stock") or "block").strip()
    ret = (form.get("return_policy") or "reclaim_value").strip()
    for value, allowed, what in ((selection, REWARD_SELECTION, "reward choice"),
                                 (oos, OUT_OF_STOCK_ACTIONS, "out-of-stock rule"),
                                 (ret, RETURN_POLICIES, "return rule")):
        if value not in allowed:
            raise Invalid(f"“{value}” is not a {what}.")

    return {
        "name": name, "scheme_type": scheme_type,
        "code": (form.get("code") or "").strip()[:32] or None,
        "start_date": start, "end_date": end,
        "active": bool(form.get("active")),
        "stackable": bool(form.get("stackable")),
        "priority": priority, "max_applications": max_apps,
        "reward_selection": selection, "out_of_stock": oos, "return_policy": ret,
        "terms": (form.get("terms") or "").strip() or None,
        "conditions": parsed_conditions, "rewards": parsed_rewards,
        "places": places,
    }


def _write(scheme, parsed):
    """Put a validated form onto a scheme row, replacing its rules wholesale.

    Replaced rather than merged: a rule that was deleted on screen has to be
    gone, and matching up rows that have no stable identity between one edit and
    the next is a reconciliation problem invented for nothing. The applications
    already recorded against the scheme are untouched — they carry their own copy
    of what the customer was given (see PromotionApplication).
    """
    for field in ("name", "scheme_type", "start_date", "end_date", "active",
                  "stackable", "priority", "max_applications",
                  "reward_selection", "out_of_stock", "return_policy", "terms"):
        setattr(scheme, field, parsed[field])

    # The columns FIRST, then the row, then the rules that point at it. A new
    # scheme is not insertable until its name is set — and every query between
    # `add` and now would autoflush it as it stands, which is how this route
    # first tried to write a scheme called nothing.
    if scheme not in db.session:
        db.session.add(scheme)
    scheme.conditions.clear()
    scheme.rewards.clear()
    scheme.places.clear()
    db.session.flush()

    for i, c in enumerate(parsed["conditions"]):
        cond = PromotionCondition(scheme_id=scheme.id, min_qty=c["min_qty"],
                                  label=c["label"], sort_order=i)
        db.session.add(cond)
        db.session.flush()
        for pid, cid in c["targets"]:
            db.session.add(PromotionConditionItem(
                condition_id=cond.id, product_id=pid, category_id=cid))

    for i, r in enumerate(parsed["rewards"]):
        rew = PromotionReward(scheme_id=scheme.id, qty=r["qty"],
                              value=r["value"], sort_order=i)
        db.session.add(rew)
        db.session.flush()
        for pid, cid in r["targets"]:
            db.session.add(PromotionRewardItem(
                reward_id=rew.id, product_id=pid, category_id=cid))

    for p in parsed["places"]:
        db.session.add(PromotionPlace(scheme_id=scheme.id, **p))


def _form_context():
    """Everything both the new and the edit form need to draw their pickers."""
    return {
        "category_groups": grouped_categories(),
        "companies": Company.query.filter_by(active=True).order_by(Company.name).all(),
        "locations": Location.query.filter_by(active=True).order_by(Location.name).all(),
        "counters": (db.session.query(Counter, Location.name)
                     .join(Location, Location.id == Counter.location_id)
                     .filter(Counter.active.is_(True))
                     .order_by(Location.name, Counter.name).all()),
        "scheme_types": SCHEME_TYPES,
        "reward_selection": REWARD_SELECTION,
        "out_of_stock_actions": OUT_OF_STOCK_ACTIONS,
        "return_policies": RETURN_POLICIES,
        "today": date.today().isoformat(),
    }


def _as_json(scheme):
    """A saved scheme in the shape the form's JavaScript builds rows from."""
    def targets(rows):
        return [{"product_id": r.product_id, "category_id": r.category_id,
                 "label": r.label} for r in rows]
    return {
        "conditions": [{"min_qty": c.min_qty, "label": c.label or "",
                        "items": targets(c.items)} for c in scheme.conditions],
        "rewards": [{"qty": r.qty, "value": r.value, "items": targets(r.items)}
                    for r in scheme.rewards],
        "places": [{"warehouse_id": p.warehouse_id, "company_id": p.company_id,
                    "location_id": p.location_id, "counter_id": p.counter_id,
                    "label": p.label} for p in scheme.places],
    }


# --------------------------------------------------------------------------
# screens
# --------------------------------------------------------------------------

@promotions_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    """Store / Sales Promotions Scheme — the scheme screen. See app/schemes.py."""
    return render_template(
        "promotions/schemes.html",
        companies=Company.query.order_by(Company.name).all(),
        locations=Location.query.order_by(Location.name).all(),
        applicable=SCHEME_APPLICABLE, filter_modes=SCHEME_FILTER_MODES,
        benefit_modes=SCHEME_BENEFIT_MODES, attributes=schemes.ATTRIBUTES,
        scheme_modes=SCHEME_MODES, default_mode=schemes.DEFAULT_MODE, today=date.today())


@promotions_bp.route("/legacy")
@login_required
@role_required("admin", "manager")
def legacy_index():
    """The older buy-X-get-Y schemes, which still run alongside."""
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()

    query = PromotionScheme.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(PromotionScheme.name.ilike(like),
                                    PromotionScheme.code.ilike(like)))
    schemes = query.order_by(PromotionScheme.priority, PromotionScheme.id).all()

    # Status is derived (see PromotionScheme.status), so it is filtered here
    # rather than in SQL — there is no column to compare against, and inventing
    # one would be a second answer to a question the dates already settle.
    today = date.today()
    counts = {"active": 0, "scheduled": 0, "expired": 0, "inactive": 0}
    for s in schemes:
        counts[s.status(today)] += 1
    if status in counts:
        schemes = [s for s in schemes if s.status(today) == status]

    # One grouped query rather than a walk of each scheme's applications: the
    # list draws a benefit figure per row, and a shop running a dozen offers
    # would otherwise fire a dozen queries to do it.
    used = dict(db.session.query(PromotionApplication.scheme_id,
                                 func.count(PromotionApplication.id))
                .group_by(PromotionApplication.scheme_id).all())
    given = dict(db.session.query(PromotionApplication.scheme_id,
                                  func.coalesce(func.sum(
                                      PromotionApplication.benefit_value), 0))
                 .group_by(PromotionApplication.scheme_id).all())

    return render_template("promotions/list.html", schemes=schemes, q=q,
                           status=status, counts=counts, used=used, given=given,
                           describe=promotions.describe, today=today)


@promotions_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def new_scheme():
    if request.method == "POST":
        try:
            parsed = _read_form(request.form)
        except Invalid as exc:
            flash(str(exc), "danger")
            return render_template("promotions/form.html", scheme=None,
                                   saved=request.form, rules="{}",
                                   **_form_context())
        # Settled before anything is put in the session, because both of these
        # are queries and a query autoflushes whatever is pending.
        code = parsed["code"] or generate_number("PROMO", PromotionScheme, "code")
        if PromotionScheme.query.filter_by(code=code).first():
            flash(f"Scheme code {code} is already in use.", "danger")
            return render_template("promotions/form.html", scheme=None,
                                   saved=request.form, rules="{}",
                                   **_form_context())
        scheme = PromotionScheme(code=code, created_by_id=current_user.id,
                                 updated_by_id=current_user.id)
        _write(scheme, parsed)
        db.session.commit()
        flash(f"Scheme {scheme.code} created — {promotions.describe(scheme)}.",
              "success")
        return redirect(url_for("promotions.detail", sid=scheme.id))

    return render_template("promotions/form.html", scheme=None, saved={},
                           rules="{}", **_form_context())


@promotions_bp.route("/<int:sid>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def edit_scheme(sid):
    scheme = PromotionScheme.query.get_or_404(sid)
    if request.method == "POST":
        try:
            parsed = _read_form(request.form)
        except Invalid as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return render_template("promotions/form.html", scheme=scheme,
                                   saved=request.form,
                                   rules=json.dumps(_as_json(scheme)),
                                   **_form_context())
        if parsed["code"] and parsed["code"] != scheme.code:
            clash = PromotionScheme.query.filter_by(code=parsed["code"]).first()
            if clash and clash.id != scheme.id:
                flash(f"Scheme code {parsed['code']} is already in use.", "danger")
                return render_template("promotions/form.html", scheme=scheme,
                                       saved=request.form,
                                       rules=json.dumps(_as_json(scheme)),
                                       **_form_context())
            scheme.code = parsed["code"]
        scheme.updated_by_id = current_user.id
        _write(scheme, parsed)
        db.session.commit()
        flash("Scheme updated.", "success")
        return redirect(url_for("promotions.detail", sid=scheme.id))

    return render_template("promotions/form.html", scheme=scheme, saved={},
                           rules=json.dumps(_as_json(scheme)), **_form_context())


@promotions_bp.route("/<int:sid>")
@login_required
@role_required("admin", "manager")
def detail(sid):
    scheme = PromotionScheme.query.get_or_404(sid)
    apps = (PromotionApplication.query.filter_by(scheme_id=sid)
            .order_by(PromotionApplication.id.desc()).limit(200).all())
    trail = (PromotionAudit.query.filter_by(scheme_id=sid)
             .order_by(PromotionAudit.id.desc()).limit(200).all())
    summary = {
        "bills": len({a.invoice_id for a in apps}),
        "times": sum(a.times_applied or 0 for a in apps),
        "qualifying": round(sum(a.qualifying_qty or 0 for a in apps), 3),
        "free": round(sum(a.reward_qty or 0 for a in apps), 3),
        "benefit": round(sum(a.benefit_value or 0 for a in apps), 2),
        # The ones nobody got, which is the number that says an offer is
        # advertised and undeliverable.
        "blocked": sum(1 for t in trail if t.event == "blocked_no_stock"),
    }
    return render_template("promotions/detail.html", scheme=scheme, apps=apps,
                           trail=trail, summary=summary,
                           description=promotions.describe(scheme),
                           today=date.today())


@promotions_bp.route("/<int:sid>/toggle", methods=["POST"])
@login_required
@role_required("admin", "manager")
def toggle(sid):
    scheme = PromotionScheme.query.get_or_404(sid)
    scheme.active = not scheme.active
    scheme.updated_by_id = current_user.id
    db.session.commit()
    flash(f"{scheme.code} is now {'active' if scheme.active else 'inactive'}.",
          "success" if scheme.active else "info")
    return redirect(request.referrer or url_for("promotions.legacy_index"))


@promotions_bp.route("/<int:sid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete(sid):
    """Remove a scheme that never ran. One that has is deactivated instead.

    A scheme with bills against it is a record of goods that left the shop, and
    deleting it would leave those applications pointing at nothing — the free
    garments would still be gone, with no offer to explain them. So the button
    changes its meaning rather than refusing: switched off, kept, still
    reportable.
    """
    scheme = PromotionScheme.query.get_or_404(sid)
    if PromotionApplication.query.filter_by(scheme_id=sid).first():
        scheme.active = False
        scheme.updated_by_id = current_user.id
        db.session.commit()
        flash(f"{scheme.code} has been used on bills, so it was deactivated "
              f"rather than deleted — its history stays reportable.", "warning")
        return redirect(url_for("promotions.detail", sid=sid))
    PromotionAudit.query.filter_by(scheme_id=sid).delete()
    db.session.delete(scheme)
    db.session.commit()
    flash("Scheme deleted — it had never been applied to a bill.", "info")
    return redirect(url_for("promotions.legacy_index"))


# --------------------------------------------------------------------------
# the pickers
# --------------------------------------------------------------------------

@promotions_bp.route("/api/products")
@login_required
@role_required("admin", "manager")
def api_products():
    """Search the product master for the qualifying / reward pickers.

    A search rather than a dropdown: the catalogue runs to thousands of variants
    — one product per size and colour — and a select carrying all of them is a
    page nobody can use and a payload nobody should send.
    """
    q = (request.args.get("q") or "").strip()
    query = Product.query.filter(Product.active.is_(True))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like),
                                    Product.sku.ilike(like),
                                    Product.barcode.ilike(like)))
    rows = query.order_by(Product.name).limit(40).all()
    return jsonify([{"id": p.id, "sku": p.sku, "barcode": p.barcode, "name": p.name,
                     "price": p.selling_price, "stock": p.stock_qty,
                     "category": p.category.name if p.category else ""}
                    for p in rows])


@promotions_bp.route("/api/preview", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_preview():
    """Try a cart against the live schemes without billing anything.

    An offer that reads correctly on the form and gives nothing at the till is
    the whole reason this exists — a category that holds no stock, a priority
    that lets an earlier scheme take the garments first. This answers it before
    a customer does.
    """
    data = request.get_json(silent=True) or {}
    outcome = promotions.evaluate(data.get("items") or [],
                                  choices=data.get("choices") or {})
    return jsonify(outcome.to_json())


# --------------------------------------------------------------------------
# the scheme screen's API — Store / Sales Promotions Scheme (app/schemes.py)
# --------------------------------------------------------------------------

def _scheme_json(s):
    return {
        "id": s.id, "code": s.code, "name": s.name,
        "company_id": s.company_id, "location_id": s.location_id,
        "valid_from": s.valid_from.isoformat() if s.valid_from else "",
        "valid_to": s.valid_to.isoformat() if s.valid_to else "",
        "applicable": s.applicable, "mode": s.mode or "bill_item", "active": bool(s.active),
        "status": s.status(),
        "filters": [{"mode": f.mode, "label": schemes.filter_label(f.mode),
                     "data": schemes._data(f), "summary": schemes.filter_summary(f)}
                    for f in s.filters],
        "benefits": [{"mode": b.mode, "qty_from": b.qty_from or 0, "qty_to": b.qty_to or 0,
                      "price_from": b.price_from or 0, "price_to": b.price_to or 0,
                      "value": b.value or 0, "additional": b.additional or 0,
                      "flat": bool(b.flat), "item_code": b.item_code or ""}
                     for b in s.benefits],
    }


def _num(v):
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        raise Invalid(f"“{v}” is not a number")


def _read_scheme(data):
    """The screen's JSON, checked. Raises Invalid with the reason."""
    name = (data.get("name") or "").strip()
    if not name:
        raise Invalid("Give the scheme a name.")
    applicable = data.get("applicable") or ""
    if applicable not in dict(SCHEME_APPLICABLE):
        raise Invalid("Choose when the scheme applies (Applicable).")
    mode = data.get("mode") or schemes.DEFAULT_MODE.get(applicable, "bill_item")
    if mode not in dict(SCHEME_MODES):
        raise Invalid("Choose the Mode — On Bill Value or On Bill Item.")
    vf, vt = _date(data.get("valid_from")), _date(data.get("valid_to"))
    if vf and vt and vt < vf:
        raise Invalid("The validity ends before it starts.")
    modes = dict(SCHEME_FILTER_MODES)
    filters = []
    for f in data.get("filters") or []:
        if f.get("mode") not in modes:
            raise Invalid(f"Unknown filter mode {f.get('mode')!r}.")
        d = f.get("data") or {}
        if not isinstance(d, dict):
            raise Invalid("A filter row is malformed.")
        filters.append((f["mode"], d))
    bmodes = dict(SCHEME_BENEFIT_MODES + SCHEME_BENEFIT_LEGACY)
    benefits = []
    for b in data.get("benefits") or []:
        if b.get("mode") not in bmodes:
            raise Invalid(f"Unknown benefit mode {b.get('mode')!r}.")
        row = {k: _num(b.get(k)) for k in ("qty_from", "qty_to", "price_from",
                                            "price_to", "value", "additional")}
        if row["qty_to"] and row["qty_to"] < row["qty_from"]:
            raise Invalid("A benefit's qty range ends before it starts.")
        if row["price_to"] and row["price_to"] < row["price_from"]:
            raise Invalid("A benefit's price range ends before it starts.")
        if b["mode"] in ("discount_pct", "discounted_item") and (row["value"] > 100 or row["additional"] > 100):
            raise Invalid("A discount cannot be more than 100%.")
        if b["mode"] == "combo" and row["qty_from"] < 1:
            raise Invalid("Combo Offer: set Qty Between (from) to the pieces that make one combo.")
        row["flat"] = bool(b.get("flat")) and b["mode"] in ("fixed", "combo")
        row["item_code"] = (str(b.get("item_code") or "").strip().upper() or None)             if b["mode"] == "discounted_item" else None
        if b["mode"] == "discounted_item" and not row["item_code"]:
            raise Invalid("Discounted Item: choose the item that gets the discount.")
        if b["mode"] == "free_item" and (row["value"] < 1 or row["qty_from"] < 1):
            raise Invalid("Free item: Qty From is how many to buy and Value how many "
                          "come free — both at least 1.")
        if not row["value"]:
            raise Invalid(f"{bmodes[b['mode']]}: enter its Value.")
        benefits.append((b["mode"], row))
    if not benefits:
        raise Invalid("Add at least one benefit — what the qualifying goods get.")

    def _id(v):
        return int(v) if str(v or "").isdigit() else None
    return {"name": name[:128], "applicable": applicable, "mode": mode, "valid_from": vf, "valid_to": vt,
            "company_id": _id(data.get("company_id")), "location_id": _id(data.get("location_id")),
            "active": bool(data.get("active", True)), "filters": filters, "benefits": benefits}


@promotions_bp.route("/api/schemes")
@login_required
@role_required("admin", "manager")
def api_schemes():
    """Search: the sales schemes, newest first — and the older offers beside them."""
    q = (request.args.get("q") or "").strip()
    query = SalesScheme.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(SalesScheme.name.ilike(like), SalesScheme.code.ilike(like)))
    rows = query.order_by(SalesScheme.id.desc()).limit(200).all()
    used = dict(db.session.query(SalesSchemeUse.scheme_id, func.count(SalesSchemeUse.id))
                .group_by(SalesSchemeUse.scheme_id).all())
    legacy = PromotionScheme.query.order_by(PromotionScheme.id.desc()).limit(100).all()

    def _d(v):
        return v.strftime("%d-%m-%Y") if v else "…"
    return jsonify({
        "schemes": [{"id": s.id, "code": s.code, "name": s.name,
                     "applicable": s.applicable_label, "status": s.status(),
                     "valid": f"{_d(s.valid_from)} – {_d(s.valid_to)}",
                     "describe": schemes.describe(s), "bills": used.get(s.id, 0)} for s in rows],
        "legacy": [{"id": p.id, "code": p.code, "name": p.name, "status": p.status(),
                    "describe": promotions.describe(p),
                    "url": url_for("promotions.detail", sid=p.id)} for p in legacy
                   if not q or q.lower() in (p.name + p.code).lower()],
    })


@promotions_bp.route("/api/schemes/<int:sid>")
@login_required
@role_required("admin", "manager")
def api_scheme(sid):
    return jsonify(_scheme_json(SalesScheme.query.get_or_404(sid)))


@promotions_bp.route("/api/schemes", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_scheme_save():
    """Save — a new scheme, or the one being edited (`id`)."""
    data = request.get_json(silent=True) or {}
    try:
        parsed = _read_scheme(data)
    except Invalid as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    sid = data.get("id")
    s = SalesScheme.query.get(int(sid)) if str(sid or "").isdigit() else None
    if s is None:
        s = SalesScheme(code=generate_number("SCH", SalesScheme, "code"),
                        created_by_id=current_user.id)
        db.session.add(s)
    for k in ("name", "applicable", "mode", "valid_from", "valid_to", "company_id", "location_id", "active"):
        setattr(s, k, parsed[k])
    # The rows are replaced wholesale — the screen always sends all of them.
    # A Coupon benefit keeps the campaign its coupons were issued from.
    keep_campaign = next((b.campaign_id for b in s.benefits if b.mode == "coupon" and b.campaign_id), None)
    s.filters = [SalesSchemeFilter(mode=m, data=json.dumps(d), sort_order=i)
                 for i, (m, d) in enumerate(parsed["filters"])]
    s.benefits = [SalesSchemeBenefit(mode=m, sort_order=i,
                                     campaign_id=keep_campaign if m == "coupon" else None, **row)
                  for i, (m, row) in enumerate(parsed["benefits"])]
    db.session.commit()
    return jsonify({"ok": True, "scheme": _scheme_json(s)})


@promotions_bp.route("/api/schemes/<int:sid>/toggle", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_scheme_toggle(sid):
    s = SalesScheme.query.get_or_404(sid)
    s.active = not s.active
    db.session.commit()
    return jsonify({"ok": True, "active": s.active, "status": s.status()})


@promotions_bp.route("/api/schemes/<int:sid>/delete", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_scheme_delete(sid):
    """Delete a scheme no bill has used; one that has is switched off instead,
    so the bills that carry it can still say what it was."""
    s = SalesScheme.query.get_or_404(sid)
    if SalesSchemeUse.query.filter_by(scheme_id=s.id).first():
        s.active = False
        db.session.commit()
        return jsonify({"ok": True, "deleted": False,
                        "message": f"{s.code} has been used on bills, so it was switched off instead."})
    db.session.delete(s)
    db.session.commit()
    return jsonify({"ok": True, "deleted": True})


@promotions_bp.route("/api/scheme-values")
@login_required
@role_required("admin", "manager")
def api_scheme_values():
    """The values the filter dropdowns offer: the warehouse's masters (Masters →
    Product Attributes, its suppliers and purchase invoices), merged with what
    the shop's own products carry. One spelling per value, whatever its case."""
    def distinct(col):
        return [str(v) for (v,) in db.session.query(col).filter(col.isnot(None)).distinct()
                if str(v).strip()]

    def merge(*lists):
        seen = {}
        for lst in lists:
            for v in lst or []:
                seen.setdefault(str(v).strip().upper(), str(v).strip())
        return sorted(seen.values(), key=str.upper)

    try:
        wh = warehouse_items.fetch_vocabulary()
    except Exception:                                # noqa: BLE001
        wh = {}
    # the scheme's attribute key -> the warehouse master's
    wh_key = {"brand": "brand", "material": "material", "pattern": "pattern",
              "colour": "color", "style": "style", "size": "size",
              "type": "product_type", "sleeve": "sleeve", "fit": "fit"}
    out = {"category": merge([c.name for c in Category.query.all()])}
    for key, _label, field in schemes.ATTRIBUTES:
        if field:
            out[key] = merge(wh.get(wh_key.get(key, key)), distinct(getattr(Product, field)))
    out["supplier"] = merge(wh.get("supplier"), distinct(Product.supplier))
    out["invoice"] = merge(wh.get("invoice"), distinct(Product.purchase_invoice))
    out["design_code"] = merge(wh.get("design_no"), distinct(Product.design_no))
    out["marker"] = merge(distinct(Product.marker))
    return jsonify(out)


@promotions_bp.route("/api/simulate", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_simulate():
    """Try the scheme on the screen against a sample bill — saved or not, and
    whatever its Applicable says (On Simulation schemes run nowhere else)."""
    data = request.get_json(silent=True) or {}
    try:
        parsed = _read_scheme(data.get("scheme") or {})
    except Invalid as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # An unsaved scheme, built in memory and never added to the session.
    s = SalesScheme(code=(data.get("scheme") or {}).get("code") or "(unsaved)",
                    name=parsed["name"], applicable=parsed["applicable"], mode=parsed["mode"])
    s.filters = [SalesSchemeFilter(mode=m, data=json.dumps(d)) for m, d in parsed["filters"]]
    s.benefits = [SalesSchemeBenefit(mode=m, **row) for m, row in parsed["benefits"]]
    agreement = (data.get("agreement_code") or "").strip() or None
    lines, shown = [], []
    for n, it in enumerate(data.get("items") or []):
        p = warehouse_items.resolve_scan(str(it.get("code") or "").strip())
        if p is None:
            shown.append({"code": it.get("code"), "error": "no such product"})
            continue
        qty = _num(it.get("qty") or 1)
        lines.append(schemes.Line(n, p, qty, p.selling_price, p.gst_rate, _num(it.get("manual_pct"))))
        shown.append({"n": n, "code": it.get("code"), "name": p.name, "qty": qty,
                      "price": round(p.selling_price * (1 + (p.gst_rate or 0) / 100), 2)})
    bill = sum(l.charged for l in lines)
    a = schemes.award(s, lines, bill, agreement)
    elig = {l.idx for l in schemes.eligible(s, lines, bill, agreement)}
    for row in shown:
        if "n" in row:
            pct = a.line_pct.get(row["n"], 0) if a else 0
            row.update(pct=pct, eligible=row["n"] in elig,
                       amount=round(row["price"] * row["qty"] * (1 - pct / 100), 2))
    for obj in list(db.session.new):   # the in-memory scheme must never be saved
        db.session.expunge(obj)
    return jsonify({"ok": True, "lines": shown,
                    "applies": a is not None and (a.amount > 0 or a.coupon_value > 0 or a.points > 0),
                    "saving": round(a.amount, 2) if a else 0,
                    "coupon": round(a.coupon_value, 2) if a else 0,
                    "points": round(a.points, 2) if a else 0,
                    "bill": round(bill, 2)})
