"""
Relational model for the document-intake system.

Supplier         one row per vendor Essa buys from
SupplierProfile  the LEARNED template for a supplier ("train once per format").
                 Holds detection keys, tax behaviour, column mapping and a
                 reference example. Versioned so retraining never loses history.
Document         one uploaded invoice image/PDF + its lifecycle status
Extraction       an engine run over a Document -> canonical JSON + confidence.
                 A Document can have several (raw draft, human-corrected, ...).
LineItem         denormalised line rows for querying/reporting once confirmed.
"""
import datetime as dt
import uuid as _uuid
from sqlalchemy import (Column, Integer, String, Float, Text, DateTime,
                        ForeignKey, JSON, Boolean, UniqueConstraint)
from sqlalchemy.orm import relationship, backref
from .database import Base


def now():
    return dt.datetime.utcnow()


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    gstin = Column(String, index=True)
    pan = Column(String)
    state = Column(String)
    state_code = Column(String)
    address = Column(Text)
    phone = Column(String)
    email = Column(String)
    bank = Column(JSON, default=dict)
    aliases = Column(JSON, default=list)   # alternate names seen on invoices
    created_at = Column(DateTime, default=now)

    profiles = relationship("SupplierProfile", back_populates="supplier",
                            cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="supplier")

    @property
    def active_profile(self):
        active = [p for p in self.profiles if p.is_active]
        return max(active, key=lambda p: p.version) if active else None


class SupplierProfile(Base):
    """The trained format for one supplier. This is what makes 'train once,
    reuse forever' work: created/updated when a human confirms a correction."""
    __tablename__ = "supplier_profiles"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    template_key = Column(String, index=True)
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)

    # --- detection: how we recognise this format on a fresh upload ---
    detect_gstin = Column(String, index=True)
    detect_keywords = Column(JSON, default=list)   # strings that appear in header

    # --- extraction guidance handed to whichever provider runs ---
    tax_mode = Column(String, default="auto")      # intra_state | inter_state | auto
    default_tax_rates = Column(JSON, default=dict)  # {"igst":5} or {"cgst":2.5,"sgst":2.5}
    has_tds = Column(Boolean, default=False)
    column_map = Column(JSON, default=dict)         # printed header -> canonical field
    field_hints = Column(JSON, default=dict)        # regex/labels for header fields
    uom_default = Column(String, default="PCS")

    # --- a corrected example used as a few-shot reference for the vision model ---
    reference_example = Column(JSON, default=dict)

    trained_from_document_id = Column(Integer, ForeignKey("documents.id"))
    sample_count = Column(Integer, default=0)       # how many docs confirmed this profile
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    supplier = relationship("Supplier", back_populates="profiles")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    # Every page of this document, first included, when it was photographed as
    # more than one — a sixty-line bill prints as "Tax Invoice" and "Tax
    # Invoice(Page 2)", and only the last page carries the totals.
    #
    # stored_path stays the FIRST page rather than becoming a list: it is read
    # in a dozen places that want "the image of this document", and the answer
    # to that is still page one. NULL on every row that predates this, which is
    # why the readers below treat empty as "just stored_path".
    pages = Column(JSON)
    content_hash = Column(String, index=True)
    mime = Column(String)
    document_type = Column(String, default="invoice")   # invoice | lr_register | purchase_order
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    # Which warehouse's desk this bill belongs to. Stamped from whichever
    # warehouse the person keying it is working inside, so Erode's invoice queue
    # is Erode's and Karur's clerk never has to scroll past it.
    #
    # NULL on every row that predates warehouse workspaces, and those stay
    # visible everywhere on purpose — an invoice nobody has assigned is still
    # somebody's to deal with, and hiding it until it is claimed is how it goes
    # unpaid. See services/scope.
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    # uploaded -> extracted -> needs_review -> confirmed -> posted
    status = Column(String, default="uploaded", index=True)
    uploaded_at = Column(DateTime, default=now)

    supplier = relationship("Supplier", back_populates="documents")
    extractions = relationship("Extraction", back_populates="document",
                               cascade="all, delete-orphan", order_by="Extraction.id")
    line_items = relationship("LineItem", back_populates="document",
                              cascade="all, delete-orphan")

    @property
    def latest_extraction(self):
        return self.extractions[-1] if self.extractions else None


class Extraction(Base):
    __tablename__ = "extractions"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"))
    provider = Column(String)                    # seeded | tesseract | claude_vision | human
    profile_id = Column(Integer, ForeignKey("supplier_profiles.id"), nullable=True)
    data = Column(JSON)                          # canonical invoice dict
    confidence = Column(Float, default=0.0)
    warnings = Column(JSON, default=list)         # reconciliation issues
    field_flags = Column(JSON, default=dict)      # field -> "ok"|"review"
    is_correction = Column(Boolean, default=False)
    raw_text = Column(Text)
    created_at = Column(DateTime, default=now)

    document = relationship("Document", back_populates="extractions")


class LineItem(Base):
    __tablename__ = "line_items"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"))
    barcode = Column(String)
    description = Column(String)
    hsn = Column(String)
    qty = Column(Float)
    uom = Column(String)
    rate = Column(Float)
    amount = Column(Float)

    document = relationship("Document", back_populates="line_items")


# ============================================================================
#  Inventory + Purchase / GRN  (the module that consumes confirmed extractions)
# ============================================================================
class Product(Base):
    """The inventory master. One row per distinct stock item. Identified by
    barcode when the supplier prints one, otherwise by (description, hsn,
    supplier). Stock qty and a weighted-average cost are maintained by the
    stock-movement ledger — never edited directly."""
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    # Which business line this item belongs to, stamped from the warehouse that
    # received it. A saree and a t-shirt are not two rows of one catalogue that
    # happen to differ; they are different trades, and this is what lets the
    # inventory screen, the label designer and the phone form show one warehouse's
    # goods without the other's.
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True)
    sku = Column(String, unique=True, index=True)      # internal code we assign
    barcode = Column(String, index=True)               # supplier barcode if any
    description = Column(String, nullable=False)
    hsn = Column(String, index=True)
    uom = Column(String, default="PCS")
    # The unit this product is COUNTED in — its stock figure, its labels and its
    # sale are all one of these. A pillow cover is a PAIR, so a dozen received is
    # six of them; a handkerchief is a PCS, so a dozen is twelve. `uom` above is
    # kept in step and is what screens display.
    #
    # `pieces_per_unit` is frozen here at creation rather than read from the
    # UnitType master every time. The master is editable, and stock already on the
    # shelf was counted, valued and labelled under the rule in force when it
    # arrived — re-deriving it later would silently restate the quantity of goods
    # nobody has touched. See services/unit_types.py.
    unit_type = Column(String, index=True)             # UnitType.code
    pieces_per_unit = Column(Float, default=1.0)       # individual items in one unit
    mrp = Column(Float)
    primary_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    # maintained by stock movements:
    stock_qty = Column(Float, default=0.0)
    avg_cost = Column(Float, default=0.0)              # weighted-average purchase cost
    last_rate = Column(Float)
    created_at = Column(DateTime, default=now)

    # --- physical-detail attributes captured by the warehouse mobile app ---
    # (employee inspects each product and records what they see)
    color = Column(String)
    size = Column(String)
    pattern = Column(String)
    fit = Column(String)
    product_type = Column(String)      # the video's "Type"
    material = Column(String)
    design_no = Column(String)
    # From the stock master (Attributes Reference.xlsx, "Quanto Report"): the
    # columns Essa actually keeps against 13,851 stock rows are PRODUCT, BRAND,
    # COLOUR, PATTERN, STYLE, FIT, SLEEVE, TYPE, MATERIAL, SIZE. Everything but
    # these three was already here; without them a t-shirt could not record whose
    # label was on it, whether it was RNS or a straight cut, or whether it had
    # sleeves — and brand in particular is not decoration, it is identity: an
    # ESSA t-shirt and a YUVA t-shirt are different stock items.
    brand = Column(String, index=True)
    style = Column(String)             # RNS | Straight Cut | Umbrella | Casual | …
    sleeve = Column(String)            # Full | Half | Sleeveless | 3/4th
    # category master classification, mapped from the invoice description by
    # services/categorize.py (auto-applied only when the match is confident)
    category = Column(String, index=True)          # e.g. LADIES-T-SHIRT
    category_section = Column(String)              # OVERALL | KIDS | LADIES | MENS
    sale_price = Column(Float)
    # Discount off MRP, in percent — the selling lever ("20% off 1000 = 800").
    # Named for the SALE side because `LineItem.discount_pct` already means the
    # supplier's trade discount on what we PAID; the two must never be confused.
    # Replaced `margin_pct` (markup over cost), which nobody was filling in.
    sale_discount_pct = Column(Float)
    # Attributes this item's CATALOGUE defines that Product has no column for —
    # a saree's weave, its zari, its border, its pallu. {key: value}.
    #
    # A JSON column rather than ten more columns, because the alternative is a
    # migration every time a business line is added, and the columns would be
    # NULL for every item outside that line. The ten above keep their columns:
    # they are what matching, size splits, labels and every existing screen read,
    # and moving them in here to be tidy would rewrite all of it for no gain.
    # See models.CatalogueAttribute.
    attrs = Column(JSON)
    detailed = Column(Boolean, default=False, index=True)
    detailed_at = Column(DateTime)
    detailed_by = Column(String)

    primary_supplier = relationship("Supplier")
    movements = relationship("StockMovement", back_populates="product",
                             cascade="all, delete-orphan", order_by="StockMovement.id")
    # Cascaded for the same reason the movements are: unposting a GRN deletes the
    # products it created outright (see inventory._product_is_orphan), and a
    # balance row left pointing at a deleted product is a foreign key violation
    # that fails the whole unpost — on SQLite as well, which enforces them here.
    balances = relationship("StockBalance", back_populates="product",
                            cascade="all, delete-orphan")

    @property
    def stock_value(self):
        return round((self.stock_qty or 0) * (self.avg_cost or 0), 2)


class PurchaseOrder(Base):
    """What we asked a supplier for, before anything was sent.

    THE FIRST DOCUMENT IN THE CHAIN, and until now the missing one. Intake began
    at the supplier's invoice, which means the system could say what arrived and
    what it cost but never what was *ordered* — so nothing could be three-way
    matched, and "Invoice Vs Purchase Order" was one of six reports that had to
    be listed as absent rather than empty.

    Deliberately NOT folded into `Purchase`. A GRN is a receipt: it is raised
    against a supplier's own invoice, it carries their tax arithmetic, and posting
    it moves stock. An order moves nothing, is raised by us rather than read off
    their document, and exists for a while with no invoice anywhere. Two records,
    two lifecycles. `LREntry.purchase_order_id` is the first link built between
    the order and what arrives against it.

    THE LIFECYCLE IS THE POINT. draft → pending → confirmed, with cancelled
    reachable from any of them. Only a CONFIRMED order may be received against,
    which is what makes "no LR without a confirmed PO" expressible at all — see
    services/purchase_orders.confirm.

    Two routes in, exactly as LR has, and `entry_source` says which: `manual` is
    the form, `import` is a photographed order read by the extraction engine and
    then confirmed by a human. Both write this same row, so nothing downstream
    needs to know which way an order arrived.
    """
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True)
    #: Our own running number (PO-00001). Issued by services/numbering, so a
    #: second business can have its own prefix without editing Python.
    po_no = Column(String, index=True)
    po_date = Column(String)              # ISO, via services/dates
    # Which building placed the order and expects the goods. Scoped exactly like
    # a GRN or an LR entry — a buyer at Erode should not be reading Karur's book.
    # NULL on rows raised before workspaces, which stay visible everywhere.
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    #: What was typed before a supplier master row exists. An order is often
    #: raised against a name someone has only just been given, and refusing to
    #: record it until the master is filled in is how orders end up on paper.
    supplier_name = Column(String)
    # --- the header fields the buying office fills in ---
    # `company` is per-order rather than read from config.COMPANY_NAME because an
    # order names the entity that will be billed, and that is the one field here
    # that a second billing entity would need before anything else. It defaults to
    # the configured company and is almost always left alone.
    company = Column(String)
    brand = Column(String)
    item = Column(String)
    place = Column(String)
    transport = Column(String)
    agent = Column(String)
    purchaser = Column(String)
    #: Header discount, a percentage off the line total. Per-line rates stay the
    #: supplier's quoted figures so the order still reads like what was agreed.
    discount_pct = Column(Float)
    # --- totals, recomputed from the lines on every write ---
    subtotal = Column(Float, default=0.0)
    discount_amount = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    #: draft | pending | confirmed | cancelled
    status = Column(String, default="draft", index=True)
    entry_source = Column(String, default="manual")   # manual | import
    #: The photographed order this was read from, when it came in that way. Same
    #: relationship Purchase has to its invoice, and the reason a PO can be
    #: extracted by the existing engine without a second document table.
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    notes = Column(Text)
    confirmed_at = Column(DateTime, nullable=True)
    confirmed_by = Column(String)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    supplier = relationship("Supplier")
    warehouse = relationship("Warehouse")
    document = relationship("Document")
    lines = relationship("PurchaseOrderLine", back_populates="order",
                         cascade="all, delete-orphan",
                         order_by="PurchaseOrderLine.id")

    @property
    def is_open(self):
        """Whether this order can still be received against."""
        return self.status == "confirmed"


class PurchaseOrderLine(Base):
    """One row of an order — what we asked for, at the price we were quoted.

    `size` is kept as free text for the same reason `PurchaseLine.size` is: the
    trade writes a whole size mix in one cell ("30:2, 32:4, 34:4") and parsing it
    into columns at this point would impose a reading on a figure that is still
    only a request. It becomes a breakdown at the GRN, where the boxes are open.
    """
    __tablename__ = "purchase_order_lines"
    id = Column(Integer, primary_key=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), index=True)
    #: The description as the buyer wrote it — the "Particulars" column.
    particulars = Column(String)
    size = Column(String)
    qty = Column(Float)
    uom = Column(String)
    rate = Column(Float)
    amount = Column(Float)
    # Carried per line as well as on the header: an order that is all one brand
    # says so once at the top, and a mixed order says it per row. Blank here
    # means "the header's", resolved when the line is read.
    brand = Column(String)
    design_no = Column(String)
    hsn = Column(String)
    notes = Column(String)

    order = relationship("PurchaseOrder", back_populates="lines")


class Purchase(Base):
    """A purchase / GRN header, created from a confirmed Document extraction.
    status: draft -> posted. Posting writes stock movements (idempotent)."""
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    # Which building the goods were unloaded at. Chosen on the GRN screen and
    # settled at post — a draft may be raised before anybody knows, but stock
    # cannot land nowhere, so posting resolves a blank to the default warehouse.
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    grn_no = Column(String)
    invoice_number = Column(String)
    invoice_date = Column(String)
    taxable_total = Column(Float, default=0.0)
    tax_total = Column(Float, default=0.0)
    grand_total = Column(Float, default=0.0)
    status = Column(String, default="draft", index=True)   # draft | posted
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)

    document = relationship("Document")
    supplier = relationship("Supplier")
    warehouse = relationship("Warehouse")
    lines = relationship("PurchaseLine", back_populates="purchase",
                         cascade="all, delete-orphan", order_by="PurchaseLine.id")


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"))
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    barcode = Column(String)
    description = Column(String)
    hsn = Column(String)
    qty = Column(Float)
    uom = Column(String)
    rate = Column(Float)
    amount = Column(Float)
    # The size cell exactly as the supplier printed it — often the whole size
    # mix in one string, "30:2, 32:4, 34:4, 36:2". Kept verbatim rather than
    # parsed into the column, because it is the document's own words and the
    # reading of it is offered, never imposed: services/size_split.py turns it
    # into breakdown rows for a human to accept.
    size = Column(String)
    # Brand and design number, off the invoice's own columns. They were being
    # dropped at this boundary: typed on the review screen, gone by the GRN, and
    # the product created here was born with neither. Brand is not decoration —
    # an ESSA frock and a YUVA frock are different stock items — and the design
    # number is how this trade tells two frocks apart at all. See
    # `named_attrs`: together with the size they are what makes a line that names
    # itself match like a breakdown row rather than on its description alone.
    brand = Column(String)
    design_no = Column(String)
    # The rest of what a line can say it is, set on the GRN screen — style,
    # material, pattern, sleeve and type. Applied to the product at post, and
    # inherited by any breakdown row that leaves them blank, the same way the
    # line's prices are.
    style = Column(String)
    material = Column(String)
    pattern = Column(String)
    sleeve = Column(String)
    product_type = Column(String)
    # Retail, per piece. `mrp` is off the invoice — suppliers print it and it was
    # being dropped here, so an undivided line created a product with no MRP even
    # though the bill stated one. The other two are not on any bill: nobody prints
    # your shelf price for you. They are set on the invoice review screen
    # (MRP − sale discount % = sell price) and applied at post, so a product is
    # born priced instead of landing in Inventory with nothing on it.
    #
    # A breakdown row overrides all three per variant — a shop's price belongs to
    # "L / Red", not to the bundle the supplier billed — and inherits these when
    # it leaves them blank. See PurchaseLineSplit.
    mrp = Column(Float)
    sale_price = Column(Float)
    sale_discount_pct = Column(Float)                 # off MRP; see Product.sale_discount_pct
    is_new_product = Column(Boolean, default=False)   # matched vs newly created
    # category master classification chosen at GRN time. Set here, the product is
    # born mapped instead of landing "unmapped" for someone to fix in Inventory;
    # left blank, services/categorize.py still auto-maps from the description.
    category = Column(String)
    # What one of these IS — piece, pair, dozen. Chosen here, or left to the rule
    # that reads it off the description, and applied at post: the billed quantity
    # in `uom` is converted into this unit. `qty` / `uom` / `rate` above stay the
    # supplier's own figures, so the invoice keeps reconciling either way.
    unit_type = Column(String)

    purchase = relationship("Purchase", back_populates="lines")
    product = relationship("Product")
    splits = relationship("PurchaseLineSplit", back_populates="line",
                          cascade="all, delete-orphan", order_by="PurchaseLineSplit.id")
    shortages = relationship("GrnShortage", back_populates="line",
                             cascade="all, delete-orphan", order_by="GrnShortage.id")

    @property
    def split_qty(self):
        """Total quantity accounted for by the attribute breakdown (0 if none)."""
        return round(sum((s.qty or 0) for s in self.splits), 3)

    @property
    def is_split(self):
        return bool(self.splits)

    @property
    def received_qty(self):
        """What actually arrived — billed, less what was short, plus any excess.

        This, not `qty`, is the quantity that becomes stock. `qty` stays the
        supplier's figure so the invoice keeps reconciling against their document;
        the difference between the two is exactly the set of GrnShortage rows."""
        net = sum((s.signed_qty for s in self.shortages), 0.0)
        return round(float(self.qty or 0) + net, 3)


class ProductUnit(Base):
    """One physical piece, under a SKU that has many.

    Receiving 8 of ESSA-00008 makes ONE inventory record with a stock of 8 — the
    master, the valuation and the ledger all stay at SKU level, because that is
    what a weighted-average cost and a stock report are about. What this table adds
    is an *identity per piece*: ESSA-00008-001 … -008, each with its own QR, all
    pointing back at the same SKU.

    That is what lets a label be stuck on a garment rather than on a shelf edge. A
    scan of any child code resolves to the product (so every existing scan point
    keeps working), while the code itself says which of the eight it is — which is
    what a returns desk, a lost-piece query or a per-piece audit needs and a shared
    SKU code can never answer.

    Deliberately NOT stock rows: eight units under a stock of eight is the same
    eight garments, not sixteen. `status` tracks where a piece is; the quantity
    remains the ledger's answer.
    """
    __tablename__ = "product_units"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), index=True)
    code = Column(String, unique=True, index=True)     # ESSA-00008-003
    seq = Column(Integer)                              # 3 — its number within the SKU
    # which receipt put this piece on the floor, so a unit can be traced to its GRN
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=True, index=True)
    bundle_id = Column(Integer, ForeignKey("bundles.id"), nullable=True, index=True)
    status = Column(String, default="in_stock", index=True)   # in_stock | dispatched | sold | returned
    # printing is tracked so the screen can offer Print the first time and Reprint
    # afterwards — a label reprinted because it tore is worth telling apart from
    # one that was never printed
    print_count = Column(Integer, default=0)
    last_printed_at = Column(DateTime, nullable=True)
    last_printed_by = Column(String)
    created_at = Column(DateTime, default=now)

    product = relationship("Product", backref=backref(
        "units", cascade="all, delete-orphan", order_by="ProductUnit.seq"))


class Bundle(Base):
    """The carton a GRN line physically arrived as, and the code stuck on it.

    Deliberately **not** a Product and never a stock row. The 50 pieces inside are
    already counted as the items they became; counting the carton as well would
    double the warehouse. What a bundle owns is a *handling* identity — something
    to scan when putting it on a rack, finding it again, or opening it — plus the
    receipt it came from and where it is now.

    This is the first of the two labels an item can carry, and they answer
    different questions. The bundle label says "this box, from this GRN, holds 50
    women's t-shirts across four sizes" and is printed the moment the GRN posts,
    because the goods are on the floor and have to be put somewhere. The product
    label says "this garment is ESSA-00004, L, Red, MRP 899" and is printed later,
    when the box is opened and its contents tagged for sale. Printing item labels
    at GRN would mean tagging 50 loose garments that are about to sit in a carton
    for a fortnight; printing only item labels would leave the carton itself
    anonymous on the rack.

    Life: stored (labelled and put away) → opened → tagged (its items now carry
    their own labels, so the bundle stops being the unit anyone handles).
    """
    __tablename__ = "bundles"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True)      # ESSA-B-00001, what its QR resolves to
    purchase_id = Column(Integer, ForeignKey("purchases.id"), index=True)
    line_id = Column(Integer, ForeignKey("purchase_lines.id"), index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    # copied off the line at post, not read through it: a bundle label is a
    # historical record of what was received, and must still read correctly if the
    # GRN is later unposted, corrected and posted again
    description = Column(String)
    hsn = Column(String)
    uom = Column(String)
    qty = Column(Float)                                  # pieces in the carton
    item_count = Column(Integer, default=0)              # distinct products inside
    grn_no = Column(String)
    invoice_number = Column(String)
    location = Column(String, index=True)                # where it was put away
    status = Column(String, default="stored", index=True)  # stored | opened | tagged
    received_at = Column(DateTime, default=now)
    located_at = Column(DateTime, nullable=True)
    located_by = Column(String)
    opened_at = Column(DateTime, nullable=True)
    tagged_at = Column(DateTime, nullable=True)
    tagged_by = Column(String)
    created_at = Column(DateTime, default=now)

    purchase = relationship("Purchase")
    line = relationship("PurchaseLine", backref=backref("bundle", uselist=False))
    supplier = relationship("Supplier")

    @property
    def products(self):
        """What is inside — the items this carton's line produced."""
        if not self.line:
            return []
        if self.line.is_split:
            return [s.product for s in self.line.splits if s.product]
        return [self.line.product] if self.line.product else []


class PurchaseLineSplit(Base):
    """One VARIANT of a GRN line — the attribute breakdown of a billed bundle.

    Suppliers bill a bundle — "WOMEN T-SHIRT, 250 PCS" — and never print the mix,
    but the goods arrive as distinct items and inventory has to carry each on its
    own. So the warehouse breaks the line up here, at GRN, before anything reaches
    stock: 50 S in cotton, 50 M in cotton, 70 L printed, and so on. Posting turns
    every row into its own Product (own SKU + QR) with its own inward movement,
    which is what lets one variant be priced, scanned and dispatched by itself.

    A variant's identity is its whole attribute tuple (see SPLIT_ATTRS in
    services/inventory.py) — same description with a different colour or material
    is a different stock item, so re-buying an exact variant merges while anything
    new is created.

    The invoice line itself is left untouched — it stays the record of what the
    supplier actually billed, so invoice arithmetic and the payables side keep
    reconciling against the original document."""
    __tablename__ = "purchase_line_splits"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("purchase_lines.id"))
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    # --- the attributes that make this row a distinct stock item ---
    size = Column(String)
    color = Column(String)
    material = Column(String)
    pattern = Column(String)
    fit = Column(String)
    product_type = Column(String)
    design_no = Column(String)
    # the rest of the stock master's attribute set — see Product.brand
    brand = Column(String)
    style = Column(String)
    sleeve = Column(String)
    # not part of identity — a classification the product carries (falls back to
    # the line's category, then to auto-mapping from the description)
    category = Column(String)
    # nor is this: a variant is the same KIND of thing as its line, so it inherits
    # the line's unit type unless someone says otherwise (a bundle of assorted
    # goods where one row is sold in pairs and another by the piece)
    unit_type = Column(String)
    qty = Column(Float, default=0.0)
    rate = Column(Float)                 # unit cost (defaults to the line's rate)
    mrp = Column(Float)                  # retail prices are per variant, not per bundle
    sale_price = Column(Float)
    sale_discount_pct = Column(Float)    # off MRP; see Product.sale_discount_pct
    # a QR/barcode scanned on the floor pins this variant to an existing product
    # instead of leaving it to the description + attribute match
    code = Column(String)
    is_new_product = Column(Boolean, default=False)   # set when posting created it
    created_at = Column(DateTime, default=now)

    line = relationship("PurchaseLine", back_populates="splits")
    product = relationship("Product")

    @property
    def variant_label(self):
        """The attributes as one readable string — "L · Red · Cotton"."""
        # brand first: on a rack it is what the eye reaches for before the size
        bits = [self.brand, self.size, self.color, self.material, self.pattern,
                self.fit, self.style, self.sleeve, self.product_type, self.design_no]
        return " · ".join(str(b) for b in bits if b)

    @property
    def effective_rate(self):
        """Unit cost for this size: its own if set, otherwise what was billed."""
        if self.rate is not None:
            return self.rate
        return self.line.rate if self.line else None

    @property
    def amount(self):
        return round((self.qty or 0) * (self.effective_rate or 0), 2)


class GrnShortage(Base):
    """What the invoice billed and the cartons did not deliver.

    A supplier bills 50 pieces; 40 come out of the boxes. Until now the receiving
    screen had only two answers, and both were lies: invent ten pieces so the
    breakdown balances (stock gains units that do not exist), or leave the GRN
    unpostable forever. This is the third and true answer — *ten were short* — and
    it is recorded here, on the floor, at the moment the boxes are opened, by the
    only person who can know it.

    That timing is the whole point, and it is why this belongs to the Receive flow
    and not to Inventory. Once a GRN posts, the difference has already been
    absorbed: the stock figure is 40, the invoice says 50, and nothing on the
    system remembers why. Recorded *before* posting, the gap is a document — with
    a quantity, a reason and a name against it — that the debit note is later
    built from without anyone re-counting anything.

    `kind` says what happened to the units, and the only thing that separates the
    three is which side of the count they land on:

      * **short** — never arrived. Never becomes stock.
      * **damaged** — arrived, but rejected at the dock and set aside for the
        supplier. Also never becomes stock: taking it in and writing it off again
        would put goods we refused into the valuation for as long as the paperwork
        takes. Damage found *later*, in stock we already accepted, is a purchase
        return — that is the existing module, and it reverses stock because there
        is stock to reverse.
      * **excess** — more arrived than was billed. The mirror case, and it *does*
        become stock: the goods are on the floor whatever the invoice says.

    So `PurchaseLine.received_qty` = billed − short − damaged + excess, and it is
    that figure the attribute breakdown has to add up to and that posting turns
    into stock. The invoice line is left alone, exactly as it is for a breakdown:
    it stays the record of what the supplier billed, so the payables side keeps
    reconciling against their own document.

    Money is deliberately absent from this table. A shortage is a *fact about a
    count*; what it is worth is a fact about the GRN, derived from the line rate
    whenever it is asked for (services/shortages.unit_cost). Freezing a rate here
    would be a second place for the same number to live, and the debit note —
    which is where it becomes financial — re-derives from the GRN anyway.

    `waived` is the one decision a human makes afterwards: the supplier is sending
    the rest, or it is not worth the paperwork. A waived shortage stays on the
    record and stops being offered for claim; a claimed one is answered by the
    posted debit note that references it."""
    __tablename__ = "grn_shortages"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id"), index=True)
    line_id = Column(Integer, ForeignKey("purchase_lines.id"), index=True)
    kind = Column(String, default="short", index=True)   # short | damaged | excess
    qty = Column(Float, default=0.0)                     # always positive
    # Which variant it was, when the person opening the carton could tell — free
    # text, not a link to a breakdown row. The breakdown is cleared and rebuilt
    # every time it is edited, so a foreign key into it would dangle; and the
    # arithmetic is at line level regardless, because the supplier billed a bundle
    # and never said what was in it.
    variant = Column(String)
    reason = Column(String)                              # torn / wet / not in box / …
    note = Column(Text)
    recorded_by = Column(String)
    recorded_at = Column(DateTime, default=now)
    # accepted rather than claimed — the supplier is re-sending, or it is too small
    # to raise a debit note for. Kept on the record either way.
    waived = Column(Boolean, default=False, index=True)
    waived_reason = Column(String)
    waived_by = Column(String)
    waived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)

    purchase = relationship("Purchase")
    line = relationship("PurchaseLine", back_populates="shortages")

    #: kinds that reduce what was received — the ones a supplier can be debited for
    CLAIMABLE_KINDS = ("short", "damaged")

    @property
    def signed_qty(self):
        """This row's effect on the received count: negative for goods that never
        made it in, positive for goods that turned up over and above the bill."""
        q = float(self.qty or 0)
        return q if self.kind == "excess" else -q

    @property
    def claimable(self):
        """Whether this row can be put on a debit note at all. Excess is the
        mirror case — the supplier under-billed us, which is their credit to raise
        and not ours to debit."""
        return self.kind in self.CLAIMABLE_KINDS


class AuditSession(Base):
    """One stock-count run: somebody walking a rack with a phone.

    THE SESSION IS THE POINT, and it is why this is a table rather than a screen
    that calls `/lookup` in a loop. A count is not a series of unrelated
    questions — it is one piece of work with a beginning, a set of findings and
    an end, and the questions it answers ("what did we check", "what was missing
    on the 3rd", "who counted this shelf") can only be asked of something that
    was recorded as a whole.

    Stock is NOT adjusted here, deliberately. A scan says what the shelf holds
    against what the system believes; deciding to change the ledger is a separate
    act with its own audit trail (`inventory.adjust_stock`, which writes a real
    movement). Letting a count silently correct the books would make the one
    record that is supposed to be independent of them derived from them.
    """
    __tablename__ = "audit_sessions"
    id = Column(Integer, primary_key=True)
    code = Column(String, index=True)              # AUD-00001
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    status = Column(String, default="open", index=True)     # open | closed
    note = Column(String)
    started_at = Column(DateTime, default=now)
    started_by = Column(String)
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(String)

    warehouse = relationship("Warehouse")
    scans = relationship("AuditScan", back_populates="session",
                         cascade="all, delete-orphan", order_by="AuditScan.id")


class AuditScan(Base):
    """One tag read during a count, and what the system said about it.

    Everything here is recorded AT THE MOMENT OF THE SCAN and never refreshed.
    `stock_qty` is what the books said when the shelf was looked at; re-deriving
    it later would turn the count into a report on today's figures and destroy
    the only thing it was for. Same reason `product_name` is copied rather than
    joined: a product renamed or deleted afterwards must not rewrite what the
    counter saw.

    ONE ROW PER DISTINCT CODE PER SESSION. Scanning the same label again does not
    add a row — it bumps `times_seen` and is reported back as a duplicate, which
    is what the screen needs to say "you have already counted this" without
    quietly double-counting the shelf.
    """
    __tablename__ = "audit_scans"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("audit_sessions.id"), index=True)
    #: exactly what came off the label — a product QR, a piece code, a barcode
    code = Column(String, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    #: available | not_available | unknown
    #: `unknown` is a code that resolved to nothing at all, which is a different
    #: finding from a product that resolved and has no stock — the first is a
    #: master-data problem, the second is a shelf problem, and collapsing them
    #: would send someone to fix the wrong thing.
    result = Column(String, index=True)
    product_name = Column(String)
    sku = Column(String)
    stock_qty = Column(Float)
    location = Column(String)
    times_seen = Column(Integer, default=1)
    scanned_at = Column(DateTime, default=now)
    last_seen_at = Column(DateTime, default=now)
    scanned_by = Column(String)

    session = relationship("AuditSession", back_populates="scans")
    product = relationship("Product")


class PhysicalAudit(Base):
    """A counted stocktake of a warehouse: how many are there, against how many
    the books say — and, once somebody approves it, the correction.

    THE SECOND AUDIT, AND DELIBERATELY NOT THE FIRST ONE AGAIN. `AuditSession`
    above asks *is this item on the shelf* — one row per tag read, a presence
    check somebody walks a rack with a phone to answer. This asks *how many*, and
    that is a different document: it is opened over a defined SET of stock (the
    filters on the left of the screen, snapshotted in `scope`), every line
    carries a figure whether or not anybody has been to look at it yet, and the
    gap between counted and expected is the finding. Folding the two together
    would mean one of the two questions could no longer be asked.

    A COUNT IS STILL NOT AN ADJUSTMENT. Counting writes nothing to the ledger.
    The variance becomes real stock only through `apply` — after the count has
    been completed, reviewed and approved — and then only as StockMovement rows
    carrying `change_reason`, never as a silent write of a balance. That is the
    same rule `AuditSession` keeps and the same one the shop's own audit keeps
    (Textile Retail Shop/app/audits.py); this module is the one that finally has
    a way THROUGH it, rather than an exception to it.

    `scope` is the filter set the count was opened over, kept as JSON. A count of
    "MENS / BRAND=YUVA / SIZE=XL" that later reads as a count of the whole
    warehouse would report every other rack as missing, so what was being counted
    is part of the record rather than something to reconstruct afterwards.
    """
    __tablename__ = "physical_audits"
    id = Column(Integer, primary_key=True)
    code = Column(String, index=True)                  # PSA-00001
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    #: counting | completed | reviewed | approved | cancelled
    status = Column(String, default="counting", index=True)
    note = Column(String)
    #: Why the books are being corrected — the box at the foot of the screen. It
    #: rides onto every movement `apply` writes, because an adjustment nobody can
    #: explain six months later is an adjustment nobody can defend.
    change_reason = Column(String)
    #: What was being counted: {warehouse, supplier, brand, size, …}. See above.
    scope = Column(JSON)
    started_at = Column(DateTime, default=now)
    started_by = Column(String)
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(String)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String)
    #: When the variance was written into the ledger. Set once, and checked
    #: before writing, so an approved count cannot correct the same shelf twice.
    applied_at = Column(DateTime, nullable=True)
    applied_by = Column(String)

    warehouse = relationship("Warehouse")
    lines = relationship("PhysicalAuditLine", back_populates="audit",
                         cascade="all, delete-orphan",
                         order_by="PhysicalAuditLine.id")

    @property
    def is_open(self):
        return self.status == "counting"


class PhysicalAuditLine(Base):
    """One stock item on a count: what the books said, and what was on the shelf.

    EVERY DESCRIPTIVE FIELD IS COPIED, NOT JOINED, for the reason `AuditScan`
    gives above and one more: this grid is read across, so the brand, size and
    design a counter was looking at when they typed a figure have to be the ones
    that print on the variance report months later. A product re-detailed in the
    meantime must not rewrite what somebody counted.

    `counted_qty` IS NULL UNTIL SOMEBODY LOOKS. Null and zero are the two
    findings a count must never confuse — "nobody has been to this rack" and "the
    rack is empty" — and only the first may be left out of an adjustment. A
    single quantity box cannot say the first, which is why `clear_count` exists
    as its own action rather than as typing a blank.

    `uan` is this system's own unique article number, which is `Product.sku`
    (ESSA-00001). The reference ERP's grid shows it beside Barcode because the
    two are different identifiers with different owners: the barcode is the
    supplier's, printed on the garment before it ever reached us, and is
    routinely blank or shared; the UAN is ours and is never either.
    """
    __tablename__ = "physical_audit_lines"
    id = Column(Integer, primary_key=True)
    audit_id = Column(Integer, ForeignKey("physical_audits.id"), index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)

    # --- identity, as it stood when the line was made ---
    uan = Column(String, index=True)               # our SKU, ESSA-00001
    barcode = Column(String, index=True)           # the supplier's, if there is one
    description = Column(String)
    section = Column(String)                       # category_section: MENS | LADIES | …
    category = Column(String)
    brand = Column(String)
    size = Column(String)
    design_no = Column(String)
    color = Column(String)

    # --- the books, frozen when the line appeared ---
    system_qty = Column(Float, default=0.0)        # "Stock" on the grid
    cost_price = Column(Float)                     # weighted-average cost, here
    net_price = Column(Float)                      # what it sells for

    # --- what was found ---
    #: NULL until counted. See the class note — this is the load-bearing null.
    counted_qty = Column(Float, nullable=True)
    #: How many times a tag for this line was read or uploaded. A count typed by
    #: hand leaves it at zero, which is itself worth seeing on a variance report.
    scans = Column(Integer, default=0)
    #: opened | scanned | uploaded | added — how this line got onto the count. A
    #: line that arrived by SCAN and not from the filtered set is stock found
    #: where the filters said it would not be, and that is a finding in itself.
    source = Column(String, default="opened")
    counted_at = Column(DateTime, nullable=True)
    counted_by = Column(String)
    note = Column(String)

    audit = relationship("PhysicalAudit", back_populates="lines")
    product = relationship("Product")

    @property
    def difference(self):
        """Counted minus the books. None while nobody has looked."""
        if self.counted_qty is None:
            return None
        return round(float(self.counted_qty) - float(self.system_qty or 0), 3)

    @property
    def line_status(self):
        """pending · matched · short · excess — what the row's chip says."""
        gap = self.difference
        if gap is None:
            return "pending"
        if gap == 0:
            return "matched"
        return "short" if gap < 0 else "excess"


class StockMovement(Base):
    """Append-only ledger. Every stock change is a row with the running balance,
    so inventory is always reconstructable and auditable.

    `warehouse_id` says WHERE the change happened. It is nullable because every
    row written before warehouses held stock has no answer of its own — those are
    backfilled to the one warehouse that existed (services/stock_locations.
    backfill), which is not a guess: the whole app was built on there being one.

    `balance_after` is the balance AT THAT WAREHOUSE, not the company total. The
    backfill leaves the historic figures correct precisely because a single
    warehouse's balance and the company's were the same number.
    """
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    qty_delta = Column(Float)                 # +inward / -outward
    kind = Column(String)                     # inward | outward | adjustment
    ref_type = Column(String)                 # purchase | ...
    ref_id = Column(Integer)
    rate = Column(Float)                      # unit cost for this movement
    balance_after = Column(Float)
    note = Column(String)
    created_at = Column(DateTime, default=now)

    product = relationship("Product", back_populates="movements")


class StockBalance(Base):
    """How much of one product stands in one warehouse, and what it cost.

    Derivable from the ledger by summing it, and kept anyway — the alternative is
    a GROUP BY over every movement ever written each time a screen asks what is
    on a shelf, and the screens ask constantly (the picker checks before every
    dispatch line). The ledger stays the source of truth: this row is only ever
    written by services/stock_locations, alongside the movement that changed it,
    and `rebuild` recomputes the whole table from the ledger when the two are
    ever doubted.

    `avg_cost` is per warehouse because valuation is per warehouse. The same
    shirt bought at ₹180 into Erode and ₹200 into Karur is worth different money
    in each building, and a warehouse-wise stock valuation that used one blended
    figure would report a value neither warehouse holds.

    Product.stock_qty and Product.avg_cost remain the COMPANY totals, rolled up
    from these rows. That is what keeps every screen written before this table
    existed reading the same numbers it always did.
    """
    __tablename__ = "stock_balances"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), index=True, nullable=False)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True, nullable=False)
    qty = Column(Float, default=0.0)
    avg_cost = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=now, onupdate=now)

    product = relationship("Product", back_populates="balances")
    warehouse = relationship("Warehouse")

    # One row per product per building. Without this, two concurrent receipts
    # into the same warehouse each insert their own row and the stock figure
    # becomes whichever of them a later query happens to read first.
    __table_args__ = (UniqueConstraint("product_id", "warehouse_id",
                                       name="uq_balance_product_warehouse"),)

    @property
    def value(self):
        return round((self.qty or 0) * (self.avg_cost or 0), 2)


# ============================================================================
#  Stock Outward / Stock Inward  (warehouse → destination, and its acceptance)
# ============================================================================
class StockOutward(Base):
    """A dispatch / inter-location transfer out of the warehouse, and the record
    the destination accepts it on.

    One row, two screens, because they are two ends of ONE movement: Stock
    Outward is the warehouse packing and sending it (posting decrements stock via
    a negative StockMovement, kind='outward'), Stock Inward is the destination
    counting what turned up and accepting it. Splitting them into two documents
    would mean reconciling the pair; keeping the sent qty and the accepted qty on
    the same line makes a short delivery visible by subtraction.

    draft → posted (dispatched, stock out) → received (counted and accepted).

    WHERE FROM AND WHERE TO used to be two strings. They are now rows, and which
    of the two "to" columns is set decides what KIND of movement this is:

      * `to_warehouse_id` — a warehouse-to-warehouse TRANSFER. Stock leaves the
        source at post and ARRIVES at the destination when it is received, so the
        goods exist in exactly one building at a time and are visibly in transit
        in between. This is what the transfer screen raises.
      * `to_store_id` — a dispatch to a shop. Stock leaves the warehouse and does
        not come back into this ledger: the till's own database owns a store's
        stock and reads these dispatches to build it (see pos_mount and the
        shop's app/transfers). Recording an arrival here as well would put the
        same garment on two systems' shelves.
      * neither — a customer or a destination nobody has registered yet, exactly
        as before.

    `to_destination` is still written, always, with the chosen place's name. The
    shop matches by name and every row raised before this existed only has the
    string, so it stays the thing history is readable from."""
    __tablename__ = "stock_outwards"
    id = Column(Integer, primary_key=True)
    code = Column(String)                       # package / order code
    date = Column(String)
    from_company = Column(String, default="Essa Garments Private Limited")
    from_location = Column(String, default="WAREHOUSE")
    from_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    to_destination = Column(String)             # store / customer
    to_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    to_store_id = Column(Integer, ForeignKey("stores.id"), index=True)
    # A store sending goods BACK (Store → Transfer Goods): the branch it came
    # from. Raised by the shop with the stock already off its shelves, so it
    # arrives here posted, and the warehouse's Stock Inward counts it in.
    from_store_id = Column(Integer, ForeignKey("stores.id"), index=True)
    packed_by = Column(String)
    received_by = Column(String)                # who accepted it at the far end
    received_date = Column(String)              # the date they wrote on the note
    received_at = Column(DateTime, nullable=True)   # when it was keyed in
    status = Column(String, default="draft", index=True)   # draft | posted | received
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)

    lines = relationship("StockOutwardLine", back_populates="outward",
                         cascade="all, delete-orphan", order_by="StockOutwardLine.id")
    from_warehouse = relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = relationship("Warehouse", foreign_keys=[to_warehouse_id])
    to_store = relationship("Store", foreign_keys=[to_store_id])
    from_store = relationship("Store", foreign_keys=[from_store_id])

    @property
    def is_transfer(self):
        """Whether the far end is a warehouse of ours, i.e. whether receiving it
        puts stock back into this ledger."""
        return self.to_warehouse_id is not None

    @property
    def total_qty(self):
        return sum((l.qty or 0) for l in self.lines)

    @property
    def total_accepted(self):
        """What the destination took in. Unreceived lines count as nothing
        accepted YET — reading them as fully accepted would show a dispatch
        nobody has looked at as if it had been checked and agreed."""
        if self.status != "received":
            return 0.0
        return sum((l.accepted_qty if l.accepted_qty is not None else (l.qty or 0))
                   for l in self.lines)

    @property
    def shortfall(self):
        """Sent minus accepted — the transfer discrepancy, once received."""
        if self.status != "received":
            return 0.0
        return round(self.total_qty - self.total_accepted, 3)


class StockOutwardLine(Base):
    __tablename__ = "stock_outward_lines"
    id = Column(Integer, primary_key=True)
    outward_id = Column(Integer, ForeignKey("stock_outwards.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    barcode = Column(String)
    description = Column(String)
    qty = Column(Float)                         # sent / transferred
    accepted_qty = Column(Float)               # accepted at destination (defaults to qty)
    rate = Column(Float)                        # cost at dispatch

    outward = relationship("StockOutward", back_populates="lines")
    product = relationship("Product")

    @property
    def short_qty(self):
        """How many of this line failed to arrive (0 until it is received)."""
        if self.accepted_qty is None:
            return 0.0
        return round(float(self.qty or 0) - float(self.accepted_qty or 0), 3)


# ============================================================================
#  Supplier Payments  (Finance / Supplier Payment — accounts payable)
# ============================================================================
class Payment(Base):
    """A supplier payment receipt settling one or more purchase invoices, with
    optional discount, TDS and debit-note adjustments (matching the app)."""
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    receipt_no = Column(String, index=True)     # ESP#####
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    date = Column(String)
    mode = Column(String, default="NEFT")       # NEFT/RTGS | Cash | Cheque
    bank = Column(String)
    cheque_no = Column(String)
    cheque_date = Column(String)
    ref_no = Column(String)
    remarks = Column(String)
    gross_amount = Column(Float, default=0.0)   # sum of selected invoice totals
    discount_total = Column(Float, default=0.0)
    tds_total = Column(Float, default=0.0)
    debit_adjust_total = Column(Float, default=0.0)
    paid_amount = Column(Float, default=0.0)    # actual cash out
    created_at = Column(DateTime, default=now)

    supplier = relationship("Supplier")
    allocations = relationship("PaymentAllocation", back_populates="payment",
                               cascade="all, delete-orphan")


class PaymentAllocation(Base):
    """How a payment is split across the invoices it settles."""
    __tablename__ = "payment_allocations"
    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"))
    purchase_id = Column(Integer, ForeignKey("purchases.id"))
    invoice_number = Column(String)
    invoice_total = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    tds = Column(Float, default=0.0)
    debit_adjust = Column(Float, default=0.0)
    settled = Column(Float, default=0.0)        # discount+tds+debit+cash applied to this invoice

    payment = relationship("Payment", back_populates="allocations")
    purchase = relationship("Purchase")


# ============================================================================
#  Purchase Return  (Warehouse / Purchase Return — debit note to supplier)
# ============================================================================
class PurchaseReturn(Base):
    """Goods returned to a supplier against a reference purchase invoice.
    Posting reverses stock (negative StockMovement, kind='return') and raises a
    debit note that reduces the supplier's payable for that invoice."""
    __tablename__ = "purchase_returns"
    id = Column(Integer, primary_key=True)
    code = Column(String)                       # PR-#####  (debit note no)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    purchase_id = Column(Integer, ForeignKey("purchases.id"), nullable=True)  # reference invoice
    invoice_number = Column(String)             # reference invoice no
    date = Column(String)
    reason = Column(String)
    taxable_total = Column(Float, default=0.0)
    tax_total = Column(Float, default=0.0)
    total = Column(Float, default=0.0)          # debit note value (reduces payable)
    status = Column(String, default="draft", index=True)   # draft | posted
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)

    supplier = relationship("Supplier")
    purchase = relationship("Purchase")
    lines = relationship("PurchaseReturnLine", back_populates="ret",
                         cascade="all, delete-orphan", order_by="PurchaseReturnLine.id")


class PurchaseReturnLine(Base):
    """One product going back to the supplier, valued at what we PAID for it.

    `rate` is the received price — the GRN cost of this exact item: the rate on
    the invoice line, or, when the billed bundle was broken down, the rate of the
    variant being returned. It is never the sale price or the MRP: a debit note
    settles a supplier account, so it can only carry what that supplier charged
    us. services/returns.grn_cost() is the single place that decides it, and it is
    re-derived from the GRN at post time so a stale draft cannot slip through.

    `purchase_line_id` / `split_id` record WHICH received row this line is
    returning, which is what makes that re-derivation exact — and what lets the
    screen show the batch and how many of it were bought in the first place.

    `shortage_id` marks the other kind of line entirely: goods the supplier billed
    and never delivered (see GrnShortage). Financially it settles the same way —
    it reduces what we owe on that invoice at the same GRN rate — but it must NOT
    move stock, because the units it debits never entered stock in the first
    place. `post()` reads this column for exactly that reason."""
    __tablename__ = "purchase_return_lines"
    id = Column(Integer, primary_key=True)
    return_id = Column(Integer, ForeignKey("purchase_returns.id"))
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    purchase_line_id = Column(Integer, ForeignKey("purchase_lines.id"), nullable=True)
    split_id = Column(Integer, ForeignKey("purchase_line_splits.id"), nullable=True)
    # set when this line claims a receiving shortage instead of returning goods
    shortage_id = Column(Integer, ForeignKey("grn_shortages.id"), nullable=True)
    barcode = Column(String)
    description = Column(String)
    hsn = Column(String)
    uom = Column(String)
    qty = Column(Float)                         # qty returned
    rate = Column(Float)                        # GRN / received cost per unit
    amount = Column(Float)

    ret = relationship("PurchaseReturn", back_populates="lines")
    product = relationship("Product")
    purchase_line = relationship("PurchaseLine")
    split = relationship("PurchaseLineSplit")
    shortage = relationship("GrnShortage")

    @property
    def is_shortage_claim(self):
        """True when this line debits goods that were never received, so posting
        it must value the claim without touching the stock ledger."""
        return self.shortage_id is not None


# ============================================================================
#  Businesses — the legal entities this platform runs
# ============================================================================
#  Until now "the company" was two environment variables, ESSA_COMPANY_NAME and
#  ESSA_COMPANY_GSTIN. That is exactly right for one company and wrong the moment
#  there are two: Essa and Taqua carry DIFFERENT GSTINs, file separate returns,
#  and a month's trading that cannot be split between them is a month's work to
#  unpick. The till already knew this — the shop has carried a `companies` table
#  with a GSTIN per row, and stamps `Invoice.company_id`, for as long as it has
#  had a picker. The warehouse is the half that did not.
#
#  So a Business sits above Warehouse:
#
#      BUSINESS  ── warehouses ── stores ── POS terminals
#
#  A CATALOGUE is a different axis and is deliberately not folded in here: it is
#  what a warehouse TRADES IN (garments, silks) and owns the categories and
#  attributes. One business may run several lines; one line may be run by several
#  businesses. Collapsing the two would make "sells sarees" and "files its own
#  GST return" the same fact, and they are not.
# ============================================================================
class Business(Base):
    """A legal entity. Whose GSTIN goes on the bill, and whose return it lands in."""
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True)
    # The internal identifier, generated here and never typed by anybody. `code`
    # below is the one people use and may change; this one cannot, which is what
    # makes it safe for another system to hold onto.
    uuid = Column(String, unique=True, index=True, default=lambda: str(_uuid.uuid4()))
    #: What people type and see — ESSA, TAQUA. Unique, short, upper-case.
    code = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)                  # Essa
    legal_name = Column(String)                            # Essa Garments Private Limited
    gstin = Column(String, index=True)
    pan = Column(String)

    address = Column(String)
    city = Column(String)
    state = Column(String)
    #: The GST state code — 33 for Tamil Nadu. It decides whether a sale is
    #: inter-state, so it is a field and not part of the address blob.
    state_code = Column(String)
    country = Column(String, default="India")
    pincode = Column(String)
    phone = Column(String)
    email = Column(String)

    currency = Column(String, default="INR")
    timezone = Column(String, default="Asia/Kolkata")
    #: Which month the financial year opens in — April in India. Document numbers
    #: that carry a year (GRN-ES-2627-0001) are stamped from this, so a company
    #: on a different year does not have to be a code change.
    fy_start_month = Column(Integer, default=4)

    is_default = Column(Boolean, default=False, index=True)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    warehouses = relationship("Warehouse", back_populates="business")


class NumberSequence(Base):
    """How one kind of document numbers itself, here.

    Every number this app issues used to be a format string in the code:
    `f"ESSA-{n:05d}"` for a SKU (in TWO files), `f"GRN-{year}-"`, `f"LRE-{n:05d}"`,
    `f"OUT-{n:05d}"`, `f"PR-{n:05d}"`. A second business cannot have GRN-ES-2627-0001
    while the first has GRN-TQ-2627-0001 without editing Python, which means it
    cannot happen at all on a warehouse PC.

    RESOLUTION IS MOST-SPECIFIC-FIRST: a row for this warehouse beats a row for
    its business, which beats a row for everybody. So a company can set its house
    style once and one warehouse can still differ.

    THE COUNTER IS A HINT, NOT THE TRUTH. `next_number` is where the search
    starts; services/numbering then steps over anything already issued before
    handing a number out. That rule is inherited deliberately from the GRN
    numbering it replaces, whose docstring says why: a counter has to be kept in
    step with rows that get deleted, and the first time the two disagree it hands
    out a number that already exists — on the one field people quote to each
    other when a delivery is queried.
    """
    __tablename__ = "number_sequences"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    #: NULL = every warehouse of this business. Set = this one only.
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    #: purchase_order | grn | lr | invoice | transfer | debit_note | adjustment |
    #: pos_invoice | sku
    doc = Column(String, index=True, nullable=False)

    prefix = Column(String, default="")                    # "GRN-ES-"
    #: Whether the financial year goes in the number, and how it is written:
    #: "short" → 2627, "long" → 2026-27, "calendar" → 2026.
    use_year = Column(Boolean, default=False)
    year_format = Column(String, default="short")
    padding = Column(Integer, default=5)                   # 0001 vs 00001
    separator = Column(String, default="-")
    next_number = Column(Integer, default=1)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    business = relationship("Business")
    warehouse = relationship("Warehouse")

    # One rule per document per place. Two would make "which format applies"
    # depend on row order, and the answer would change when either was edited.
    __table_args__ = (UniqueConstraint("business_id", "warehouse_id", "doc",
                                       name="uq_sequence_scope_doc"),)


# ============================================================================
#  Catalogues — what a warehouse actually trades in
# ============================================================================
#  A garment warehouse and a silk warehouse run the SAME process — LR entry,
#  invoice entry, GRN, inventory, labels, outward, inward, POS — over completely
#  different goods. Essa receives t-shirts that have a sleeve and a fit; Taqua
#  receives sarees that have a weave and a zari border and no sleeve at all.
#
#  Before this, there was one category master (686 rows, every one of them a
#  garment) and one attribute vocabulary, and both were global. Pointing a silk
#  warehouse at them would have offered its receiving clerk ESSA KIDS-TRUNK as a
#  category for a Kanchipuram saree, and no way at all to record what it actually
#  is. Tagging every master row with a WAREHOUSE instead would have been worse in
#  the other direction: Essa, Palakkad and Madurai all stock garments, so the same
#  686 rows would exist three times and drift apart the first time one was edited.
#
#  So the unit of separation is the BUSINESS LINE, not the building. A catalogue
#  owns the categories, the attributes and the option lists; a warehouse points at
#  one; and any number of warehouses can share it. A master row with no catalogue
#  is SHARED by all of them — which is what payment modes and transporters are,
#  and what every row that predates this is treated as until somebody says
#  otherwise.
# ============================================================================
class Catalogue(Base):
    """A line of business — the goods a warehouse deals in, and their vocabulary."""
    __tablename__ = "catalogues"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True, nullable=False)   # GARMENTS | SILKS
    name = Column(String, nullable=False)
    description = Column(String)
    # The one a warehouse falls back to when nothing has been said, and the one
    # every pre-existing row belongs to. Exactly one is default; see
    # services/catalogues.
    is_default = Column(Boolean, default=False, index=True)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    warehouses = relationship("Warehouse", back_populates="catalogue")
    # Cascaded because these two ARE the catalogue — its attributes and their
    # values have no meaning without it, and nothing else points at them. Without
    # the cascade, deleting a line that has been set up at all fails on a foreign
    # key, which is a 500 on the one path the router had already checked was safe.
    # Categories, products and warehouses are deliberately NOT cascaded: those
    # are real records that outlive the classification, and the router refuses
    # the delete while any of them exist.
    attributes = relationship("CatalogueAttribute", back_populates="catalogue",
                              cascade="all, delete-orphan")
    options = relationship("AttributeOption", back_populates="catalogue",
                           cascade="all, delete-orphan")


class CatalogueAttribute(Base):
    """Which attributes this catalogue records against an item, and what to call them.

    Ten of them already have a column on Product — colour, size, pattern, fit,
    product type, material, design no, brand, style, sleeve — because those are
    what the garment stock master has always kept. A catalogue that uses one of
    those names it in `column`, and the value goes to the real column: the
    matching, the splits, the labels and every screen written before catalogues
    existed keep working unchanged.

    An attribute a catalogue needs and Product has no column for — a weave, a zari
    border, a pallu — has `column` empty and is stored in `Product.attrs`. That is
    the difference between adding a business line and adding a migration each time
    one is added.

    `identity` says whether two items differing only in this attribute are two
    stock items or one. Size and colour are identity; a note is not. It is what
    inventory.match_product compares on, so getting it wrong merges goods that
    should be separate — hence it is stated, not guessed.
    """
    __tablename__ = "catalogue_attributes"
    id = Column(Integer, primary_key=True)
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True, nullable=False)
    key = Column(String, nullable=False)          # color | weave | zari | …
    label = Column(String)                        # what the screen calls it
    column = Column(String)                       # Product column, when it has one
    identity = Column(Boolean, default=True)
    sort = Column(Integer, default=0)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    catalogue = relationship("Catalogue", back_populates="attributes")

    __table_args__ = (UniqueConstraint("catalogue_id", "key",
                                       name="uq_cat_attr_key"),)


class AttributeOption(Base):
    """One value offered in one attribute's dropdown, for one catalogue.

    These used to be a JSON file shipped beside the code, which was right while
    there was one vocabulary and it came out of Essa's own stock master. It stops
    being right the moment a second business line has to be able to build its own
    from an empty list, through the app, without a release."""
    __tablename__ = "attribute_options"
    id = Column(Integer, primary_key=True)
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True, nullable=False)
    attr = Column(String, index=True, nullable=False)
    value = Column(String, nullable=False)
    sort = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    catalogue = relationship("Catalogue", back_populates="options")

    __table_args__ = (UniqueConstraint("catalogue_id", "attr", "value",
                                       name="uq_attr_option"),)


# ============================================================================
#  Masters — product categories (from the GRN Excel), agents, transporters
# ============================================================================
class Category(Base):
    """Product category master imported from GRN PRODUCT DETAILS.xlsx
    (sections OVERALL / KIDS / LADIES / MENS). Used to classify products.

    `catalogue_id` is which business line owns this category. NULL means shared
    by all of them — which is what every row imported before catalogues existed
    is NOT: those are moved to the garment catalogue on the first run, because
    ESSA KIDS-TRUNK is a garment category and offering it to a silk warehouse is
    the exact problem catalogues exist to solve."""
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True)
    section = Column(String, index=True)      # sheet: OVERALL | KIDS | LADIES | MENS
    name = Column(String, index=True)         # e.g. KIDS-BABASUIT
    created_at = Column(DateTime, default=now)


class CategoryAlias(Base):
    """A supplier's wording, and the category a human said it means.

    The rules in services/categorize.py cover the wordings we thought of; suppliers
    keep inventing more, and no list written up front ever finishes. So the moment
    someone sets a category on a GRN line by hand, that correction is kept and the
    same wording maps itself next time — the engine gets better with use instead of
    waiting on a developer to add another synonym.

    The key is the CANONICAL form of the description (gender words replaced by the
    master's own vocabulary, sizes and noise stripped), not the raw text, so one
    correction covers every spelling that canonicalises the same way: teach it
    "Ladies Tee" and "Women's Tee" is taught too.

    `source` records who said so, and `hits` how often it has been used — together
    they are the evidence for reviewing a mapping that turns out to be wrong.

    ONE WORDING CAN MEAN TWO THINGS. "Plain cotton" is a category in the garment
    master and a different one in a silk master, so an alias belongs to a
    catalogue. `key` is unique across the whole table and always has been, and
    that column cannot be widened into a composite key without rebuilding the
    table — which on the one SQLite database that holds Essa's data is a real
    risk for no real gain. So the CATALOGUE IS FOLDED INTO THE KEY instead: the
    default line keeps the bare canonical text, exactly as every existing row has
    it, and any other line prefixes its code. See categorize._alias_key.

    `catalogue_id` is carried alongside so the aliases of one line can be listed
    and reviewed; the key is what enforces the separation."""
    __tablename__ = "category_aliases"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, index=True)   # canonical description text
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True)
    sample = Column(String)                         # a raw description that produced it
    category = Column(String, index=True)           # the master name it maps to
    section = Column(String)
    source = Column(String, default="human")        # human | import
    hits = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class UnitType(Base):
    """A unit of handling, and how many pieces are in one of it.

    Suppliers bill in dozens; Essa stocks, labels and sells in whatever the item
    actually is. A handkerchief is a piece, a pillow cover is a pair, a towel is a
    piece — and a dozen of each is a different number of things to put a QR on.
    One table answers both halves of that, because they are the same question
    asked twice:

      * **the billed unit** — DOZ on the invoice means `pieces` = 12,
      * **the stock unit** — PAIR for a pillow cover means `pieces` = 2.

    So 1 DOZ of pillow covers is 12 pieces is 6 pairs, and 6 is the number of
    labels, the number of stock units and the number the shop sells. The
    arithmetic is nothing more than pieces-in ÷ pieces-per-stock-unit, which is
    exactly why both ends live in one master: add "HALF DOZEN = 6" once and it
    works as a purchase unit and as a selling unit without a second thought.

    `aliases` is what makes it survive real invoices, where the same unit is
    printed DOZ, DZN, DZ, Doz. and DOZEN by five suppliers. `pieces` is a float
    only so an odd trade unit (a "set" of 1.5?) is expressible; every seeded value
    is a whole number.

    Editing `pieces` here does NOT re-value existing stock: a product freezes its
    own factor at creation (Product.pieces_per_unit), because the goods on the
    shelf were counted under the rule that was in force when they arrived.
    """
    __tablename__ = "unit_types"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True)    # PCS | PAIR | DOZEN | SET | BOX
    name = Column(String)                             # "Pair"
    pieces = Column(Float, default=1.0)               # individual items in one of these
    aliases = Column(JSON, default=list)              # ["DOZ", "DZN", "DZ"]
    countable = Column(Boolean, default=True)         # False for MTR/KG — no pieces to tag
    is_seed = Column(Boolean, default=False)          # shipped with the system
    sort = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    @property
    def pieces_per(self):
        p = float(self.pieces or 0)
        return p if p > 0 else 1.0


class UnitRule(Base):
    """Which unit type a product is, decided from what it is called.

    "Make Unit Type configurable per product" cannot mean somebody choosing it on
    every GRN line forever — the same twenty products come in every week, and a
    setting that must be re-entered is a setting that will be got wrong. So the
    rule is stated once against the wording ("pillow cover" → PAIR) and every line
    that says pillow cover is born a pair.

    Matched on a lower-cased substring of the description (or against a category
    master name when `scope` is "category"), longest pattern first, so
    "pillow cover" beats a broader "cover". A choice made by hand on a GRN line is
    remembered here the same way category corrections are — see
    services/unit_types.learn — which is what stops the list going stale.
    """
    __tablename__ = "unit_rules"
    id = Column(Integer, primary_key=True)
    pattern = Column(String, index=True)              # "pillow cover" (lower case)
    scope = Column(String, default="keyword")         # keyword | category
    unit_type = Column(String, index=True)            # UnitType.code
    source = Column(String, default="seed")           # seed | human
    hits = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class MasterRecord(Base):
    """One row of any master, stored against its definition.

    Seventeen master screens would otherwise be seventeen tables that differ only
    in their column names, and a field added to one of them would mean a
    migration before anyone could type it. The shape of each master lives in
    services/master_defs.py; the values live here, in `data`.

    What is NOT in `data` is deliberate: `code` and `name` are lifted out into
    real columns because they are the two things every other master looks a
    record up by — a dropdown sourced from `master:tax` needs to list and search
    them without opening a JSON blob per row. Everything else is the definition's
    business.

    This backs the masters that had no home. Supplier, Agent and Transport keep
    their own tables (they are created automatically from documents and are wired
    into the LR and invoice flows); their extra ERP fields are stored here
    against the same `code`, so neither copy has to know about the other.
    """
    __tablename__ = "master_records"
    id = Column(Integer, primary_key=True)
    master = Column(String, index=True)          # master_defs key: "tax", "brand", …
    code = Column(String, index=True)            # the master's own code, when it has one
    name = Column(String, index=True)            # what a dropdown shows
    data = Column(JSON, default=dict)            # every other field, by key
    grids = Column(JSON, default=dict)           # {grid_key: [row, …]} for child tables
    matrix = Column(JSON, default=dict)          # {row: {column: bool}} — Product's switchboard
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    phone = Column(String)
    created_at = Column(DateTime, default=now)


class Transport(Base):
    __tablename__ = "transports"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    phone = Column(String)
    created_at = Column(DateTime, default=now)


class LREntry(Base):
    """LR Entry register — one row per received consignment.

    Two ways in, and `entry_source` says which:
      * "import" — OCR/vision reads a photographed LR book page or the TRANSPORT
        Excel, and a batch of rows lands at once. This is the fast path.
      * "manual" — one consignment keyed in on the LR Entry form, for when the
        goods arrive with no register page to photograph.

    The columns below are the union of what the register page carries and what
    the Transport Entry form captures, so either route fills the same record."""
    __tablename__ = "lr_entries"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    # The warehouse the consignment is coming TO. A lorry is booked in against
    # the building expecting it, and the transport desk at one branch should not
    # be reading another branch's register.
    #
    # NULL on rows raised before workspaces existed; those stay visible from
    # every warehouse rather than disappearing. See services/scope.
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    entry_source = Column(String, default="import")   # import | manual
    # Our own running number for the entry (LRE-00001). Distinct from `lr_no`,
    # which is the TRANSPORTER's docket number printed on the consignment.
    lr_entry_no = Column(String, index=True)
    lr_entry_date = Column(String)       # when the entry was booked in the office
    lr_mode = Column(String)             # Hand Delivery | Transport | Courier | …
    recv_date = Column(String)
    transport = Column(String)
    bundle = Column(Float)
    boxes = Column(Float)
    lr_no = Column(String)
    lr_date = Column(String)
    supplier_name = Column(String)
    agent = Column(String)
    agent_commission = Column(Float)     # % or flat, as the agent is engaged
    inv_no = Column(String)
    inv_date = Column(String)
    qty = Column(Float)                  # No Of Pieces
    amount = Column(Float)               # Goods Value
    auto_transfer_location = Column(String)   # onward branch, or NONE to keep here
    purchase_manager = Column(String)
    # The house policy: ninety days to move, unless a delivery says otherwise.
    # Defaulted HERE as well as on the form so a row that never passes through
    # the form — one read off a register page, one posted by the phone — is not
    # the only kind with nothing recorded. A blank left the Item Locator's stock
    # age with nothing to call late. See DEFAULT_HOLDING_DAYS in the frontend.
    stock_holding_days = Column(Float, default=90)
    additional_margin = Column(Float)
    # Freight settles as mode + amount; the flag mirrors the form's checkbox and
    # says the charge APPLIES at all, which a zero amount cannot express (nothing
    # yet quoted vs quoted at nil).
    paid_topay = Column(String)          # TOPAY | NO | PAID
    freight_applicable = Column(Boolean, default=False)
    freight_amount = Column(Float)       # the FREIGHT line alone
    # What the transporter is actually owed — the "G. TOTAL" printed at the foot
    # of an LR copy. Freight is only the first line of that bill: a Golden
    # Transport LR reads Freight 425, H.C. 10, S.T. Charge 20, G. TOTAL 455, and
    # for a long time this system recorded the 425 and dropped the 30. That made
    # every transport payment report short by whatever the sundries came to, with
    # nothing on screen to say a number was missing.
    #
    # Kept as its own column rather than derived, because the printed total is
    # the document: if the charges do not add up to it, the LR is still what the
    # lorry will be paid against. `freight_charges` holds the named lines that
    # made it up — {"H.C.": 10, "S.T. Charge": 20} — as a map rather than a column
    # apiece, because every transporter prints a different set of them and eight
    # mostly-empty columns is exactly the trap this table was cleaned of once
    # before.
    freight_total = Column(Float)
    freight_charges = Column(JSON, default=dict)
    # Columns dropped after they proved to be dead weight in this warehouse — no
    # consignment ever carried one: company, bundle_rack, section, remark,
    # due_date, pay_mode, package_slip_no, slip_date, actual_weight,
    # charged_weight, from_city, receiving_city, loading_charge/
    # loading_applicable, cash_cheque, and the earlier `place` and `purchaser`.
    # `_migrate` drops them; older databases may still hold the physical column,
    # but nothing maps or reads it.
    item = Column(String)
    # Who physically RECEIVED the consignment. Not typed on the desktop and not
    # read off the register page: only the warehouse knows who took the packages,
    # so they record it from the phone app (POST /api/lr/{id}/receive) as the
    # goods land. The desktop shows it read-only.
    received_by = Column(String)
    # Freight settlement is recorded as mode + amount only (paid_topay,
    # freight_amount). `paid_by`, `cash_receiver_mobile` and `cash_cheque` columns
    # may still exist in older databases; they are no longer mapped or read.
    # --- what the page was written in, when it wasn't English ---
    # A Tamil register is read in Tamil and stored in English, because that is
    # what every search box, master and report downstream is in. The reading is
    # not allowed to be the only copy though: `original_values` is {field: the
    # text on the page} for each value that changed, so the row can always show
    # what the clerk actually wrote — which is the record, and the thing anyone
    # disputing a reading will ask for. Numbers never appear here; they are never
    # translated. See services/translate.py.
    source_language = Column(String)          # "Tamil" — None when it was English
    original_values = Column(JSON, default=dict)
    # --- the order this consignment is arriving against ---
    # Nullable, and that is not a loophole. Enforcement lives on the MANUAL route
    # only (routers/lr), because the import route reads a whole register page at
    # once and a register page names no purchase order — holding it to this would
    # reject rows off a perfectly good page. An imported row that cannot be tied
    # to an order is flagged, not refused. See services/lr_link.po_match.
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"),
                               nullable=True, index=True)
    # --- invoice linkage (cross-fill): set when a matching invoice is uploaded ---
    invoice_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    # The receipt raised against this consignment, once one exists. Written here
    # as well as on the GRN because this is the row the transport desk has open
    # when a delivery is queried — see services/inventory._publish_grn_no.
    grn_no = Column(String, index=True)
    matched = Column(Boolean, default=False, index=True)   # an invoice has been linked
    # fields where the linked invoice DISAGREES with the register value we kept:
    # [{"field","register","invoice"}] — register value is retained, conflict flagged
    mismatches = Column(JSON, default=list)
    created_at = Column(DateTime, default=now)

    purchase_order = relationship("PurchaseOrder")
    attachments = relationship("LRAttachment", back_populates="entry",
                               cascade="all, delete-orphan",
                               order_by="LRAttachment.id")


class LRAttachment(Base):
    """A file pinned to one LR entry — the LR copy, a weighment slip, a photo of
    damaged bundles. Separate from `Document` on purpose: a Document is something
    the extraction engine reads and learns a supplier format from, whereas these
    are evidence kept against the consignment and never extracted."""
    __tablename__ = "lr_attachments"
    id = Column(Integer, primary_key=True)
    lr_id = Column(Integer, ForeignKey("lr_entries.id"), index=True)
    doc_type = Column(String)            # from the `attachment_type` master
    filename = Column(String)
    stored_path = Column(String)
    mime = Column(String)
    created_at = Column(DateTime, default=now)

    entry = relationship("LREntry", back_populates="attachments")


class LabelTemplate(Base):
    """The DESIGN of a label — where each field sits on the sticker, never the
    values that will be printed in those places.

    This is the whole reason label design is its own module rather than a corner
    of Inventory. A product's data belongs to the GRN that created it; how that
    data is laid out on a 50×35mm sticker is a decision made once by a manager
    and then used by the warehouse for months. Storing a product's name IN a
    template would mean a template per product; storing the *reference*
    ("product_name goes at 4mm/9mm, 8pt, bold") means one template prints the
    whole warehouse, and a product renamed on its GRN prints correctly the next
    day without anyone reopening the designer.

    `elements` is the design: a list of

        {"id": "e3", "field": "size", "x": 4, "y": 18, "w": 22, "h": 5,
         "size": 8, "bold": true, "align": "left", "prefix": "Size: ",
         "locked": false, "visible": true, "z": 3}

    with x/y/w/h in MILLIMETRES from the label's top-left corner, because that is
    what the printer works in and what the person holding a ruler against the
    output can check. Field keys are the catalogue in services/label_designer.py;
    an element naming a field that catalogue no longer has renders as blank
    rather than breaking the sheet, so a template outlives a renamed attribute.

    `locked` is per element and is enforced in the UI only — it stops a warehouse
    user nudging the QR off the sticker by accident, which is a usability guard,
    not a permission. Deleting or moving a locked element is still possible after
    unlocking it, which is the point.

    One template at a time may be `is_default`; that is the one the printing
    screen opens on. Setting it is a router operation because it has to clear the
    previous default in the same transaction — see routers/labels.set_default.
    """
    __tablename__ = "label_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String)
    # the sticker itself. Millimetres, and floats — 50 × 35 is common, so is 38.1.
    width_mm = Column(Float, default=50.0)
    height_mm = Column(Float, default=35.0)
    padding_mm = Column(Float, default=2.0)
    border = Column(Boolean, default=True)
    #: what this template prints one of: "product" (one label per SKU) or
    #: "unit" (one label per physical piece, carrying that piece's own code)
    target = Column(String, default="product", index=True)
    font = Column(String, default="Arial, Helvetica, sans-serif")
    elements = Column(JSON, default=list)
    is_default = Column(Boolean, default=False, index=True)
    active = Column(Boolean, default=True, index=True)
    created_by = Column(String)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class MasterOption(Base):
    """One entry in a small dropdown list, keyed by `kind`.

    The big masters earn their own table (Supplier, Agent, Transport, Category)
    because they carry structure — a GSTIN, a phone, a section. These do not: a
    rack, a warehouse section, a pay mode or a city is a name and nothing else,
    and there are a dozen such lists on the LR Entry form alone. A table apiece
    would be a dozen near-identical models and routers; one keyed table is the
    same data with one place to maintain.

    Lists that are fixed vocabulary (lr_mode, attachment_type, …) are seeded
    once; the free ones (company, rack, section, …) fill themselves from whatever
    is entered, the same way agents and transporters already do."""
    __tablename__ = "master_options"
    id = Column(Integer, primary_key=True)
    kind = Column(String, index=True)    # see masters.OPTION_KINDS
    value = Column(String, index=True)
    # Which business line this value belongs to. NULL means SHARED, and that is
    # the right default for nearly all of these: a payment mode, a rack label or
    # a transport mode is the same whichever goods are in the box. A list that
    # genuinely differs per line is tagged, and each catalogue then sees the
    # shared values plus its own.
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True)
    sort = Column(Integer, default=0)    # seeded lists keep their given order
    created_at = Column(DateTime, default=now)


# ============================================================================
#  Notifications
# ============================================================================
class NotificationState(Base):
    """What has been READ of a standing queue — not a log of events.

    Nearly everything worth telling someone about in this system is a condition
    rather than an occurrence: "four GRNs are in draft", "eleven lines have been
    dead for six months". Written as an event log it would file the same four
    drafts again on every check and the list would be unreadable inside a day.

    So the notices themselves are derived live (services/notifications.py) and
    only the acknowledgement is stored: which key was read, and at what COUNT. A
    queue read at four drafts goes quiet, and speaks up again at five — because
    what someone acknowledged was four, and a fifth is news. It never re-alerts
    for a queue that shrank; that is the direction nobody needs chasing about.

    `muted` is the stronger form, for a queue a particular warehouse simply does
    not work that way — it stays out of the list until it is unmuted, whatever
    the count does."""
    __tablename__ = "notification_states"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, index=True)   # see notifications.RULES
    first_seen = Column(DateTime, default=now)      # since when this has been open
    read_level = Column(Float, default=0.0)         # the count acknowledged
    read_at = Column(DateTime, nullable=True)
    read_by = Column(String)
    muted = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=now, onupdate=now)


class NotificationRecipient(Base):
    """Who should be told, and on what number.

    Delivery today is in-app: the bell on the desktop and the Notifications tab
    on the warehouse phone. The number is held against the person so a channel
    that sends to a phone can be switched on later without anybody re-collecting
    the roster — and so the list answers "who is meant to be watching this",
    which is a question the warehouse has an answer to and the software did not."""
    __tablename__ = "notification_recipients"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    mobile = Column(String)                 # as dialled, +91… or ten digits
    role = Column(String)                   # what they watch — free text
    #: which levels reach them: ["critical", "warn", "info"]
    levels = Column(JSON, default=list)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)


# ============================================================================
#  Dead stock clearance  (Inventory → Dead Stock & Clearance)
# ============================================================================
class ClearanceCampaign(Base):
    """A run at clearing slow-moving stock: which lines, at what discount, over
    which dates.

    It is a PLAN over stock that already exists, not stock of its own. There is
    no quantity held here, no ledger and no movement — a line points at the
    product it came from, and when that product sells at the till the campaign's
    realisation moves because the sale moved. Anything else would be a second
    stock record to keep in step with the first, and the first is the one the
    business runs on (see services/dead_stock.campaign_lines).

    The dates are what makes the reading possible: sold-and-realised are the
    till's own figures for these products BETWEEN starts_on and ends_on, so two
    campaigns over the same SKU in different months each own their own result."""
    __tablename__ = "clearance_campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    status = Column(String, default="draft", index=True)   # draft | active | closed
    starts_on = Column(String)                             # ISO YYYY-MM-DD
    ends_on = Column(String)
    note = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime, default=now)
    closed_at = Column(DateTime, nullable=True)

    lines = relationship("ClearanceLine", back_populates="campaign",
                         cascade="all, delete-orphan", order_by="ClearanceLine.id")


class ClearanceLine(Base):
    """One product in a campaign, at the price the campaign was approved on.

    The age, band, discount and price are frozen copies of what the register
    showed when the line was added. Read live instead, every line would re-price
    itself quietly as the stock went on ageing, and the campaign could no longer
    say what it had promised — or be judged against it afterwards."""
    __tablename__ = "clearance_lines"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("clearance_campaigns.id"), index=True)
    product_id = Column(Integer, ForeignKey("products.id"), index=True)
    qty = Column(Float)                    # what was put into the campaign
    days_idle = Column(Integer)
    bucket = Column(String)                # the age band, as labelled at the time
    discount_pct = Column(Float)
    cost_price = Column(Float)             # weighted-average cost, for the margin
    mrp = Column(Float)
    clearance_price = Column(Float)
    expected_realisation = Column(Float)
    action = Column(String, default="Review")   # see dead_stock.ACTIONS
    note = Column(String)
    added_at = Column(DateTime, default=now)

    campaign = relationship("ClearanceCampaign", back_populates="lines")
    product = relationship("Product")


class User(Base):
    """A person who can sign in, on the desktop app or the phone.

    Three roles, ranked — see services/users.ROLE_RANK. `user` runs the floor
    (receive, count, scan, print), `admin` also owns the setup the floor works
    against (masters, suppliers, label design) and the money screens, and
    `superadmin` additionally owns this table and the server's own settings.

    The password is never stored. `password_hash` holds a PBKDF2 digest and its
    salt; `token_seed` is folded into every token this user is issued, so
    changing a password (or a super admin resetting one) invalidates the
    sessions already out there instead of leaving them valid until they expire.
    """
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="user")
    full_name = Column(String)
    active = Column(Boolean, default=True)
    token_seed = Column(String)
    # Kept so a super admin can see who has never signed in — a created-and-
    # forgotten account is the one most worth deactivating.
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=now)
    created_by = Column(String)
    # Per-screen access, on top of the role: {"screens": {"purchases": ["view",
    # "create"]}, "data": ["hide_cost_price"], "locations": [...]}. NULL or empty
    # means the role decides on its own, exactly as it did before this column
    # existed — which is what keeps every account that predates it working. The
    # role stays the ceiling; this can only narrow. See services/permissions.
    permissions = Column(JSON)


class AppSetting(Base):
    """Settings changed from the UI rather than from the environment — the
    vision key and model, the provider preference, the dead-stock ladder.

    These used to be a JSON file beside the database, which worked while there
    was one long-running server with a disk. There no longer always is: on a
    serverless deployment the filesystem is read-only, and each instance holds
    its own module memory, so a key saved by whichever instance served the
    settings screen would be invisible to the one that served the next upload —
    vision would appear to switch itself off at random.

    A row is the same for everyone and outlives every instance.
    """
    __tablename__ = "app_settings"
    key = Column(String, primary_key=True)
    value = Column(JSON)
    updated_at = Column(DateTime, default=now, onupdate=now)


# ============================================================================
#  Locations — Warehouse → Store → POS terminal
# ============================================================================
#  Until now "where" was a NAME. Stock Outward dispatches to `to_destination`,
#  free text; the branch a consignment is forwarded to is an LR column; and the
#  list somebody maintains is `master_options` of kind `auto_transfer_location`
#  — a bare string with no code, no address and no way to be switched off.
#
#  That was enough while there was one warehouse and a handful of branches. It
#  stops being enough at ten warehouses and fifty stores, because a name cannot
#  answer "which warehouse does this store belong to", cannot be renamed without
#  orphaning every row that spelled it the old way, and cannot be deactivated.
#
#  THE SHOP ALREADY DEPENDS ON THIS LIST. The retail app (Textile Retail Shop)
#  keeps its own `locations`/`counters` in its own database and syncs the names
#  from `auto_transfer_location`, matching case-insensitively — see the shop's
#  app/places.sync_locations. So `Store.name` is unique and stays the join key:
#  these tables become the system of record, and services/locations.py mirrors
#  every store back into the old option list so nothing that reads it breaks.
#
#  Stock is NOT split by location here yet. Product.stock_qty is still one
#  figure and StockMovement still has no location column; splitting them is the
#  next change and a much larger one. These tables are what that change needs to
#  exist first, and what the Locations screen manages in the meantime.
# ============================================================================
class LocationProfile:
    """The postal and statutory identity every trading place carries.

    One definition on all three levels rather than only on the warehouse,
    because the thing that needs these is a DOCUMENT and all three appear on
    one: a GRN is received at a warehouse, a transfer note is addressed to a
    store, a retail bill is printed at a till. Each has to show that place's
    own address, its own GSTIN and the person to ring about it — a branch
    registered separately for GST has a different number from the warehouse
    supplying it, so printing the parent's would put the wrong one on the bill.

    EVERY COLUMN IS NULLABLE, and that is load-bearing twice over. It is what
    lets `_add_missing_columns` in main.py add them to an existing database
    with a plain ALTER TABLE — a not-null column is skipped there and reported
    as needing a hand-written migration. And it is honest: an install that has
    only ever needed a name should not meet a form that refuses to save
    without a CIN.

    Kept as separate columns rather than one address blob because the state
    decides whether a sale is inter-state, and "which places are in Kerala" is
    a question somebody actually asks.
    """
    # Garments | Silks | Franchise — what sort of place this is, in the words
    # used here. Deliberately NOT the catalogue: `Warehouse.catalogue_id`
    # decides which categories and attributes a building's GRN offers and is
    # refused once the building holds stock, whereas a franchise store still
    # sells one of those same lines. Folding "Franchise" into the catalogue
    # list would make it a business line with an empty product master.
    loc_type = Column(String)
    # Two lines, because that is how an address is printed on an invoice and
    # how it is read out over a phone. One field means every document that has
    # to lay it out is left guessing where to break it.
    address = Column(String)
    address2 = Column(String)
    city = Column(String)
    district = Column(String)
    state = Column(String)
    # The GST state code — 33 for Tamil Nadu. Filled from the first two digits
    # of `gstin` rather than typed, which is the rule services/businesses.py
    # has always applied to the company's own number.
    state_code = Column(String)
    country = Column(String)
    pincode = Column(String)
    contact_person = Column(String)
    phone = Column(String)
    email = Column(String)
    # This place's own GST registration.
    gstin = Column(String)
    # Corporate Identity Number — the MCA registration that sits beside the
    # GSTIN on a letterhead. Not derivable from it, so it is its own column.
    cin = Column(String)


class Warehouse(LocationProfile, Base):
    """A building that holds stock. The top of the location tree."""
    __tablename__ = "warehouses"
    id = Column(Integer, primary_key=True)
    # Unique so it can be matched by name the way the shop matches stores, and
    # so two people cannot create the same building twice.
    name = Column(String, unique=True, index=True, nullable=False)
    code = Column(String, unique=True, index=True)
    # What this building trades in — which categories its GRN offers, which
    # attributes its items carry, which option lists its forms read. Blank falls
    # back to the default catalogue, which is what a single-business-line install
    # has always implicitly been.
    catalogue_id = Column(Integer, ForeignKey("catalogues.id"), index=True)
    # Whose warehouse it is. Nullable because every building that predates the
    # Business table belongs to the one company this install has always been —
    # filled in on the first run, not guessed at every read.
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    # The internal identifier, as distinct from `code` which people type. See
    # models.Business.uuid for why both exist.
    uuid = Column(String, unique=True, index=True, default=lambda: str(_uuid.uuid4()))
    # Central | Regional | Distribution | Transit | Other — what kind of building
    # this is, which decides nothing today and is what a report will group by.
    kind = Column(String)
    # Deactivated rather than deleted, always: a warehouse that closed still has
    # last year's transfers filed against it, and removing the row to tidy a
    # dropdown would orphan them.
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    stores = relationship("Store", back_populates="warehouse",
                          order_by="Store.name")
    catalogue = relationship("Catalogue", back_populates="warehouses")
    business = relationship("Business", back_populates="warehouses")


class Store(LocationProfile, Base):
    """A place that sells, supplied by one warehouse.

    `name` is unique across the whole company, not just within its warehouse.
    That is deliberate and load-bearing: the shop's own Location rows are
    matched to these by name alone, and two stores called "Main Branch" under
    different warehouses would be one row down there — with one pool of stock
    and one set of bills covering both.
    """
    __tablename__ = "stores"
    id = Column(Integer, primary_key=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    code = Column(String, unique=True, index=True)
    # Whose store it is. A store need not belong to the same company as the
    # warehouse that supplies it — that is exactly what a franchise branch is —
    # so this is asked for here rather than inherited from the parent.
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    warehouse = relationship("Warehouse", back_populates="stores")
    floors = relationship("Floor", back_populates="store",
                          order_by="Floor.sort_order, Floor.name")
    terminals = relationship("PosTerminal", back_populates="store",
                             order_by="PosTerminal.name")


class Floor(Base):
    """A storey of a store, and the bill series billed from it.

    Between the store and the till because that is where it is in the building:
    a store has floors, a floor has tills. It exists as a row rather than as a
    label on the terminal because it carries something the tills share — the
    **bill prefix**. A shop numbering its ground-floor bills TG26-001 and its
    first-floor bills TF26-001 is keeping separate registers, and which register
    a bill lands in is decided by where the till stands, not by the till.

    A floor is NOT a stock location, for the same reason a terminal is not (see
    PosTerminal): the store owns the stock. Giving a floor its own holding would
    mean a garment on a shelf belonging to the first floor and a sale on the
    ground floor failing for stock in the same building.

    `prefix` is the whole point and doubles as this level's code, so there is no
    separate `code` column — two identifiers for one row is how they come to
    disagree. It is what the retail app reads to number a bill; see the shop's
    app/billing_numbers.py, which mirrors these rows and never invents one.
    """
    __tablename__ = "floors"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), index=True, nullable=False)
    name = Column(String, nullable=False)
    #: TG / TF / TS / TT — "Taqua Ground", "Taqua First", and so on. Upper case
    #: and alphanumeric, because it is the front of a document number that has to
    #: read back cleanly.
    prefix = Column(String, index=True)
    #: The order the storeys are listed in — ground floor first, not "First"
    #: before "Ground" because F sorts before G.
    sort_order = Column(Integer, default=0)
    # Deactivated rather than deleted, like every other level here: a floor that
    # closed still has last year's bills numbered from it.
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)

    store = relationship("Store", back_populates="floors")
    terminals = relationship("PosTerminal", back_populates="floor")


class PosTerminal(LocationProfile, Base):
    """One till at one store.

    A terminal is NOT a stock location. The store owns the stock; a terminal
    only records sales against it. Giving each till its own holding would mean
    a garment on the shelf belonging to counter 2, and a sale at counter 1
    failing for stock that is standing in front of the person selling it.

    It carries the same identity columns as its store all the same, because a
    till PRINTS. What goes at the head of a retail bill is the counter's own
    address and GSTIN where it has been given one, and a counter run under a
    franchise agreement bills under a different number from the store it stands
    in. Left blank — which is the ordinary case — the bill falls back to the
    store's, so nothing has to be typed twice to keep working.
    """
    __tablename__ = "pos_terminals"
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), index=True, nullable=False)
    name = Column(String, nullable=False)
    code = Column(String, unique=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    #: Which storey this till stands on, and therefore which bill series it
    #: draws from. Nullable, and that is not an oversight: every till that
    #: existed before floors did has no answer, and a till nobody has placed yet
    #: must keep selling — on the shop's plain bill series — rather than
    #: refusing a customer over an incomplete master.
    floor_id = Column(Integer, ForeignKey("floors.id"), index=True)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=now)
    #: When this till was last switched off, and None while it is open.
    #:
    #: A till is not deleted the day it stops being used. Its number is on every
    #: bill it ever printed, and those bills are read back for a year afterwards —
    #: a return, an audit, a GST return that has to name the counter that raised
    #: the invoice. So removal waits: close it, and it can be deleted a year
    #: later. This column is what that year is counted from, which is why the
    #: date is kept rather than the fact — see routers/locations.delete_terminal.
    #:
    #: Reopening clears it. A till that came back into use has not been retired
    #: at all, and leaving the old date behind would let a counter that billed
    #: yesterday be deleted because it happened to be shut this time last year.
    deactivated_at = Column(DateTime)

    store = relationship("Store", back_populates="terminals")
    floor = relationship("Floor", back_populates="terminals")

    # Two tills at one store are two drawers and must be told apart; the same
    # till name at two different stores is ordinary ("Counter 1" everywhere).
    __table_args__ = (UniqueConstraint("store_id", "name",
                                       name="uq_terminal_store_name"),)


# ---------------------------------------------------------------------------
#  Price revisions
#  -------------------------------------------------------------------------
#  Changing what something sells for, one item or ten thousand.
#
#  A PRICE CHANGE IS AN EVENT, NOT AN EDIT. The same rule this app already
#  applies to stock — a movement, never a silently rewritten figure — because a
#  price carries exactly the same weight: it is what a customer was charged, it
#  is printed on labels, and "why is this 400 when it was 500 last week" has to
#  be answerable by looking rather than by remembering. So every change writes
#  down what it was, what it became, who did it and why, grouped into the batch
#  it was applied in.
#
#  COST IS NOT A PRICE AND IS NOT CHANGED HERE. `avg_cost` is what the goods
#  actually cost, derived by weighted average from the GRNs that brought them in
#  (services/inventory). Letting somebody type over it would make the one figure
#  that is arithmetic into a figure that is an opinion, and every margin and
#  valuation in the app reads it.

#: The three fields a revision may move. Deliberately a closed list: it is what
#: the screens offer, what the service will write, and what the history means.
PRICE_FIELDS = ("mrp", "sale_price", "sale_discount_pct")

#: How a new value is worked out from the old one.
#:   set            — the same figure for every item
#:   percent        — up or down by a percentage of what it is now
#:   amount         — up or down by a fixed number of rupees
#:   discount_off_mrp — sale price becomes MRP less a percentage
PRICE_OPERATIONS = ("set", "percent", "amount", "discount_off_mrp")


class PriceRevision(Base):
    """One price change, applied to one product or to ten thousand at once.

    The batch is the unit because a bulk change is one decision — "20% off all
    ladies chudithars for the festival" — and the questions worth asking of it
    are asked of the whole: what did we change, when, why, and put it back.
    Recording ten thousand unrelated edits would answer none of those.
    """
    __tablename__ = "price_revisions"
    id = Column(Integer, primary_key=True)
    number = Column(String, unique=True, index=True)      # PRC-00001
    #: mrp | sale_price | sale_discount_pct
    field = Column(String, index=True)
    #: set | percent | amount | discount_off_mrp
    operation = Column(String)
    value = Column(Float)
    #: Round the result to the nearest this many rupees — 0 or null for none.
    #: Retail prices are 499 and 1,290, not 487.63, and a percentage change
    #: without rounding produces the second.
    round_to = Column(Float)
    #: What was picked, in words, exactly as the screen described it. Kept as
    #: text rather than as the filter itself: a saved query would return a
    #: different set of products when read back next year, and the one thing this
    #: row has to say is what was done AT THE TIME.
    scope = Column(String)
    note = Column(String)

    product_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=now, index=True)
    created_by = Column(String)
    #: Set when the batch was put back. A reverted revision keeps every line —
    #: what was done and undone is more of a record than what was never done.
    reverted_at = Column(DateTime)
    reverted_by = Column(String)

    changes = relationship("PriceChange", back_populates="revision",
                           cascade="all, delete-orphan",
                           order_by="PriceChange.id")


class PriceChange(Base):
    """One product's before and after, inside a revision.

    `sku` and `description` are copied because this is a receipt: a product
    renamed or deleted later must not rewrite what was repriced. `old_value` is
    what makes a revert possible at all, and is the reason this is a row rather
    than a counter.
    """
    __tablename__ = "price_changes"
    id = Column(Integer, primary_key=True)
    revision_id = Column(Integer, ForeignKey("price_revisions.id"), index=True)
    product_id = Column(Integer, ForeignKey("products.id"), index=True)
    sku = Column(String, index=True)
    description = Column(String)
    field = Column(String)
    old_value = Column(Float)
    new_value = Column(Float)

    revision = relationship("PriceRevision", back_populates="changes")
    product = relationship("Product")


# ============================================================================
#  Audit trail — who did what, when, and where
# ============================================================================
class AuditEvent(Base):
    """One thing somebody DID: a GRN posted, a price changed, an account
    restricted, a sign-in, a refused attempt.

    Written by the auth middleware for every change that reaches a route (see
    services/audit), not by each route remembering to — a trail that depends on a
    line in every handler is a trail with holes exactly where nobody thought to
    look. Reads are not recorded: opening a screen is not an act, and a log full
    of them buries the ones that are.

    The name, role and warehouse are copied onto the row rather than joined:
    this is a record of what was true AT THE TIME. A user renamed or a warehouse
    closed next year must not rewrite who did what last March.
    """
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    username = Column(String, index=True)
    full_name = Column(String)
    role = Column(String)
    method = Column(String)
    path = Column(String)
    #: The screen the path belongs to (security.POLICY) — what the trail filters on.
    screen = Column(String, index=True)
    #: create | modify | delete | print | signin | signout | password
    action = Column(String)
    #: ok | refused | failed — a refused change is an exception worth seeing.
    outcome = Column(String, default="ok", index=True)
    status = Column(Integer)
    warehouse_id = Column(Integer, index=True)
    warehouse_name = Column(String)
    #: One plain sentence: "posted GRN GRN-2026-00012 — 50 units, ₹42,000".
    summary = Column(String)
    #: The document number the sentence is about, so it can be traced.
    ref = Column(String, index=True)
