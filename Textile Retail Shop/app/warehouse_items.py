"""The shop's catalogue, taken from the warehouse's items.

Everything the shop sells is an item the warehouse holds: same SKU, same
description, same category code, and the same QR printed on its tag. This module
keeps the two in step, so a garment scanned at the counter resolves to the record
the GRN created upstairs instead of something re-typed into the till.

**Read-only.** The warehouse database is opened `mode=ro` and never written. It is
the system of record for what stock exists and where it came from; a till is not
the place to amend that.

**Stock is not copied.** `products.stock_qty` here is what the SHOP holds — put
there by a Stock Outward to the store, or by hand — and a sync leaves it alone.
Copying the warehouse's holding would tell a cashier they can sell forty pieces
sitting in a box across town. What is copied is what identifies and prices an
item: sku, description, the attribute tuple, HSN, uom, MRP/sale price, average
cost, its category, and the QR payload printed on its label.

**Scanning.** `resolve_scan()` is the one entry point every scan point uses — the
billing counter, the floor app, and inventory's scan-to-add. It accepts a shop
SKU, a printed barcode, a warehouse QR payload (`E1|…` for a SKU tag, `EU1|…` for
a single garment), or a bare per-piece code, and returns the shop product. An item
the warehouse has but the shop hasn't seen yet is imported on the spot, so a tag
that exists is never a tag that fails to scan.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.dbpatch import apply_all
from app.models import Category, Product, StockMovement

SHOP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WAREHOUSE_DB = SHOP_DIR.parent / "backend" / "data" / "essa.db"

# The warehouse's QR format, mirrored from backend/app/services/barcode_svc.py.
# Positional and pipe-delimited, NOT JSON — the key names and quotes of JSON push
# the symbol past what a phone reads off a 17mm tag. ORDER IS THE FORMAT and is
# append-only: a new attribute goes on the END so last year's labels still decode.
# test_warehouse_sync.py asserts what this builds is byte-identical to what the
# warehouse itself prints, which is what keeps the duplication honest.
QR_TAG = "E1"
QR_ORDER = ["id", "sku", "barcode", "description", "hsn", "uom", "mrp",
            "sale_price", "category", "category_section", "product_type",
            "size", "color", "pattern", "fit", "material", "design_no"]
# One garment's tag: which piece, and the SKU it hangs off. Mirrors UNIT_ORDER
# in barcode_svc — the values come from the unit, then from its product.
UNIT_TAG = "EU1"
UNIT_ORDER = [("code", "unit"), ("sku", "product"), ("id", "unit"),
              ("description", "product"), ("size", "product"),
              ("color", "product"), ("mrp", "product")]

# Warehouse column -> shop column. Stock is absent on purpose: it is seeded once
# when an item first arrives and is the shop's own from then on (see _apply).
# `name` is absent too — it comes from the category, see item_name().
FIELD_MAP = {
    "description": "description", "hsn": "hsn_code", "size": "size",
    "color": "color", "material": "fabric", "barcode": "barcode", "mrp": "mrp",
    "product_type": "product_type", "pattern": "pattern", "fit": "fit",
    "design_no": "design_no",
}

# What a re-sync compares to decide whether anything actually moved.
TRACKED = ["name", "description", "hsn_code", "unit", "selling_price", "cost_price",
           "category_id", "size", "color", "fabric", "barcode", "mrp",
           "product_type", "pattern", "fit", "design_no", "warehouse_id",
           "warehouse_qr"]

# The shop's unit dropdown. A warehouse uom outside it stays whatever the shop
# already had rather than being forced to a wrong one.
SHOP_UNITS = {"pcs", "mtr", "kg", "set"}


def warehouse_db_path():
    """The warehouse SQLite file, when there is one. ESSA_WAREHOUSE_DB overrides
    it, which is what lets the shop run somewhere the warehouse folder isn't a
    sibling. Returns None when the warehouse is not a file at all — see
    warehouse_url."""
    return Path(os.environ.get("ESSA_WAREHOUSE_DB") or DEFAULT_WAREHOUSE_DB)


def warehouse_url():
    """How to reach the warehouse database, as a SQLAlchemy URL.

    This used to be a path and nothing else: the shop opened the warehouse's
    SQLite file directly, read-only, because both halves sat in one folder on one
    PC. Deployed, they do not — the warehouse is a Postgres database and there is
    no file to open, so `available()` would answer False and the till would
    quietly stop importing warehouse items on a scan. That is the failure this
    exists to prevent, and it is a quiet one: everything keeps working except the
    link between the two halves of the business.

    ESSA_DATABASE_URL is the warehouse's own variable, so on a deployment where
    both run this is already set and correct.
    """
    url = os.environ.get("ESSA_DATABASE_URL", "").strip()
    if url:
        # what SQLAlchemy wants, versus what most dashboards hand out
        return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url
    path = warehouse_db_path()
    return f"sqlite:///{path.as_posix()}" if path and path.is_file() else ""


_engine = None


def _get_engine():
    """One engine per process, built lazily. Built on first use rather than at
    import because the shop is importable without a warehouse at all."""
    global _engine
    url = warehouse_url()
    if not url:
        return None
    if _engine is None or str(_engine.url) != url:
        try:
            _engine = create_engine(url, pool_pre_ping=True)
        except SQLAlchemyError:
            return None
    return _engine


class _Con:
    """The read-only handle the rest of this module expects.

    It keeps the shape the sqlite3 connection had — `execute(...).fetchall()`,
    rows accessed as `row["column"]` — so the queries below did not have to be
    rewritten around a different result object. Rows come back as plain dicts,
    which answer both `row["x"]` and `row.keys()` the way sqlite3.Row did.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        # The queries here were written with sqlite3's positional `?`. Named
        # parameters are the portable spelling, so the marker is translated
        # rather than every call site being rewritten.
        if params:
            named = {f"p{i}": v for i, v in enumerate(params)}
            for i in range(len(params)):
                sql = sql.replace("?", f":p{i}", 1)
        else:
            named = {}
        return _Result(self._conn.execute(text(sql), named))

    def commit(self):
        """For the few writes the shop makes to the warehouse — a store's own
        transfer notes (app/store_stock.py). Everything else only reads."""
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except SQLAlchemyError:
            pass

    def close(self):
        try:
            self._conn.close()
        except SQLAlchemyError:
            pass


class _Result:
    def __init__(self, res):
        self._res = res

    def fetchall(self):
        return [dict(r._mapping) for r in self._res]

    def fetchone(self):
        r = self._res.fetchone()
        return dict(r._mapping) if r is not None else None


def _connect():
    """A connection to the warehouse, or None when there is no warehouse to read."""
    eng = _get_engine()
    if eng is None:
        return None
    try:
        return _Con(eng.connect())
    except SQLAlchemyError:
        return None


def available():
    """Whether the warehouse can be read from here."""
    con = _connect()
    if con is None:
        return False
    con.close()
    return True


def _num(v):
    """Trim pointless decimals — '499' not '499.0', exactly as the printer does."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _qesc(v):
    return _num(v).replace("\\", "\\\\").replace("|", "\\|")


def qr_payload(row):
    """The QR the warehouse prints for this item, rebuilt field for field."""
    vals = []
    for col in QR_ORDER:
        v = row[col] if col in row.keys() else None
        vals.append("" if v in (None, "") else _qesc(v))
    while vals and vals[-1] == "":       # trailing empties carry no information
        vals.pop()
    return "|".join([QR_TAG] + vals)


def unit_qr_payload(unit, product):
    """The QR printed on one garment's tag, rebuilt field for field."""
    vals = []
    for col, src in UNIT_ORDER:
        row = unit if src == "unit" else product
        # the unit's own 'id' position carries the product it belongs to
        key = "product_id" if (col == "id" and src == "unit") else col
        v = row[key] if key in row.keys() else None
        vals.append("" if v in (None, "") else _qesc(v))
    while vals and vals[-1] == "":
        vals.pop()
    return "|".join([UNIT_TAG] + vals)


def _split_escaped(s):
    """Split on unescaped '|' only, honouring backslash escapes."""
    out, cur, esc = [], [], False
    for ch in s:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_payload(text):
    """Decode a scanned warehouse QR into a dict, or None if it isn't one.

    Only the identifiers are taken. The rest of the payload is a snapshot from the
    day the tag was printed, and the record it points at is what's current.
    """
    parts = _split_escaped(str(text or "").strip())
    if parts[0] == QR_TAG:
        keys = ["id", "sku", "barcode"]
    elif parts[0] == UNIT_TAG:
        keys = ["unit_code", "sku", "id"]
    else:
        return None
    out = {"tag": parts[0]}
    for key, val in zip(keys, parts[1:]):
        val = (val or "").strip()
        if val:
            out[key] = val
    return out


# ---- reading the warehouse ---------------------------------------------------

def fetch_items():
    """Every product the warehouse holds, newest id last."""
    con = _connect()
    if con is None:
        return []
    try:
        return con.execute("SELECT * FROM products ORDER BY id").fetchall()
    except SQLAlchemyError:
        return []
    finally:
        con.close()


def _read_column(sql):
    """One query's `v` column as a list of non-blank strings, on its own
    connection — so a table this warehouse doesn't have costs only that list,
    and on Postgres a failed statement can't poison the next one."""
    con = _connect()
    if con is None:
        return []
    try:
        return [str(r["v"]).strip() for r in con.execute(sql).fetchall()
                if r["v"] is not None and str(r["v"]).strip()]
    except SQLAlchemyError:
        return []
    finally:
        con.close()


def fetch_vocabulary():
    """The warehouse's masters, as pick-lists for the shop's screens: the
    product attribute options (Masters → Product Attributes, keyed brand, color,
    material, pattern, style, fit, sleeve, product_type, size), the suppliers,
    the purchase invoice numbers and the design numbers. Empty lists when there
    is no warehouse to read."""
    out = {}
    con = _connect()
    if con is not None:
        try:
            for r in con.execute("SELECT attr, value FROM attribute_options").fetchall():
                if r["attr"] and r["value"] is not None and str(r["value"]).strip():
                    out.setdefault(str(r["attr"]), []).append(str(r["value"]).strip())
        except SQLAlchemyError:
            pass
        finally:
            con.close()
    out["supplier"] = _read_column("SELECT name AS v FROM suppliers")
    out["invoice"] = _read_column("SELECT DISTINCT invoice_number AS v FROM purchases")
    out["design_no"] = _read_column("SELECT DISTINCT design_no AS v FROM products")
    return out


#: {warehouse supplier id: name}, read once per sync — see fetch_supplier_names.
_SUPPLIERS = {}


def fetch_supplier_names():
    """The warehouse's suppliers by id — what a sales scheme's Invoice filter
    reads as the item's supplier. Its own connection and its own failure: a
    warehouse with no suppliers table still syncs its products."""
    con = _connect()
    if con is None:
        return {}
    try:
        return {r["id"]: r["name"] for r in
                con.execute("SELECT id, name FROM suppliers").fetchall()}
    except SQLAlchemyError:
        return {}
    finally:
        con.close()


def fetch_units(warehouse_id):
    """Every garment the warehouse holds for this item, each with its own QR.

    This is what the shop's product page lists: the piece codes are what a tag
    scanned off an individual garment carries, so the shop can print and check
    them without asking the warehouse for a screen.
    """
    con = _connect()
    if con is None:
        return []
    try:
        product = con.execute("SELECT * FROM products WHERE id = ?",
                              (warehouse_id,)).fetchone()
        if product is None:
            return []
        rows = con.execute(
            "SELECT code, seq, status, product_id FROM product_units "
            "WHERE product_id = ? ORDER BY seq", (warehouse_id,)).fetchall()
        return [{"code": r["code"], "seq": r["seq"], "status": r["status"],
                 "qr": unit_qr_payload(r, product)} for r in rows]
    except SQLAlchemyError:
        return []
    finally:
        con.close()


def fetch_item(sku=None, warehouse_id=None, unit_code=None):
    """One warehouse product, found by whichever identifier a scan produced."""
    con = _connect()
    if con is None:
        return None
    try:
        if unit_code:
            row = con.execute(
                "SELECT p.* FROM products p JOIN product_units u ON u.product_id = p.id "
                # COLLATE NOCASE is SQLite's alone. lower() on both sides is the
                # case-insensitive match every dialect agrees on, and a scanned
                # piece code arrives in whatever case the scanner produced.
                "WHERE lower(u.code) = lower(?)", (unit_code,)).fetchone()
            if row:
                return row
        if sku:
            row = con.execute(
                "SELECT * FROM products WHERE lower(sku) = lower(?)", (sku,)).fetchone()
            if row:
                return row
        if warehouse_id:
            row = con.execute(
                "SELECT * FROM products WHERE id = ?", (warehouse_id,)).fetchone()
            if row:
                return row
    except SQLAlchemyError:
        return None
    finally:
        con.close()
    return None


# ---- writing the shop's copy -------------------------------------------------

def _ensure_columns():
    apply_all()


def _category_ids():
    return {c.name: c.id for c in Category.query.all()}


def item_name(row):
    """What the shop calls this item: its category code.

    The warehouse's `description` is free text off a supplier's invoice — a
    garment the business codes as LADIES-T-SHIRT arrives described as "Women's
    T-Shirt", and the next supplier writes it differently again. The category is
    the vocabulary every GRN, stock report and price list already uses, so naming
    the product after it is what makes the till, the shelf label and the invoice
    say the same words.

    The description isn't discarded — it stays on the product, where the detail
    screen can show what the supplier actually called it. Falls back to the
    description, then the SKU, for anything the warehouse hasn't categorised.
    """
    for candidate in (row["category"], row["description"], row["sku"]):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _snapshot(p):
    return tuple(getattr(p, attr) for attr in TRACKED)


def _apply(p, row, cat_ids, created=False):
    """Copy a warehouse row onto a shop product. True if anything changed."""
    before = _snapshot(p)

    p.name = item_name(row)

    for src, dest in FIELD_MAP.items():
        v = row[src] if src in row.keys() else None
        if v not in (None, ""):
            setattr(p, dest, v)

    sid = row["primary_supplier_id"] if "primary_supplier_id" in row.keys() else None
    if sid and _SUPPLIERS.get(sid):
        p.supplier = _SUPPLIERS[sid]

    uom = str(row["uom"] or "").strip().lower()
    if uom in SHOP_UNITS:
        p.unit = uom

    # What the shop charges: the warehouse's sale price, else its MRP.
    price = row["sale_price"] if row["sale_price"] not in (None, "") else row["mrp"]
    if price not in (None, ""):
        p.selling_price = float(price)
    if row["avg_cost"] not in (None, ""):
        p.cost_price = float(row["avg_cost"])

    # Both sides read the same category master, so this matches by name.
    if row["category"]:
        cid = cat_ids.get(row["category"])
        if cid:
            p.category_id = cid

    p.warehouse_id = row["id"]
    p.warehouse_qr = qr_payload(row)

    if created:
        # The shop opens with what the warehouse handed over. Only on the way in:
        # after this the count is the shop's own, and a later sync must not
        # overwrite it or every sale would be undone the next time the warehouse
        # was read — nothing is written back, so the shop's figure is the only
        # record that it sold anything.
        p.stock_qty = float(row["stock_qty"] or 0)

    return before != _snapshot(p)


def _new_product(row, cat_ids):
    """Bring one warehouse item in for the first time, opening stock and all."""
    # selling_price is NOT NULL; _apply sets the real one a moment later.
    p = Product(sku=row["sku"], name=item_name(row),
                selling_price=0.0, stock_qty=0.0)
    db.session.add(p)
    _apply(p, row, cat_ids, created=True)
    if p.stock_qty:
        # The shop's stock log should say where its opening count came from,
        # the same as any other movement. flush() first — the log needs the id.
        db.session.flush()
        db.session.add(StockMovement(
            product_id=p.id, change=p.stock_qty, reason="opening",
            reference=f"warehouse:{row['sku']}"))
    return p


def import_item(row, cat_ids=None):
    """Create or refresh the shop's copy of one warehouse item. Returns it."""
    _ensure_columns()
    if cat_ids is None:
        cat_ids = _category_ids()
    p = Product.query.filter_by(sku=row["sku"]).first()
    if p is None:
        p = _new_product(row, cat_ids)
    else:
        _apply(p, row, cat_ids)
    db.session.commit()
    return p


def sync_warehouse_items():
    """Bring the shop's catalogue in line with the warehouse. Safe to call always."""
    _ensure_columns()
    rows = fetch_items()
    if not rows:
        return {"added": 0, "updated": 0, "total": Product.query.count(),
                "available": available()}

    cat_ids = _category_ids()
    _SUPPLIERS.clear()
    _SUPPLIERS.update(fetch_supplier_names())
    existing = {p.sku: p for p in Product.query.all()}
    added = updated = 0
    for row in rows:
        p = existing.get(row["sku"])
        if p is None:
            _new_product(row, cat_ids)
            added += 1
        elif _apply(p, row, cat_ids):
            updated += 1
    db.session.commit()
    return {"added": added, "updated": updated, "total": Product.query.count(),
            "available": True}


# ---- picking up what the warehouse just did ---------------------------------
#
# A product detailed in the warehouse's mobile app is written straight to its
# database. The shop notices by watching that file rather than by being told:
# there is no message to miss, and nothing to configure between the two.

_last_signature = None


def _warehouse_signature():
    """A stamp that changes whenever the warehouse database is written.

    The warehouse runs SQLite in `delete` journal mode, so every commit rewrites
    the database file itself — there is no side WAL holding changes back, and one
    stat() is enough to know. The -wal is checked anyway, cheaply, in case that
    ever changes.
    """
    url = warehouse_url()
    if url.startswith("sqlite"):
        path = warehouse_db_path()
        stamps = []
        for candidate in (path, path.with_name(path.name + "-wal")):
            try:
                st = candidate.stat()
            except OSError:
                continue
            stamps.append((st.st_mtime_ns, st.st_size))
        return tuple(stamps) or None

    # No file to stat when the warehouse is Postgres, so the stamp is read from
    # the rows instead: how many products there are, the highest id, and the
    # latest detailing. That covers the three ways the warehouse changes in a way
    # the shop cares about — a product added, a product detailed, a product
    # removed — which is what this is asked to notice. It costs one aggregate
    # query rather than a stat(), so `sync_if_stale` is no longer free; it is
    # still far cheaper than the sync it decides against.
    con = _connect()
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT count(*) AS n, max(id) AS mx, max(detailed_at) AS det FROM products"
        ).fetchone()
        return (row["n"], row["mx"], str(row["det"])) if row else None
    except SQLAlchemyError:
        return None
    finally:
        con.close()


def sync_if_stale():
    """Sync only if the warehouse has been written since the last time.

    This is what puts a detail posted from the mobile app into the shop without
    waiting for a restart: the next page load sees the file has moved and pulls
    the change in. When nothing has changed it costs a stat() and returns.
    """
    global _last_signature
    signature = _warehouse_signature()
    if signature is None or signature == _last_signature:
        return None
    result = sync_warehouse_items()
    _last_signature = signature
    return result


# ---- the one scan entry point ------------------------------------------------

def resolve_scan(code, allow_import=True):
    """The shop product a scan means, or None.

    Accepts a shop SKU, a printed barcode, a warehouse QR payload (`E1|…` or
    `EU1|…`), or a bare per-piece code like `ESSA-00002-007`. The shop's own rows
    are tried first so the till keeps working with no warehouse attached.
    """
    code = str(code or "").strip()
    if not code:
        return None

    payload = parse_payload(code)
    if payload:
        # The tag is a snapshot; resolve by the identifiers inside it so the
        # caller always gets the live record.
        for sku in [payload.get("sku")]:
            if sku:
                p = Product.query.filter_by(sku=sku, active=True).first()
                if p:
                    return p
        if payload.get("id"):
            p = Product.query.filter_by(warehouse_id=int(payload["id"]),
                                        active=True).first() \
                if str(payload["id"]).isdigit() else None
            if p:
                return p
    else:
        p = Product.query.filter(
            (Product.sku == code) | (Product.barcode == code),
            Product.active.is_(True)).first()
        if p:
            return p

    if not allow_import:
        return None

    # Not in the shop yet — ask the warehouse and bring it in.
    row = fetch_item(
        sku=(payload or {}).get("sku") or code,
        warehouse_id=int(payload["id"]) if payload and str(payload.get("id", "")).isdigit() else None,
        unit_code=(payload or {}).get("unit_code") or code,
    )
    if row is None:
        return None
    return import_item(row)
