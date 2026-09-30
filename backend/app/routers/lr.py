import os
import hashlib
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session, joinedload, selectinload
from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..config import UPLOAD_DIR
from ..services import lr as lr_svc
from ..services import storage
from ..services import lr_link
from ..services import masters as masters_svc
from ..services import dates
from ..services import scope
from .. import runtime

router = APIRouter(prefix="/api/lr", tags=["lr-entry"])

# Every column an LR entry accepts from a client, whichever route it came in by.
# One list so the import path and the manual form can never drift apart — a
# column added to the model is added here and both routes carry it.
WRITABLE = [
    "lr_mode", "lr_no", "lr_date", "recv_date", "lr_entry_date",
    "supplier_name", "agent", "agent_commission", "transport",
    "auto_transfer_location", "purchase_manager",
    "stock_holding_days", "additional_margin",
    "bundle", "boxes", "qty", "amount",
    "inv_no", "inv_date",
    "paid_topay", "freight_applicable", "freight_amount", "freight_total",
    "freight_charges", "item",
    # The order this consignment is arriving against. Writable on both routes —
    # an imported row can be tied to an order after the fact, from the grid —
    # but only REQUIRED on the manual one; see REQUIRE_PO_ON_MANUAL.
    "purchase_order_id",
]
_NUMERIC = {"agent_commission", "stock_holding_days", "additional_margin",
            "bundle", "boxes", "qty", "amount", "freight_amount", "freight_total"}
#: Foreign keys. Kept apart from _NUMERIC because a float row id is not an id —
#: SQLite will happily store 12.0 in an INTEGER column and the join then misses.
_INTEGER = {"purchase_order_id"}
_BOOLEAN = {"freight_applicable"}
# The named charge lines under the freight — {"H.C.": 10, "S.T. Charge": 20}. A
# map rather than a column each, because every transporter prints a different set
# of them (see LREntry.freight_total).
_JSON = {"freight_charges"}
# An LR prints "TO PAY"; a register page writes "ToPay"; the form offers "TOPAY".
# They are one fact, and the reports that ask "what do we owe the transporters"
# filter on it exactly — so a consignment written with a space in it silently
# vanished from Transport Pending Bills. Normalised here, at the single funnel
# every LR write passes through, rather than at each place that reads it.
_PAID_TOPAY = {"TOPAY": "TOPAY", "TO PAY": "TOPAY", "TO-PAY": "TOPAY",
               "PAY": "TOPAY", "TO_PAY": "TOPAY",
               "PAID": "PAID", "PREPAID": "PAID", "PAID BY SUPPLIER": "PAID",
               "NO": "NO", "NONE": "NO", "NIL": "NO", "-": "NO"}


def normalise_paid_topay(value):
    """"To Pay" / "TOPAY" / "to-pay" → TOPAY. Anything unrecognised is kept
    verbatim — a word nobody anticipated is still what the page said."""
    key = " ".join(str(value or "").upper().split())
    return _PAID_TOPAY.get(key, value)
# Stored as ISO text so the range filter below (`recv_date >= date_from`, a plain
# SQL string comparison) is arithmetic rather than alphabetical. With "31/07/2026"
# in the column, "01/08/2026" sorts before it and a July-to-August search quietly
# returns the wrong rows — see services/dates.py.
_DATES = {"recv_date", "lr_date", "lr_entry_date", "inv_date"}

# Marked * on the Transport Entry form. Enforced on MANUAL entry only: a
# register page carries none of Company/Agent, so holding an import to the same
# bar would reject perfectly good rows read off a real page.
REQUIRED_MANUAL = [
    ("lr_mode", "LR Mode"), ("lr_no", "LR No"),
    ("lr_date", "LR Date"), ("supplier_name", "Supplier"),
    ("agent", "Agent/Commission"), ("lr_entry_date", "LR Entry Date"),
    ("bundle", "No Of Bundles"), ("boxes", "No Of Boxes"), ("qty", "No Of Pieces"),
]

def require_po_on_manual():
    """Whether a hand-keyed consignment must name a confirmed purchase order.

    ON THE MANUAL ROUTE ONLY, and that asymmetry is the whole design. The
    business rule is "goods arrive against an order", and the form is where
    somebody is sitting with the paperwork and can say which one. The IMPORT
    route is never held to it, whatever this returns: a transporter's register
    page names no purchase order anywhere on it, so holding those rows to this
    would reject twenty good consignments because the page they were
    photographed from could not carry a field the office invented. Imported rows
    are FLAGGED instead (`po_missing`) and tied to an order from the grid.

    Read per call rather than captured at import, so turning the policy off from
    the settings screen takes effect without a restart.
    """
    return bool(runtime.get("require_po_for_lr"))


# lists that learn from what is typed on the form (see masters.OPEN_OPTIONS)
_LEARNS = {"purchase_manager": "purchase_manager"}


def check_purchase_order(db, po_id, required=False):
    """The order this consignment cites, or None. Raises when it is not usable.

    Three separate failures, told apart because the fix for each is different:
    nothing cited, cited something that isn't there, and cited an order that is
    not in a state goods may arrive against. A single "invalid purchase order"
    would leave the transport desk guessing which.
    """
    if not po_id:
        if required:
            raise HTTPException(
                400, "Required: a confirmed Purchase Order. Goods are booked in "
                     "against an order — raise or confirm one first.")
        return None
    po = db.get(models.PurchaseOrder, int(po_id))
    if not po:
        raise HTTPException(400, f"purchase order {po_id} does not exist")
    if po.status != "confirmed":
        raise HTTPException(
            400, f"{po.po_no} is {po.status} — only a confirmed order can have "
                 f"goods booked in against it")
    return po


def _coerce(field, value):
    """Blank → None; numeric columns → float; checkbox columns → bool; dates → ISO.

    Everything arrives as a string from a form, and a blank numeric must become
    NULL rather than 0 — "no weight recorded" and "weighed nil" are different.

    The single funnel for every LR write — save, create and patch all pass through
    here — which is why date normalisation belongs here and not in three places."""
    if value is None:
        return None
    if field in _BOOLEAN:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if field in _JSON:
        # only ever a {name: amount} map; anything else is a client sending the
        # wrong shape and is dropped rather than stored to break a later read
        return {str(k): v for k, v in value.items() if v not in (None, "")} \
            if isinstance(value, dict) and value else None
    if isinstance(value, str):
        value = value.strip()
    if value == "":
        return None
    if field in _NUMERIC:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field in _INTEGER:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field in _DATES:
        # readable → ISO; unreadable → kept verbatim, because a date this cannot
        # parse is still what the register page said
        return dates.normalise(value)
    if field == "paid_topay":
        return normalise_paid_topay(value)
    return value


def _apply(entry, data, db, fields=WRITABLE):
    """Write the given fields onto an entry and let the open masters learn."""
    for f in fields:
        if f in data:
            setattr(entry, f, _coerce(f, data[f]))
    # An amount arriving without its checkbox means the charge applies — a
    # register page carries the figure and no tick, and so does a PATCH that
    # settles the freight from the grid. Without this the form would show the
    # amount greyed out behind an unticked box that contradicts it.
    if ("freight_amount" in data or "freight_total" in data) \
            and "freight_applicable" not in data:
        entry.freight_applicable = (entry.freight_amount is not None
                                    or entry.freight_total is not None)
    for field, kind in _LEARNS.items():
        if data.get(field):
            masters_svc.get_or_create_option(db, kind, str(data[field]).strip())
    if data.get("agent"):
        masters_svc.get_or_create_agent(db, str(data["agent"]).strip())
    if data.get("transport"):
        masters_svc.get_or_create_transport(db, str(data["transport"]).strip())


class SaveLR(BaseModel):
    document_id: Optional[int] = None
    rows: List[Dict[str, Any]]


class LRIn(BaseModel):
    """One consignment keyed in on the LR Entry form. Every field is optional
    here and checked against REQUIRED_MANUAL in the handler, so the response can
    name all the missing boxes at once instead of failing on the first."""
    model_config = {"extra": "allow"}


class LRUpdate(BaseModel):
    """A partial edit. Only the fields actually sent are touched, so correcting
    the freight on a row never disturbs what was read off the register."""
    model_config = {"extra": "allow"}


class ReceiveConsignment(BaseModel):
    """Sent by the warehouse phone app when the packages are taken in."""
    received_by: str


@router.post("/extract")
async def extract(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an LR register image/PDF → OCR/vision returns the consignment rows
    (no manual entry). Also stores the source document."""
    raw = await file.read()
    h = hashlib.sha256(raw).hexdigest()
    ext = os.path.splitext(file.filename)[1] or ".bin"
    stored = storage.save(raw, f"{h[:16]}{ext}")
    doc = models.Document(filename=file.filename, stored_path=stored, content_hash=h,
                          mime=file.content_type, status="uploaded", document_type="lr_register")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    res = lr_svc.extract_lr(storage.materialise(stored) or stored)
    # flag rows that already exist in the DB (or repeat within this upload)
    rows, dup_summary = lr_link.annotate_duplicates(db, res.get("rows", []))
    res["rows"] = rows
    return {"document_id": doc.id, "duplicates": dup_summary, **res}


@router.post("/save")
def save(body: SaveLR, db: Session = Depends(get_db),
         wid: Optional[int] = Depends(scope.current)):
    """Persist the (reviewed) LR rows and auto-create transport + supplier
    masters. Duplicates (already in the DB, or repeated within this batch) are
    skipped authoritatively on the server, even if the client didn't filter
    them, so a re-uploaded register never creates duplicate records."""
    saved = 0
    skipped = 0
    existing = lr_link._index_existing(db)
    seen = {}
    issued = []                       # entry numbers handed out in this batch
    for r in body.rows:
        if not any(r.get(k) for k in ("lr_no", "supplier_name", "inv_no")):
            continue
        key = lr_link.dup_key(r)
        if key is not None:
            candidates = existing.get(key, []) + seen.get(key, [])
            if lr_link.is_exact_duplicate(r, candidates):   # skip only exact twins
                skipped += 1
                continue
            seen.setdefault(key, []).append(r)
        entry = models.LREntry(document_id=body.document_id, entry_source="import",
                               warehouse_id=wid)
        _apply(entry, r, db)
        # A translated row keeps the page's own words. Handled here rather than
        # through WRITABLE because these two are provenance, not data entry: the
        # import records them and the edit form must never be able to rewrite what
        # the register said.
        orig = r.get("original_values")
        if isinstance(orig, dict) and orig:
            entry.original_values = {k: v for k, v in orig.items() if v}
            entry.source_language = (r.get("source_language") or "").strip() or None
        entry.lr_entry_no = lr_svc.next_entry_no(db, issued)
        issued.append(entry.lr_entry_no)
        # the office books the entry the day the page is imported; the register's
        # own dates (lr_date, recv_date) are what the consignment actually did
        entry.lr_entry_date = entry.lr_entry_date or _today()
        db.add(entry)
        # received_by is intentionally not taken from the imported page — the
        # warehouse sets it from the phone app when the goods actually arrive
        # register the supplier name in the supplier master if unseen
        name = (r.get("supplier_name") or "").strip()
        if name and not db.query(models.Supplier).filter(models.Supplier.name == name).first():
            db.add(models.Supplier(name=name))
        saved += 1
    db.commit()
    return {"ok": True, "saved": saved, "skipped_duplicates": skipped}


def _today():
    import datetime as dt
    return dt.date.today().isoformat()


@router.post("")
def create(body: LRIn, db: Session = Depends(get_db),
           wid: Optional[int] = Depends(scope.current)):
    """Key in ONE consignment — the LR Entry form's Save / Save&Next.

    The import path is still the fast way in; this exists for the consignment
    that turns up with no register page to photograph, and for correcting one
    that did. Duplicates are reported, not blocked: a human typing a row they
    can see in front of them outranks a fingerprint match, so the response says
    what it collided with and lets the form warn."""
    data = body.model_dump()
    missing = [label for f, label in REQUIRED_MANUAL if _coerce(f, data.get(f)) is None]
    if missing:
        raise HTTPException(400, "Required: " + ", ".join(missing))
    # Checked before anything is written, so a rejected consignment leaves no
    # half-made row and no supplier invented from its name.
    check_purchase_order(db, _coerce("purchase_order_id",
                                     data.get("purchase_order_id")),
                         required=require_po_on_manual())

    # Booked in against the warehouse expecting the lorry — whichever one the
    # person keying it is working inside.
    entry = models.LREntry(entry_source="manual", warehouse_id=wid)
    _apply(entry, data, db)
    entry.lr_entry_no = lr_svc.next_entry_no(db)
    entry.lr_entry_date = entry.lr_entry_date or _today()
    db.add(entry)

    name = (entry.supplier_name or "").strip()
    if name and not db.query(models.Supplier).filter(models.Supplier.name == name).first():
        db.add(models.Supplier(name=name))

    key = lr_link.dup_key(data)
    clash = lr_link._index_existing(db).get(key, []) if key else []
    db.commit()
    db.refresh(entry)
    return {**_row_out(entry),
            "duplicate_of": [e.id for e in clash if e.id != entry.id] or None}


def _att_out(a: models.LRAttachment):
    return {"id": a.id, "doc_type": a.doc_type, "filename": a.filename,
            "mime": a.mime, "url": f"/api/lr/attachments/{a.id}/file"}


def _row_out(e: models.LREntry):
    out = {f: getattr(e, f) for f in WRITABLE}
    out.update({
        "id": e.id, "lr_entry_no": e.lr_entry_no, "entry_source": e.entry_source,
        "document_id": e.document_id, "received_by": e.received_by,
        "matched": bool(e.matched), "invoice_document_id": e.invoice_document_id,
        # The receipt raised against this consignment, once one exists — filled
        # by services/inventory when the GRN is created for the linked invoice.
        "grn_no": e.grn_no,
        "mismatches": e.mismatches or [],
        # set only when the page was NOT in English — the grid shows a badge and
        # the original text behind it, so a translated reading is never presented
        # as if the register had been written in English
        "source_language": e.source_language,
        "original_values": e.original_values or {},
        "freight_applicable": bool(e.freight_applicable),
        # The order this consignment came in against. `po_no` travels with the id
        # so the register can name it without a second call per row, and
        # `po_missing` is what the grid badges: a row read off a register page
        # carries no order and has to be tied to one by hand, which is a job to
        # be done rather than an error to be shown.
        "po_no": e.purchase_order.po_no if e.purchase_order else None,
        "po_status": e.purchase_order.status if e.purchase_order else None,
        "po_missing": e.purchase_order_id is None,
        "attachments": [_att_out(a) for a in e.attachments],
    })
    return out


# What `_row_out` reads off each row's relations, loaded with the page rather than
# one query per row — the register is polled every 15 s, 500 rows at a time.
_ROW_LOADS = (joinedload(models.LREntry.purchase_order),
              selectinload(models.LREntry.attachments))


def _filtered(db, received="all", q="", supplier="", transport="",
              status="all", date_from="", date_to=""):
    """The register narrowed by the Search panel's filters.

    Dates compare as strings because every date in this system is stored as
    ISO YYYY-MM-DD, where lexical order IS chronological order."""
    query = db.query(models.LREntry)
    if received == "pending":
        query = query.filter((models.LREntry.received_by == None)   # noqa: E711
                             | (models.LREntry.received_by == ""))
    elif received == "received":
        query = query.filter(models.LREntry.received_by != None,    # noqa: E711
                             models.LREntry.received_by != "")
    if status == "linked":
        query = query.filter(models.LREntry.matched == True)        # noqa: E712
    elif status == "unlinked":
        query = query.filter((models.LREntry.matched == False)      # noqa: E712
                             | (models.LREntry.matched == None))    # noqa: E711
    for col, val in ((models.LREntry.supplier_name, supplier),
                     (models.LREntry.transport, transport)):
        if val:
            query = query.filter(col.ilike(f"%{val}%"))
    # both sides normalised to ISO, so this stays a string comparison that happens
    # to be chronological — the only form in which it is correct
    if dates.to_iso(date_from):
        query = query.filter(models.LREntry.recv_date >= dates.to_iso(date_from))
    if dates.to_iso(date_to):
        query = query.filter(models.LREntry.recv_date <= dates.to_iso(date_to))
    if q:
        like = f"%{q}%"
        query = query.filter(
            models.LREntry.lr_no.ilike(like)
            | models.LREntry.inv_no.ilike(like)
            | models.LREntry.lr_entry_no.ilike(like)
            | models.LREntry.supplier_name.ilike(like)
            | models.LREntry.item.ilike(like))
    return query


@returns_json(router.get(""))
def list_lr(received: str = "all", limit: int = 500, db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """The register, newest first. `received=pending|received` filters by whether
    the warehouse has taken the consignment in — that's what the phone app lists,
    and it keeps the payload small as the register grows.

    Narrowed to consignments coming to the warehouse this call is made inside;
    rows booked before workspaces existed have no warehouse and stay on every
    register. See services/scope."""
    rows = scope.lr_entries(_filtered(db, received=received), wid).options(*_ROW_LOADS).order_by(
        models.LREntry.id.desc()).limit(max(1, min(limit, 2000))).all()
    return [_row_out(e) for e in rows]


@returns_json(router.get("/search"))
def search(q: str = "", supplier: str = "", transport: str = "", received: str = "all",
           status: str = "all", date_from: str = "", date_to: str = "",
           limit: int = 500, db: Session = Depends(get_db)):
    """The form's Search. Returns matching rows plus the totals for them, because
    the question behind a filtered register is nearly always "how many pieces and
    how much freight came from this supplier / this month"."""
    query = _filtered(db, received=received, q=q, supplier=supplier,
                      transport=transport, status=status,
                      date_from=date_from, date_to=date_to)
    total = query.count()
    rows = query.options(*_ROW_LOADS).order_by(
        models.LREntry.id.desc()).limit(max(1, min(limit, 2000))).all()

    def _sum(attr):
        return round(sum(float(getattr(e, attr) or 0) for e in rows), 2)

    return {"count": total, "shown": len(rows),
            "totals": {"qty": _sum("qty"), "bundle": _sum("bundle"),
                       "boxes": _sum("boxes"), "amount": _sum("amount"),
                       "freight_amount": _sum("freight_amount"),
                       # what the transporters are owed — freight plus the charge
                       # lines under it, which is the figure a payment run needs
                       "freight_total": _sum("freight_total")},
            "rows": [_row_out(e) for e in rows]}


_TOTALS = ("qty", "bundle", "boxes", "amount", "freight_amount", "freight_total")


@returns_json(router.get("/page"))
def page_lr(page: int = 1, size: int = 100, search: bool = False,
            q: str = "", supplier: str = "", transport: str = "", received: str = "all",
            status: str = "all", date_from: str = "", date_to: str = "",
            db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """One page of the register, newest first, so the desk reads it 100 at a
    time instead of the newest 500 and nothing older.

    `search=false` is the register exactly as `list_lr` builds it (this
    warehouse's consignments plus the unassigned ones, the `received` filter);
    `search=true` is the Search panel exactly as `search` builds it (every
    filter; not narrowed to the warehouse, as that route is not). The rows are
    `_row_out`, the same fields. `total` counts every match, and in search mode
    `totals` adds up every match too — `search` added up only the rows it
    returned, which stopped being the whole answer past its 500-row limit."""
    if search:
        query = _filtered(db, received=received, q=q, supplier=supplier,
                          transport=transport, status=status,
                          date_from=date_from, date_to=date_to)
    else:
        query = scope.lr_entries(_filtered(db, received=received), wid)
    E = models.LREntry
    size = max(1, min(int(size), 2000))
    total = query.count()
    pages = max(1, -(-total // size))
    page = min(max(1, int(page)), pages)
    rows = (query.options(*_ROW_LOADS).order_by(E.id.desc())
                 .offset((page - 1) * size).limit(size).all())
    out = {"rows": [_row_out(e) for e in rows], "total": total, "page": page,
           "size": size, "pages": pages}
    if search:
        # the same addition `search` does — float(v or 0), newest first, rounded
        # once at the end — over every matching row rather than the returned ones
        sums = [0] * len(_TOTALS)          # 0, not 0.0, with no rows — as sum() gives
        for vals in query.with_entities(*[getattr(E, a) for a in _TOTALS]).order_by(E.id.desc()):
            for i, v in enumerate(vals):
                sums[i] += float(v or 0)
        out["totals"] = {a: round(s, 2) for a, s in zip(_TOTALS, sums)}
    return out


@router.patch("/{entry_id}")
def update_entry(entry_id: int, body: LRUpdate, db: Session = Depends(get_db)):
    """Edit an entry — the freight settlement when the lorry delivers, the rack
    once the bundles are put away, or any other field the form holds. Only the
    fields actually sent are touched, so the extracted register values stay put."""
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    data = body.model_dump()
    unknown = [k for k in data if k not in WRITABLE]
    if unknown:
        raise HTTPException(400, f"not editable: {', '.join(sorted(unknown))}")
    # Tying an imported row to its order goes through the same check the form
    # does, so the one route that exists to FIX a missing link cannot be the one
    # that creates a bad one. Not `required` here: clearing the link is allowed,
    # and a row that never had one is exactly what this route is for.
    if "purchase_order_id" in data:
        check_purchase_order(db, _coerce("purchase_order_id",
                                         data["purchase_order_id"]))
    _apply(e, data, db, fields=[k for k in WRITABLE if k in data])
    db.commit()
    db.refresh(e)
    return _row_out(e)


@router.delete("/{entry_id}")
def delete_entry(entry_id: int, db: Session = Depends(get_db)):
    """Remove an entry keyed in by mistake. Refused once an invoice is linked to
    it — unlink there first, so the two records never disagree about whether the
    consignment happened."""
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    if e.matched:
        raise HTTPException(400, "This entry is linked to an invoice — unlink it first.")
    for a in e.attachments:
        storage.delete(a.stored_path)   # already gone is fine; the row still goes
    db.delete(e)
    db.commit()
    return {"ok": True}


@router.get("/receivers")
def receivers(db: Session = Depends(get_db)):
    """Names that have received consignments before, so the phone app can offer
    them instead of making everyone type a colleague's name from scratch."""
    rows = db.query(models.LREntry.received_by).filter(
        models.LREntry.received_by != None,                        # noqa: E711
        models.LREntry.received_by != "").distinct().all()
    return sorted({(r[0] or "").strip() for r in rows} - {""}, key=str.lower)


@router.post("/{entry_id}/receive")
def receive_consignment(entry_id: int, body: ReceiveConsignment,
                        db: Session = Depends(get_db)):
    """Record who took delivery of a consignment — sent by the warehouse phone app.

    This is the only way `received_by` is set: the register page doesn't carry it
    and the desktop doesn't type it, because the people who handle the packages are
    the ones who know who took them."""
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    name = (body.received_by or "").strip()
    if not name:
        raise HTTPException(400, "who received it? a name is required")
    e.received_by = name
    db.commit()
    db.refresh(e)
    return _row_out(e)


@router.delete("/{entry_id}/receive")
def unreceive_consignment(entry_id: int, db: Session = Depends(get_db)):
    """Undo a receipt taken by mistake — the row goes back to pending. Kept
    reversible on purpose: a mis-tap on the floor shouldn't need a desk to fix."""
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    e.received_by = None
    db.commit()
    db.refresh(e)
    return _row_out(e)


# ---- file attachments --------------------------------------------------------
#
# The LR copy, the weighment slip, a photo of a torn bundle. Stored beside the
# uploads but NOT registered as a `Document`: a Document is fed to the extraction
# engine and trains a supplier profile, whereas these are evidence filed against
# the consignment and must never end up in that pipeline.

@router.post("/{entry_id}/attachments")
async def add_attachment(entry_id: int, file: UploadFile = File(...),
                         doc_type: str = Form(""), db: Session = Depends(get_db)):
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    h = hashlib.sha256(raw).hexdigest()
    ext = os.path.splitext(file.filename or "")[1] or ".bin"
    stored = storage.save(raw, f"lr{entry_id}_{h[:16]}{ext}")
    a = models.LRAttachment(lr_id=e.id, doc_type=(doc_type or "").strip() or "Other",
                            filename=file.filename, stored_path=stored,
                            mime=file.content_type)
    db.add(a)
    db.commit()
    db.refresh(a)
    return _att_out(a)


@router.get("/attachments/{att_id}/file")
def attachment_file(att_id: int, db: Session = Depends(get_db)):
    a = db.get(models.LRAttachment, att_id)
    raw = storage.read(a.stored_path) if a else None
    if raw is None:
        raise HTTPException(404, "attachment not found")
    # Served from here rather than redirected to, for the same reason as the
    # invoice images in documents.py: where these are stored on a deployment the
    # URLs are public and permanent, and an LR copy belongs behind the login.
    return Response(raw, media_type=a.mime or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{a.filename or "attachment"}"'})


@router.delete("/attachments/{att_id}")
def delete_attachment(att_id: int, db: Session = Depends(get_db)):
    a = db.get(models.LRAttachment, att_id)
    if not a:
        raise HTTPException(404, "attachment not found")
    # The same bytes can back several attachments (upload the same LR copy to two
    # entries and the content hash collides), so only drop the file when this was
    # the last row pointing at it.
    others = db.query(models.LRAttachment).filter(
        models.LRAttachment.stored_path == a.stored_path,
        models.LRAttachment.id != a.id).count()
    if not others:
        storage.delete(a.stored_path)
    db.delete(a)
    db.commit()
    return {"ok": True}


# Declared last: a bare "/{entry_id}" would otherwise swallow "/search" and
# "/receivers", which FastAPI matches in declaration order.
@router.get("/{entry_id}")
def get_entry(entry_id: int, db: Session = Depends(get_db)):
    e = db.get(models.LREntry, entry_id)
    if not e:
        raise HTTPException(404, "LR entry not found")
    return _row_out(e)
