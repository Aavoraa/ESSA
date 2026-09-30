"""Physical Stock Audit — counting a warehouse by quantity, and correcting it.

    pick what to count → open → count / scan / upload → complete
      → review → approve → apply

THE SHAPE OF THE DOCUMENT, and why it is not the other audit. `stock_audit.py`
beside this one asks *is this item on the shelf*: one row per tag read, a
presence check for somebody walking a rack with a phone. This asks *how many are
there*, which needs a different document — opened over a defined SET of stock,
with a line per item whether or not anybody has been to look at it yet, and a
variance that is the whole output. Neither question can be asked of the other's
record, so there are two.

WHAT THE FILTERS ARE FOR, AND WHY THEY ARE SAVED. Nobody counts a warehouse.
They count the MENS rack, or everything of one brand, or one supplier's goods —
and the set they walked is part of what the count means. A count of "BRAND=YUVA"
that later read as a count of the building would report every other rack as
missing stock. So `scope` is snapshotted onto the header at open, and the
variance is only ever measured against the lines inside it.

**A COUNT IS NOT AN ADJUSTMENT.** Counting writes nothing to the ledger.
`apply()` is a separate, approved act that writes real StockMovement rows through
`stock_locations.apply` — the one door stock moves through in this app — each
carrying the audit's number and its change reason. This is the same rule
`AuditSession` keeps; the difference is that this module gives it a way through
rather than leaving the correction to be retyped somewhere else.

THREE KINDS OF LINE ARE LEFT ALONE BY `apply`, and each for its own reason:

  * **Uncounted** — nobody looked. Not a finding, and adjusting on it would turn
    an unfinished count into a warehouse-wide write-off.
  * **Issued mid-count** ("Remove Sales") — stock legitimately left the building
    between the figure being frozen and the shelf being walked, so the gap is the
    dispatch, not a loss. Held out and flagged rather than silently absorbed.
  * **No product** — an uploaded or scanned code that matches nothing. That is a
    master-data problem and there is no balance to correct.

Each is reported back by `apply`, so the screen can say what it did *not* do.
"""
import datetime as dt

from sqlalchemy.orm import Session

from .. import models
from . import barcode_svc, numbering, scope, stock_locations

#: What a count may become next. A table rather than a chain of ifs, so the
#: screen and the server cannot hold different opinions about the lifecycle.
NEXT_STATUS = {
    "counting":  {"completed", "cancelled"},
    "completed": {"reviewed", "counting", "cancelled"},
    "reviewed":  {"approved", "completed", "cancelled"},
    "approved":  set(),      # finished: apply the variance, or leave it standing
    "cancelled": set(),
}

STATUS_STAMPS = {
    "completed": ("completed_at", "completed_by"),
    "reviewed":  ("reviewed_at", "reviewed_by"),
    "approved":  ("approved_at", "approved_by"),
}

#: The left panel, as filter key -> the Product column it narrows on. Declared
#: once because three things read it: the query that opens a count, the option
#: lists the screen draws, and the scope snapshot written onto the header. Three
#: hand-kept copies of this list is three chances for a filter that silently
#: stops filtering.
PRODUCT_FILTERS = {
    # their ERP's "Product" is the product GROUP, which is this system's category
    # master — see services/master_defs. Not Product.description.
    "product":  "category",
    "section":  "category_section",
    "brand":    "brand",
    "colour":   "color",
    "material": "material",
    "pattern":  "pattern",
    "style":    "style",
    "sleeve":   "sleeve",
    "fit":      "fit",
    "size":     "size",
    "type":     "product_type",
    "design":   "design_no",
}

#: Everything else the panel offers, handled by its own clause below.
#:
#: "Show All Rows" is deliberately ABSENT. It decides what the grid draws, not
#: what is being counted, and a view preference snapshotted onto the document
#: would be a tickbox that stops responding the moment a count is open — frozen
#: on, with nothing on the screen to say why it can no longer be turned off.
#: `direct_edit` and `remove_sales` are here because they are the opposite: rules
#: the count is taken under, which somebody has to be able to read off it
#: afterwards to know what the variance means.
OTHER_FILTERS = ("company", "location", "supplier", "barcode", "barcode_type",
                 "remove_sales", "direct_edit")


class AuditError(Exception):
    """Something the count will not do, with the reason a person needs."""


# ---------------------------------------------------------------------------
#  choosing what to count
# ---------------------------------------------------------------------------
def clean_scope(raw: dict) -> dict:
    """The filter set, with the blanks dropped.

    An untouched dropdown must not reach the header as `""`, or a count of
    everything would be recorded as a count of items whose brand is the empty
    string — which is a different set, and usually an empty one.
    """
    raw = raw or {}
    out = {}
    for key in list(PRODUCT_FILTERS) + list(OTHER_FILTERS):
        val = raw.get(key)
        if isinstance(val, bool):
            if val:
                out[key] = True
            continue
        text = str(val or "").strip()
        if text:
            out[key] = text
    return out


def _location_map(db: Session, warehouse_id) -> dict:
    """{product_id: put-away location} for one warehouse, in one pass.

    A product's location is the rack its carton went to (`Bundle.location`), and
    reaching it means going through the GRN line that produced the item. Built as
    a map rather than looked up per row because a count runs to thousands of
    lines and the alternative is a query each.

    Best-effort, and many installs put nothing here at all. A blank comes back
    absent rather than as an empty string, so the screen can say nothing instead
    of drawing a label with nothing after it.
    """
    q = (db.query(models.Bundle)
           .join(models.Purchase, models.Bundle.purchase_id == models.Purchase.id)
           .filter(models.Bundle.location.isnot(None),
                   models.Bundle.location != ""))
    if warehouse_id:
        q = q.filter(models.Purchase.warehouse_id == warehouse_id)
    out = {}
    # Oldest first, so the most recent put-away wins where a product has been
    # received into two racks — which is the one somebody is most likely to find
    # it in today.
    for bundle in q.order_by(models.Bundle.id.asc()).all():
        for product in bundle.products:
            if product is not None:
                out[product.id] = bundle.location
    return out


def candidates(db: Session, warehouse_id, filters: dict):
    """The products a count with these filters would cover, in grid order.

    Narrowed to stock this warehouse has anything to do with — including items
    whose balance here has fallen to zero, because "the books say none and the
    shelf has three" is exactly the finding a count exists to produce, and
    dropping those rows would hide it.
    """
    return _narrowed(db, warehouse_id, clean_scope(filters),
                     scope.products(db, db.query(models.Product), warehouse_id,
                                    include_zero=True), load=True)


def preview(db: Session, warehouse_id, filters: dict) -> dict:
    """How much a count with these filters would cover: items, and pieces here.

    The same products `candidates` picks, and the same figure per product that
    `stock_locations.qty_at` gives (its balance at this warehouse — the company
    total when no warehouse), added up the same way — but read as ids and one
    set of balances instead of every product as an object and a balance query
    each. Over a whole warehouse (~400K products) that was over a minute and a
    half, and this is the screen's Search button."""
    filters = clean_scope(filters)
    base = scope.products(db, db.query(models.Product), warehouse_id,
                          include_zero=True, as_subquery=True)
    ids = _narrowed(db, warehouse_id, filters, base, load=False)
    if warehouse_id:
        B = models.StockBalance
        held = {}
        for pid, qty in (db.query(B.product_id, B.qty)
                           .filter(B.warehouse_id == warehouse_id).order_by(B.id)):
            held.setdefault(pid, qty)          # qty_at reads the first such row
        qty = sum(float(held.get(pid) or 0) for pid in ids)
    else:
        stock = dict(db.query(models.Product.id, models.Product.stock_qty))
        qty = sum(float(stock.get(pid) or 0) for pid in ids)
    return {"items": len(ids), "qty": round(float(qty), 3), "filters": filters}


def _narrowed(db: Session, warehouse_id, filters, query, load):
    """`query` narrowed by the panel: products in grid order (`load`), or just
    their ids in that order."""

    for key, column in PRODUCT_FILTERS.items():
        value = filters.get(key)
        if value:
            query = query.filter(getattr(models.Product, column) == value)

    if filters.get("supplier"):
        supplier = filters["supplier"]
        if str(supplier).isdigit():
            query = query.filter(models.Product.primary_supplier_id == int(supplier))
    if filters.get("company") and str(filters["company"]).isdigit():
        # A Catalogue has no company of its own — a WAREHOUSE does, and the
        # catalogue is the trade it deals in. So the company filter reaches stock
        # through the catalogues its buildings carry, which is the only link
        # between the two that exists rather than the one it would be tidy to
        # have. Almost always a no-op: a count is taken inside one warehouse, and
        # that warehouse belongs to one company already.
        cat_ids = {w.catalogue_id for w in db.query(models.Warehouse).filter(
            models.Warehouse.business_id == int(filters["company"])).all()
            if w.catalogue_id}
        query = (query.filter(models.Product.catalogue_id.in_(cat_ids))
                 if cat_ids else query.filter(models.Product.id.is_(None)))
    if filters.get("barcode"):
        # WHICH identifier to match on is what Barcode Type decides, and the two
        # are not interchangeable. A supplier's barcode is theirs — it is
        # routinely blank, and two suppliers can print the same EAN on different
        # goods — while the UAN is ours and never either. Matching both at once
        # is the right default for somebody holding a tag they have not looked
        # at; being able to say which is what makes a collision resolvable.
        code = str(filters["barcode"]).strip()
        kind = filters.get("barcode_type") or "any"
        if kind == "uan":
            query = query.filter(models.Product.sku == code)
        elif kind == "supplier":
            query = query.filter(models.Product.barcode == code)
        else:
            query = query.filter((models.Product.barcode == code)
                                 | (models.Product.sku == code))

    if not load:
        query = query.with_entities(models.Product.id)
    rows = query.order_by(models.Product.description).all()
    if not load:
        rows = [pid for (pid,) in rows]

    if filters.get("location"):
        where = _location_map(db, warehouse_id)
        want = str(filters["location"]).strip().lower()
        rows = [p for p in rows
                if (where.get(p.id if load else p) or "").strip().lower() == want]
    return rows


def filter_options(db: Session, warehouse_id) -> dict:
    """What each dropdown on the left may offer, for THIS warehouse's stock.

    Read off the products actually held here rather than off the masters, for
    the reason the panel exists: a counter narrowing to a brand wants the brands
    on these shelves, and a list carrying every brand the company has ever bought
    makes them scroll past choices that would return nothing.
    """
    # Each dropdown's DISTINCT raw values from the database, then the same
    # strip / drop-blank / sort as before — the set of stripped values is the
    # same whether the duplicates go before or after the strip. The building's
    # every product (~360K) used to be read into Python to find a few hundred
    # values per column.
    held_q = scope.products(db, db.query(models.Product), warehouse_id,
                            include_zero=True, as_subquery=True)
    from .distinct import distinct_values          # one scan for every column
    distinct = distinct_values(held_q, [getattr(models.Product, c) for c in
                                        sorted(set(PRODUCT_FILTERS.values())) + ["primary_supplier_id"]])
    out = {}
    for key, column in PRODUCT_FILTERS.items():
        out[key] = sorted({(v or "").strip() for v in distinct[column]} - {""})
    supplier_ids = {v for v in distinct["primary_supplier_id"] if v}
    out["supplier"] = [
        {"id": s.id, "name": s.name} for s in db.query(models.Supplier)
        .filter(models.Supplier.id.in_(supplier_ids))
        .order_by(models.Supplier.name).all()] if supplier_ids else []
    out["company"] = [{"id": b.id, "name": b.name} for b in
                      db.query(models.Business).order_by(models.Business.name).all()]
    where = _location_map(db, warehouse_id)
    # the held ids only matter when there are put-away locations to match
    held = {pid for (pid,) in held_q.with_entities(models.Product.id)} if where else set()
    out["location"] = sorted({v for k, v in where.items() if k in held and v})
    return out


# ---------------------------------------------------------------------------
#  opening a count
# ---------------------------------------------------------------------------
def next_code(db: Session, warehouse_id=None):
    """The next count number, stepping over anything already issued."""
    used = {c for (c,) in db.query(models.PhysicalAudit.code).filter(
        models.PhysicalAudit.code.isnot(None)).all()}
    return numbering.next_number(db, "physical_audit", warehouse_id=warehouse_id,
                                 is_taken=lambda code: code in used)


def current(db: Session, warehouse_id):
    """The count being taken in this building, or None."""
    q = db.query(models.PhysicalAudit).filter(
        models.PhysicalAudit.status == "counting")
    q = scope.physical_audits(q, warehouse_id)
    return q.order_by(models.PhysicalAudit.id.desc()).first()


def _freeze(db: Session, audit, line):
    """Pin the book figure to what it says AT THE MOMENT THE SHELF IS LOOKED AT.

    THIS IS THE LOAD-BEARING MOMENT OF THE WHOLE MODULE, and it is the count, not
    the opening. A line is created when the sheet is drawn up — which may be days
    before anybody walks that rack — and the figure it opens with is a display
    value. Freeze it there and a delivery that lands in between is charged to the
    counter: the books read 20, the shelf genuinely holds 20, the counter writes
    20, and the sheet reports a variance of +8 against a number that went stale
    while it sat there.

    Frozen here instead, the arithmetic `apply` does is exact. The correction it
    writes is `counted − frozen`, applied to whatever the balance has become
    since; and because the frozen figure was the balance when the shelf was
    walked, everything the ledger recorded after that point cancels out of both
    sides. Goods legitimately dispatched the next morning stay dispatched, and
    what is left is the gap the counter actually found.

    Once a line carries a count the figure is fixed for good — re-typing a total
    does not move it, because a variance that restated itself after the fact is
    the one thing this document exists not to produce. Clearing a line puts it
    back to never-counted (see `clear`), and counting it again re-freezes, which
    is right: that is a fresh look at the shelf.
    """
    if line.counted_qty is not None or not line.product_id:
        return
    line.system_qty = round(float(stock_locations.qty_at(
        db, line.product_id, audit.warehouse_id) or 0), 3)
    line.cost_price = stock_locations.cost_at(db, line.product_id,
                                              audit.warehouse_id)


def _line_for(db: Session, audit, product, *, where=None, source="opened"):
    """This count's line for a product, against today's books if new.

    The figures here are the sheet's OPENING view — what to expect on the rack.
    What the variance is measured against is pinned separately, by `_freeze`, at
    the moment somebody actually counts the row.
    """
    line = next((l for l in audit.lines if l.product_id == product.id), None)
    if line is not None:
        return line
    qty = stock_locations.qty_at(db, product.id, audit.warehouse_id)
    line = models.PhysicalAuditLine(
        audit_id=audit.id, product_id=product.id,
        uan=product.sku, barcode=product.barcode,
        # `description` is what this app calls a product's name — there is no
        # `name` column on Product.
        description=product.description,
        section=product.category_section, category=product.category,
        brand=product.brand, size=product.size, design_no=product.design_no,
        color=product.color,
        system_qty=round(float(qty or 0), 3),
        cost_price=stock_locations.cost_at(db, product.id, audit.warehouse_id),
        net_price=product.sale_price if product.sale_price is not None else product.mrp,
        counted_qty=None, scans=0, source=source)
    if where is not None:
        line.note = where.get(product.id) or None
    audit.lines.append(line)
    db.add(line)
    return line


def open_audit(db: Session, warehouse_id, *, by=None, note=None, filters=None):
    """Start a count over the filtered set. Refuses while one is already open.

    One open count per warehouse, because two people counting the same racks into
    two documents produce two variances for one shelf and no way to say which is
    the answer. Unlike the scan-through audit next door, this REFUSES rather than
    handing back the existing one: that one is picked up mid-rack from a phone
    all day, while this is opened deliberately over a chosen set, and silently
    adopting a count someone else scoped differently would file readings against
    the wrong document.
    """
    if not warehouse_id:
        raise AuditError("Say which warehouse is being counted — a count belongs "
                         "to the building whose shelves were walked.")
    running = current(db, warehouse_id)
    if running is not None:
        raise AuditError(
            f"{running.code} is already being counted here, started "
            f"{running.started_at:%d-%m-%Y %H:%M}. Finish or cancel it first.")

    filters = clean_scope(filters)
    products = candidates(db, warehouse_id, filters)
    if not products:
        raise AuditError("Nothing matches those filters, so there is nothing to "
                         "count. Widen them and try again.")

    audit = models.PhysicalAudit(
        code=next_code(db, warehouse_id), warehouse_id=warehouse_id,
        status="counting", started_by=by, scope=filters,
        note=(note or "").strip()[:256] or None)
    db.add(audit)
    db.flush()

    where = _location_map(db, warehouse_id)
    for product in products:
        _line_for(db, audit, product, where=where, source="opened")
    db.flush()
    return audit


# ---------------------------------------------------------------------------
#  counting
# ---------------------------------------------------------------------------
def _open_or_refuse(audit):
    if not audit.is_open:
        raise AuditError(f"{audit.code} is {audit.status} — counting is finished.")


def record(db: Session, audit, line, qty, *, by=None, note=None):
    """Write what was actually on the shelf for one line."""
    _open_or_refuse(audit)
    try:
        counted = round(float(qty), 3)
    except (TypeError, ValueError):
        raise AuditError("A physical count must be a number.")
    if counted < 0:
        raise AuditError("A physical count cannot be negative — a shelf holds "
                         "none of something, never less than none.")
    _freeze(db, audit, line)
    line.counted_qty = counted
    line.counted_at = dt.datetime.utcnow()
    line.counted_by = by
    if note is not None:
        line.note = (note or "").strip()[:256] or None
    return line


def clear(db: Session, audit, line):
    """Put a line back to never-counted. NOT the same as counting zero.

    Its own action because "we have not been to this rack" and "we went and it
    was empty" are the two findings a count must never confuse, and a quantity
    box alone cannot say the first.
    """
    _open_or_refuse(audit)
    line.counted_qty = None
    line.counted_at = None
    line.counted_by = None
    return line


def scan(db: Session, audit, code, *, qty=1, by=None):
    """Read one tag into the count.

    Returns (line, message). The message is what the screen says out loud — an
    item found outside the filtered set, or one being counted a second time.

    A tag read repeatedly ADDS, because that is what scanning a rack is: one
    beep, one garment. Typing into the Count box REPLACES, because that is what
    typing a total is. Two verbs that look alike and are not, so they are two
    entry points rather than one with a flag.
    """
    _open_or_refuse(audit)
    code = (code or "").strip()
    if not code:
        raise AuditError("Nothing was scanned.")

    product = barcode_svc.resolve(db, code)
    if product is None:
        from . import units as unit_svc
        unit = unit_svc.resolve(db, code)
        product = unit.product if unit is not None else None
    if product is None:
        raise AuditError(f"Nothing in the catalogue matches “{code}”.")

    message = ""
    line = next((l for l in audit.lines if l.product_id == product.id), None)
    if line is None:
        # Outside what this count was opened over. Whether that is allowed is the
        # "Direct Add/Remove" box: with it on, stock found where the filters said
        # it would not be is a finding worth recording; with it off, a count of
        # the MENS rack stays a count of the MENS rack.
        if not (audit.scope or {}).get("direct_edit"):
            raise AuditError(
                f"{product.description} is not in this count — it is outside the "
                f"filters {audit.code} was opened over. Tick Direct Add/Remove to "
                f"add what you find anyway.")
        line = _line_for(db, audit, product, source="scanned")
        db.flush()
        message = (f"{product.description} was not in the filtered set — added, "
                   f"and flagged as found off-scope.")
    elif line.counted_qty is not None:
        message = (f"Already counted — {product.description} stood at "
                   f"{_trim(line.counted_qty)}.")

    _freeze(db, audit, line)
    line.counted_qty = round(float(line.counted_qty or 0) + float(qty or 1), 3)
    line.scans = (line.scans or 0) + 1
    line.counted_at = dt.datetime.utcnow()
    line.counted_by = by
    return line, message


def add_product(db: Session, audit, product, *, by=None):
    """Put an item on the count by hand — for a tag that will not scan."""
    _open_or_refuse(audit)
    if not (audit.scope or {}).get("direct_edit"):
        raise AuditError("Tick Direct Add/Remove to put items on this count by "
                         "hand — it was opened over a filtered set.")
    existing = next((l for l in audit.lines if l.product_id == product.id), None)
    if existing is not None:
        return existing
    line = _line_for(db, audit, product, source="added")
    db.flush()
    return line


def remove_line(db: Session, audit, line):
    """Take a row back off an open count.

    The other half of Direct Add/Remove. Only a row NOBODY COUNTED may go: a
    counted row is a reading somebody took, and deleting those is how a count
    quietly becomes the count somebody wanted. Clear the figure first if it was
    a mis-scan — that leaves the row, and the fact that it was cleared, visible.
    """
    _open_or_refuse(audit)
    if not (audit.scope or {}).get("direct_edit"):
        raise AuditError("Tick Direct Add/Remove to take rows off this count.")
    if line.counted_qty is not None:
        raise AuditError(f"{line.description} has been counted at "
                         f"{_trim(line.counted_qty)}. Clear the count first — a "
                         f"reading somebody took should not vanish.")
    db.delete(line)
    return True


def upload(db: Session, audit, rows, *, by=None):
    """Take counts off a handheld's file — {code, qty} per row.

    The scanner in the racks is not always the screen at the desk. A gun is
    walked round a warehouse all morning, docked, and its file dropped here; what
    comes back says exactly what landed, what did not, and why, because a silent
    partial import of a morning's counting is the worst possible outcome.

    Quantities REPLACE rather than add. A file is a statement of what was found,
    and running the same file twice must not double the warehouse.
    """
    _open_or_refuse(audit)
    allow_new = bool((audit.scope or {}).get("direct_edit"))
    where = _location_map(db, audit.warehouse_id)
    applied, added, unknown, off_scope = 0, 0, [], []

    for row in rows or []:
        code = str((row or {}).get("code") or "").strip()
        if not code:
            continue
        try:
            qty = round(float((row or {}).get("qty", 1) or 0), 3)
        except (TypeError, ValueError):
            qty = 0.0
        if qty < 0:
            qty = 0.0

        product = barcode_svc.resolve(db, code)
        if product is None:
            from . import units as unit_svc
            unit = unit_svc.resolve(db, code)
            product = unit.product if unit is not None else None
        if product is None:
            unknown.append(code)
            continue

        line = next((l for l in audit.lines if l.product_id == product.id), None)
        if line is None:
            if not allow_new:
                off_scope.append(code)
                continue
            line = _line_for(db, audit, product, where=where, source="uploaded")
            db.flush()
            added += 1
        _freeze(db, audit, line)
        line.counted_qty = qty
        line.scans = (line.scans or 0) + 1
        line.counted_at = dt.datetime.utcnow()
        line.counted_by = by
        if line.source == "opened":
            line.source = "uploaded"
        applied += 1

    return {"applied": applied, "added": added,
            "unknown": unknown[:50], "unknown_count": len(unknown),
            "off_scope": off_scope[:50], "off_scope_count": len(off_scope)}


def synchronize(db: Session, audit):
    """Bring the sheet up to date: today's figures, and stock that has arrived.

    WHY ONLY THE UNCOUNTED. A line somebody has already counted keeps the figure
    it was measured against — restating that would rewrite a variance after the
    fact, which is the one thing this document exists not to do. An uncounted
    line carries no finding yet, so refreshing it costs nothing.

    THE VARIANCE DOES NOT DEPEND ON ANYBODY PRESSING THIS. `_freeze` pins each
    line at the moment it is counted, so a count taken on a stale sheet is still
    measured against the right number. What this does is make the sheet READ
    correctly before it is walked — an expected quantity two days out of date
    sends a counter looking for goods that were dispatched on Tuesday.

    Items received into the warehouse since the count opened, and inside its
    filters, are added: they are on the shelf now, and a count that ignored them
    would report the rack short by exactly the delivery.
    """
    _open_or_refuse(audit)
    refreshed = 0
    for line in audit.lines:
        if line.counted_qty is not None or not line.product_id:
            continue
        qty = round(float(stock_locations.qty_at(
            db, line.product_id, audit.warehouse_id) or 0), 3)
        if qty != round(float(line.system_qty or 0), 3):
            line.system_qty = qty
            refreshed += 1
        line.cost_price = stock_locations.cost_at(db, line.product_id,
                                                  audit.warehouse_id)

    have = {l.product_id for l in audit.lines}
    where = _location_map(db, audit.warehouse_id)
    added = 0
    for product in candidates(db, audit.warehouse_id, audit.scope or {}):
        if product.id in have:
            continue
        _line_for(db, audit, product, where=where, source="opened")
        added += 1
    db.flush()
    return {"refreshed": refreshed, "added": added}


# ---------------------------------------------------------------------------
#  the way through: complete → review → approve
# ---------------------------------------------------------------------------
def set_status(db: Session, audit, status, *, by=None):
    """Move a count along, or refuse with the reason."""
    if status not in NEXT_STATUS:
        raise AuditError(f"“{status}” is not a status a count can have.")
    allowed = NEXT_STATUS.get(audit.status, set())
    if status not in allowed:
        if not allowed:
            raise AuditError(f"{audit.code} is {audit.status} and cannot be changed.")
        raise AuditError(f"{audit.code} is {audit.status} — it can only become "
                         f"{' or '.join(sorted(allowed))}.")
    if status == "completed" and not [l for l in audit.lines
                                      if l.counted_qty is not None]:
        raise AuditError("Nothing has been counted yet, so there is nothing to "
                         "complete. Cancel it instead if the count is not going "
                         "ahead.")
    audit.status = status
    stamp = STATUS_STAMPS.get(status)
    if stamp:
        setattr(audit, stamp[0], dt.datetime.utcnow())
        setattr(audit, stamp[1], by)
    if status == "counting":
        # Re-opened to finish counting. The completion stamp goes with it —
        # leaving it would say the count was finished at a time it was not.
        audit.completed_at = audit.completed_by = None
    return audit


def _issued_since(db: Session, line, audit) -> float:
    """Pieces that LEFT this warehouse after the line's figure was frozen.

    What the "Remove Sales" box is about. A dispatch raised while the count was
    running makes the shelf legitimately disagree with the number on the line,
    and charging that to the counter would write off goods that are on a lorry.
    """
    if not line.product_id:
        return 0.0
    since = line.counted_at or audit.started_at
    if since is None:
        return 0.0
    q = (db.query(models.StockMovement)
           .filter(models.StockMovement.product_id == line.product_id,
                   models.StockMovement.qty_delta < 0,
                   models.StockMovement.created_at >= since))
    if audit.warehouse_id:
        q = q.filter(models.StockMovement.warehouse_id == audit.warehouse_id)
    return round(-sum(float(m.qty_delta or 0) for m in q.all()), 3)


def apply(db: Session, audit, *, by=None, reason=None):
    """Write the approved variance into the ledger. Once, and only once.

    One movement per line that disagrees, through `stock_locations.apply` — the
    single door stock moves through in this app — carrying the audit's number and
    its change reason. Never a direct write of a balance, so the correction is a
    row somebody can find and question afterwards, exactly like every other stock
    change here.

    See the module note for the three kinds of line this deliberately skips.
    Returns what it did AND what it did not, because a correction that quietly
    passed over half the sheet is not a correction anybody can rely on.
    """
    if audit.status != "approved":
        raise AuditError(f"{audit.code} is {audit.status}. Only an approved count "
                         f"may correct stock.")
    if audit.applied_at is not None:
        raise AuditError(f"{audit.code} was applied on "
                         f"{audit.applied_at:%d-%m-%Y %H:%M} — stock has been "
                         f"corrected once and must not be moved again.")
    reason = (reason or audit.change_reason or "").strip()
    if not reason:
        raise AuditError("Give a change reason before correcting stock — an "
                         "adjustment nobody can explain later is one nobody can "
                         "defend.")
    audit.change_reason = reason[:256]

    drop_issued = bool((audit.scope or {}).get("remove_sales"))
    moved = skipped_issued = skipped_noproduct = 0
    for line in audit.lines:
        gap = line.difference
        if gap is None or gap == 0:
            continue
        product = line.product
        if product is None:
            skipped_noproduct += 1
            continue
        if drop_issued and gap < 0 and _issued_since(db, line, audit) >= abs(gap):
            # Short by no more than what left the building since — the lorry
            # explains it, so the ledger is already right.
            skipped_issued += 1
            continue
        stock_locations.apply(
            db, product, audit.warehouse_id, gap,
            kind="adjustment", ref_type="physical_audit", ref_id=audit.id,
            rate=line.cost_price or product.avg_cost or 0,
            note=f"{audit.code} · {audit.change_reason}")
        moved += 1

    audit.applied_at = dt.datetime.utcnow()
    audit.applied_by = by
    db.flush()
    return {"moved": moved, "skipped_issued": skipped_issued,
            "skipped_no_product": skipped_noproduct,
            "uncounted": sum(1 for l in audit.lines if l.counted_qty is None)}


# ---------------------------------------------------------------------------
#  reading it back
# ---------------------------------------------------------------------------
def totals(db: Session, audit) -> dict:
    """The strip across the top of the grid.

    The four figures are the reference screen's, given the only meanings they can
    honestly carry here:

      available  what the books say this warehouse holds, over the counted set
      uploaded   what has actually been counted so far
      valid      counted lines that AGREE with the books + counted lines that do
                 not (the screen's "n + m") — between them, every line looked at
      missing    the net shortfall: pieces the books expect that nobody found
    """
    lines = audit.lines
    counted = [l for l in lines if l.counted_qty is not None]
    matched = sum(1 for l in counted if l.difference == 0)
    short = sum(-l.difference for l in counted if l.difference < 0)
    excess = sum(l.difference for l in counted if l.difference > 0)
    return {
        "lines": len(lines),
        "counted_lines": len(counted),
        "pending_lines": len(lines) - len(counted),
        "available": round(sum(float(l.system_qty or 0) for l in lines), 3),
        "uploaded": round(sum(float(l.counted_qty or 0) for l in counted), 3),
        "valid": matched,
        "variance_lines": len(counted) - matched,
        "missing": round(short, 3),
        "excess": round(excess, 3),
        "off_scope": sum(1 for l in lines if l.source in ("scanned", "added")),
    }


def line_out(line) -> dict:
    return {
        "id": line.id, "product_id": line.product_id,
        "barcode": line.barcode, "uan": line.uan,
        "product": line.description, "section": line.section,
        "category": line.category, "brand": line.brand, "size": line.size,
        "design": line.design_no, "colour": line.color,
        "count": line.scans or 0,
        "qty": line.counted_qty,
        "stock": round(float(line.system_qty or 0), 3),
        "difference": line.difference,
        "status": line.line_status,
        "cost_price": line.cost_price,
        "net_price": line.net_price,
        "source": line.source or "opened",
        "location": line.note,
        "counted_at": line.counted_at.isoformat() if line.counted_at else None,
        "counted_by": line.counted_by,
    }


def audit_out(db: Session, audit, *, with_lines=True) -> dict:
    out = {
        "id": audit.id, "code": audit.code, "status": audit.status,
        "warehouse_id": audit.warehouse_id,
        "warehouse": audit.warehouse.name if audit.warehouse else None,
        "note": audit.note, "change_reason": audit.change_reason,
        "scope": audit.scope or {},
        "started_at": audit.started_at.isoformat() if audit.started_at else None,
        "started_by": audit.started_by,
        "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
        "reviewed_at": audit.reviewed_at.isoformat() if audit.reviewed_at else None,
        "approved_at": audit.approved_at.isoformat() if audit.approved_at else None,
        "approved_by": audit.approved_by,
        "applied_at": audit.applied_at.isoformat() if audit.applied_at else None,
        "applied_by": audit.applied_by,
        "can_apply": audit.status == "approved" and audit.applied_at is None,
        "totals": totals(db, audit),
    }
    if with_lines:
        out["lines"] = [line_out(l) for l in audit.lines]
    return out


def _trim(n):
    n = float(n or 0)
    return str(int(n)) if abs(n - int(n)) < 1e-6 else f"{n:g}"
