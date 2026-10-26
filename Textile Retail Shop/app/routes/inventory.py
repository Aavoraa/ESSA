import json
import urllib.request
import urllib.parse
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.paging import paged
from app import warehouse_items
from app.models import Floor, Product, Category, StockMovement
from app.master_categories import SECTION_ORDER, grouped_categories
from app.utils import role_required

inventory_bp = Blueprint("inventory", __name__)


def _http_get_json(url, timeout=6):
    """Small stdlib HTTP GET returning parsed JSON (no extra dependency)."""
    req = urllib.request.Request(url, headers={"User-Agent": "TaquaSilks-POS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _clean(value):
    """Trim a value and drop useless placeholders some APIs return."""
    v = (value or "").strip()
    return "" if v.upper() in ("N/A", "NA", "NONE", "-") else v


def lookup_barcode_online(code):
    """Look up an international EAN/UPC barcode in public product databases.

    Requires internet. Returns a normalized dict {found, name, brand, ...} or
    {"found": False}. Never raises — callers fall back to manual entry.
    Coverage is best for packaged/branded goods; many textile SKUs won't be found.
    """
    code = (code or "").strip()
    if not code:
        return {"found": False}

    # 1) UPCitemdb trial endpoint — general merchandise / global brands (no key).
    try:
        data = _http_get_json(
            "https://api.upcitemdb.com/prod/trial/lookup?upc=" + urllib.parse.quote(code)
        )
        items = data.get("items") or []
        if items:
            it = items[0]
            name = (it.get("title") or "").strip()
            if name:
                images = it.get("images") or []
                return {
                    "found": True, "source": "upcitemdb", "code": code,
                    "name": name,
                    "brand": _clean(it.get("brand")),
                    "description": _clean(it.get("description")),
                    "category": _clean(it.get("category")),
                    "image": images[0] if images else "",
                }
    except Exception:
        pass

    # 2) Open Food Facts — packaged goods (no key).
    try:
        data = _http_get_json(
            "https://world.openfoodfacts.org/api/v0/product/"
            + urllib.parse.quote(code) + ".json"
        )
        if data.get("status") == 1:
            p = data.get("product") or {}
            name = (p.get("product_name") or p.get("generic_name") or "").strip()
            if name:
                return {
                    "found": True, "source": "openfoodfacts", "code": code,
                    "name": name,
                    "brand": _clean(p.get("brands")),
                    "description": _clean(p.get("generic_name")),
                    "category": _clean((p.get("categories") or "").split(",")[0]),
                    "image": p.get("image_url") or "",
                }
    except Exception:
        pass

    return {"found": False, "code": code}


def _floors():
    """Every storey a product can be placed on, ordered as a building is."""
    return (Floor.query.filter_by(active=True)
            .order_by(Floor.location_id, Floor.sort_order, Floor.name).all())


@inventory_bp.route("/")
@login_required
def list_products():
    q = request.args.get("q", "").strip()
    cat_id = request.args.get("category", type=int)
    query = Product.query.filter_by(active=True)
    if q:
        query = query.filter(
            (Product.name.ilike(f"%{q}%")) | (Product.sku.ilike(f"%{q}%")) |
            (Product.fabric.ilike(f"%{q}%")) | (Product.color.ilike(f"%{q}%"))
        )
    if cat_id:
        query = query.filter_by(category_id=cat_id)
    # a page at a time: the catalogue is the warehouse's (416K), and every row
    # draws a QR — the whole list took minutes and held the server (app/paging)
    page = paged(query.order_by(Product.name, Product.id))
    return render_template("inventory/list.html", products=page.items, page=page,
                           category_groups=grouped_categories(), q=q, cat_id=cat_id)


@inventory_bp.route("/api/barcode-lookup")
@login_required
@role_required("admin", "manager")
def api_barcode_lookup():
    """Scan-to-add: resolve a scanned barcode to a product.

    Order: (1) if we already stock this barcode, say so; (2) otherwise try the
    online product databases; (3) if nothing is found, the caller still opens the
    New Product form with the barcode pre-filled for manual entry.
    """
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "No barcode provided"}), 400

    # Our own shelves first, then the warehouse — a tag off a warehouse item is
    # answered by the warehouse's own record, never by a guess from a public
    # database that has never heard of this shop's stock.
    existing = warehouse_items.resolve_scan(code)
    if existing:
        return jsonify({
            "existing": True, "code": code,
            "product": {
                "id": existing.id, "name": existing.name, "sku": existing.sku,
                "stock": existing.stock_qty, "unit": existing.unit,
            },
            "edit_url": url_for("inventory.edit_product", pid=existing.id),
        })

    info = lookup_barcode_online(code)
    info["existing"] = False
    info["add_url"] = url_for("inventory.new_product")
    return jsonify(info)


def _scheme_fields(p, form):
    """The attributes a sales scheme can filter on that the warehouse doesn't
    send (brand, style, sleeve, marker) or that a shop-made item needs set by
    hand (supplier, purchase invoice and date). See app/schemes.py."""
    from datetime import datetime as _dt
    for key in ("brand", "style", "sleeve", "marker", "supplier", "purchase_invoice"):
        if key in form:
            setattr(p, key, (form.get(key) or "").strip() or None)
    if "purchase_date" in form:
        raw = (form.get("purchase_date") or "").strip()
        try:
            p.purchase_date = _dt.strptime(raw, "%Y-%m-%d").date() if raw else None
        except ValueError:
            p.purchase_date = None


@inventory_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def new_product():
    category_groups = grouped_categories()
    # Values a barcode scan may have pre-filled (all optional).
    prefill = {
        "barcode": (request.args.get("barcode") or "").strip(),
        "name": (request.args.get("name") or "").strip(),
        "description": (request.args.get("description") or "").strip(),
    }
    if request.method == "POST":
        try:
            p = Product(
                sku=request.form["sku"].strip(),
                name=request.form["name"].strip(),
                description=request.form.get("description", ""),
                category_id=int(request.form["category_id"]) if request.form.get("category_id") else None,
                hsn_code=request.form.get("hsn_code", "5208"),
                unit=request.form.get("unit", "pcs"),
                cost_price=float(request.form.get("cost_price") or 0),
                selling_price=float(request.form["selling_price"]),
                gst_rate=float(request.form.get("gst_rate") or 5),
                stock_qty=float(request.form.get("stock_qty") or 0),
                reorder_level=float(request.form.get("reorder_level") or 5),
                color=request.form.get("color", ""),
                size=request.form.get("size", ""),
                fabric=request.form.get("fabric", ""),
                floor_id=request.form.get("floor_id", type=int) or None,
            )
            _scheme_fields(p, request.form)
            db.session.add(p)
            db.session.flush()
            if p.stock_qty > 0:
                db.session.add(StockMovement(
                    product_id=p.id, change=p.stock_qty, reason="opening", reference="init"
                ))
            db.session.commit()
            flash(f"Product '{p.name}' added.", "success")
            return redirect(url_for("inventory.list_products"))
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {e}", "danger")
    return render_template("inventory/form.html", product=None,
                           category_groups=category_groups, prefill=prefill,
                           floors=_floors())


@inventory_bp.route("/<int:pid>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def edit_product(pid):
    p = Product.query.get_or_404(pid)
    if request.method == "POST":
        old_stock = p.stock_qty
        p.sku = request.form["sku"].strip()
        p.name = request.form["name"].strip()
        p.description = request.form.get("description", "")
        p.category_id = int(request.form["category_id"]) if request.form.get("category_id") else None
        p.hsn_code = request.form.get("hsn_code", "5208")
        p.unit = request.form.get("unit", "pcs")
        # A warehouse item's prices belong to the warehouse and are copied down
        # on every sync. Ignored here rather than saved-and-reverted: a form that
        # appears to accept a change and quietly loses it is worse than one that
        # does not offer it. The shop's own products are untouched by any sync,
        # so those still save.
        if not p.warehouse_id:
            p.cost_price = float(request.form.get("cost_price") or 0)
            p.selling_price = float(request.form["selling_price"])
        p.gst_rate = float(request.form.get("gst_rate") or 5)
        p.stock_qty = float(request.form.get("stock_qty") or 0)
        p.reorder_level = float(request.form.get("reorder_level") or 5)
        p.color = request.form.get("color", "")
        p.size = request.form.get("size", "")
        p.fabric = request.form.get("fabric", "")
        _scheme_fields(p, request.form)
        p.floor_id = request.form.get("floor_id", type=int) or None
        if p.stock_qty != old_stock:
            db.session.add(StockMovement(
                product_id=p.id, change=p.stock_qty - old_stock,
                reason="adjustment", reference=f"user:{current_user.username}"
            ))
        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("inventory.list_products"))
    # The garments behind this product, each with the QR on its own tag. Read
    # live rather than copied: pieces are sold and re-labelled upstairs, and a
    # stale list of them would be worse than none.
    units = warehouse_items.fetch_units(p.warehouse_id) if p.warehouse_id else []
    return render_template("inventory/form.html", product=p, units=units,
                           category_groups=grouped_categories(),
                           floors=_floors())


@inventory_bp.route("/<int:pid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_product(pid):
    p = Product.query.get_or_404(pid)
    p.active = False
    db.session.commit()
    flash("Product archived.", "info")
    return redirect(url_for("inventory.list_products"))


@inventory_bp.route("/categories", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def categories():
    if request.method == "POST":
        name = request.form["name"].strip()
        if name and not Category.query.filter_by(name=name).first():
            section = (request.form.get("section") or "").strip().upper()
            db.session.add(Category(
                name=name,
                section=section if section in SECTION_ORDER else None,
                description=request.form.get("description", ""),
            ))
            db.session.commit()
            flash("Category added.", "success")
        return redirect(url_for("inventory.categories"))

    # One grouped count rather than a query per row: the master runs to hundreds
    # of codes and the template's `c.products|length` was a lookup for each one.
    counts = dict(
        db.session.query(Product.category_id, func.count(Product.id))
        .group_by(Product.category_id).all()
    )
    groups = grouped_categories()
    return render_template("inventory/categories.html", groups=groups, counts=counts,
                           sections=SECTION_ORDER,
                           total=sum(len(cats) for _, cats in groups))


@inventory_bp.route("/sync-warehouse", methods=["POST"])
@login_required
@role_required("admin", "manager")
def sync_warehouse():
    """Pull the warehouse's items in now, rather than waiting for a restart."""
    if not warehouse_items.available():
        flash("The warehouse database isn't reachable from here — nothing synced.", "warning")
        return redirect(request.referrer or url_for("inventory.list_products"))
    r = warehouse_items.sync_warehouse_items()
    if r["added"] or r["updated"]:
        flash(f"Warehouse sync: {r['added']} item(s) added, {r['updated']} updated.", "success")
    else:
        flash("Warehouse sync: already up to date.", "info")
    return redirect(request.referrer or url_for("inventory.list_products"))


@inventory_bp.route("/movements")
@login_required
@role_required("admin", "manager")
def movements():
    moves = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(200).all()
    return render_template("inventory/movements.html", movements=moves)


# ---------- Labels ----------
#
# The shop prints the warehouse's QR and nothing else. It used to mint its own
# EAN-13 for every product on top of that, which meant two codes on one garment
# for the same item — and only one of them meant anything upstairs. A tag that
# scans in the shop but not in the warehouse is worse than no tag, so the shop
# stopped issuing them: the QR the warehouse put on the item is the code.
@inventory_bp.route("/<int:pid>/label")
@inventory_bp.route("/<int:pid>/barcode")           # old link, kept working
@login_required
def product_label(pid):
    """Printable QR label(s) for a single product."""
    p = Product.query.get_or_404(pid)
    copies = request.args.get("copies", default=1, type=int) or 1
    copies = max(1, min(copies, 200))
    return render_template("inventory/labels.html", products=[p] * copies, single=p)


@inventory_bp.route("/<int:pid>/pieces")
@login_required
def product_pieces(pid):
    """Printable tag for every garment of this product.

    One label per piece, carrying the code and QR the warehouse issued for it —
    so a tag reprinted in the shop is the same tag, and scanning it upstairs
    still finds the piece it belongs to.
    """
    p = Product.query.get_or_404(pid)
    units = warehouse_items.fetch_units(p.warehouse_id) if p.warehouse_id else []
    return render_template("inventory/pieces.html", product=p, units=units)


@inventory_bp.route("/labels")
@inventory_bp.route("/barcodes")                    # old link, kept working
@login_required
def labels_sheet():
    """Printable sheet of QR labels for all products (optionally filtered)."""
    cat_id = request.args.get("category", type=int)
    query = Product.query.filter_by(active=True)
    if cat_id:
        query = query.filter_by(category_id=cat_id)
    # a sheet per page of the list, for the same reason as the list itself
    page = paged(query.order_by(Product.name, Product.id))
    return render_template("inventory/labels.html", products=page.items, page=page, single=None)
