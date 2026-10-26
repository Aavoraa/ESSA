"""Store → Stock Inward and Store → Transfer Goods.

STOCK INWARD is where a branch counts in what was sent to it: a warehouse
dispatch addressed to it (Stock Outward, posted), or goods another branch sent
across. Scan what came out of the box, correct a count, Receive — and only then
is it this branch's stock. Received from a warehouse, the dispatch is marked
received in the warehouse too, with the counted quantities, so a shortfall shows
there as the transfer discrepancy it is.

TRANSFER GOODS is the other direction: goods leaving this branch, back to a
warehouse or across to another branch. Saving takes them off this branch's
shelves at once. To a warehouse, the note is written to the warehouse as a
posted dispatch from this store, and the warehouse counts it in on its own Stock
Inward; to a branch, it waits in that branch's Stock Inward, unless Immediate
Transfer moved it straight in.

The warehouse is reached through the shop's own connection to its database
(warehouse_items._connect). Reading is what the shop does everywhere; the writes
here are the only ones — this store's own notes, and the receipt of a dispatch
addressed to it.
"""
import csv
import io
from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app import places
from app import transfers
from app import warehouse_items as wh
from app.models import (Location, LocationStock, Product, StockMovement,
                        StoreTransfer, StoreTransferLine, TransferReceipt, User)

store_stock_bp = Blueprint("storestock", __name__)


# ---- shared ------------------------------------------------------------------
def _norm(name):
    return " ".join(str(name or "").split()).lower()


def _locations():
    """The branches this till may work for — the warehouse's, when it was opened
    from one (places.picker_options narrows the same way)."""
    return [db.session.get(Location, l["id"]) for l in places.picker_options()["locations"]]


def _here(locs):
    """The branch the till chose, else the first one it may use."""
    lid = session.get("location_id")
    for l in locs:
        if l.id == lid:
            return l
    return locs[0] if locs else None


def _warehouses():
    """[{id, name}] of the warehouses, read from the warehouse; [] without one."""
    con = wh._connect()
    if con is None:
        return []
    try:
        rows = con.execute("SELECT id, name, active FROM warehouses ORDER BY name").fetchall()
    except SQLAlchemyError:
        return []
    finally:
        con.close()
    return [{"id": r["id"], "name": r["name"]} for r in rows
            if r.get("active") is None or r.get("active")]


def _product_out(p, location_id=None):
    here = None
    if location_id:
        row = LocationStock.query.filter_by(location_id=location_id, product_id=p.id).first()
        here = round(row.qty, 3) if row else None
    return {"product_id": p.id, "sku": p.sku, "barcode": p.barcode or "", "name": p.name,
            "size": getattr(p, "size", None) or "", "stock": round(p.stock_qty or 0, 3),
            "stock_here": here}


def _bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ==== STOCK INWARD ===============================================================
def _wh_pending(locs):
    """Warehouse dispatches on the road to these branches: posted, addressed by
    name to one of them, and not a store's own return."""
    names = {_norm(l.name): l for l in locs}
    con = wh._connect()
    if con is None:
        return []
    sql = ("SELECT o.id AS id, o.code AS code, o.date AS date, o.to_destination AS dest, "
           "o.from_location AS from_loc, o.packed_by AS packed_by, "
           "COUNT(l.id) AS n, SUM(COALESCE(l.qty, 0)) AS qty "
           "FROM stock_outwards o LEFT JOIN stock_outward_lines l ON l.outward_id = o.id "
           "WHERE o.status = 'posted' {extra} "
           "GROUP BY o.id, o.code, o.date, o.to_destination, o.from_location, o.packed_by "
           "ORDER BY o.id DESC")
    try:
        try:
            rows = con.execute(sql.format(extra="AND o.from_store_id IS NULL")).fetchall()
        except SQLAlchemyError:
            con.rollback()
            rows = con.execute(sql.format(extra="")).fetchall()
        whs = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM warehouses").fetchall()}
        src = {r["id"]: r["from_warehouse_id"] for r in con.execute(
            "SELECT id, from_warehouse_id FROM stock_outwards WHERE status = 'posted'").fetchall()}
    except SQLAlchemyError:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        loc = names.get(_norm(r["dest"]))
        if loc is None:
            continue
        out.append({"kind": "wh", "id": r["id"], "code": r["code"] or f"#{r['id']}",
                    "date": r["date"] or "", "from": whs.get(src.get(r["id"])) or r["from_loc"] or "Warehouse",
                    "to": loc.name, "to_id": loc.id, "packed_by": r["packed_by"] or "",
                    "lines": int(r["n"] or 0), "qty": round(float(r["qty"] or 0), 3)})
    return out


def _st_pending(locs):
    ids = [l.id for l in locs]
    rows = (StoreTransfer.query.filter(StoreTransfer.status == "sent",
                                       StoreTransfer.to_location_id.in_(ids))
            .order_by(StoreTransfer.id.desc()).all()) if ids else []
    return [{"kind": "st", "id": t.id, "code": t.code, "date": t.date or "",
             "from": t.from_location.name if t.from_location else "",
             "to": t.to_location.name if t.to_location else "", "to_id": t.to_location_id,
             "packed_by": t.packed_by or "", "lines": len(t.lines), "qty": t.total_qty}
            for t in rows]


@store_stock_bp.route("/stock-inward/")
@login_required
def inward():
    # Anything the warehouse itself marked received is taken in first, so the
    # list below is only what is still waiting on this end.
    try:
        transfers.sync_transfers()
    except SQLAlchemyError:
        db.session.rollback()
    locs = _locations()
    here = _here(locs)
    return render_template("store_stock/inward.html", locations=locs, here=here,
                           users=User.query.order_by(User.full_name).all(),
                           today=date.today().isoformat())


@store_stock_bp.route("/stock-inward/api/pending")
@login_required
def inward_pending():
    locs = _locations()
    return jsonify({"ok": True, "rows": _wh_pending(locs) + _st_pending(locs)})


def _wh_lines(oid):
    con = wh._connect()
    if con is None:
        return None, None
    try:
        head = con.execute("SELECT id, code, status, to_destination AS dest, date "
                           "FROM stock_outwards WHERE id = ?", (int(oid),)).fetchone()
        lines = con.execute("SELECT id, product_id, barcode, description, qty "
                            "FROM stock_outward_lines WHERE outward_id = ? ORDER BY id",
                            (int(oid),)).fetchall()
    except SQLAlchemyError:
        return None, None
    finally:
        con.close()
    return head, lines


@store_stock_bp.route("/stock-inward/api/<kind>/<int:tid>")
@login_required
def inward_detail(kind, tid):
    if kind == "wh":
        head, lines = _wh_lines(tid)
        if head is None:
            return _bad("That dispatch could not be read from the warehouse.", 404)
        out = []
        for l in lines:
            p = Product.query.filter_by(warehouse_id=l["product_id"]).first() if l["product_id"] else None
            out.append({"line_id": l["id"], "barcode": l["barcode"] or (p.barcode if p else "") or "",
                        "sku": p.sku if p else "", "name": (p.name if p else None) or l["description"] or "",
                        "qty": round(float(l["qty"] or 0), 3),
                        "codes": [c for c in {l["barcode"], p.barcode if p else None,
                                              p.sku if p else None} if c]})
        return jsonify({"ok": True, "code": head["code"], "status": head["status"],
                        "to": head["dest"], "lines": out})
    if kind == "st":
        t = db.session.get(StoreTransfer, tid)
        if t is None:
            return _bad("No such transfer.", 404)
        return jsonify({"ok": True, "code": t.code, "status": t.status,
                        "to": t.to_location.name if t.to_location else t.to_name,
                        "lines": [{"line_id": l.id, "barcode": l.barcode or l.product.barcode or "",
                                   "sku": l.product.sku, "name": l.product.name,
                                   "qty": round(l.qty or 0, 3),
                                   "codes": [c for c in {l.barcode, l.product.barcode, l.product.sku} if c]}
                                  for l in t.lines]})
    return _bad("Unknown kind.", 404)


def _accepted(raw, sent):
    """What was counted for a line: the figure given, never below 0 and never
    above what was sent — a delivery cannot grow on the road. Left out, the
    whole line counts as arrived."""
    if raw is None or raw == "":
        return sent
    return max(0.0, min(sent, _num(raw, sent)))


@store_stock_bp.route("/stock-inward/api/<kind>/<int:tid>/receive", methods=["POST"])
@login_required
def inward_receive(kind, tid):
    data = request.get_json(silent=True) or {}
    counted = {str(k): v for k, v in (data.get("accepted") or {}).items()}
    by = (data.get("received_by") or current_user.full_name or "").strip()[:128]
    when = (data.get("date") or date.today().isoformat())[:10]
    allowed = {l.id: l for l in _locations()}

    if kind == "st":
        t = db.session.get(StoreTransfer, tid)
        if t is None:
            return _bad("No such transfer.", 404)
        if t.status != "sent":
            return _bad("This transfer has already been received.")
        if t.to_location_id not in allowed:
            return _bad("This transfer is for a branch this till does not work for.", 403)
        pieces = 0.0
        for l in t.lines:
            q = _accepted(counted.get(str(l.id)), float(l.qty or 0))
            l.accepted_qty = q
            if q > 0:
                transfers.move(t.to_location_id, l.product_id, q)
                l.product.stock_qty = round(float(l.product.stock_qty or 0) + q, 3)
                db.session.add(StockMovement(product_id=l.product_id, change=q, reason="transfer-in",
                                             reference=f"{t.code} ← {t.from_location.name if t.from_location else ''}"))
                pieces += q
        t.status = "received"
        t.received_by = by
        t.received_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "pieces": pieces, "code": t.code})

    if kind != "wh":
        return _bad("Unknown kind.", 404)
    head, lines = _wh_lines(tid)
    if head is None:
        return _bad("That dispatch could not be read from the warehouse.", 404)
    if head["status"] != "posted":
        return _bad("This dispatch is not on its way any more — it has been received already.")
    place = next((l for l in allowed.values() if _norm(l.name) == _norm(head["dest"])), None)
    if place is None:
        return _bad("This dispatch is addressed to a branch this till does not work for.", 403)
    done = {r[0] for r in db.session.query(TransferReceipt.wh_line_id)
            .filter(TransferReceipt.wh_line_id.in_([l["id"] for l in lines] or [0])).all()}

    # this branch's stock first, held back until the warehouse has taken the receipt
    marks = []
    pieces = 0.0
    for l in lines:
        sent = float(l["qty"] or 0)
        q = _accepted(counted.get(str(l["id"])), sent)
        marks.append((q, l["id"]))
        if l["id"] in done or q <= 0:
            continue
        product = Product.query.filter_by(warehouse_id=l["product_id"]).first() if l["product_id"] else None
        if product is None and l["product_id"]:
            product = transfers._import(l["product_id"])
        if product is None:
            continue
        transfers.move(place.id, product.id, q)
        product.stock_qty = round(float(product.stock_qty or 0) + q, 3)
        db.session.add(TransferReceipt(wh_line_id=int(l["id"]), wh_outward_id=tid, code=head["code"],
                                       location_id=place.id, product_id=product.id, qty=q,
                                       received_on=when, applied_at=datetime.utcnow()))
        db.session.add(StockMovement(product_id=product.id, change=q, reason="transfer-in",
                                     reference=f"{head['code'] or 'transfer'} → {place.name}"))
        pieces += q
    db.session.flush()

    con = wh._connect()
    if con is None:
        db.session.rollback()
        return _bad("The warehouse cannot be reached, so nothing was received.", 503)
    try:
        for q, lid in marks:
            con.execute("UPDATE stock_outward_lines SET accepted_qty = ? WHERE id = ?", (q, int(lid)))
        con.execute("UPDATE stock_outwards SET status = 'received', received_by = ?, "
                    "received_date = ?, received_at = ? WHERE id = ? AND status = 'posted'",
                    (by, when, datetime.utcnow(), int(tid)))
        con.commit()
    except SQLAlchemyError as e:
        con.rollback()
        db.session.rollback()
        return _bad(f"The warehouse did not take the receipt: {e.__class__.__name__}", 500)
    finally:
        con.close()
    db.session.commit()
    short = round(sum(float(l["qty"] or 0) for l in lines) - sum(q for q, _ in marks), 3)
    return jsonify({"ok": True, "pieces": round(pieces, 3), "short": short, "code": head["code"]})


# ==== TRANSFER GOODS ==============================================================
@store_stock_bp.route("/transfer-goods/")
@login_required
def transfer():
    locs = _locations()
    return render_template("store_stock/transfer.html", locations=locs, here=_here(locs),
                           all_locations=Location.query.filter_by(active=True).order_by(Location.name).all(),
                           warehouses=_warehouses(),
                           users=User.query.order_by(User.full_name).all(),
                           today=date.today().isoformat())


@store_stock_bp.route("/transfer-goods/api/scan")
@login_required
def transfer_scan():
    code = (request.args.get("code") or "").strip()
    kind = request.args.get("type") or "barcode"
    if not code:
        return _bad("Scan or type a code.")
    p = None
    if kind == "sku":
        p = Product.query.filter(db.func.lower(Product.sku) == code.lower()).first()
    else:
        p = wh.resolve_scan(code)
    if p is None:
        return _bad(f"Nothing matches “{code}”.", 404)
    return jsonify({"ok": True, "item": _product_out(p, request.args.get("location_id", type=int)),
                    "code": code})


@store_stock_bp.route("/transfer-goods/api/upload", methods=["POST"])
@login_required
def transfer_upload():
    """A file of codes — one per line, or `code,qty` — turned into lines."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return _bad("Choose a file.")
    raw = f.read()
    try:
        textv = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        textv = raw.decode("latin-1")
    lid = request.form.get("location_id", type=int)
    found, missing = {}, []
    for row in csv.reader(io.StringIO(textv)):
        if not row or not str(row[0]).strip():
            continue
        code = str(row[0]).strip()
        if code.lower() in ("barcode", "code", "sku"):
            continue                       # a heading row
        qty = _num(row[1], 1.0) if len(row) > 1 and str(row[1]).strip() else 1.0
        p = wh.resolve_scan(code)
        if p is None:
            missing.append(code)
            continue
        if p.id in found:
            found[p.id]["qty"] += qty
        else:
            found[p.id] = dict(_product_out(p, lid), qty=qty, code=code)
    return jsonify({"ok": True, "filename": f.filename[:255],
                    "lines": list(found.values()), "missing": missing})


def _next_code():
    """ST-YYMM-0001, counting within the month."""
    stem = "ST-" + date.today().strftime("%y%m") + "-"
    last = (StoreTransfer.query.filter(StoreTransfer.code.like(stem + "%"))
            .order_by(StoreTransfer.code.desc()).first())
    n = int(last.code.rsplit("-", 1)[1]) + 1 if last and last.code.rsplit("-", 1)[1].isdigit() else 1
    return f"{stem}{n:04d}"


def _store_id(con, name):
    """The warehouse's id for this branch (its `stores` row), matched by name."""
    try:
        rows = con.execute("SELECT id, name FROM stores").fetchall()
    except SQLAlchemyError:
        return None
    for r in rows:
        if _norm(r["name"]) == _norm(name):
            return r["id"]
    return None


@store_stock_bp.route("/transfer-goods/api/save", methods=["POST"])
@login_required
def transfer_save():
    data = request.get_json(silent=True) or {}
    allowed = {l.id: l for l in _locations()}
    src = allowed.get(int(data.get("from_location_id") or 0))
    if src is None:
        return _bad("Choose the From Location — a branch this till works for.")
    to = str(data.get("to") or "")
    to_loc = to_wh = None
    if to.startswith("loc:"):
        to_loc = db.session.get(Location, int(to[4:] or 0))
        if to_loc is None:
            return _bad("Choose the To Location.")
        if to_loc.id == src.id:
            return _bad("From and To are the same branch.")
    elif to.startswith("wh:"):
        to_wh = next((w for w in _warehouses() if str(w["id"]) == to[3:]), None)
        if to_wh is None:
            return _bad("That warehouse cannot be found.")
    else:
        return _bad("Choose the To Location.")

    lines = []
    for raw in data.get("lines") or []:
        p = db.session.get(Product, int(raw.get("product_id") or 0))
        q = _num(raw.get("qty"))
        if p is None or q <= 0:
            continue
        lines.append((p, q, str(raw.get("barcode") or p.barcode or p.sku)[:64]))
    if not lines:
        return _bad("Add at least one item.")
    for p, q, _ in lines:
        if q > float(p.stock_qty or 0) + 1e-9:
            return _bad(f"{p.name} ({p.sku}): only {float(p.stock_qty or 0):g} in stock, {q:g} to send.")

    immediate = bool(data.get("immediate")) and to_loc is not None
    t = StoreTransfer(code=_next_code(), date=(data.get("date") or date.today().isoformat())[:10],
                      packed_by=(data.get("packed_by") or "")[:128], from_location_id=src.id,
                      to_location_id=to_loc.id if to_loc else None,
                      to_warehouse_id=to_wh["id"] if to_wh else None,
                      to_name=to_loc.name if to_loc else to_wh["name"],
                      immediate=immediate, remarks=(data.get("remarks") or "").strip() or None,
                      filename=(data.get("filename") or "")[:255] or None,
                      status="received" if immediate else "sent", created_by_id=current_user.id)
    db.session.add(t)
    for p, q, bc in lines:
        t.lines.append(StoreTransferLine(product_id=p.id, barcode=bc, qty=q,
                                         accepted_qty=q if immediate else None))
        # off this branch's shelves now
        transfers.move(src.id, p.id, -q)
        p.stock_qty = round(float(p.stock_qty or 0) - q, 3)
        db.session.add(StockMovement(product_id=p.id, change=-q, reason="transfer-out",
                                     reference=f"{t.code} → {t.to_name}"))
        if immediate:
            transfers.move(to_loc.id, p.id, q)
            p.stock_qty = round(float(p.stock_qty or 0) + q, 3)
            db.session.add(StockMovement(product_id=p.id, change=q, reason="transfer-in",
                                         reference=f"{t.code} ← {src.name}"))
    if immediate:
        t.received_by = current_user.full_name
        t.received_at = datetime.utcnow()
    db.session.flush()

    if to_wh:
        # written to the warehouse as a dispatch from this store, already posted:
        # the goods have left, and the warehouse's Stock Inward counts them in
        con = wh._connect()
        if con is None:
            db.session.rollback()
            return _bad("The warehouse cannot be reached, so nothing was sent.", 503)
        try:
            now = datetime.utcnow()
            row = con.execute(
                "INSERT INTO stock_outwards (code, date, from_company, from_location, from_store_id, "
                "to_destination, to_warehouse_id, packed_by, status, posted_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?) RETURNING id",
                (t.code, t.date, (src.company.name if getattr(src, "company", None) else None),
                 src.name, _store_id(con, src.name), to_wh["name"], int(to_wh["id"]),
                 t.packed_by or None, now, now)).fetchone()
            oid = row["id"]
            for p, q, bc in lines:
                con.execute("INSERT INTO stock_outward_lines (outward_id, product_id, barcode, "
                            "description, qty, rate) VALUES (?, ?, ?, ?, ?, ?)",
                            (oid, p.warehouse_id, bc, p.name, q, float(p.cost_price or 0)))
            con.commit()
            t.wh_outward_id = oid
        except SQLAlchemyError as e:
            con.rollback()
            db.session.rollback()
            return _bad(f"The warehouse did not take the transfer: {e.__class__.__name__}", 500)
        finally:
            con.close()
    db.session.commit()
    return jsonify({"ok": True, "id": t.id, "code": t.code, "status": t.status,
                    "to": t.to_name, "pieces": t.total_qty})


def _wh_status(oid):
    con = wh._connect()
    if con is None or not oid:
        return None
    try:
        r = con.execute("SELECT status FROM stock_outwards WHERE id = ?", (int(oid),)).fetchone()
    except SQLAlchemyError:
        return None
    finally:
        con.close()
    return r["status"] if r else None


@store_stock_bp.route("/transfer-goods/api/find")
@login_required
def transfer_find():
    """A transfer by its package code — this store's own, read back."""
    code = (request.args.get("code") or "").strip()
    t = StoreTransfer.query.filter(db.func.lower(StoreTransfer.code) == code.lower()).first() if code else None
    if t is None:
        return _bad(f"No transfer with package code “{code}”.", 404)
    status = t.status
    if t.wh_outward_id:
        # sent to a warehouse: it is received when the warehouse says so
        status = "received" if _wh_status(t.wh_outward_id) == "received" else "sent"
    return jsonify({"ok": True, "transfer": {
        "code": t.code, "date": t.date, "packed_by": t.packed_by or "",
        "from": t.from_location.name if t.from_location else "", "to": t.to_name,
        "immediate": bool(t.immediate), "remarks": t.remarks or "", "filename": t.filename or "",
        "status": status,
        "lines": [{"sku": l.product.sku, "barcode": l.barcode, "name": l.product.name,
                   "qty": l.qty, "accepted": l.accepted_qty} for l in t.lines]}})
