"""
GRN + inventory service — turns a confirmed extraction into stock.

Two phases, deliberately separated so a human can review before stock moves:

  build_grn_from_document(doc)  -> a DRAFT Purchase whose lines are matched to
                                   existing Products (by barcode, else by
                                   description+HSN+supplier). Lines with no match
                                   are flagged is_new_product.

  post_grn(purchase)            -> creates the new Products, appends one inward
                                   StockMovement per line, and updates each
                                   product's stock_qty + weighted-average cost.
                                   Idempotent: a purchase can only post once.

Weighted-average cost keeps valuation stable across purchases at different rates:
    new_avg = (old_qty*old_avg + in_qty*in_rate) / (old_qty + in_qty)
"""
import datetime as dt
from rapidfuzz import fuzz
from .. import models
from . import shortages as shortage_svc
from . import stock_locations as stock_loc
from . import catalogues as cat_svc


def purchase_catalogue_id(db, purchase):
    """The business line a receipt belongs to — its warehouse's.

    A GRN does not choose a catalogue of its own. The building it is unloaded at
    decides, because that is what a catalogue IS: the goods that warehouse
    trades in. Two answers for one receipt would be one answer too many.
    """
    cat = cat_svc.for_warehouse(db, getattr(purchase, "warehouse_id", None))
    return cat.id if cat else None


def _norm(s):
    return (s or "").strip().lower()


# The attributes that make a breakdown row a distinct stock item. Same list the
# phone detail form and the QR payload carry, so a variant created at GRN is
# already the record the warehouse and the label expect.
#
# All ten of the stock master's attribute columns (Attributes Reference.xlsx)
# except PRODUCT, which is the category and is carried separately. Every one of
# them is part of IDENTITY: an ESSA t-shirt and a YUVA t-shirt in the same size
# and colour are two stock items, not one, and folding them together would put
# two suppliers' goods behind a single weighted-average cost.
SPLIT_ATTRS = ("size", "color", "material", "pattern", "fit", "product_type",
               "design_no", "brand", "style", "sleeve")
# per-variant money fields — these describe price, never identity
SPLIT_PRICES = ("rate", "mrp", "sale_price", "sale_discount_pct")


def _clean(v):
    v = v.strip() if isinstance(v, str) else v
    return v or None


#: What a GRN LINE can say about its goods beyond size, brand and design — set
#: on the GRN screen. Part of identity like every SPLIT_ATTRS key.
LINE_ATTRS = ("style", "material", "pattern", "sleeve", "product_type")


def named_attrs(size=None, brand=None, design_no=None, **extra):
    """The identity attributes a LINE names about itself, shaped like a split row.

    An invoice keyed size by size — one line per size of a run, each carrying its
    brand and design — describes stock items exactly as precisely as a breakdown
    does. It has to be matched the same way, and this is what lets it be: without
    it FROCK/16 and FROCK/18 both score 100 against an existing "FROCK" and four
    sizes collapse back into the one stock item the split existed to separate.

    A size cell holding a RUN is not a size. "30:2, 32:4" is four garments hiding
    in one line, and the answer to it is the breakdown, not a product called FROCK
    whose size is the string "30:2, 32:4".

    Returns None when the line names nothing — a plain bundle line then matches on
    its description exactly as it always did.
    """
    from . import size_split
    size = _clean(size)
    if size and size_split.parse(size)["rows"]:
        size = None
    got = {"size": size, "brand": _clean(brand), "design_no": _clean(design_no)}
    got.update({a: _clean(extra.get(a)) for a in LINE_ATTRS})
    return {a: got.get(a) for a in SPLIT_ATTRS} if any(got.values()) else None


def line_named_attrs(line):
    """`named_attrs` for a PurchaseLine."""
    return named_attrs(getattr(line, "size", None), getattr(line, "brand", None),
                       getattr(line, "design_no", None),
                       **{a: getattr(line, a, None) for a in LINE_ATTRS})


def match_product(db, barcode, description, hsn, supplier_id, attrs=None,
                  catalogue_id=None):
    """Find an existing product for a purchase line. Returns Product or None.

    `attrs` is passed when matching a variant row from an attribute breakdown.
    Identity is then the WHOLE attribute tuple, compared exactly (blank included):
    the same description in a different colour or material is a different stock
    item, so an exact re-buy merges and anything new is created. Matching on only
    the attributes that happen to be filled in would quietly fold "L" into
    "L / Red" for whichever arrived first.

    `catalogue_id` confines the search to one business line, and it matters more
    than it looks. Descriptions across trades collide — a silk "PLAIN COTTON" and
    a garment "PLAIN COTTON" score 100 against each other — and a match across
    the two would file a saree against a t-shirt's stock record, merge their
    costs into one weighted average, and put a garment's category and attributes
    on it. A barcode still wins outright: the supplier printed it against one
    specific article, which is a stronger statement than any similarity score.
    """
    if barcode:
        p = db.query(models.Product).filter(models.Product.barcode == barcode).first()
        if p:
            return p
    # no barcode (AMS / Matoshree / Mehak): match on description + HSN, prefer same supplier
    q = db.query(models.Product)
    if hsn:
        q = q.filter(models.Product.hsn == hsn)
    if catalogue_id:
        # A product that predates catalogues carries none, and belongs to the
        # default line — it is still a candidate for that line and for no other.
        from . import catalogues as cat_svc
        default = cat_svc.default_catalogue(db)
        if default and default.id == catalogue_id:
            q = q.filter((models.Product.catalogue_id == catalogue_id)
                         | (models.Product.catalogue_id.is_(None)))
        else:
            q = q.filter(models.Product.catalogue_id == catalogue_id)
    candidates = q.all()
    best, best_score = None, 0
    for c in candidates:
        if attrs is not None and any(_norm(getattr(c, a, None)) != _norm(attrs.get(a))
                                     for a in SPLIT_ATTRS):
            continue
        score = fuzz.token_sort_ratio(_norm(description), _norm(c.description))
        if c.primary_supplier_id == supplier_id:
            score += 5  # tie-break toward the same supplier
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 90 else None


def _next_sku(db, warehouse_id=None):
    """The next product SKU, in whatever format this business has configured.

    Delegates to services/numbering, which keeps the property this function
    always had: it steps over any code already issued rather than trusting a
    counter. See barcode_svc._next_sku — the same rule was written twice, and
    now both call one place.
    """
    from . import numbering
    return numbering.next_number(
        db, "sku", warehouse_id=warehouse_id,
        is_taken=lambda code: db.query(models.Product).filter(
            models.Product.sku == code).first() is not None)


def next_grn_no(db, when=None, warehouse_id=None):
    """The next GRN number: GRN-2026-00001, GRN-2026-00002, …

    Numbered per calendar year, so the year in the number says when the goods
    were received without anyone opening the row, and the count restarts each
    January instead of climbing forever.

    The sequence is read from the numbers already issued rather than from a
    counter, and it steps over any that are taken. A counter would have to be
    kept in step with rows that get deleted, and the first time the two
    disagreed it would hand out a number that already existed — on the one field
    people quote to each other when a delivery is queried.
    """
    from . import numbering
    return numbering.next_number(
        db, "grn", warehouse_id=warehouse_id, when=(when or dt.datetime.utcnow()),
        is_taken=lambda code: db.query(models.Purchase).filter(
            models.Purchase.grn_no == code).first() is not None)


def _known_categories(db, cat_id):
    """The category master a GRN may use, keyed lower-case -> master spelling.
    A category typed on the invoice's Line Items grid is kept only when it is in
    here (the rule the GRN line edit enforces); anything else is left to the
    auto-classifier rather than inventing a category."""
    return {c.name.strip().lower(): c.name for c in db.query(models.Category).filter(
        (models.Category.catalogue_id == cat_id) | (models.Category.catalogue_id.is_(None)))}


def sync_draft_categories(db, purchase, items):
    """Category is set on the invoice, not on the GRN, so a DRAFT GRN built
    before it was typed picks it up here. Lines pair up by position; when the
    invoice and the GRN no longer have the same number of lines, nothing is
    guessed."""
    if purchase is None or purchase.status != "draft" or not items:
        return
    lines = sorted(purchase.lines, key=lambda l: l.id)
    if len(items) != len(lines):
        return
    known = _known_categories(db, purchase_catalogue_id(db, purchase))
    for it, line in zip(items, lines):
        name = known.get(str(it.get("category") or "").strip().lower())
        if name and name != line.category:
            line.category = name
    db.flush()


def build_grn_from_document(db, doc):
    """Create (or return existing) a draft GRN for a confirmed document."""
    existing = db.query(models.Purchase).filter(
        models.Purchase.document_id == doc.id).first()
    if existing:
        ex = doc.latest_extraction
        sync_draft_categories(db, existing, (ex.data or {}).get("line_items") if ex else None)
        return existing

    ex = doc.latest_extraction
    data = ex.data if ex else {}
    inv = data.get("invoice", {}) or {}
    tot = data.get("totals", {}) or {}
    meta = data.get("meta", {}) or {}

    # Where the goods came in. Read from the document, not from the purchase —
    # the purchase does not exist yet at this point, and reaching for it here is
    # an UnboundLocalError on every invoice that does not carry its own GRN
    # number, which is most of them. Carried onto the purchase below too, so the
    # receipt is scoped to the same warehouse its number was drawn from.
    warehouse_id = doc.warehouse_id

    # A number the invoice itself carried is kept — some suppliers print the
    # buyer's GRN reference on the bill, and ours must not talk over theirs.
    # Otherwise one is allocated HERE, at the moment a receipt actually exists.
    # Allocating earlier, on the review screen, would spend numbers on invoices
    # that are never received and leave gaps nobody can account for.
    grn_no = ((meta.get("grn_no") or "").strip()
              or next_grn_no(db, warehouse_id=warehouse_id))

    purchase = models.Purchase(
        document_id=doc.id, supplier_id=doc.supplier_id,
        warehouse_id=warehouse_id,
        grn_no=grn_no, invoice_number=inv.get("number"),
        invoice_date=inv.get("date"),
        taxable_total=tot.get("taxable_total") or 0.0,
        tax_total=tot.get("tax_total") or 0.0,
        grand_total=tot.get("grand_total") or 0.0,
        status="draft",
    )
    db.add(purchase)
    db.flush()

    cat_id = purchase_catalogue_id(db, purchase)
    known_cats = _known_categories(db, cat_id)
    for it in data.get("line_items", []):
        line_cat = known_cats.get(str(it.get("category") or "").strip().lower())
        # a line that names a size, a brand or a design is matched on that tuple,
        # the way a breakdown row is — see named_attrs
        match = match_product(db, it.get("barcode"), it.get("description"),
                              it.get("hsn"), doc.supplier_id,
                              attrs=named_attrs(it.get("size"), it.get("brand"),
                                                it.get("design"),
                                                **{a: it.get(a) for a in LINE_ATTRS}),
                              catalogue_id=cat_id)
        db.add(models.PurchaseLine(
            purchase_id=purchase.id,
            product_id=match.id if match else None,
            barcode=it.get("barcode"), description=it.get("description"),
            hsn=it.get("hsn"), qty=it.get("qty"), uom=it.get("uom") or "PCS",
            rate=it.get("rate"),
            # the size cell verbatim — a size run like "30:2, 32:4" is the
            # breakdown the supplier already counted, and size_split reads it
            size=it.get("size"),
            # the invoice's own Brand and Design columns. The document calls it
            # `design`; the stock master has always called it `design_no`.
            brand=it.get("brand"), design_no=it.get("design"),
            category=line_cat,
            # style, material, pattern, sleeve and type, typed on the invoice's
            # Line Items grid — the product is born with them at post
            **{a: (str(it.get(a)).strip() or None) if it.get(a) else None for a in LINE_ATTRS},
            amount=it.get("amount") if it.get("amount") is not None else it.get("taxable_value"),
            # retail: the MRP the supplier printed, and the shelf price someone
            # set on the review screen. Carried through so the product this line
            # creates is priced from the moment it exists.
            mrp=it.get("mrp"), sale_price=it.get("sale_price"),
            sale_discount_pct=it.get("sale_discount_pct"),
            is_new_product=match is None,
        ))
    db.flush()
    _publish_grn_no(db, doc, purchase.grn_no)
    return purchase


def backfill_grn_numbers(db):
    """Give already-created GRNs their number, and push it to the register.

    Two kinds of row need it. GRNs raised before numbering existed have none at
    all; GRNs raised before the number was PUBLISHED have one that never reached
    the consignment it belongs to. Both look the same from the LR register — an
    empty GRN column beside a delivery that has plainly been received.

    Runs at startup and is a no-op once there is nothing to fill. Numbers are
    handed out in posting order, so the sequence follows the order the goods
    actually came in rather than the order the rows happen to be read.
    """
    filled = 0
    unnumbered = (db.query(models.Purchase)
                    .filter((models.Purchase.grn_no.is_(None))
                            | (models.Purchase.grn_no == ""))
                    .order_by(models.Purchase.posted_at.asc().nullslast(),
                              models.Purchase.id.asc()).all())
    for p in unnumbered:
        p.grn_no = next_grn_no(db, p.posted_at or p.created_at,
                               warehouse_id=p.warehouse_id)
        db.flush()                       # so the next call sees this one taken
        filled += 1

    # …and every GRN whose number never reached the register row beside it.
    for p in db.query(models.Purchase).filter(
            models.Purchase.grn_no.isnot(None),
            models.Purchase.document_id.isnot(None)).all():
        db.query(models.LREntry).filter(
            models.LREntry.invoice_document_id == p.document_id,
            (models.LREntry.grn_no.is_(None)) | (models.LREntry.grn_no == "")
        ).update({"grn_no": p.grn_no}, synchronize_session=False)
    db.commit()
    return filled


def _publish_grn_no(db, doc, grn_no):
    """Put the allocated number back where people will look for it.

    Three places carry it and they are read by three different people: the
    invoice's own GRN & Notes panel, which is where whoever keyed the bill looks;
    the GRN itself; and the consignment row in the LR register, which is what the
    transport desk has in front of it when a delivery is queried. A number that
    existed only on the GRN would have to be looked up from the other two.

    The extraction's data is REPLACED rather than mutated in place: SQLAlchemy
    does not notice a change made inside a JSON column, so a mutated dict is
    quietly never written.
    """
    if not grn_no:
        return
    ex = doc.latest_extraction
    if ex and not ((ex.data.get("meta") or {}).get("grn_no") or "").strip():
        data = dict(ex.data or {})
        data["meta"] = {**(data.get("meta") or {}), "grn_no": grn_no}
        ex.data = data

    # The consignment this invoice was matched to, if the register knows it.
    db.query(models.LREntry).filter(
        models.LREntry.invoice_document_id == doc.id
    ).update({"grn_no": grn_no}, synchronize_session=False)


# ---------------------------------------------------------------------------
#  Size splits — one billed bundle line becomes one product per size
# ---------------------------------------------------------------------------
# quantities are floats, so "adds up" needs a tolerance rather than ==
SPLIT_TOLERANCE = 0.001


def _opt_float(v, default=None):
    if v is None or v == "":
        return default
    return float(v)


def split_status(line):
    """How far a line's attribute breakdown has got: totals plus whether it balances.

    The target is what ARRIVED, not what was billed. A supplier invoices 50 and
    40 come out of the boxes; the ten short are recorded as a GrnShortage and the
    breakdown then has to add up to 40. Balancing against the billed 50 would
    leave the receiver to either invent ten pieces or never post the GRN — see
    services/shortages.py.

    A line with no breakdown counts as balanced — there is nothing to add up.
    Reporting False there reads as "this line is broken" to anything consuming
    the API, when it simply isn't broken down."""
    line_qty = float(line.qty or 0)
    short = shortage_svc.line_totals(line)
    target = short["received_qty"]
    base = {"line_qty": line_qty, "received_qty": target,
            "short_qty": short["missing_qty"], "excess_qty": short["excess_qty"],
            "has_shortage": short["rows"] > 0}
    if not line.is_split:
        return {**base, "split_qty": 0.0, "remainder": 0.0, "balanced": True}
    return {**base, "split_qty": line.split_qty,
            "remainder": round(target - line.split_qty, 3),
            "balanced": abs(target - line.split_qty) <= SPLIT_TOLERANCE}


def set_line_splits(db, line, rows):
    """Replace a line's attribute breakdown ([] clears it). ValueError on bad input.

    Only a draft can be broken down — once stock has moved, changing the breakdown
    would mean rewriting posted movements. Rows need not add up to the billed
    quantity yet: the breakdown is worked on incrementally and only has to balance
    at post."""
    if line.purchase.status == "posted":
        raise ValueError("this GRN is posted — its breakdown can no longer change")

    clean, seen = [], set()
    for r in rows or []:
        attrs = {a: (str(r.get(a) or "").strip() or None) for a in SPLIT_ATTRS}
        if not any(attrs.values()):
            raise ValueError("every row needs at least one attribute "
                             "(size, colour, material, pattern, fit, type or design no)")
        key = tuple(_norm(attrs[a]) for a in SPLIT_ATTRS)
        label = " · ".join(v for v in attrs.values() if v)
        if key in seen:
            raise ValueError(f"“{label}” appears twice — merge those rows")
        seen.add(key)
        try:
            qty = _opt_float(r.get("qty"), 0) or 0
            prices = {p: _opt_float(r.get(p), line.rate if p == "rate" else None)
                      for p in SPLIT_PRICES}
        except (TypeError, ValueError):
            raise ValueError(f"“{label}”: quantity and prices must be numbers")
        if qty <= 0:
            raise ValueError(f"“{label}”: quantity must be greater than zero")
        clean.append(dict(qty=qty, code=(r.get("code") or None),
                          category=(str(r.get("category") or "").strip() or None),
                          # blank inherits the line's unit at post — a variant is
                          # the same kind of article as the bundle it came out of
                          unit_type=(str(r.get("unit_type") or "").strip().upper() or None),
                          **attrs, **prices))

    line.splits.clear()          # delete-orphan drops the previous breakup
    db.flush()
    for r in clean:
        db.add(models.PurchaseLineSplit(line_id=line.id, **r))
    if clean:
        # a split line no longer receives stock itself, so it must not look like
        # it maps to one product — the sizes do
        line.product_id = None
        line.is_new_product = False
    else:
        # breakup removed: fall back to matching the bundle line as a whole
        match = match_product(db, line.barcode, line.description, line.hsn,
                              line.purchase.supplier_id, attrs=line_named_attrs(line),
                              catalogue_id=purchase_catalogue_id(db, line.purchase))
        line.product_id = match.id if match else None
        line.is_new_product = match is None
    db.flush()
    return line


def _receive_into_stock(db, product, qty, rate, purchase, note=None):
    """Append one inward movement at the receiving warehouse.

    The weighted-average cost rolls at THAT warehouse and the company figures are
    rolled up from the buildings beneath them — see services/stock_locations. The
    warehouse comes off the GRN, which `post_grn` has already resolved, so a
    receipt can never land nowhere.
    """
    ref = f"GRN {purchase.grn_no or ''} / Inv {purchase.invoice_number or ''}".strip()
    product.last_rate = float(rate or 0)
    stock_loc.apply(db, product, purchase.warehouse_id, float(qty or 0),
                    kind="inward", ref_type="purchase", ref_id=purchase.id,
                    rate=rate, note=f"{ref} · {note}" if note else ref)


def apply_category(db, product, name):
    """Set a product's category from the master and keep its section in step.
    Returns True when a category was applied.

    The master lists most names twice — once under their gender section and once
    under OVERALL — so the specific section is the one worth keeping: a
    LADIES-T-SHIRT belongs to LADIES, and reporting it as OVERALL would lose the
    only thing the section column is for.

    Looked up inside the item's own business line, so a name that exists in two
    catalogues resolves to the section its own line gives it."""
    if not name:
        return False
    product.category = name
    q = db.query(models.Category).filter(models.Category.name == name)
    if getattr(product, "catalogue_id", None):
        q = q.filter((models.Category.catalogue_id == product.catalogue_id)
                     | (models.Category.catalogue_id.is_(None)))
    rows = q.all()
    chosen = next((c for c in rows if c.section and c.section != "OVERALL"),
                  rows[0] if rows else None)
    product.category_section = chosen.section if chosen else None
    return True


def _create_product(db, purchase, line, split=None, mint_codes=False, unit=None):
    """Create the inventory master row for a received line (or one of its variants).

    `mint_codes` assigns the SKU *and* an internal EAN-13 straight away, which is
    what variants need: the supplier never printed a code for "L / Red" alone, so
    the label has to be ours before the goods can be scanned or dispatched.

    `unit` is (code, pieces_per_unit) — what this product is COUNTED in, which is
    not necessarily what the supplier billed. A dozen pillow covers is six pairs,
    and the product born here is a PAIR with six of it, not a DOZ with one. Frozen
    on the row at creation; see services/unit_types.py."""
    from . import unit_types as ut
    code, per = unit or (ut.default_code(db, line.uom), 1.0)
    product = models.Product(
        # a variant carries no supplier barcode — the bundle's code covered the
        # whole bundle — so it gets one of ours below instead
        sku=None if mint_codes else _next_sku(db, purchase.warehouse_id),
        barcode=None if split else line.barcode,
        description=line.description or "(unnamed)", hsn=line.hsn,
        uom=code, unit_type=code, pieces_per_unit=per,
        primary_supplier_id=purchase.supplier_id,
        # The business line this item belongs to, from the warehouse receiving
        # it. Set at birth because it decides which categories classify it, which
        # attributes it carries, and which stock records it may ever merge with.
        catalogue_id=purchase_catalogue_id(db, purchase),
        stock_qty=0.0, avg_cost=0.0,
    )
    if split is not None:
        for a in SPLIT_ATTRS:
            setattr(product, a, getattr(split, a, None))
    else:
        # No breakdown, but the LINE may still say what this is. An invoice keyed
        # one line per size is a breakdown done earlier, and dropping what it says
        # here is what left a product called FROCK with no size, no brand and no
        # design after somebody had typed all three.
        for a, v in (line_named_attrs(line) or {}).items():
            if v:
                setattr(product, a, v)
    # Retail pricing: the variant's own where it has any, otherwise the line's.
    # A breakdown that only names sizes should not throw away a price set for the
    # whole bundle on the review screen — the sizes are the same goods.
    for fld in ("mrp", "sale_price", "sale_discount_pct"):
        v = getattr(split, fld, None) if split is not None else None
        setattr(product, fld, v if v is not None else getattr(line, fld, None))
    db.add(product)
    db.flush()
    if mint_codes:
        from . import barcode_svc
        barcode_svc.assign_identifiers(db, product)     # sku + internal EAN-13 → QR
    # A category chosen on the GRN row wins — someone looked at the goods. Only
    # when none was chosen do we classify from the invoice description, and then
    # only a confident match is applied; the rest stay blank for review.
    chosen = (getattr(split, "category", None) if split is not None else None) or line.category
    if not apply_category(db, product, chosen):
        from . import categorize
        categorize.categorise_product(db, product)
    db.flush()
    return product


def line_unit_type(db, line, split=None, product=None):
    """(code, pieces_per_unit) — the unit this row's goods are counted in.

    Three cases, and the order between them is the whole of the rule:

      1. **A product that already carries a unit keeps it.** Re-buying pillow
         covers has to land on the same stock record, counted the same way. A GRN
         line that says otherwise is a line to correct, never a reason to restate
         what is already on the shelf.
      2. **A product that predates unit types keeps counting the way it has** —
         its own UOM, matched into the master. Applying today's rule to it would
         silently re-read a stock of 12 pieces as 12 pairs.
      3. **A new product** takes what the GRN line says, or what the master's
         rules read off its description, or the default piece.
    """
    from . import unit_types as ut
    if product is not None and product.unit_type:
        return product.unit_type, float(product.pieces_per_unit or 1.0) or 1.0
    if product is not None:
        t = ut.match_uom(db, product.uom)
        code = t.code if t else ut.DEFAULT_CODE
        per = t.pieces_per if t else 1.0
        ut.apply_to_product(db, product, code, per)     # frozen from here on
        return code, per
    code, per, _ = ut.resolve(
        db, explicit=(getattr(split, "unit_type", None) if split is not None else None)
                     or line.unit_type,
        description=line.description,
        category=(getattr(split, "category", None) if split is not None else None)
                 or line.category,
        uom=line.uom)
    return code, per


def post_grn(db, purchase):
    """Commit a draft GRN to inventory. Returns a summary dict.

    This is where a billed dozen becomes twelve handkerchiefs or six pairs of
    pillow covers: `qty` and `uom` on the line stay the supplier's own figures,
    and the conversion into the product's unit happens once, here, at the moment
    the goods become stock. The rate converts with it, so ₹600 a dozen is ₹100 a
    pair and the valuation still adds up to what was paid."""
    if purchase.status == "posted":
        return {"ok": False, "error": "already posted", "purchase_id": purchase.id}

    # WHERE the goods land, settled once, here, before a single movement is
    # written. A draft may be raised before anybody has said which building took
    # the delivery — but stock cannot stand nowhere, so a blank resolves to the
    # default warehouse and is WRITTEN BACK to the GRN. Leaving it blank on the
    # row while posting its stock somewhere would make the receipt unable to say
    # where its own goods went, and would make an unpost guess a second time.
    purchase.warehouse_id = stock_loc.resolve_warehouse_id(db, purchase.warehouse_id)
    db.flush()
    # And therefore WHAT KIND of goods these are: the receiving warehouse's
    # business line. Everything this GRN creates is stamped with it, and nothing
    # it matches against comes from outside it.
    cat_id = purchase_catalogue_id(db, purchase)

    # A half-finished breakdown must never reach stock: if the rows don't add up
    # to what actually arrived, posting would quietly lose or invent units. The
    # target is the RECEIVED quantity — billed less any shortage recorded at the
    # dock — so a short delivery balances honestly instead of forcing the receiver
    # to type pieces that were never in the box.
    off = []
    for l in purchase.lines:
        st = split_status(l)
        if st["received_qty"] < -SPLIT_TOLERANCE:
            return {"ok": False, "purchase_id": purchase.id,
                    "error": f"“{(l.description or '')[:32]}”: more recorded short "
                             f"than was billed ({float(l.qty or 0):g})"}
        if l.is_split and not st["balanced"]:
            gap = round(st["received_qty"] - l.split_qty, 3)
            off.append(
                f"“{(l.description or ('line ' + str(l.id)))[:32]}”: "
                + (f"{gap:g} piece(s) remaining" if gap > 0
                   else f"{-gap:g} piece(s) over")
                + f" — the size breakdown totals {l.split_qty:g} of "
                  f"{st['received_qty']:g} received"
                + (f" ({float(l.qty or 0):g} billed, {st['short_qty']:g} short)"
                   if st["has_shortage"] else ""))
    if off:
        # the breakdown is the source of truth for what exists, so it has to
        # account for every piece before any of them gets a SKU and a QR
        return {"ok": False, "purchase_id": purchase.id,
                "error": "; ".join(off) + ". Please complete the size breakdown "
                                          "before posting to inventory."}

    from . import units as unit_svc
    from . import unit_types as ut
    created, updated, split_rows, pieces = 0, 0, 0, 0
    short_qty, short_value, nothing_arrived = 0.0, 0.0, 0
    converted = []                      # rows where the billed unit wasn't the stock one
    # one identity per physical piece, under the SKU that receives it — the stock
    # figure stays at SKU level, this is the layer a garment tag hangs off
    def _serialise(product, qty):
        nonlocal pieces
        made = unit_svc.create_for_receipt(db, product, qty, purchase)
        pieces += len(made)
        return made

    for line in purchase.lines:
        st = split_status(line)
        for sh in line.shortages:
            if sh.claimable:
                short_qty += float(sh.qty or 0)
                short_value += shortage_svc.value(sh)
        # billed, and not one piece of it turned up. There is no product to create,
        # no stock to move and no carton to label — the whole of this line is the
        # claim recorded against it.
        if st["received_qty"] <= SPLIT_TOLERANCE and st["has_shortage"]:
            nothing_arrived += 1
            continue
        if line.is_split:
            for sp in line.splits:
                # a breakdown row inherits what the line says when it says nothing
                for a in LINE_ATTRS:
                    if not getattr(sp, a, None) and getattr(line, a, None):
                        setattr(sp, a, getattr(line, a))
                product = db.get(models.Product, sp.product_id) if sp.product_id else None
                if not product and sp.code:
                    from . import barcode_svc      # a scanned QR/barcode wins
                    product = barcode_svc.resolve(db, sp.code)
                if not product:
                    attrs = {a: getattr(sp, a, None) for a in SPLIT_ATTRS}
                    product = match_product(db, None, line.description, line.hsn,
                                            purchase.supplier_id, attrs=attrs,
                                            catalogue_id=cat_id)
                if not product:
                    product = _create_product(db, purchase, line, split=sp,
                                              mint_codes=True,
                                              unit=line_unit_type(db, line, sp))
                    sp.is_new_product = True
                    created += 1
                else:
                    sp.is_new_product = False
                    updated += 1
                    # an existing variant can still pick up prices set on this GRN,
                    # and any attribute it was missing (e.g. matched by scanned QR)
                    for f in ("mrp", "sale_price", "sale_discount_pct"):
                        v = getattr(sp, f)
                        v = v if v is not None else getattr(line, f, None)
                        if v is not None:
                            setattr(product, f, v)
                    for a in SPLIT_ATTRS:
                        if getattr(sp, a) and not getattr(product, a):
                            setattr(product, a, getattr(sp, a))
                    if not product.category:
                        apply_category(db, product, sp.category or line.category)
                sp.product_id = product.id
                if line.hsn and not product.hsn:
                    product.hsn = line.hsn
                # the billed unit is the supplier's; the stock unit is this
                # product's, and a breakdown row of "1 DOZ" of pillow covers
                # becomes six pairs here and nowhere else
                code, _ = line_unit_type(db, line, sp, product)
                conv = ut.convert(db, sp.qty, line.uom, code, sp.effective_rate)
                if not conv["whole"]:
                    ok, why = ut.check_line(db, f"{line.description} · {sp.variant_label}",
                                            sp.qty, line.uom, code)
                    return {"ok": False, "purchase_id": purchase.id, "error": why}
                note = " · ".join(x for x in (sp.variant_label or None,
                                              conv["explain"] if conv["converted"] else None) if x)
                _receive_into_stock(db, product, conv["units"], conv["rate_per_unit"],
                                    purchase, note=note or None)
                _serialise(product, conv["units"])
                if conv["converted"]:
                    converted.append(conv["explain"])
                split_rows += 1
            continue

        product = db.get(models.Product, line.product_id) if line.product_id else None
        if not product:
            # whole line, no variants
            product = _create_product(db, purchase, line,
                                      unit=line_unit_type(db, line))
            line.product_id = product.id
            line.is_new_product = True
            created += 1
        else:
            updated += 1
            if not product.category:
                apply_category(db, product, line.category)
            # a re-buy picks up whatever pricing this GRN states, the same way a
            # variant does — a repriced line is the point of typing it
            for fld in ("mrp", "sale_price", "sale_discount_pct"):
                if getattr(line, fld, None) is not None:
                    setattr(product, fld, getattr(line, fld))
            # …and any attribute the line names that the product was missing
            for a, v in (line_named_attrs(line) or {}).items():
                if v and not getattr(product, a, None):
                    setattr(product, a, v)
        if line.hsn and not product.hsn:
            product.hsn = line.hsn
        # what arrived, not what was billed — the difference is the shortage
        recv = st["received_qty"]
        code, _ = line_unit_type(db, line, None, product)
        conv = ut.convert(db, recv, line.uom, code, line.rate)
        if not conv["whole"]:
            ok, why = ut.check_line(db, line.description, recv, line.uom, code)
            return {"ok": False, "purchase_id": purchase.id, "error": why}
        note = " · ".join(x for x in (
            (f"received {recv:g} of {float(line.qty or 0):g} billed"
             if st["has_shortage"] else None),
            conv["explain"] if conv["converted"] else None) if x)
        _receive_into_stock(db, product, conv["units"], conv["rate_per_unit"],
                            purchase, note=note or None)
        _serialise(product, conv["units"])
        if conv["converted"]:
            converted.append(conv["explain"])

    purchase.status = "posted"
    purchase.posted_at = dt.datetime.utcnow()
    if purchase.document:
        purchase.document.status = "posted"
    db.flush()
    # the goods are now on the floor and have to go somewhere, so each carton gets
    # its handling label here — the item labels come later, at tagging
    from . import bundles as bundle_svc
    made = bundle_svc.create_for_purchase(db, purchase)
    return {"ok": True, "purchase_id": purchase.id,
            "warehouse_id": purchase.warehouse_id,
            "warehouse": purchase.warehouse.name if purchase.warehouse else None,
            "products_created": created, "products_updated": updated,
            "lines": len(purchase.lines), "size_rows": split_rows,
            "bundles": [b.code for b in made], "pieces": pieces,
            # every row whose billed unit was not its stock unit, spelled out —
            # a receipt that turned 2 DOZ into 12 pairs has to say so, or the
            # stock figure looks like it lost ten of something
            "converted": converted,
            # what the supplier billed and never delivered. Reported at post
            # because this is the moment it stops being a note on a screen and
            # becomes a claim: the stock figure is now final and does not include it.
            "short_qty": round(short_qty, 3),
            "short_value": round(short_value, 2),
            "lines_not_received": nothing_arrived}


# ---------------------------------------------------------------------------
#  Unpost — reverse a posted GRN so it can be corrected and posted again
# ---------------------------------------------------------------------------
#  A GRN can be posted, unposted and posted again, so its footprint in the ledger
#  is every row it ever wrote: the inward rows AND the reversal rows from earlier
#  unposts. All of them come out of the replay together. Excluding only the inwards
#  would leave a stale reversal behind to be counted twice — reporting a shortfall
#  that isn't there and leaving the average cost inflated by an inward whose
#  compensating row was ignored.
GRN_REF_TYPES = ("purchase", "purchase_unpost")


def _grn_movements(db, purchase):
    """Every stock row this GRN has written, across all of its posts and unposts."""
    return db.query(models.StockMovement).filter(
        models.StockMovement.ref_type.in_(GRN_REF_TYPES),
        models.StockMovement.ref_id == purchase.id).order_by(models.StockMovement.id).all()


# Replaying the ledger to recompute stock and weighted-average cost lives in
# services/stock_locations (`replay` / `rebuild`) — it is per warehouse now,
# which is where an average is actually kept. Unposting reads it through there.


def unpost_blockers(db, purchase):
    """Everything that has to be cleared before this GRN can be unposted."""
    out = []
    for r in db.query(models.PurchaseReturn).filter(
            models.PurchaseReturn.purchase_id == purchase.id,
            models.PurchaseReturn.status == "posted").all():
        out.append(f"debit note {r.code} was raised against this invoice")
    for a in db.query(models.PaymentAllocation).filter(
            models.PaymentAllocation.purchase_id == purchase.id).all():
        out.append(f"payment {a.payment.receipt_no if a.payment else ''} is settled "
                   f"against this invoice".replace("  ", " "))
    # stock that has already left the warehouse cannot be un-received
    rows = _grn_movements(db, purchase)
    exclude = {m.id for m in rows}
    seen = set()
    for pid, _wid in _movements_by_location(rows):
        if pid in seen:
            continue
        seen.add(pid)
        product = db.get(models.Product, pid)
        if not product:
            continue
        # Checked per WAREHOUSE, not on the company total. A shirt received into
        # Erode and since transferred to Karur nets to zero across the company,
        # so a company-level check would happily unpost it — and leave Erode
        # holding minus twenty of something that is standing in Karur.
        for wid, (qty, _avg) in stock_loc.replay(db, product, exclude).items():
            if qty < -SPLIT_TOLERANCE:
                wh = db.get(models.Warehouse, wid)
                where = f" at {wh.name}" if wh else ""
                out.append(f"“{product.description}” ({product.sku}) would go to "
                           f"{qty:g}{where} — {-qty:g} of it has already been "
                           f"dispatched or returned")
    return out


def _movements_by_location(movements):
    """{(product_id, warehouse_id): [movements]} — the grain a reversal works at.

    Grouped by warehouse as well as product because the compensating row has to
    be written where the original one landed. A GRN receives into one building,
    but a GRN posted, unposted and re-posted after the default warehouse changed
    has rows in two, and pooling them would reverse both out of whichever one was
    read first."""
    out = {}
    for mv in movements:
        out.setdefault((mv.product_id, mv.warehouse_id), []).append(mv)
    return out


def _net_and_rate(mvs):
    """(net quantity this GRN still contributes, the cost it came in at).

    Net, because an earlier unpost may already have compensated part of it — and a
    product can legitimately receive twice from one GRN if two breakdown rows
    resolve to it. The rate is quantity-weighted across the inward rows, so a
    single compensating row values correctly whatever mix it undoes."""
    net = round(sum(float(m.qty_delta or 0) for m in mvs), 3)
    inwards = [m for m in mvs if m.kind == "inward" and float(m.qty_delta or 0) > 0]
    qty = sum(float(m.qty_delta or 0) for m in inwards)
    rate = (sum(float(m.qty_delta or 0) * float(m.rate or 0) for m in inwards) / qty
            if qty else 0.0)
    return net, round(rate, 4)


def _product_is_orphan(db, product, purchase):
    """True when a product this GRN created carries nothing else.

    Those are removed outright, so unposting really does undo the post instead of
    leaving zero-stock ghosts behind with SKUs burnt. Anything with its own history
    — phone-recorded details, another GRN, a dispatch, a return — is kept (at zero
    stock), because deleting it would destroy data this GRN never created."""
    if product.detailed or round(float(product.stock_qty or 0), 3) != 0:
        return False
    mine = ("purchase", "purchase_unpost")
    foreign_mv = db.query(models.StockMovement).filter(
        models.StockMovement.product_id == product.id,
        ~((models.StockMovement.ref_id == purchase.id)
          & (models.StockMovement.ref_type.in_(mine)))).count()
    if foreign_mv:
        return False
    other_line = db.query(models.PurchaseLine).filter(
        models.PurchaseLine.product_id == product.id,
        models.PurchaseLine.purchase_id != purchase.id).count()
    other_split = db.query(models.PurchaseLineSplit).join(
        models.PurchaseLine,
        models.PurchaseLineSplit.line_id == models.PurchaseLine.id).filter(
        models.PurchaseLineSplit.product_id == product.id,
        models.PurchaseLine.purchase_id != purchase.id).count()
    outward = db.query(models.StockOutwardLine).filter(
        models.StockOutwardLine.product_id == product.id).count()
    returned = db.query(models.PurchaseReturnLine).filter(
        models.PurchaseReturnLine.product_id == product.id).count()
    return not (other_line or other_split or outward or returned)


def unpost_grn(db, purchase):
    """Reverse a posted GRN and put it back to draft so it can be corrected.

    For products that existed before this GRN, nothing is erased: each inward row
    gets a compensating `reversal` row and the product's stock and weighted-average
    cost are replayed from the remaining ledger. Products this GRN created (and
    nothing else has touched) are removed along with their rows — for those, "as if
    never posted" is the honest outcome and leaves no zero-stock ghosts."""
    if purchase.status != "posted":
        return {"ok": False, "error": "this GRN isn't posted", "purchase_id": purchase.id}
    blockers = unpost_blockers(db, purchase)
    if blockers:
        return {"ok": False, "purchase_id": purchase.id,
                "error": "can't unpost — " + "; ".join(blockers)}

    rows = _grn_movements(db, purchase)
    qty_reversed, n_reversed = 0.0, 0
    touched = set()
    for (pid, wid), mvs in _movements_by_location(rows).items():
        product = db.get(models.Product, pid)
        if not product:
            continue
        net, rate = _net_and_rate(mvs)
        if net == 0:                       # an earlier unpost already undid this
            continue
        # Written at the warehouse the goods were received into, so that building
        # gives them back rather than the company losing them from nowhere.
        stock_loc.apply(db, product, wid, -net, kind="reversal",
                        ref_type="purchase_unpost", ref_id=purchase.id, rate=rate,
                        note=f"Unposted GRN {purchase.grn_no or ''} / "
                             f"Inv {purchase.invoice_number or ''}".strip())
        qty_reversed += net
        n_reversed += 1
        touched.add(product.id)
    # `apply` moved the quantity; only a replay can restate the weighted-average
    # cost, which cannot be un-mixed arithmetically. Done once per product after
    # every reversal it takes, so a GRN that received twice into one warehouse is
    # replayed once rather than mid-way through its own undoing.
    for pid in touched:
        product = db.get(models.Product, pid)
        if product:
            stock_loc.rebuild(db, product)
    db.flush()

    removed, kept = [], []
    for line in purchase.lines:
        holders = list(line.splits) if line.is_split else [line]
        for h in holders:
            product = db.get(models.Product, h.product_id) if h.product_id else None
            if product and h.is_new_product and _product_is_orphan(db, product, purchase):
                removed.append(product.sku or f"#{product.id}")
                db.delete(product)                  # cascades its movements
                h.product_id = None
                if h is line:                       # draft shows "new" again
                    line.is_new_product = True
            else:
                if product and h.is_new_product:
                    kept.append(product.sku or f"#{product.id}")
                # the product exists now, so a re-post updates rather than creates
                h.is_new_product = False

    purchase.status = "draft"
    purchase.posted_at = None
    if purchase.document and purchase.document.status == "posted":
        purchase.document.status = "confirmed"
    # the cartons describe a receipt that, as of now, did not happen — and their
    # labels point at products this unpost may have just deleted. The per-piece
    # codes go the same way: those garments were never received.
    from . import bundles as bundle_svc
    from . import units as unit_svc
    pieces_dropped = unit_svc.remove_for_purchase(db, purchase)
    dropped = bundle_svc.remove_for_purchase(db, purchase)
    db.flush()
    return {"ok": True, "purchase_id": purchase.id, "movements_reversed": n_reversed,
            "qty_reversed": round(qty_reversed, 3),
            "products_removed": removed, "products_kept": kept,
            "bundles_removed": dropped, "pieces_removed": pieces_dropped}


def adjust_stock(db, product, new_qty, note="manual adjustment", warehouse_id=None):
    """Set a product's stock to an exact figure via an adjustment movement
    (never edit stock_qty directly — the ledger stays the source of truth).

    `new_qty` is what was COUNTED AT ONE WAREHOUSE, because that is the only
    thing anybody can count: somebody walked a building and found eleven. With no
    warehouse named the count is taken against the default one, which is what a
    single-warehouse install has always meant by it.

    Reading `new_qty` as a company total would be the dangerous alternative — a
    count of eleven in Erode would wipe out everything standing in Karur.
    """
    warehouse_id = stock_loc.resolve_warehouse_id(db, warehouse_id)
    on_hand = stock_loc.qty_at(db, product.id, warehouse_id)
    delta = round(float(new_qty) - on_hand, 3)
    if delta == 0:
        return None
    return stock_loc.apply(db, product, warehouse_id, delta, kind="adjustment",
                           ref_type="adjustment", rate=product.avg_cost or 0,
                           note=note)


# There is no update_product() any more, on purpose. A product is whatever its GRN
# made it: description / HSN / UOM off the invoice, category and prices from the
# breakdown, physical attributes from the phone app. Editing it here would let the
# master data disagree with the document and the stock ledger that produced it, so
# corrections go through unpost → fix the GRN line → post again. Stock is corrected
# by adjust_stock (a movement, never a silent overwrite) and identifiers by
# barcode_svc.assign_identifiers.


def inventory_summary(db):
    """Headline stock figures — over records that ARE stock.

    Only products traceable to a posted GRN are counted. Anything else is either
    debris or a record deliberately kept at zero after an unpost, and neither is
    stock; including them would put a valuation on goods no receipt ever brought
    in. The excluded count is reported rather than swallowed, so a number that
    quietly dropped can always be explained."""
    from . import integrity
    ctx = integrity.Context(db)
    everything = db.query(models.Product).all()
    products = [p for p in everything if ctx.product_state(p) == integrity.POSTED]
    excluded = len(everything) - len(products)
    total_value = round(sum(p.stock_value for p in products), 2)
    total_units = sum(p.stock_qty or 0 for p in products)
    return {
        "product_count": len(products),
        "total_units": total_units,
        "total_stock_value": total_value,
        "excluded_products": excluded,
    }
