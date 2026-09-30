import os
import json
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..config import DATA_DIR
from ..services import inventory as inv
from ..services import barcode_svc
from ..services import stock_view
from ..services import scope

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


class StockAdjust(BaseModel):
    new_qty: float
    note: Optional[str] = "manual adjustment"


class ProductDetail(BaseModel):
    """Physical-inspection attributes submitted from the warehouse mobile app.

    The full attribute set Essa keeps against its stock — the columns of the
    stock master (Attributes Reference.xlsx): brand, colour, pattern, style, fit,
    sleeve, type, material, size.

    `attrs` carries anything this item's CATALOGUE records that the stock master
    has no column for — a saree's weave, its zari, its border. Keys the catalogue
    does not declare are ignored rather than stored: a value nothing can display
    is not data. See services/catalogues.write_attrs.
    """
    attrs: Optional[dict] = None
    color: Optional[str] = None
    size: Optional[str] = None
    pattern: Optional[str] = None
    fit: Optional[str] = None
    product_type: Optional[str] = None
    material: Optional[str] = None
    design_no: Optional[str] = None
    brand: Optional[str] = None
    style: Optional[str] = None
    sleeve: Optional[str] = None
    mrp: Optional[float] = None
    sale_price: Optional[float] = None
    sale_discount_pct: Optional[float] = None
    detailed_by: Optional[str] = None


# The attribute vocabularies, read from Essa's own masters rather than invented
# here. data/product_attributes.json is generated from Attributes Reference.xlsx
# ("Quanto Report", 13,851 stock rows) and holds the real values in use — 300
# brands, 88 colours — ordered by how often each occurs, with the placeholders
# (Unspecified, No Color) last.
#
# STYLE comes from data/style_master.json instead: the full 264-entry list with
# its ERP codes, of which the stock rows only exercise 78. A vocabulary is a list
# of what MAY be recorded, not a summary of what has been, so the master wins —
# but it is ordered by use, so the 78 anyone actually reaches for are at the top
# and the other 179 are below rather than in the way. The names are kept in the
# master's own casing, because that string is what gets stored on the product and
# printed on its label, and it has to match the ERP it came from.
#
# It was a hand-written starter list before: eight fits that were not Essa's
# eight, four "patterns" where the master has twelve, and no brand, style or
# sleeve at all. A dropdown that does not offer the value someone is looking at
# teaches them to type it by hand, and typed-by-hand is how "REGULAR FIT",
# "Regular fit" and "Reg. Fit" become three fits.
#
# Free text is still accepted everywhere, and whatever is already in the database
# is merged in below — the list guides, it does not gate.
_ATTR_FILE = os.path.join(DATA_DIR, "product_attributes.json")
_FALLBACK_OPTIONS = {
    "color": ["White", "Black", "Grey", "Blue", "Green", "Red", "Cream", "Maroon", "Multi"],
    "size": ["S", "M", "L", "XL", "XXL", "Free Size"],
    "pattern": ["Plain", "Print", "Check", "Stripes"],
    "fit": ["Regular Fit", "Slim Fit", "Narrow Fit", "Ankle Fit"],
    "product_type": [], "material": ["Cotton", "Hosiery", "Synthetic", "Jean", "Lycra"],
    "brand": ["Essa"], "style": [], "sleeve": ["Full", "Half", "Sleeveless", "3/4th"],
}
#: uom is ours, not the master's — it is the unit-type master's business
_UOM_OPTIONS = ["PCS", "PAIR", "SET", "BOX", "DOZEN", "MTR", "KG"]


def _base_options():
    """The master's vocabularies, or a small fallback if the file is missing."""
    opts = {k: list(v) for k, v in _FALLBACK_OPTIONS.items()}
    try:
        with open(_ATTR_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for key, vals in data.items():
            if key.startswith("_") or key == "products" or not isinstance(vals, list):
                continue
            opts[key] = [str(v) for v in vals if str(v).strip()]
    except (OSError, ValueError):
        pass                      # ship without the master rather than 500
    opts["uom"] = list(_UOM_OPTIONS)
    return opts


BASE_OPTIONS = _base_options()


def _localise(d: dict, db: Session, product, wid, balances=None):
    """Restate a product payload as THIS warehouse sees it.

    Inside a warehouse the word "Stock" on a screen means that warehouse's
    stock — a picker at Erode reading the company total would pick goods that
    are standing in Karur. So `stock_qty`, `avg_cost` and `stock_value` are
    replaced with the local figures, and the company-wide ones are kept beside
    them under their own names rather than thrown away: "we have none here but
    forty elsewhere" is the answer to a shortage, and it is only reachable if
    both numbers survive.
    """
    if not wid or product is None:
        return d
    from ..services import stock_locations as stock_loc
    if balances is not None:
        # the list's one read of this warehouse's balances; the same arithmetic
        # as qty_at / cost_at, without two queries per product
        row = balances.get(product.id)
        qty = float(row[0] or 0) if row else 0.0
        cost = (float(row[1] or 0) if row and (row[0] or 0) > stock_loc.TOLERANCE
                else float(product.avg_cost or 0))
    else:
        qty = stock_loc.qty_at(db, product.id, wid)
        cost = stock_loc.cost_at(db, product.id, wid)
    return {**d,
            "company_stock_qty": d.get("stock_qty"),
            "company_stock_value": d.get("stock_value"),
            "stock_qty": qty, "avg_cost": cost,
            "stock_value": round(qty * cost, 2),
            "warehouse_id": wid,
            "stock_elsewhere": round(float(product.stock_qty or 0) - qty, 3)}


def _product_out(p: models.Product, ctx=None):
    """One product. `ctx` adds its provenance and whether its labels may print —
    passed in by the callers that already built one, so a list of 500 products
    doesn't re-derive the same lookup 500 times."""
    d = _product_fields(p)
    if ctx is not None:
        from ..services import integrity
        rep = integrity.product_report(ctx.db, p, ctx)
        d.update({"state": rep["state"], "live_units": rep["live_units"],
                  "expected_units": rep["expected_units"],
                  "units_match": rep["units_match"],
                  "can_print": rep["can_print"], "print_block": rep["print_block"],
                  "issues": rep["issues"]})
    return d


def _product_fields(p: models.Product):
    return {
        # `name` is what it is CALLED (its category), `description` what the
        # supplier's bill called it — see stock_view.display_name
        "id": p.id, "sku": p.sku, "barcode": p.barcode,
        "name": stock_view.display_name(p), "description": p.description,
        "hsn": p.hsn, "uom": p.uom, "mrp": p.mrp, "stock_qty": p.stock_qty,
        # what one of these is. `stock_qty` is a count of THESE — six pairs, not
        # twelve pillow covers — so a screen showing the number must show the unit
        "unit_type": p.unit_type, "pieces_per_unit": p.pieces_per_unit,
        "avg_cost": p.avg_cost, "last_rate": p.last_rate, "stock_value": p.stock_value,
        "supplier_name": p.primary_supplier.name if p.primary_supplier else None,
        # physical-detail attributes — the stock master's full set
        "color": p.color, "size": p.size, "pattern": p.pattern, "fit": p.fit,
        "product_type": p.product_type, "material": p.material, "design_no": p.design_no,
        "brand": p.brand, "style": p.style, "sleeve": p.sleeve,
        "sale_price": p.sale_price, "sale_discount_pct": p.sale_discount_pct,
        "category": p.category, "category_section": p.category_section,
        # Which business line this item belongs to, and the attributes its line
        # records that have no column above. Carried on every product payload so
        # a screen can draw a saree's weave beside a t-shirt's sleeve without
        # knowing in advance which it is looking at.
        "catalogue_id": p.catalogue_id,
        "attrs": p.attrs if isinstance(p.attrs, dict) else {},
        "detailed": bool(p.detailed), "detailed_by": p.detailed_by,
        "detailed_at": p.detailed_at.isoformat() if p.detailed_at else None,
        # when the product first came into stock — Label Printing lists the
        # newest first, because a fresh receipt is what is waiting for tags
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """Headline stock figures, for this warehouse when inside one.

    Not the company's totals divided up — the warehouse's own balances, summed.
    A workspace showing the company valuation under a heading that names one
    building would be the most expensive kind of wrong number to have on a
    screen."""
    if wid:
        from ..services import stock_locations as stock_loc
        rows = stock_loc.stock_at(db, wid)
        ids = {r["product_id"] for r in rows}
        detailed = db.query(models.Product).filter(
            models.Product.id.in_(ids or {0}),
            models.Product.detailed == True).count()  # noqa: E712
        return {
            "product_count": len(rows),
            "total_units": round(sum(r["qty"] for r in rows), 3),
            "total_stock_value": round(sum(r["value"] for r in rows), 2),
            "excluded_products": 0,
            "detailed": detailed,
            "pending_detail": len(rows) - detailed,
            "warehouse_id": wid,
        }
    s = inv.inventory_summary(db)
    s["detailed"] = db.query(models.Product).filter(models.Product.detailed == True).count()  # noqa: E712
    s["pending_detail"] = s["product_count"] - s["detailed"]
    return s


@returns_json(router.get("/product-options"))
def product_options(warehouse_id: Optional[int] = None,
                    catalogue_id: Optional[int] = None,
                    db: Session = Depends(get_db)):
    """The attribute dropdowns for one business line, and which attributes apply.

    Two things come back, and screens need both:

      * `attributes` — WHICH attributes this catalogue records, in order, with
        their labels. A silk catalogue has no sleeve and no fit; a form that
        drew ten garment fields regardless would ask a saree what length its
        sleeves are.
      * the option lists themselves, per attribute key.

    Values already recorded against this catalogue's own products are merged in,
    so a value typed once is offered next time — the list guides, it never gates,
    and free text stays accepted everywhere.

    Passing neither warehouse nor catalogue answers for the default line, which
    is exactly what this endpoint has always returned.
    """
    from ..services import catalogues as cat_svc
    if warehouse_id and not catalogue_id:
        c = cat_svc.for_warehouse(db, warehouse_id)
        catalogue_id = c.id if c else None
    catalogue_id = cat_svc.resolve_id(db, catalogue_id)

    attrs = cat_svc.attributes(db, catalogue_id)
    opts = {k: list(v) for k, v in cat_svc.options(db, catalogue_id).items()}

    # Values this catalogue's products already carry, merged in. Restricted to
    # its OWN products: offering a silk warehouse every colour a t-shirt has ever
    # been is how one line's vocabulary leaks into another's.
    cols = {a["key"]: getattr(models.Product, a["column"])
            for a in attrs if a["column"]}
    # every column's distinct values in one scan (services/distinct)
    from ..services.distinct import distinct_values
    found = distinct_values(db.query(models.Product).filter(
        (models.Product.catalogue_id == catalogue_id)
        | (models.Product.catalogue_id.is_(None))), list({c.key: c for c in cols.values()}.values()))
    for key, col in cols.items():
        opts.setdefault(key, [])
        # case-insensitive: the master says "Regular Fit", and a product already
        # carrying "REGULAR FIT" is the same fit, not a second one to offer
        have = {v.strip().lower() for v in opts[key]}
        for val in sorted({v for v in found[col.key] if v}):
            if val.strip().lower() not in have:
                opts[key].append(val)
                have.add(val.strip().lower())

    # uom is ours, not a catalogue's — it is the unit-type master's business, and
    # a metre is a metre whichever trade is measuring in it
    opts["uom"] = list(BASE_OPTIONS.get("uom") or [])
    opts["attributes"] = attrs
    opts["catalogue_id"] = catalogue_id
    return opts


def _ordered_id_chunks(db, query, chunk):
    """The ids `query` returns under ORDER BY description, id — `chunk` at a time.

    On PostgreSQL each chunk is fetched after the last (description, id) seen:
    a top-N sort per chunk, stopping as soon as the caller has enough, instead
    of sorting every match (363K for the phone's default list) up front. Rows
    with no description sort LAST there (NULLS LAST), so they follow, by id —
    the order the single ORDER BY returned them in. Elsewhere (SQLite sorts
    NULLs first) the whole ordered id list is read, as before."""
    P = models.Product
    base = query.with_entities(P.id, P.description)
    if db.bind.dialect.name != "postgresql":
        ids = [i for (i, _d) in base.order_by(P.description, P.id)]
        for i in range(0, len(ids), chunk):
            yield ids[i:i + chunk]
        return
    from sqlalchemy import tuple_
    last = None
    while True:
        q = base.filter(P.description.isnot(None))
        if last is not None:
            q = q.filter(tuple_(P.description, P.id) > tuple_(*last))
        rows = q.order_by(P.description, P.id).limit(chunk).all()
        if not rows:
            break
        yield [r[0] for r in rows]
        last = (rows[-1][1], rows[-1][0])
    last_id = None
    while True:
        q = base.filter(P.description.is_(None))
        if last_id is not None:
            q = q.filter(P.id > last_id)
        rows = q.order_by(P.id).limit(chunk).all()
        if not rows:
            break
        yield [r[0] for r in rows]
        last_id = rows[-1][0]


@returns_json(router.get("/products"))
def list_products(status: str = "all", q: str = "", held: bool = False,
                  limit: Optional[int] = None, offset: int = 0, db: Session = Depends(get_db),
                  wid: Optional[int] = Depends(scope.current)):
    """status: all | pending (not yet detailed) | detailed | excluded. q: text search.

    Stock is only ever created by posting a GRN, so a product that traces back to
    no posted GRN is not stock and is kept out of every normal listing — it would
    otherwise sit there looking exactly like the real thing, scannable and
    printable. `status=excluded` shows precisely those, which is what the
    Inventory Repair screen lists; the count is on `/summary` either way, so the
    exclusion is never silent.

    `held=true` narrows the list to what this warehouse holds now (the
    `include_zero=False` scope), and `limit` caps it. Both exist for a catalogue
    too large to send whole — hundreds of thousands of SKUs after years of
    receipts, where the full list cannot be built or drawn. The screens list what
    is held and search the rest through `q`, which is matched in the database
    rather than over every product in Python. Called with neither, the answer is
    exactly what it always was."""
    from ..services import integrity
    # Only what THIS warehouse has anything to do with. Items whose balance here
    # has fallen to zero are kept: they are this building's stock lines, and a
    # list that dropped them the moment they sold out would hide the re-orders.
    # as a subquery: the ORDER BY below (description, id) decides the order
    # completely, so the plan cannot change which rows come first
    query = scope.products(db, db.query(models.Product), wid, include_zero=not held,
                           as_subquery=True)
    if status == "pending":
        query = query.filter((models.Product.detailed == False) | (models.Product.detailed.is_(None)))  # noqa: E712
    elif status == "detailed":
        query = query.filter(models.Product.detailed == True)  # noqa: E712
    if q:
        # the same four fields and the same case-insensitive substring test as
        # before, asked of the database
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(*(func.lower(func.coalesce(c, "")).like(like, escape="\\")
                                   for c in (models.Product.description, models.Product.sku,
                                             models.Product.barcode, models.Product.hsn))))
    # id breaks ties, so products sharing a description come back in the same
    # order every time — and the same ones fall inside `limit`
    order = (models.Product.description, models.Product.id)
    loaded = query.options(selectinload(models.Product.primary_supplier))

    def keep(chunk, ctx):
        if status == "excluded":
            return [p for p in chunk if ctx.product_state(p) != integrity.POSTED]
        return [p for p in chunk if ctx.product_state(p) == integrity.POSTED]

    # Provenance read for exactly the products found, not the whole catalogue.
    # With a limit, it is read a slice at a time in list order and stops once
    # the limit is filled: a search matching thousands of products shows the
    # first few hundred, and the same ones, whichever way it is read.
    # `offset` skips that many rows of the same ordered, filtered list — the
    # phone reads the catalogue a page at a time (its default list is 363K
    # products). Without it the answer is exactly what it always was.
    offset = max(0, int(offset or 0))
    if limit:
        # Only the ids are read in full, in list order; whole products are
        # loaded a chunk at a time until the page is filled — the same rows in
        # the same order as loading every match first, without building an
        # object for each of hundreds of thousands of matches.
        want, CHUNK, kept = offset + max(1, int(limit)), 2000, []
        for chunk_ids in _ordered_id_chunks(db, query, CHUNK):
            by_id = {p.id: p for p in loaded.filter(models.Product.id.in_(chunk_ids))}
            chunk = [by_id[j] for j in chunk_ids if j in by_id]
            kept += keep(chunk, integrity.Context(db, product_ids=[p.id for p in chunk]))
            if len(kept) >= want:
                break
        ps = kept[offset:want]
        ctx = integrity.Context(db, product_ids=[p.id for p in ps])
    else:
        ps = loaded.order_by(*order).all()
        ctx = integrity.Context(db, product_ids=[p.id for p in ps])
        ps = keep(ps, ctx)
    # Everything the rows below read per product, read once for the whole list:
    # the piece codes of exactly these products, and this warehouse's balances.
    ctx.preload_units([p.id for p in ps])
    balances = None
    if wid:
        B = models.StockBalance
        bq = db.query(B.product_id, B.qty, B.avg_cost).filter(B.warehouse_id == wid)
        if limit:
            # a page reads the balances of its own rows, not the warehouse's 363K
            bq = bq.filter(B.product_id.in_([p.id for p in ps]))
        balances = {pid: (q_, c_) for pid, q_, c_ in bq}
    return [_localise(_product_out(p, ctx), db, p, wid, balances) for p in ps]


@router.get("/products/{prod_id}")
def get_product(prod_id: int, db: Session = Depends(get_db)):
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    out = _product_out(p)
    out["movements"] = [{
        "id": m.id, "qty_delta": m.qty_delta, "kind": m.kind,
        "ref_type": m.ref_type, "ref_id": m.ref_id, "rate": m.rate,
        "balance_after": m.balance_after, "note": m.note,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    } for m in p.movements]
    return out


# There is deliberately NO product-edit endpoint here. Inventory is a view of what
# the GRN produced, not a second entry form: description / HSN / UOM come off the
# invoice, category and prices are set on the GRN breakdown, and the physical
# attributes are recorded in the phone app below. Correcting any of it means
# unposting the GRN, fixing the line and posting again — so a product's history and
# its master data can never disagree.


@router.post("/products/{prod_id}/detail")
def detail_product(prod_id: int, body: ProductDetail, db: Session = Depends(get_db)):
    """Save physical-inspection attributes (from the mobile app) and mark the
    product as detailed. Feeds straight into the shared inventory database."""
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    data = body.model_dump(exclude_none=True)
    for k in (*inv.SPLIT_ATTRS, "mrp", "sale_price", "sale_discount_pct"):
        if k in data:
            setattr(p, k, data[k])
    # Whatever this item's own business line records beyond those columns. Routed
    # through the catalogue so each value lands where that attribute belongs —
    # and so a key this line does not declare is dropped rather than stored.
    if data.get("attrs"):
        from ..services import catalogues as cat_svc
        cat_svc.write_attrs(db, p, data["attrs"])
    p.detailed = True
    p.detailed_at = dt.datetime.utcnow()
    p.detailed_by = data.get("detailed_by") or p.detailed_by or "mobile"
    # all details are set → mint sku + barcode so the product is now scannable
    ident = barcode_svc.assign_identifiers(db, p)
    db.commit()
    db.refresh(p)
    out = _product_out(p)
    out["identifiers_generated"] = ident["generated"]
    return out


class GenBarcode(BaseModel):
    force: Optional[bool] = False


@router.post("/products/{prod_id}/generate-barcode")
def generate_barcode(prod_id: int, db: Session = Depends(get_db)):
    """Assign the product its SKU if it hasn't got one — the only identifier we
    issue, and what its QR resolves to. Requires the product to be fully detailed
    first. (Path kept for compatibility; no barcode is minted any more.)"""
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    if not p.detailed:
        raise HTTPException(400, "Set all product details first, then generate the code.")
    ident = barcode_svc.assign_identifiers(db, p)
    db.commit()
    db.refresh(p)
    out = _product_out(p)
    out["identifiers_generated"] = ident["generated"]
    return out


# ---------------------------------------------------------------------------
#  Per-piece codes — one identity per garment under a SKU that has many
# ---------------------------------------------------------------------------
class UnitsPrinted(BaseModel):
    ids: list[int]
    printed_by: Optional[str] = None


def _unit_out(u: models.ProductUnit):
    return {
        "id": u.id, "code": u.code, "seq": u.seq, "status": u.status,
        "product_id": u.product_id, "purchase_id": u.purchase_id,
        "print_count": u.print_count or 0,
        "last_printed_at": u.last_printed_at.isoformat() if u.last_printed_at else None,
        "last_printed_by": u.last_printed_by,
    }


@router.get("/products/{prod_id}/sources")
def product_sources(prod_id: int, db: Session = Depends(get_db)):
    """The receipts (invoices) this product came in on, newest first — each with
    how many it brought — so the product can be traced back to its invoice."""
    PL, S, Pu = models.PurchaseLine, models.PurchaseLineSplit, models.Purchase
    qty = {}
    for pid, q, line_id in (db.query(PL.purchase_id, PL.qty, PL.id)
                            .filter(PL.product_id == prod_id).all()):
        qty.setdefault(pid, []).append(("line", line_id, q))
    for pid, q in (db.query(PL.purchase_id, S.qty).join(PL, PL.id == S.line_id)
                   .filter(S.product_id == prod_id).all()):
        qty.setdefault(pid, []).append(("split", None, q))
    if not qty:
        return []
    out = []
    for p in (db.query(Pu).filter(Pu.id.in_(list(qty))).order_by(Pu.id.desc()).all()):
        parts = qty[p.id]
        # a line that is broken down counts through its rows, not itself
        split_lines = {l.id for l in p.lines if l.splits}
        n = sum(float(q or 0) for kind, line_id, q in parts
                if kind == "split" or line_id not in split_lines)
        out.append({"purchase_id": p.id, "document_id": p.document_id, "grn_no": p.grn_no,
                    "invoice_number": p.invoice_number, "invoice_date": p.invoice_date,
                    "supplier_name": p.supplier.name if p.supplier else None,
                    "status": p.status, "qty": round(n, 3)})
    return out


@router.get("/products/{prod_id}/units")
def product_units(prod_id: int, db: Session = Depends(get_db)):
    """The individual pieces under one SKU — what the Inventory screen shows when
    the quantity is clicked. `serialisable` explains an empty list rather than
    leaving the screen to guess (fabric sold by the metre has no pieces).

    Each piece carries its own `state`, and the payload says outright whether the
    labels may print and why not. A dead code looks exactly like a live one on
    screen — same format, same QR, same product — so the difference has to be
    stated, not left for the eye."""
    from ..services import units as unit_svc
    from ..services import integrity
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    ctx = integrity.Context(db, product_ids=[p.id])
    rep = integrity.product_report(db, p, ctx)
    rows = db.query(models.ProductUnit).filter(
        models.ProductUnit.product_id == prod_id).order_by(models.ProductUnit.seq).all()
    ok, reason = unit_svc.can_serialise(p.uom, rep["expected_units"] or p.stock_qty, db)
    return {
        "product": {"id": p.id, "sku": p.sku, "description": p.description,
                    "name": stock_view.display_name(p),
                    "uom": p.uom, "stock_qty": p.stock_qty, "mrp": p.mrp,
                    "size": p.size, "color": p.color, "category": p.category,
                    # one code per stock unit — a pair of pillow covers is one
                    # article and gets one label, not two
                    "unit_type": p.unit_type, "pieces_per_unit": p.pieces_per_unit},
        "count": len(rows), "serialisable": ok, "reason": reason,
        # --- integrity: what may be printed, and what is merely left over ---
        "state": rep["state"], "live_units": rep["live_units"],
        "orphan_units": rep["orphan_units"], "expected_units": rep["expected_units"],
        "units_match": rep["units_match"],
        "can_print": rep["can_print"], "print_block": rep["print_block"],
        "issues": rep["issues"],
        "units": [{**_unit_out(u), "state": ctx.unit_state(u)} for u in rows],
    }


@router.post("/products/{prod_id}/units/generate")
def generate_units(prod_id: int, db: Session = Depends(get_db)):
    """Give stock received before per-piece codes existed its missing codes.

    Refused for a product that isn't stock: minting identities for garments no
    posted GRN ever brought in is how a phantom becomes scannable."""
    from ..services import units as unit_svc
    from ..services import integrity
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    rep = integrity.product_report(db, p)
    if rep["state"] != integrity.POSTED:
        raise HTTPException(400, "this product doesn't belong to a posted GRN, so it "
                                 "isn't stock — no piece codes can be issued for it")
    made, reason = unit_svc.backfill(db, p)
    if not made and reason:
        raise HTTPException(400, reason)
    db.commit()
    return {"ok": True, "created": len(made), "codes": [u.code for u in made]}


@router.get("/units/{uid}/qr.svg")
def unit_qr_svg(uid: int, scale: int = 4, db: Session = Depends(get_db)):
    from fastapi.responses import Response
    u = db.get(models.ProductUnit, uid)
    if not u:
        raise HTTPException(404, "piece not found")
    return Response(barcode_svc.unit_qr_svg(u, scale=max(1, min(scale, 10))),
                    media_type="image/svg+xml")


@router.get("/units/{uid}/qr.png")
def unit_qr_png(uid: int, scale: int = 6, db: Session = Depends(get_db)):
    from fastapi.responses import Response
    u = db.get(models.ProductUnit, uid)
    if not u:
        raise HTTPException(404, "piece not found")
    png = barcode_svc.unit_qr_png(u, scale=max(1, min(scale, 20)))
    if not png:
        raise HTTPException(503, "QR library not available on the server")
    return Response(png, media_type="image/png")


@router.get("/units/{uid}/label", response_class=HTMLResponse)
def unit_label(uid: int, db: Session = Depends(get_db)):
    """One piece's label — Print for a single garment, or Reprint a torn one.

    A single label is allowed where "print all" would be refused: reprinting one
    torn tag is exactly the job someone does while a count is being sorted out.
    What is refused is a code with no posted GRN behind it — that garment does not
    exist, so neither should its label."""
    from ..services import units as unit_svc
    from ..services import integrity
    u = db.get(models.ProductUnit, uid)
    if not u:
        raise HTTPException(404, "piece not found")
    if integrity.Context(db, product_ids=[u.product_id]).unit_state(u) != integrity.POSTED:
        raise HTTPException(400, f"{u.code} belongs to no posted GRN — it is a left-over "
                                 f"code, not a garment. Run Inventory Repair.")
    unit_svc.mark_printed(db, [u])
    db.commit()
    return HTMLResponse(barcode_svc.unit_labels_sheet([u]))


@router.get("/unit-labels", response_class=HTMLResponse)
def unit_labels(ids: str = "", product_id: int = 0, db: Session = Depends(get_db)):
    """A sheet of per-piece labels: a selection (ids=1,2,3) or every piece of one
    SKU. Opening it counts as printing them, which is what makes the screen able
    to offer Reprint afterwards.

    **Print all is refused on a mismatch.** A label is a physical thing stuck on a
    garment, so the number printed can never exceed the number of garments — and
    when the piece codes and the stock figure disagree, neither number can be
    trusted until someone says which is right. The check lives here rather than
    only in the UI because this URL is openable directly, and a guard a client can
    skip is not a guard."""
    from ..services import units as unit_svc
    from ..services import integrity
    q = db.query(models.ProductUnit)
    if ids.strip():
        want = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        rows = q.filter(models.ProductUnit.id.in_(want)).all()
        rows.sort(key=lambda u: want.index(u.id))
        # a hand-picked selection still may not include a dead code
        ctx = integrity.Context(db, product_ids=sorted({u.product_id for u in rows}))
        dead = [u.code for u in rows if ctx.unit_state(u) != integrity.POSTED]
        if dead:
            raise HTTPException(400, f"{len(dead)} of these piece codes belong to no "
                                     f"posted GRN ({', '.join(dead[:4])}"
                                     f"{'…' if len(dead) > 4 else ''}) — run Inventory "
                                     f"Repair before printing")
    elif product_id:
        p = db.get(models.Product, product_id)
        if not p:
            raise HTTPException(404, "product not found")
        ctx = integrity.Context(db, product_ids=[p.id])
        ok, why = integrity.can_print(db, p, ctx)
        if not ok:
            raise HTTPException(400, why)
        # LIVE codes only. Passing the check is not the same as every row being
        # printable: a SKU can hold the right number of live codes and still carry
        # left-over ones beside them, and "print all" must mean all the garments,
        # never all the rows.
        rows = [u for u in q.filter(models.ProductUnit.product_id == product_id).order_by(
            models.ProductUnit.seq).all()
            if ctx.unit_state(u) == integrity.POSTED]
    else:
        raise HTTPException(400, "pass ids= or product_id=")
    if not rows:
        raise HTTPException(404, "no pieces to print")
    unit_svc.mark_printed(db, rows)
    db.commit()
    return HTMLResponse(barcode_svc.unit_labels_sheet(rows))


@router.post("/units/printed")
def units_printed(body: UnitsPrinted, db: Session = Depends(get_db)):
    """Record a print without rendering a sheet — for a label printer driven
    elsewhere."""
    from ..services import units as unit_svc
    rows = db.query(models.ProductUnit).filter(
        models.ProductUnit.id.in_(body.ids)).all()
    unit_svc.mark_printed(db, rows, body.printed_by)
    db.commit()
    return {"ok": True, "marked": len(rows)}


@router.get("/units/lookup")
def unit_lookup(code: str, db: Session = Depends(get_db)):
    """Resolve a scanned per-piece label: which piece, and which product it is."""
    from ..services import units as unit_svc
    u = unit_svc.resolve(db, code)
    if not u:
        raise HTTPException(404, f"no piece for code '{code}'")
    out = _unit_out(u)
    out["product"] = _product_out(u.product) if u.product else None
    return out


@router.get("/lookup")
def lookup(code: str, db: Session = Depends(get_db)):
    """System-wide fetch: resolve a scanned/typed barcode, sku or id to a product."""
    p = barcode_svc.resolve(db, code)
    if not p:
        raise HTTPException(404, f"no product for code '{code}'")
    return _product_out(p)


@router.get("/product-card")
def product_card(code: str = "", product_id: int = 0, db: Session = Depends(get_db)):
    """The full product record as the stock screens show it — QR, name, the
    attribute tuple, and the batches it was received on.

    Same projection Stock Outward, Stock Inward and Purchase Return use for their
    lines, reachable on its own so a scan can be verified before anything is
    added to a document."""
    from ..services import stock_view
    if product_id:
        p = db.get(models.Product, product_id)
        if not p:
            raise HTTPException(404, "product not found")
        return stock_view.product_card(db, p)
    card = stock_view.card_for_code(db, code)
    if not card:
        raise HTTPException(404, f"no product for code '{code}'")
    return card


@router.get("/products/{prod_id}/barcode.svg")
def product_barcode_svg(prod_id: int, db: Session = Depends(get_db)):
    """Bare SVG of the product's barcode — for inline preview in the UI."""
    from fastapi.responses import Response
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    svg = barcode_svc.barcode_svg(p.barcode or p.sku or str(p.id))
    return Response(svg, media_type="image/svg+xml")


@router.get("/products/{prod_id}/qr.svg")
def product_qr_svg(prod_id: int, scale: int = 4, db: Session = Depends(get_db)):
    """Bare SVG of the product's QR code — for inline preview in the UI."""
    from fastapi.responses import Response
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    return Response(barcode_svc.product_qr_svg(p, scale=max(1, min(scale, 10))),
                    media_type="image/svg+xml")


@router.get("/products/{prod_id}/qr.png")
def product_qr_png(prod_id: int, scale: int = 6, db: Session = Depends(get_db)):
    """The product's QR as a PNG — what the phone app shows for a variant it has
    just created, since React Native cannot render the SVG above."""
    from fastapi.responses import Response
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    png = barcode_svc.product_qr_png(p, scale=max(1, min(scale, 20)))
    if not png:
        raise HTTPException(503, "QR library not available on the server")
    return Response(png, media_type="image/png")


@router.get("/products/{prod_id}/qr-payload")
def product_qr_payload(prod_id: int, db: Session = Depends(get_db)):
    """Exactly what is encoded in this product's QR — for debugging a label and
    for confirming what a scanner will receive."""
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    payload = barcode_svc.qr_payload(p)
    return {"product_id": p.id, "schema": barcode_svc.QR_SCHEMA,
            "bytes": len(payload.encode()), "payload": payload,
            "decoded": barcode_svc.parse_qr_payload(payload)}


@router.get("/categorize")
def categorize_description(description: str, section: Optional[str] = None,
                           limit: int = 5, warehouse_id: Optional[int] = None,
                           catalogue_id: Optional[int] = None,
                           db: Session = Depends(get_db)):
    """Map a free-text description onto the category master.

    `confident` says whether this would be applied automatically; `candidates`
    are the ranked alternatives for a human to choose from when it wouldn't.
    Pass `section` to force OVERALL/KIDS/LADIES/MENS instead of the detected one.

    Scoped to one business line: pass `warehouse_id` (the natural thing a screen
    knows) or `catalogue_id` directly. Neither means every category, which is
    what a single-catalogue install has always meant by the question."""
    from ..services import categorize
    from ..services import catalogues as cat_svc
    if warehouse_id and not catalogue_id:
        c = cat_svc.for_warehouse(db, warehouse_id)
        catalogue_id = c.id if c else None
    return categorize.suggest(db, description, limit=max(1, min(limit, 25)),
                              section=section, catalogue_id=catalogue_id)


@router.post("/products/{prod_id}/categorize")
def categorize_product(prod_id: int, force: bool = False, db: Session = Depends(get_db)):
    """Re-run category mapping for one product. By default won't overwrite a
    category that is already set — pass force=true to re-map it."""
    from ..services import categorize
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    res = categorize.categorise_product(db, p, force=force)
    db.commit()
    db.refresh(p)
    return {"product": _product_out(p), "suggestion": res}


@router.post("/categorize-all")
def categorize_all(force: bool = False, db: Session = Depends(get_db)):
    """Backfill categories across the product master — for the products that
    already existed before category mapping was added. Returns what changed and
    what needs a human, so an unmapped product is never just silently skipped."""
    from ..services import categorize
    applied, needs_review = 0, []
    for p in db.query(models.Product).all():
        if p.category and not force:
            continue
        res = categorize.categorise_product(db, p, force=force)
        if res["applied"]:
            applied += 1
        else:
            needs_review.append({
                "id": p.id, "sku": p.sku, "description": p.description,
                "best": res["best"], "candidates": res["candidates"]})
    db.commit()
    return {"ok": True, "applied": applied, "needs_review": len(needs_review),
            "unmapped": needs_review}


@router.get("/products/{prod_id}/label", response_class=HTMLResponse)
def product_label(prod_id: int, db: Session = Depends(get_db)):
    """Print-ready single QR label for one product."""
    from ..services import integrity
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    ok, why = integrity.can_print(db, p)
    if not ok:
        raise HTTPException(400, why)
    if not p.sku:
        barcode_svc.assign_identifiers(db, p)
        db.commit(); db.refresh(p)
    return HTMLResponse(barcode_svc.labels_sheet([p]))


@router.get("/labels", response_class=HTMLResponse)
def labels(ids: str = "", status: str = "detailed", db: Session = Depends(get_db)):
    """Print-ready QR-label sheet. Pass ids=1,2,3 for a selection, else all
    products matching status (default: detailed).

    Products that aren't stock are dropped from the sheet rather than failing the
    whole print — a bulk run should give you the labels it legitimately can, and
    say what it left out. An explicit id list is stricter: naming a record that is
    not stock is a mistake worth surfacing, not silently honouring."""
    from ..services import integrity
    want = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids.strip() else None
    # a selection needs only its own products' provenance; the bulk sheet is
    # every product, so it reads everything as before
    ctx = integrity.Context(db, product_ids=want) if want else integrity.Context(db)
    if ids.strip():
        ps = db.query(models.Product).filter(models.Product.id.in_(want)).all()
        ps.sort(key=lambda p: want.index(p.id))
        bad = [p for p in ps if ctx.product_state(p) != integrity.POSTED]
        if bad:
            raise HTTPException(400, f"{len(bad)} of these products belong to no posted "
                                     f"GRN ({', '.join((p.sku or '?') for p in bad[:4])}"
                                     f"{'…' if len(bad) > 4 else ''}) — run Inventory Repair")
    else:
        q = db.query(models.Product)
        if status == "detailed":
            q = q.filter(models.Product.detailed == True)  # noqa: E712
        ps = [p for p in q.order_by(models.Product.description).all()
              if ctx.product_state(p) == integrity.POSTED]
    if not ps:
        raise HTTPException(404, "nothing to print — no product here belongs to a posted GRN")
    # make sure every product on the sheet is scannable
    changed = False
    for p in ps:
        if not p.sku:
            barcode_svc.assign_identifiers(db, p); changed = True
    if changed:
        db.commit()
    return HTMLResponse(barcode_svc.labels_sheet(ps))


# ---------------------------------------------------------------------------
#  Inventory repair — find and remove what isn't stock
# ---------------------------------------------------------------------------
@router.get("/integrity")
def integrity_scan(db: Session = Depends(get_db)):
    """Read-only: every inventory record that fails a check, and why.

    This is what the Repair screen shows *before* anyone agrees to delete
    anything — orphan products, orphan piece codes, orphan cartons, and any SKU
    whose piece codes don't match what it received. `unposted_products` is listed
    separately and is never offered for deletion: those are kept on purpose."""
    from ..services import integrity
    return integrity.scan(db)


@router.post("/repair")
def integrity_repair(dry_run: bool = False, db: Session = Depends(get_db)):
    """Delete the debris — records traceable to no GRN at all and carrying no
    ledger history of their own.

    `dry_run=true` returns exactly what would go without touching anything.
    Products kept at zero stock after an unpost are never removed: they hold
    detailing recorded by hand in the warehouse, and no cleanup is worth
    destroying that silently."""
    from ..services import integrity
    result = integrity.repair(db, dry_run=dry_run)
    if not dry_run:
        db.commit()
    return result


@router.post("/products/{prod_id}/adjust-stock")
def adjust_stock(prod_id: int, body: StockAdjust, db: Session = Depends(get_db)):
    """Correct a product's stock to an exact figure (writes an adjustment
    movement — the ledger stays the source of truth)."""
    p = db.get(models.Product, prod_id)
    if not p:
        raise HTTPException(404, "product not found")
    inv.adjust_stock(db, p, body.new_qty, body.note or "manual adjustment")
    db.commit()
    db.refresh(p)
    return _product_out(p)


@router.get("/locate")
def locate(code: str = "", product_id: int = 0, db: Session = Depends(get_db)):
    """Item Locator: one scanned code, everything known about that item.

    Scanning a tag answers "what is this?" — and then, always, four more
    questions that today need four screens: where did it come from, where is it
    now, where has it gone, and what has it cost. This gathers those into one
    reply so a garment in somebody's hand can be accounted for without them
    knowing which screen holds which part of the answer.

    Every kind of tag the warehouse prints is accepted, because the person
    holding one does not know which kind it is: a product QR, a per-piece code
    off a single garment, a carton label, a barcode, or a typed SKU.

    A CARTON is answered as a carton rather than as one of the products inside
    it. A bundle label names a box, and quietly resolving it to a garment would
    answer a question nobody asked with a stock figure for the wrong thing.
    """
    from ..services import (stock_view, bundles as bundle_svc, integrity,
                            locator as loc, pos_sales)

    code = (code or "").strip()
    if not code and not product_id:
        raise HTTPException(400, "give a code to look up")

    # A carton first: bundle_svc.resolve is the only reader that can tell a
    # bundle label apart, and barcode_svc deliberately refuses them.
    if code:
        bundle = bundle_svc.resolve(db, code)
        if bundle:
            from .bundles import _out as _bundle_out
            return {"kind": "bundle", "code": code, "bundle": _bundle_out(bundle, with_items=True)}

    unit = barcode_svc._resolve_unit_row(db, code) if code else None
    product = (db.get(models.Product, product_id) if product_id
               else barcode_svc.resolve(db, code))
    if not product:
        raise HTTPException(404, f"Nothing matches '{code}'. It is not a product QR, "
                                 "a piece code, a carton label, a barcode or a SKU.")

    # --- where it came from: the receipts that brought it in ---
    receipts = []
    for pl in (db.query(models.PurchaseLine)
                 .filter(models.PurchaseLine.product_id == product.id)
                 .order_by(models.PurchaseLine.id.desc()).limit(25).all()):
        pur = pl.purchase
        receipts.append({
            "purchase_id": pur.id if pur else None,
            "grn_no": pur.grn_no if pur else None,
            "status": pur.status if pur else None,
            "invoice_number": pur.invoice_number if pur else None,
            "invoice_date": pur.invoice_date if pur else None,
            "posted_at": pur.posted_at.isoformat() if pur and pur.posted_at else None,
            "supplier": pur.supplier.name if pur and pur.supplier else None,
            "qty": pl.qty, "rate": pl.rate, "amount": pl.amount,
        })

    # --- where it is: the cartons holding it, and their locations ---
    cartons = [{
        "id": b.id, "code": b.code, "location": b.location, "status": b.status,
        "qty": b.qty, "grn_no": b.grn_no, "invoice_number": b.invoice_number,
    } for b in (db.query(models.Bundle)
                  .join(models.PurchaseLine, models.Bundle.line_id == models.PurchaseLine.id)
                  .filter(models.PurchaseLine.product_id == product.id)
                  .order_by(models.Bundle.id.desc()).limit(25).all())]

    # --- where it went: dispatches out of the warehouse ---
    dispatches = []
    for ol in (db.query(models.StockOutwardLine)
                 .filter(models.StockOutwardLine.product_id == product.id)
                 .order_by(models.StockOutwardLine.id.desc()).limit(25).all()):
        ow = ol.outward
        dispatches.append({
            "outward_id": ow.id if ow else None,
            "code": ow.code if ow else None,
            "date": ow.date if ow else None,
            "to": ow.to_destination if ow else None,
            "status": ow.status if ow else None,
            "qty": ol.qty, "accepted_qty": ol.accepted_qty, "rate": ol.rate,
        })

    # --- the ledger: every movement, which is the audit trail behind the figure ---
    movements = [{
        "at": m.created_at.isoformat() if m.created_at else None,
        "kind": m.kind, "qty_delta": m.qty_delta, "balance_after": m.balance_after,
        "rate": m.rate, "note": m.note,
    } for m in (db.query(models.StockMovement)
                  .filter(models.StockMovement.product_id == product.id)
                  .order_by(models.StockMovement.id.desc()).limit(30).all())]

    # --- the newest receipt, which is what the money and the lorry hang off ---
    newest = (db.query(models.PurchaseLine)
                .filter(models.PurchaseLine.product_id == product.id)
                .order_by(models.PurchaseLine.id.desc()).first())
    newest_purchase = newest.purchase if newest is not None else None
    consignment = loc.consignment_of(db, newest_purchase)

    # What the till knows. Read-only, and absent rather than wrong when the shop
    # is not installed beside us — see services/pos_sales.
    sales = pos_sales.bills_for_product(product.id)

    units = db.query(models.ProductUnit).filter(
        models.ProductUnit.product_id == product.id)
    return {
        "kind": "product",
        "code": code,
        # The card the stock screens already show, so the identity block here is
        # the same one seen everywhere else rather than a second opinion.
        "product": stock_view.product_card(db, product),
        # Set when the code scanned was a single garment's, not the SKU's — the
        # difference between "this design" and "this exact piece". The id rides
        # along so that ONE piece can be reprinted from here: somebody holding a
        # garment whose sticker has come off is the commonest reason to be on
        # this screen at all.
        "unit": ({"id": unit.id, "code": unit.code, "seq": unit.seq,
                  "status": unit.status, "printed": unit.print_count} if unit else None),
        # Whether labels may be printed for this item at all, and why not. The
        # same rule Inventory prints under (services/integrity): no stock means
        # no garments to label, and a piece-code count that disagrees with the
        # stock figure means the sheet would be wrong before it left the printer.
        "printing": dict(zip(("can_print", "why"), integrity.can_print(db, product))),
        "unit_counts": {
            "total": units.count(),
            "in_stock": units.filter(models.ProductUnit.status == "in_stock").count(),
        },
        "receipts": receipts,
        "cartons": cartons,
        "dispatches": dispatches,
        "movements": movements,
        # --- and the rest of the account, gathered from the other modules ---
        # The lorry that brought it, out of the transport register: a garment can
        # be traced back past its own invoice to the consignment it arrived on.
        "consignment": consignment,
        # How long it has been standing, against the holding period it was bought
        # under. The single most useful thing to say about a piece nobody sold.
        "age": loc.stock_age(newest_purchase, consignment),
        # Cost, cost with purchase tax, selling price and the margin between —
        # four figures that live on four screens and are never seen together.
        "pricing": loc.pricing_of(product, newest, newest_purchase),
        # The stock figure taken apart: purchased, transferred, returned,
        # adjusted, plus what was short or damaged and never became stock at all.
        "warehouse_stock": loc.warehouse_stock(db, product),
        # One row per destination these pieces went to, sent against accepted.
        "locations": loc.locations_of(db, product,
                                      sold=(sales.get("qty") if sales.get("available") else None)),
        "transfers": loc.transfers_of(db, product),
        # Whether it SOLD or is merely gone — two answers that look identical
        # from this side of the wall.
        "sales": sales,
    }
