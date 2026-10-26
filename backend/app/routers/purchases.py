from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from sqlalchemy import and_, case, false, func, not_, select, true
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session, joinedload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..services import inventory as inv
from ..services import barcode_svc
from ..services import shortages as short_svc
from ..services import size_split
from ..services import scope

router = APIRouter(prefix="/api/purchases", tags=["purchases"])


class SplitRows(BaseModel):
    """Attribute breakdown of one billed line. An empty list clears it."""
    rows: List[Dict[str, Any]]


class ShortageRows(BaseModel):
    """What the supplier billed on this line and the boxes did not hold. An empty
    list clears it. Each row: {kind, qty, variant, reason, note, recorded_by}."""
    rows: List[Dict[str, Any]]
    recorded_by: Optional[str] = None


class WaiveShortage(BaseModel):
    """Accept a shortage rather than claim it — the supplier is sending the
    balance, or it is too small to raise a debit note for."""
    reason: Optional[str] = None
    by: Optional[str] = None


class ReceiveAt(BaseModel):
    """Which warehouse took this delivery in."""
    warehouse_id: Optional[int] = None


class ScanCode(BaseModel):
    """A QR payload / barcode / SKU scanned to pin a line (or one of its variants)
    to an existing inventory product."""
    code: str
    split_id: Optional[int] = None


class LineEdit(BaseModel):
    """What can be set on a GRN line before posting. `category` decides the
    product's category master mapping instead of leaving it to auto-classification.
    `unit_type` decides what one of these IS — piece, pair, dozen — and therefore
    how a billed dozen converts into stock and how many QR labels it produces.
    Empty string clears either (back to auto).

    The three retail fields are the pricing carried off the invoice review — the
    MRP the supplier printed and the shelf price someone set against it. Editable
    here so a mistyped price is a correction rather than an unpost."""
    category: Optional[str] = None
    unit_type: Optional[str] = None
    mrp: Optional[float] = None
    sale_price: Optional[float] = None
    sale_discount_pct: Optional[float] = None
    # What the goods are, beyond size / brand / design — applied to the product
    # at post and inherited by breakdown rows that leave them blank.
    style: Optional[str] = None
    material: Optional[str] = None
    pattern: Optional[str] = None
    sleeve: Optional[str] = None
    product_type: Optional[str] = None


def _unit_view(db, line, split=None, qty=None, rate=None):
    """What this row's billed quantity becomes, and what decided it.

    Returned on every line so the receiving screen can show the arithmetic before
    anyone posts it — "1 DOZ → 12 pcs → 6 PAIR · 6 QR label(s)" — rather than
    discovering after the fact that a dozen became one."""
    from ..services import unit_types as ut
    product = (split.product if split is not None else
               (line.product if not line.is_split else None))
    code, _, why = ut.resolve(
        db, explicit=(split.unit_type if split is not None else None) or line.unit_type,
        description=line.description,
        category=(split.category if split is not None else None) or line.category,
        uom=line.uom, product=product)
    conv = ut.convert(db, qty, line.uom, code,
                      rate if rate is not None else line.rate)
    conv["why"] = why
    conv["locked"] = bool(product is not None and product.unit_type)
    return conv


def _split_out(s: models.PurchaseLineSplit, db: Session = None):
    p = s.product
    d = {a: getattr(s, a) for a in inv.SPLIT_ATTRS}
    d.update({
        "id": s.id, "category": s.category, "unit_type": s.unit_type,
        "qty": s.qty, "rate": s.effective_rate, "own_rate": s.rate,
        "mrp": s.mrp, "sale_price": s.sale_price, "sale_discount_pct": s.sale_discount_pct,
        "amount": s.amount, "code": s.code, "label": s.variant_label,
        "product_id": s.product_id, "is_new_product": s.is_new_product,
        "product_sku": p.sku if p else None,
        "product_barcode": p.barcode if p else None,
        "stock_after": p.stock_qty if p else None,
        # whether the warehouse has since inspected this item on the phone. The
        # breakdown supplies what the invoice implied (size, colour); detailing is
        # where fit, pattern, material and pricing come from someone holding it.
        "product_detailed": bool(p.detailed) if p else None,
    })
    if db is not None:
        d["unit"] = _unit_view(db, s.line, s, qty=s.qty, rate=s.effective_rate)
    return d


def _line_out(l: models.PurchaseLine, db: Session = None, suggest: bool = True):
    """One GRN line. `suggest` is off for a posted GRN — a category suggestion is
    only useful while the line can still be edited. Shortages are returned either
    way: on a posted GRN they are the outstanding claim against the supplier."""
    st = inv.split_status(l)
    short = short_svc.line_totals(l)
    # what the description would map to, so the grid can show (and one-click accept)
    # the mapping instead of letting products land "unmapped" for someone to fix
    suggestion = None
    if db is not None and suggest and not l.category:
        from ..services import categorize
        # Suggested from the receiving warehouse's own category master. A silk
        # GRN must never be offered a garment category, and a catalogue with no
        # categories yet correctly offers nothing.
        s = categorize.suggest(db, l.description, limit=4,
                               catalogue_id=inv.purchase_catalogue_id(db, l.purchase))
        # `via` lets the screen say WHY: "rules" is the engine's own reading,
        # "alias" is a mapping someone already taught it for this wording
        suggestion = {"best": (s["best"] or {}).get("name"), "confident": s["confident"],
                      "via": s.get("via"), "candidates": [c["name"] for c in s["candidates"]]}
    return {
        "id": l.id, "product_id": l.product_id, "barcode": l.barcode,
        "description": l.description, "hsn": l.hsn, "qty": l.qty, "uom": l.uom,
        "rate": l.rate, "amount": l.amount, "is_new_product": l.is_new_product,
        # retail, carried from the invoice review and applied to the product at
        # post — a breakdown row overrides it per variant
        "mrp": l.mrp, "sale_price": l.sale_price,
        "sale_discount_pct": l.sale_discount_pct,
        "size": l.size, "brand": l.brand, "design_no": l.design_no,
        "style": l.style, "material": l.material, "pattern": l.pattern,
        "sleeve": l.sleeve, "product_type": l.product_type,
        # "30:2, 32:4, 34:4, 36:2" read back as rows. The supplier already
        # counted the mix; offering it here means nobody re-keys a count that has
        # been done, and each size still becomes its own product with its own SKU
        # and QR. Offered, never applied on its own — see services/size_split.py.
        "size_breakdown": size_split.suggest(l) if suggest else None,
        "category": l.category, "category_suggestion": suggestion,
        # what one of these is, and what the billed quantity becomes because of it
        "unit_type": l.unit_type,
        "unit": _unit_view(db, l, qty=short["received_qty"]) if db is not None else None,
        "product_category": l.product.category if l.product else None,
        "product_sku": l.product.sku if l.product else None,
        "product_detailed": bool(l.product.detailed) if l.product else None,
        # The bundle itself is SPLIT: it is what the supplier billed, and it never
        # becomes stock — the rows below it do. Said explicitly rather than left to
        # each client to infer from a non-empty list, because "this row does not
        # move stock" is exactly the thing a receiving screen must not get wrong.
        "is_split": l.is_split,
        # the QR carries the whole product record; the code itself is the identity
        "qr_code": (l.product.barcode or l.product.sku) if l.product else None,
        "stock_after": l.product.stock_qty if l.product else None,
        "splits": [_split_out(s, db) for s in l.splits],
        "split_qty": st["split_qty"], "split_remainder": st["remainder"],
        "split_balanced": st["balanced"],
        # --- what actually arrived ---
        # `qty` above stays the supplier's figure; THIS is the number that becomes
        # stock, that the breakdown has to add up to, and that goes in the carton.
        # The two differ by exactly the shortage rows below.
        "received_qty": short["received_qty"],
        "short_qty": short["short_qty"], "damaged_qty": short["damaged_qty"],
        "excess_qty": short["excess_qty"], "missing_qty": short["missing_qty"],
        "has_shortage": short["rows"] > 0,
        "shortage_value": round(sum(short_svc.value(s) for s in l.shortages), 2),
        "shortages": ([short_svc.shortage_out(db, s) for s in l.shortages]
                      if db is not None else []),
    }


def _purchase_out(p: models.Purchase, with_lines=False, db: Session = None):
    d = {
        "id": p.id, "document_id": p.document_id, "supplier_id": p.supplier_id,
        "supplier_name": p.supplier.name if p.supplier else None,
        "grn_no": p.grn_no, "invoice_number": p.invoice_number,
        "warehouse_id": p.warehouse_id,
        "warehouse_name": p.warehouse.name if p.warehouse else None,
        "invoice_date": p.invoice_date, "taxable_total": p.taxable_total,
        "tax_total": p.tax_total, "grand_total": p.grand_total,
        "status": p.status, "line_count": len(p.lines),
        # a split line creates one product per size, not one for the line
        "new_products": (sum(1 for l in p.lines if not l.is_split and l.is_new_product)
                         + sum(1 for l in p.lines for s in l.splits if not s.product_id)),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
    }
    # Once posted, the receipt becomes a worklist: every item it created still has
    # to be looked at and detailed on the phone. Counting it here lets the GRN list
    # say so without opening each one.
    if p.status == "posted":
        holders = [h for l in p.lines for h in (l.splits if l.is_split else [l])]
        prods = [h.product for h in holders if h.product]
        d["items"] = len(holders)
        d["items_pending_detail"] = sum(1 for x in prods if not x.detailed)
    # What the supplier billed and the boxes did not hold. On a draft it is the
    # thing standing between the receipt and a truthful post; on a posted GRN it
    # is an open claim — so it is reported in both states. The headline figures
    # need no queries, which keeps them affordable on the list screen; the full
    # per-row detail (including what has already been claimed) comes with the lines.
    shorts = [sh for l in p.lines for sh in l.shortages if sh.claimable]
    d["short_qty"] = round(sum(float(sh.qty or 0) for sh in shorts), 3)
    d["short_value"] = round(sum(short_svc.value(sh) for sh in shorts), 2)
    d["short_lines"] = len(shorts)
    if with_lines and db is not None:
        d["shortages"] = short_svc.purchase_summary(db, p)
    if with_lines:
        # category suggestions are only useful while the GRN can still be edited
        d["lines"] = [_line_out(l, db, suggest=p.status != "posted") for l in p.lines]
        d["unbalanced_splits"] = [l.id for l in p.lines
                                  if l.is_split and not inv.split_status(l)["balanced"]]
    return d


@returns_json(router.get(""))
def list_purchases(db: Session = Depends(get_db),
                   wid: Optional[int] = Depends(scope.current)):
    """The GRN list for the warehouse this call is made inside.

    A draft with no warehouse chosen yet is included: it is a receipt somebody
    here has started, and it would otherwise be on nobody's screen until they
    picked a building — which is the moment they most need to find it again."""
    q = scope.purchases(db.query(models.Purchase), wid)
    # supplier and warehouse names come in with the rows — lazily they were one
    # query per distinct supplier, thousands on a list of a few years' GRNs
    ps = (q.options(joinedload(models.Purchase.supplier), joinedload(models.Purchase.warehouse))
           .order_by(models.Purchase.id.desc()).all())
    return _purchase_list_out(db, q, ps)


@returns_json(router.get("/page"))
def page_purchases(page: int = 1, size: int = 50, q: str = "", filter: str = "all",
                   date_from: str = "", date_to: str = "", supplier: str = "",
                   min_total: Optional[float] = None, max_total: Optional[float] = None,
                   db: Session = Depends(get_db),
                   wid: Optional[int] = Depends(scope.current)):
    """One page of the GRN list, with the sidebar's counts — what the GRN screen
    used to work out in the browser from all 35K receipts (17 MB).

    The same rows as `list_purchases` (same scope, newest first, same fields —
    built by the same `_purchase_list_out`, for this page only), and the same
    rules the screen applied: `filter` all | draft | posted | short (short =
    short_qty > 0) | nodoc (no uploaded invoice document behind it — the
    invoices brought over from the old system, and any keyed without a scan;
    Invoice Entry lists these beside its documents), and `q` a case-insensitive substring of supplier, invoice
    number or status — the browser's `matches()`, applied here in Python so a
    character lower-cases the same way it did there. `size` 0 is every row, as
    the screen's "All" page size was.

    The Filters panel (Invoice Entry) narrows the whole set before anything
    else, so the chip counts describe what the panel leaves: `date_from` /
    `date_to` an inclusive invoice-date range (ISO; an invoice with no readable
    date is outside any range that is set), `supplier` a case-insensitive
    substring of the supplier's name, `min_total` / `max_total` an inclusive
    range on the grand total. None of them set, nothing changes."""
    shown, counts, base = _purchase_selection(db, wid, q, filter, date_from, date_to,
                                              supplier, min_total, max_total)
    P = models.Purchase
    total = len(shown)
    size = max(0, int(size))
    pages = 1 if size == 0 else max(1, -(-total // size))
    page = min(max(1, int(page)), pages)
    ids = shown if size == 0 else shown[(page - 1) * size: page * size]
    if ids:
        page_q = base.filter(P.id.in_(ids))
        ps = (page_q.options(joinedload(P.supplier), joinedload(P.warehouse))
                    .order_by(P.id.desc()).all())
        out = _purchase_list_out(db, page_q, ps)
    else:
        out = []
    return {"rows": out, "total": total, "page": page, "size": size, "pages": pages, "counts": counts}


def _purchase_selection(db, wid, q, filter, date_from, date_to, supplier, min_total, max_total):
    """The GRN list's rules — scope, the Filters panel, the chip, the search —
    as (ids newest first, chip counts, the scoped query). One helper so the GRN
    page and the Invoice Entry list can never disagree about which receipts match."""
    from ..services import dates as date_svc
    P = models.Purchase
    base = scope.purchases(db.query(P), wid)
    rows = (base.outerjoin(models.Supplier, models.Supplier.id == P.supplier_id)
                .with_entities(P.id, P.status, P.invoice_number, models.Supplier.name, P.document_id,
                               P.invoice_date, P.grand_total)
                .order_by(P.id.desc()).all())
    lo, hi = date_svc.to_iso(date_from), date_svc.to_iso(date_to)
    who = (supplier or "").strip().lower()
    if lo or hi or who or min_total is not None or max_total is not None:
        def within(r):
            if lo or hi:
                d = date_svc.to_iso(r.invoice_date)
                if not d or (lo and d < lo) or (hi and d > hi):
                    return False
            if who and who not in (r.name or "").lower():
                return False
            total = float(r.grand_total or 0)
            if min_total is not None and total < min_total:
                return False
            if max_total is not None and total > max_total:
                return False
            return True
        rows = [r for r in rows if within(r)]
    shorts = _claimable_shortages(db, base.with_entities(P.id).subquery())
    short_ids = {pid for pid, s in shorts.items() if round(sum(float(sh.qty or 0) for sh in s), 3) > 0}
    counts = {"draft": sum(1 for r in rows if r.status == "draft"),
              "posted": sum(1 for r in rows if r.status == "posted"),
              "short": sum(1 for r in rows if r.id in short_ids), "all": len(rows),
              "nodoc": sum(1 for r in rows if r.document_id is None)}
    keep = {"all": lambda r: True, "short": lambda r: r.id in short_ids,
            "nodoc": lambda r: r.document_id is None}.get(
        filter, lambda r: r.status == filter)
    needle = (q or "").lower()

    def hit(r):
        # JS: String(v ?? '').toLowerCase().includes(q.toLowerCase())
        return not needle or any(needle in ("" if v is None else str(v)).lower()
                                 for v in (r.name, r.invoice_number, r.status))
    shown = [r.id for r in rows if keep(r) and hit(r)]
    return shown, counts, base


#: An invoice's stage in Invoice Entry: uploaded documents not yet checked,
#: checked ones with no receipt yet, receipts being worked on, and receipts in
#: stock. Shown as the progress on the invoice's own page too.
INVOICE_STAGES = ("review", "checked", "receiving", "stock")


@returns_json(router.get("/invoices"))
def invoice_page(page: int = 1, size: int = 100, q: str = "", stage: str = "all",
                 date_from: str = "", date_to: str = "", supplier: str = "",
                 min_total: Optional[float] = None, max_total: Optional[float] = None,
                 db: Session = Depends(get_db),
                 wid: Optional[int] = Depends(scope.current)):
    """Invoice Entry's list: every supplier invoice, whatever it has become.

    Uploaded documents that have no receipt yet come first — they are the work
    waiting ("review": read, not yet confirmed; "checked": confirmed, no GRN
    yet) — then every receipt, newest first ("receiving": draft; "stock":
    posted). A document that already has a receipt is listed once, as that
    receipt. The receipts follow the GRN list's own rules (_purchase_selection),
    so the same filters give the same receipts; `stage` also takes "short".
    Documents are narrowed by the same panel: invoice date, supplier, total."""
    from ..services import dates as date_svc
    from .documents import _doc_out
    D = models.Document
    docs = scope.documents(db.query(D), wid).filter(
        (D.document_type == "invoice") | (D.document_type.is_(None))).order_by(D.id.desc()).all()
    has_grn = {d for (d,) in db.query(models.Purchase.document_id)
               .filter(models.Purchase.document_id.isnot(None)).all()}
    lo, hi = date_svc.to_iso(date_from), date_svc.to_iso(date_to)
    who = (supplier or "").strip().lower()
    needle = (q or "").lower()
    doc_rows, doc_counts = [], {"review": 0, "checked": 0}
    for d in docs:
        if d.id in has_grn:
            continue
        o = _doc_out(d)
        ex = d.latest_extraction
        inv = ((ex.data if ex else {}) or {}).get("invoice") or {}
        o["invoice_date"] = inv.get("date")
        total = float(o.get("grand_total") or 0)
        if (lo or hi):
            iso = date_svc.to_iso(o["invoice_date"])
            if not iso or (lo and iso < lo) or (hi and iso > hi):
                continue
        if who and who not in (o.get("supplier_name") or "").lower():
            continue
        if min_total is not None and total < min_total:
            continue
        if max_total is not None and total > max_total:
            continue
        st = "checked" if d.status in ("confirmed", "posted") else "review"
        doc_counts[st] += 1
        o["stage"] = st
        if needle and not any(needle in ("" if v is None else str(v)).lower()
                              for v in (o.get("supplier_name"), o.get("invoice_number"), o.get("status"))):
            continue
        doc_rows.append(o)
    grn_filter = {"receiving": "draft", "stock": "posted", "short": "short"}.get(stage, "all")
    if stage in ("review", "checked"):
        grn_ids, gcounts, base = [], None, None
        _, gcounts, _ = _purchase_selection(db, wid, "", "all", date_from, date_to,
                                            supplier, min_total, max_total)
    else:
        grn_ids, gcounts, base = _purchase_selection(db, wid, q, grn_filter, date_from, date_to,
                                                     supplier, min_total, max_total)
    docs_shown = [o for o in doc_rows if stage in ("all", o["stage"])]
    keys = [("d", o["id"]) for o in docs_shown] + [("p", i) for i in grn_ids]
    total = len(keys)
    size = max(0, int(size))
    pages = 1 if size == 0 else max(1, -(-total // size))
    page = min(max(1, int(page)), pages)
    part = keys if size == 0 else keys[(page - 1) * size: page * size]
    by_doc = {o["id"]: o for o in docs_shown}
    pids = [i for k, i in part if k == "p"]
    grn_rows = {}
    if pids:
        Pm = models.Purchase
        pq = scope.purchases(db.query(Pm), wid).filter(Pm.id.in_(pids))
        ps = pq.options(joinedload(Pm.supplier), joinedload(Pm.warehouse)).order_by(Pm.id.desc()).all()
        grn_rows = {r["id"]: r for r in _purchase_list_out(db, pq, ps)}
    rows = []
    for k, i in part:
        if k == "d":
            o = by_doc[i]
            rows.append({"kind": "doc", "key": f"d{i}", "document_id": i, "purchase_id": None,
                         "stage": o["stage"], "status": o["status"],
                         "supplier_name": o.get("supplier_name"), "invoice_number": o.get("invoice_number"),
                         "invoice_date": o.get("invoice_date"), "grand_total": o.get("grand_total"),
                         "confidence": o.get("confidence"), "has_image": o.get("has_image"),
                         "filename": o.get("filename")})
        elif i in grn_rows:
            r = grn_rows[i]
            rows.append({**r, "kind": "grn", "key": f"p{i}", "purchase_id": i,
                         "stage": "stock" if r["status"] == "posted" else "receiving"})
    counts = {"review": doc_counts["review"], "checked": doc_counts["checked"],
              "receiving": gcounts["draft"], "stock": gcounts["posted"], "short": gcounts["short"],
              "all": doc_counts["review"] + doc_counts["checked"] + gcounts["all"]}
    return {"rows": rows, "total": total, "page": page, "size": size, "pages": pages, "counts": counts}

def _purchase_list_out(db: Session, q, ps):
    """`_purchase_out` for the list screen, with the same fields and the same
    arithmetic — read in three set-based queries instead of walking every GRN's
    lines, breakdown rows, products and shortages one lazy load at a time. That
    walk is a handful of queries per LINE, and with a few years of receipts
    (hundreds of thousands of lines) the list never came back.
    """
    from collections import defaultdict
    PL, S, P = models.PurchaseLine, models.PurchaseLineSplit, models.Product
    ids = q.with_entities(models.Purchase.id).subquery()
    line_ids = db.query(PL.id).filter(PL.purchase_id.in_(select(ids.c.id))).subquery()

    # Per GRN, counted by the database — the same rules the walk below the
    # model applies line by line (see _purchase_out), without sending every line
    # and breakdown row of every receipt back to be counted in Python:
    #   a line with breakdown rows holds one item per row, and each row with no
    #   product yet is a new product; any other line is one item, and new if
    #   flagged. An item is pending when it has a product not yet detailed.
    def has_product(col):
        return and_(col.isnot(None), col != 0)

    SP, LP = aliased(P), aliased(P)
    per_line = (db.query(
                    S.line_id.label("line_id"),
                    func.count().label("n"),
                    func.sum(case((not_(has_product(S.product_id)), 1), else_=0)).label("new_n"),
                    func.sum(case((and_(has_product(S.product_id),
                                        func.coalesce(SP.detailed, False) == false()), 1),
                                  else_=0)).label("pend"))
                .outerjoin(SP, SP.id == S.product_id)
                .filter(S.line_id.in_(select(line_ids.c.id)))
                .group_by(S.line_id).subquery())
    split = per_line.c.n.isnot(None)
    agg = defaultdict(lambda: {"line_count": 0, "new_products": 0, "items": 0, "pending": 0})
    for purchase_id, line_count, new_products, items, pending in (
            db.query(PL.purchase_id, func.count(PL.id),
                     func.sum(case((split, per_line.c.new_n),
                                   (PL.is_new_product == true(), 1), else_=0)),
                     func.sum(func.coalesce(per_line.c.n, 1)),
                     func.sum(case((split, per_line.c.pend),
                                   (and_(has_product(PL.product_id),
                                         func.coalesce(LP.detailed, False) == false()), 1),
                                   else_=0)))
              .outerjoin(per_line, per_line.c.line_id == PL.id)
              .outerjoin(LP, LP.id == PL.product_id)
              .filter(PL.purchase_id.in_(select(ids.c.id)))
              .group_by(PL.purchase_id)):
        agg[purchase_id] = {"line_count": int(line_count or 0),
                            "new_products": int(new_products or 0),
                            "items": int(items or 0), "pending": int(pending or 0)}

    shorts = _claimable_shortages(db, ids)

    out = []
    for p in ps:
        a = agg[p.id]
        d = {
            "id": p.id, "document_id": p.document_id, "supplier_id": p.supplier_id,
            "supplier_name": p.supplier.name if p.supplier else None,
            "grn_no": p.grn_no, "invoice_number": p.invoice_number,
            "warehouse_id": p.warehouse_id,
            "warehouse_name": p.warehouse.name if p.warehouse else None,
            "invoice_date": p.invoice_date, "taxable_total": p.taxable_total,
            "tax_total": p.tax_total, "grand_total": p.grand_total,
            "status": p.status, "line_count": a["line_count"],
            "new_products": a["new_products"],
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        }
        if p.status == "posted":
            d["items"] = a["items"]
            d["items_pending_detail"] = a["pending"]
        s = shorts.get(p.id, [])
        d["short_qty"] = round(sum(float(sh.qty or 0) for sh in s), 3)
        d["short_value"] = round(sum(short_svc.value(sh) for sh in s), 2)
        d["short_lines"] = len(s)
        out.append(d)
    return out


def _claimable_shortages(db: Session, ids):
    """purchase id → its claimable shortages, for the GRNs in the `ids` subquery."""
    from collections import defaultdict
    PL = models.PurchaseLine
    shorts = defaultdict(list)
    for sh, purchase_id in (db.query(models.GrnShortage, PL.purchase_id)
                 .join(PL, PL.id == models.GrnShortage.line_id)
                 .filter(PL.purchase_id.in_(select(ids.c.id)),
                         models.GrnShortage.kind.in_(models.GrnShortage.CLAIMABLE_KINDS))):
        shorts[purchase_id].append(sh)
    return shorts


def js_sum(values):
    """Σ exactly as the desktop's `sum()` adds a column: left to right, anything
    that is not a number counting as 0. Same doubles, same order, same result —
    so a figure totalled here matches the one the screen used to total itself."""
    total = 0
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        total += 0.0 if x != x else x                   # NaN → 0, as `|| 0` does
    return total


def dashboard_figures(db: Session, wid: Optional[int]):
    """What the warehouse dashboard reads off the GRN list, without building it.

    The same scope, the same order (newest first) and the same per-GRN shortage
    arithmetic as `list_purchases`, reduced to the figures the tiles show: the
    whole list was 35K rows and ~17 MB to count drafts and add up shortages."""
    q = scope.purchases(db.query(models.Purchase), wid)
    P = models.Purchase
    drafts = [v for (v,) in q.filter(P.status == "draft")
                              .with_entities(P.grand_total).order_by(P.id.desc())]
    shorts = _claimable_shortages(db, q.with_entities(P.id).subquery())
    newest_first = sorted(shorts, reverse=True)
    recent = (q.options(joinedload(P.supplier)).order_by(P.id.desc()).limit(6).all())
    return {
        "draft_count": len(drafts),
        "draft_value": js_sum(drafts),
        "posted_count": q.filter(P.status == "posted").count(),
        "short_lines": sum(len(shorts[pid]) for pid in newest_first),
        "short_value": js_sum(round(sum(short_svc.value(sh) for sh in shorts[pid]), 2)
                              for pid in newest_first),
        "recent": [{"id": p.id, "grn_no": p.grn_no, "status": p.status,
                    "supplier_name": p.supplier.name if p.supplier else None,
                    "grand_total": p.grand_total} for p in recent],
    }


# registered before "/{pid}", or the literal path is swallowed by the id route
@router.get("/shortage-options")
def shortage_options():
    """The shortage kinds and the suggested reasons, so the desktop and the phone
    offer one vocabulary. Reasons are a convenience — free text is accepted."""
    return {"kinds": [{"key": k, "label": v} for k, v in short_svc.KINDS.items()],
            "reasons": short_svc.REASONS}


@router.post("/from-document/{doc_id}")
def build_from_document(doc_id: int, db: Session = Depends(get_db)):
    """Create (or fetch) a DRAFT GRN from a confirmed document. Matches each
    line to inventory so the UI can show matched vs new products before posting."""
    doc = db.get(models.Document, doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    if doc.status not in ("confirmed", "posted") and not doc.latest_extraction:
        raise HTTPException(400, "confirm the extraction before creating a GRN")
    purchase = inv.build_grn_from_document(db, doc)
    db.commit()
    db.refresh(purchase)
    return _purchase_out(purchase, with_lines=True, db=db)


@router.get("/{pid}/stock")
def purchase_stock(pid: int, db: Session = Depends(get_db)):
    """The stock this receipt made: each product a line or a breakdown row of it
    became, how many this receipt brought in, what is on hand now, and this
    receipt's own piece codes (for printing its labels, and only its labels)."""
    from ..services import stock_view
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "GRN not found")
    got = {}                                     # product id -> (qty on the receipt, variant)
    for l in p.lines:
        holders = [(s.product_id, float(s.qty or 0), s.variant_label) for s in l.splits] \
            if l.splits else [(l.product_id, float(l.received_qty or 0), "")]
        for prod_id, qty, variant in holders:
            if not prod_id:
                continue
            q0, v0 = got.get(prod_id, (0.0, variant))
            got[prod_id] = (round(q0 + qty, 3), v0 or variant)
    units = {}
    for uid, prod_id in (db.query(models.ProductUnit.id, models.ProductUnit.product_id)
                         .filter(models.ProductUnit.purchase_id == pid)
                         .order_by(models.ProductUnit.id).all()):
        units.setdefault(prod_id, []).append(uid)
    rows = []
    if got:
        for prod in (db.query(models.Product).filter(models.Product.id.in_(list(got)))
                     .order_by(models.Product.id).all()):
            qty, variant = got[prod.id]
            rows.append({"product_id": prod.id, "sku": prod.sku, "barcode": prod.barcode,
                         "name": stock_view.display_name(prod), "description": prod.description,
                         "variant": variant, "size": prod.size, "color": prod.color,
                         "category": prod.category, "uom": prod.uom,
                         "received": qty, "stock_qty": prod.stock_qty,
                         "unit_ids": units.get(prod.id, []), "detailed": bool(prod.detailed)})
    return {"purchase_id": p.id, "status": p.status, "rows": rows,
            "pieces": sum(len(r["unit_ids"]) for r in rows)}


@router.get("/{pid}")
def get_purchase(pid: int, db: Session = Depends(get_db)):
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    return _purchase_out(p, with_lines=True, db=db)


@router.post("/{pid}/warehouse")
def set_warehouse(pid: int, body: ReceiveAt, db: Session = Depends(get_db)):
    """Say which warehouse this delivery was unloaded at.

    Only while the GRN is a draft. Once it is posted the stock is standing in a
    building, and changing the label on the receipt would not move it — it would
    just make the ledger disagree with the shelf. Correcting a receipt that
    landed at the wrong warehouse is unpost → set → post, which reverses the
    stock out of the first one and receives it into the second."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    if p.status == "posted":
        raise HTTPException(400, "this GRN is posted — its stock is already in a "
                                 "warehouse. Unpost it to receive it somewhere else.")
    if body.warehouse_id is not None:
        wh = db.get(models.Warehouse, body.warehouse_id)
        if not wh:
            raise HTTPException(404, "warehouse not found")
        if not wh.active:
            raise HTTPException(400, f"“{wh.name}” is switched off — goods can't be "
                                     f"received into a closed warehouse")
    p.warehouse_id = body.warehouse_id
    db.commit()
    db.refresh(p)
    return _purchase_out(p, with_lines=True, db=db)


def _draft_line(line_id: int, db: Session) -> models.PurchaseLine:
    line = db.get(models.PurchaseLine, line_id)
    if not line:
        raise HTTPException(404, "GRN line not found")
    if line.purchase.status == "posted":
        raise HTTPException(400, "this GRN is posted — its lines can no longer change")
    return line


@router.put("/lines/{line_id}/splits")
def set_splits(line_id: int, body: SplitRows, db: Session = Depends(get_db)):
    """Break one billed line into variants (or clear the breakdown with []).

    The supplier bills a bundle with no detail; the warehouse enters what actually
    arrived — 50 S cotton, 50 M cotton, 70 L printed — and posting makes each
    distinct attribute combination its own product."""
    line = _draft_line(line_id, db)
    try:
        inv.set_line_splits(db, line, body.rows)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(line)
    return _line_out(line, db)


@router.patch("/lines/{line_id}")
def edit_line(line_id: int, body: LineEdit, db: Session = Depends(get_db)):
    """Set the category master mapping for a line, so its product is created
    already mapped rather than landing 'unmapped' in Inventory. Send "" to clear
    it and fall back to auto-classification from the description.

    The mapping is also *learned*: this is the one moment someone states what a
    supplier's wording means, so the same wording maps itself on the next invoice
    instead of being asked again. Clearing the category forgets it.

    `unit_type` works the same way and for the same reason. Saying once that a
    pillow cover is a PAIR is what turns the next "1 DOZ" into six pairs and six
    labels without anyone being asked again."""
    line = _draft_line(line_id, db)
    fields = body.model_dump(exclude_unset=True)
    if "category" in fields:
        name = (fields["category"] or "").strip()
        # Checked against THIS warehouse's master. A garment category typed onto
        # a silk GRN is not a valid classification of these goods, and accepting
        # it would put an item in a list its own inventory screen never shows.
        cat_id = inv.purchase_catalogue_id(db, line.purchase)
        if name and not db.query(models.Category).filter(
                models.Category.name == name,
                (models.Category.catalogue_id == cat_id)
                | (models.Category.catalogue_id.is_(None))).first():
            cat = line.purchase.warehouse.catalogue if (
                line.purchase.warehouse and line.purchase.warehouse.catalogue) else None
            raise HTTPException(400, f"“{name}” is not in the "
                                     f"{cat.name + ' ' if cat else ''}category master")
        line.category = name or None
        from ..services import categorize
        categorize.learn_alias(db, line.description, name or None, catalogue_id=cat_id)
    if "unit_type" in fields:
        from ..services import unit_types as ut
        code = (fields["unit_type"] or "").strip().upper()
        if code and not ut.get(db, code):
            raise HTTPException(400, f"“{code}” is not a unit type — add it in Masters")
        line.unit_type = code or None
        # a product already counted in something is not re-counted by an edit
        # here; say so rather than accepting a setting that will be ignored
        if code and line.product is not None and line.product.unit_type \
                and line.product.unit_type != code:
            raise HTTPException(400,
                f"{line.product.sku or 'this product'} is already counted in "
                f"{line.product.unit_type} — changing the unit would restate stock "
                f"that is already on the shelf")
        if code:
            ut.learn(db, line.description, code)
    for fld in ("style", "material", "pattern", "sleeve", "product_type"):
        if fld in fields:
            setattr(line, fld, (fields[fld] or "").strip() or None)
    # MRP − sale discount % = sell price, kept in step here too, so the figure a
    # GRN carries is the same one the review screen would have computed
    for fld in ("mrp", "sale_price", "sale_discount_pct"):
        if fld in fields:
            setattr(line, fld, fields[fld])
    if any(k in fields for k in ("mrp", "sale_discount_pct", "sale_price")):
        mrp, pct, price = line.mrp, line.sale_discount_pct, line.sale_price
        if mrp:
            if "sale_discount_pct" in fields and pct is not None:
                line.sale_price = round(mrp * (1 - pct / 100), 2)
            elif "sale_price" in fields and price is not None:
                line.sale_discount_pct = round((1 - price / mrp) * 100, 2)
            elif "mrp" in fields and pct is not None:
                line.sale_price = round(mrp * (1 - pct / 100), 2)
    db.commit()
    db.refresh(line)
    return _line_out(line, db)


@router.put("/lines/{line_id}/shortages")
def set_shortages(line_id: int, body: ShortageRows, db: Session = Depends(get_db)):
    """Record what the supplier billed on this line and the boxes did not hold
    (or clear it with []).

    This is the step that has to happen before the GRN posts, and it belongs to
    whoever is opening the cartons — nobody else can know it. Once recorded, the
    breakdown balances against what ARRIVED rather than what was billed, so a
    short delivery posts honestly instead of forcing someone to type in pieces
    that were never in the box. The missing quantity becomes a claim the debit
    note is later built from.

    Each row: {kind: short|damaged|excess, qty, variant?, reason?, note?}."""
    line = _draft_line(line_id, db)
    try:
        short_svc.set_line_shortages(db, line, body.rows, by=body.recorded_by)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(line)
    return _line_out(line, db)


@router.get("/{pid}/shortages")
def list_shortages(pid: int, db: Session = Depends(get_db)):
    """Every shortage on this GRN, what it is worth at the GRN rate, and how much
    of it a posted debit note has already claimed."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    return short_svc.purchase_summary(db, p)


@router.post("/shortages/{sid}/waive")
def waive_shortage(sid: int, body: WaiveShortage, db: Session = Depends(get_db)):
    """Accept a shortage instead of claiming it. It stays on the record — this
    only stops it being offered on the next debit note."""
    sh = db.get(models.GrnShortage, sid)
    if not sh:
        raise HTTPException(404, "shortage not found")
    try:
        short_svc.waive(db, sh, reason=body.reason, by=body.by)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return short_svc.shortage_out(db, sh)


@router.post("/shortages/{sid}/unwaive")
def unwaive_shortage(sid: int, db: Session = Depends(get_db)):
    """Put a waived shortage back in play — the supplier never did send it."""
    sh = db.get(models.GrnShortage, sid)
    if not sh:
        raise HTTPException(404, "shortage not found")
    short_svc.unwaive(db, sh)
    db.commit()
    return short_svc.shortage_out(db, sh)


@router.post("/lines/{line_id}/scan")
def scan_line_code(line_id: int, body: ScanCode, db: Session = Depends(get_db)):
    """Attach a scanned QR code (or barcode / SKU) to a line, or to one variant of
    it, so the row posts against that exact product instead of relying on the
    description match."""
    line = _draft_line(line_id, db)
    product = barcode_svc.resolve(db, body.code)
    if not product:
        raise HTTPException(404, "no product matches that code")

    if body.split_id is not None:
        sp = db.get(models.PurchaseLineSplit, body.split_id)
        if not sp or sp.line_id != line.id:
            raise HTTPException(404, "variant row not found on this line")
        sp.code = body.code
        sp.product_id = product.id
    else:
        if line.is_split:
            raise HTTPException(400, "this line has a breakdown — scan into one of its rows")
        line.product_id = product.id
        line.is_new_product = False
    db.commit()
    db.refresh(line)
    return _line_out(line, db)


@router.post("/{pid}/unpost")
def unpost_purchase(pid: int, db: Session = Depends(get_db)):
    """Reverse a posted GRN and return it to draft so it can be corrected.

    Refused while a payment, a debit note, or a dispatch depends on it — see
    `GET /{pid}/unpost-check` for what is in the way before committing."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    result = inv.unpost_grn(db, p)
    if not result.get("ok"):
        db.rollback()
        raise HTTPException(400, result.get("error", "unpost failed"))
    db.commit()
    return result


@router.get("/{pid}/unpost-check")
def unpost_check(pid: int, db: Session = Depends(get_db)):
    """What would stop this GRN being unposted ([] = nothing), so the UI can warn
    before the user commits to it."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    if p.status != "posted":
        return {"posted": False, "blockers": []}
    return {"posted": True, "blockers": inv.unpost_blockers(db, p)}


@router.delete("/{pid}")
def delete_purchase(pid: int, db: Session = Depends(get_db)):
    """Delete a DRAFT GRN — for a duplicate or a GRN built from the wrong invoice.
    Unpost it first if it is posted. The source document is left alone, so a fresh
    GRN can be built from it (or the document itself deleted)."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    if p.status == "posted":
        raise HTTPException(400, "this GRN is posted — unpost it before deleting")
    db.delete(p)                      # cascades its lines and their breakdowns
    db.commit()
    return {"ok": True, "deleted_purchase": pid}


@router.post("/{pid}/post")
def post_purchase(pid: int, db: Session = Depends(get_db),
                  wid: Optional[int] = Depends(scope.current)):
    """Commit the GRN to inventory: create new products, post inward stock
    movements, update quantities and weighted-average cost."""
    p = db.get(models.Purchase, pid)
    if not p:
        raise HTTPException(404, "purchase not found")
    # WHERE the goods land, from the building this call is being made inside.
    #
    # A draft normally carries a warehouse already — it inherits one from the
    # document it was built from. One raised before workspaces existed does not,
    # and `post_grn` resolves a blank to the DEFAULT warehouse, which is a guess:
    # goods unloaded at Karur would be credited to Erode's balance and roll into
    # Erode's weighted average, with nothing on any screen saying so.
    #
    # The receiver is standing in the building. Stamping here says so, before a
    # single movement is written. `scope.stamp` never overwrites, so a receipt
    # that already names a warehouse — including one deliberately set through
    # /warehouse below — is left exactly as it is.
    scope.stamp(p, wid)
    result = inv.post_grn(db, p)
    if not result.get("ok"):
        db.rollback()
        raise HTTPException(400, result.get("error", "post failed"))
    db.commit()
    return result
