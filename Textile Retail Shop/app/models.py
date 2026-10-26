from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db


# ---------- Users / Staff ----------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(128))
    phone = db.Column(db.String(32))
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="cashier")  # admin/manager/cashier
    salary = db.Column(db.Float, default=0.0)
    commission_pct = db.Column(db.Float, default=0.0)  # % of sales
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendance = db.relationship("Attendance", backref="user", lazy=True)
    # Spelled out because an invoice now points at users twice — the till login
    # and the staff member served it — and SQLAlchemy cannot guess which.
    invoices = db.relationship("Invoice", backref="cashier", lazy=True,
                               foreign_keys="Invoice.cashier_id")
    sales = db.relationship("Invoice", backref="staff", lazy=True,
                            foreign_keys="Invoice.staff_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def staff_code(self):
        """What is printed on this person's ID card and typed at the counter.

        Derived from the id rather than stored, the same way a customer's card
        number is: there is nothing to keep in step, and a code can never go
        missing or be issued twice.
        """
        return f"STF{self.id:06d}"

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_manager(self):
        return self.role in ("admin", "manager")


class Attendance(db.Model):
    __tablename__ = "attendance"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    check_in = db.Column(db.DateTime, default=datetime.utcnow)
    check_out = db.Column(db.DateTime)
    notes = db.Column(db.String(256))


# ---------- Inventory ----------
class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    # Which sheet of the warehouse master this code came from: OVERALL | KIDS |
    # LADIES | MENS. Null means it predates the master or was typed in by hand —
    # see app/master_categories.py.
    section = db.Column(db.String(16), index=True)
    description = db.Column(db.String(256))
    products = db.relationship("Product", backref="category", lazy=True)


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(64), unique=True, nullable=False, index=True)
    barcode = db.Column(db.String(64), index=True)  # EAN/UPC/Code128 printed barcode
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(256))
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    hsn_code = db.Column(db.String(16), default="5208")  # cotton fabrics HSN default
    unit = db.Column(db.String(16), default="pcs")  # pcs / mtr / kg
    cost_price = db.Column(db.Float, default=0.0)
    selling_price = db.Column(db.Float, nullable=False)
    gst_rate = db.Column(db.Float, default=5.0)  # % (0/5/12/18/28)
    stock_qty = db.Column(db.Float, default=0.0)
    reorder_level = db.Column(db.Float, default=5.0)
    color = db.Column(db.String(32))
    size = db.Column(db.String(32))
    fabric = db.Column(db.String(64))  # cotton, silk, polyester, etc
    active = db.Column(db.Boolean, default=True)
    # Which warehouse item this is. Null for anything the shop added on its own.
    # `warehouse_qr` is the exact payload the warehouse prints on the item's tag,
    # kept so the shop's label carries the SAME code — see app/warehouse_items.py.
    warehouse_id = db.Column(db.Integer, index=True)
    warehouse_qr = db.Column(db.Text)
    # The rest of the warehouse's attribute tuple, copied so the shop can show a
    # product in full without the warehouse being reachable. The shop doesn't
    # price or search on these; they are what the tag and the detail screen show.
    mrp = db.Column(db.Float)
    product_type = db.Column(db.String(64))
    pattern = db.Column(db.String(64))
    fit = db.Column(db.String(64))
    design_no = db.Column(db.String(64))
    # What the sales-promotion schemes can filter on beyond the tuple above —
    # see app/schemes.py. `supplier` comes over from the warehouse with the item;
    # the rest are filled in on the product (Inventory → edit) until the
    # warehouse records them. Empty is "not known", and a filter naming a value
    # never matches an item that doesn't have one.
    brand = db.Column(db.String(64))
    style = db.Column(db.String(64))
    sleeve = db.Column(db.String(64))
    supplier = db.Column(db.String(128))
    purchase_invoice = db.Column(db.String(64))
    purchase_date = db.Column(db.Date)
    #: A marker tag on the item ("OLD STOCK", "FESTIVE") — what a scheme's
    #: Marked Item / Excluded Marker filter picks out.
    marker = db.Column(db.String(64), index=True)
    #: Which storey of the store this item is held on — where you go to find it.
    #:
    #: It is a PLACE, not a quantity. The shop's stock figure is `stock_qty` and
    #: stays one number for the whole shop; this says where those pieces are
    #: standing. That is enough for a floor-wise count, and it is as far as the
    #: honest answer goes: splitting the QUANTITY across floors would mean the
    #: till knowing which storey each sale came off, and it does not.
    #:
    #: Null until somebody says otherwise — which is usually the audit itself.
    #: Counting a garment on the second floor is the act that records it there.
    floor_id = db.Column(db.Integer, db.ForeignKey("floors.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    floor = db.relationship("Floor", backref=db.backref("products", lazy=True))

    @property
    def is_low_stock(self):
        return self.stock_qty <= self.reorder_level


class StockMovement(db.Model):
    """Audit log for every stock change."""
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    change = db.Column(db.Float, nullable=False)  # +ve = inflow, -ve = outflow
    reason = db.Column(db.String(64))  # sale / purchase / adjustment / return
    reference = db.Column(db.String(64))  # invoice#/PO#
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship("Product")


# ---------- Customers & Loyalty ----------
class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    phone = db.Column(db.String(32), index=True)
    email = db.Column(db.String(128))
    address = db.Column(db.String(256))
    gstin = db.Column(db.String(16))  # for B2B customers
    state_code = db.Column(db.String(4), default="33")
    loyalty_points = db.Column(db.Float, default=0.0)
    total_spent = db.Column(db.Float, default=0.0)
    #: For the birthday and anniversary wishes. Optional — a customer who would
    #: rather not say is still a customer — and stored as dates, so a wish can be
    #: found by day and month whatever the year.
    dob = db.Column(db.Date)
    anniversary = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoices = db.relationship("Invoice", backref="customer", lazy=True)

    @property
    def card_code(self):
        """Stable membership-card / barcode value for this customer."""
        return f"CUST{self.id:06d}"


class LoyaltyTxn(db.Model):
    __tablename__ = "loyalty_txns"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    points = db.Column(db.Float, nullable=False)  # +ve earned, -ve redeemed
    reason = db.Column(db.String(64))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# Suppliers and purchase orders used to live here. They are the warehouse's
# job now — stock reaches the shop by GRN, and buying from a mill is not
# something a till does — so the models, routes and screens were removed
# rather than left as a second, unused way to record the same thing.


# ---------- Invoices / Sales ----------
class Company(db.Model):
    """A legal entity that raises bills.

    Not decoration and not a label: a tax invoice carries the GSTIN of whoever
    issued it, and two companies trading from one shop file two returns. So the
    header the bill prints comes from the row picked at the till, and an invoice
    remembers which one raised it — otherwise a month's sales cannot be split
    between them afterwards, and that is the one thing this has to survive.
    """
    __tablename__ = "companies"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    gstin = db.Column(db.String(20))
    address = db.Column(db.String(256))
    state_code = db.Column(db.String(4))
    phone = db.Column(db.String(32))
    #: what a till starts on before anybody chooses. The shop's own config
    #: becomes this on first run, so a single-company shop never sees the picker
    #: as a decision it has to make.
    is_default = db.Column(db.Boolean, default=False, index=True)
    active = db.Column(db.Boolean, default=True, index=True)
    locations = db.relationship("Location", backref="company", lazy=True)


class Location(db.Model):
    """A place that sells — a branch, a floor, a counter's address.

    The names come from the WAREHOUSE's own list (app/places.sync_locations), not
    from a second master kept here. The warehouse already dispatches stock to
    these places by name; a shop with its own spelling of the same branch cannot
    be asked "what did we send there, and what did they sell".
    """
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    #: True for a name this shop added itself, so a sync that no longer lists it
    #: does not delete it. See places.sync_locations.
    local = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True, index=True)
    counters = db.relationship("Counter", backref="location", lazy=True)


class Floor(db.Model):
    """A storey of a store, and the bill series billed from it.

    NOT to be confused with `app/routes/floor.py`, which is **Floor Sales** — a
    salesperson building a cart on a phone while walking the shop. Same English
    word, unrelated ideas: this one is a floor of a building, and the only reason
    it is a table is that the bill number depends on it.

    That is the whole job. A shop that numbers Ground Floor bills TG26-001 and
    First Floor bills TF26-001 is keeping four separate registers, and which
    register a bill belongs in is decided by where the till stands. So the
    mapping lives here as data — add a floor, give it a prefix — rather than as a
    list in the billing code that a fifth floor would have to be edited into.

    `prefix` is what the bill number starts with. It is what makes the number
    unique, so two floors given the SAME prefix deliberately share one running
    series rather than each minting TG26-001 (see app/billing_numbers.py).
    """
    __tablename__ = "floors"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"),
                            nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    #: TG / TF / TS / TT — "Taqua Ground", "Taqua First", and so on.
    prefix = db.Column(db.String(8), nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0)
    #: True for a floor this shop added itself, running with no warehouse to
    #: read. False for one mirrored from the warehouse's Locations master, which
    #: is where floors belong — see app/places.sync_places. The flag is what
    #: stops a sync retiring a row it never created, exactly as on Location.
    local = db.Column(db.Boolean, default=False)
    #: `floors.id` in the warehouse, for the row this mirrors. Matching on the id
    #: rather than the name is what lets a floor be RENAMED upstairs without the
    #: shop making a second one and leaving the tills pointing at the old.
    wh_id = db.Column(db.Integer, index=True)
    active = db.Column(db.Boolean, default=True, index=True)

    location = db.relationship("Location",
                               backref=db.backref("floors", lazy=True))
    counters = db.relationship("Counter", backref="floor", lazy=True)
    __table_args__ = (db.UniqueConstraint("location_id", "name",
                                          name="uq_floor_location_name"),)


class Counter(db.Model):
    """One till at one location. Two tills at a branch are two drawers."""
    __tablename__ = "counters"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"),
                            nullable=False, index=True)
    #: Which storey this till stands on, and therefore which bill series it
    #: draws from. Nullable, and that is not an oversight: every till that
    #: existed before floors did has no answer, and a till on a shop floor
    #: nobody has mapped yet must keep billing — on the shop's plain INV- series
    #: — rather than refusing a customer because a master is incomplete.
    floor_id = db.Column(db.Integer, db.ForeignKey("floors.id"), index=True)
    #: `pos_terminals.id` in the warehouse, for the till this mirrors. Null for
    #: the "Counter 1" this shop creates for a branch the warehouse has not
    #: given a till of its own — see app/places.sync_places.
    wh_id = db.Column(db.Integer, index=True)
    active = db.Column(db.Boolean, default=True, index=True)
    __table_args__ = (db.UniqueConstraint("location_id", "name",
                                          name="uq_counter_location_name"),)


class BillSequence(db.Model):
    """The running number behind one bill series — TG26-001, TG26-002, …

    One row per (prefix, financial year), and that pair is the scope for a
    reason: the bill number IS `prefix + year + number`, so anything sharing
    those two must share the counter or the two tills would mint the same
    string. Keying the series on the floor instead would let two floors
    configured with the same prefix both produce TG26-001 — a duplicate bill
    number, which is the one thing this table exists to prevent.

    The number is handed out by an atomic `UPDATE … SET last_number =
    last_number + 1`, never by reading it and adding one in Python. See
    app/billing_numbers.py for why that distinction is the whole point.
    """
    __tablename__ = "bill_sequences"
    id = db.Column(db.Integer, primary_key=True)
    prefix = db.Column(db.String(8), nullable=False)
    #: "26" for the year starting April 2026. Empty for a series that does not
    #: reset annually — the shop's own INV- numbering is one.
    fin_year = db.Column(db.String(8), nullable=False, default="")
    last_number = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("prefix", "fin_year",
                                          name="uq_bill_sequence"),)


class LocationStock(db.Model):
    """How much of one product is at one place.

    `Product.stock_qty` is what this SHOP holds altogether, and it stays the
    figure the till sells against. This is the split of it: what arrived at each
    branch and what has sold there. The two are kept in step, not derived from
    each other — a shop whose branches were stocked before any of this existed
    has a total and no split, and inventing one by dividing would be making up
    numbers about real goods.
    """
    __tablename__ = "location_stock"
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"),
                            nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"),
                           nullable=False, index=True)
    qty = db.Column(db.Float, default=0.0)
    location = db.relationship("Location")
    product = db.relationship("Product")
    __table_args__ = (db.UniqueConstraint("location_id", "product_id",
                                          name="uq_location_stock"),)


class TransferReceipt(db.Model):
    """One dispatched line from the warehouse, taken into a branch's stock.

    The point of this table is that it happens ONCE. The sync reads the
    warehouse's outward lines on every start, and without a record of what has
    already been applied, a shop's stock would grow by the whole delivery every
    time the till was restarted. So the warehouse's own line id is unique here,
    and it is the thing that makes the import safe to repeat.

    It is also the audit trail: which transfer, on what day, brought these pieces
    to this branch. `qty` is what was ACCEPTED at the far end, not what was sent —
    the warehouse already records the difference as a discrepancy, and a shop that
    took in the sent figure would be holding pieces that never arrived.
    """
    __tablename__ = "transfer_receipts"
    id = db.Column(db.Integer, primary_key=True)
    #: StockOutwardLine.id in the warehouse — the natural key, and unique so the
    #: same dispatch cannot be taken in twice
    wh_line_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    wh_outward_id = db.Column(db.Integer, index=True)
    code = db.Column(db.String(32))                # the transfer note's code
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    qty = db.Column(db.Float, default=0.0)
    received_on = db.Column(db.String(16))
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)
    location = db.relationship("Location")
    product = db.relationship("Product")


#: Where a store transfer stands. `sent` — the goods are off this branch's
#: shelves and on their way; `received` — counted in at the other end.
STORE_TRANSFER_STATUSES = ("sent", "received")


class StoreTransfer(db.Model):
    """Goods a branch sends away (Store → Transfer Goods): back to a warehouse,
    or across to another branch.

    The stock leaves this branch the moment it is saved. Sent to a WAREHOUSE it
    is also written to the warehouse as a posted dispatch from this store
    (`wh_outward_id`), and the warehouse counts it in on its own Stock Inward.
    Sent to another BRANCH it waits in that branch's Stock Inward — or, ticked
    Immediate Transfer, is moved straight in.
    """
    __tablename__ = "store_transfers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, index=True)      # the package code
    date = db.Column(db.String(10))
    packed_by = db.Column(db.String(128))
    from_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    #: one of these two: another branch here, or a warehouse
    to_location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    to_warehouse_id = db.Column(db.Integer, index=True)
    to_name = db.Column(db.String(128))
    immediate = db.Column(db.Boolean, default=False)
    remarks = db.Column(db.Text)
    filename = db.Column(db.String(255))        # the barcode file the lines came from
    status = db.Column(db.String(16), default="sent", index=True)
    wh_outward_id = db.Column(db.Integer, index=True)
    received_by = db.Column(db.String(128))
    received_at = db.Column(db.DateTime)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    from_location = db.relationship("Location", foreign_keys=[from_location_id])
    to_location = db.relationship("Location", foreign_keys=[to_location_id])
    lines = db.relationship("StoreTransferLine", backref="transfer", lazy=True,
                            cascade="all, delete-orphan", order_by="StoreTransferLine.id")

    @property
    def total_qty(self):
        return round(sum(l.qty or 0 for l in self.lines), 3)


class StoreTransferLine(db.Model):
    __tablename__ = "store_transfer_lines"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("store_transfers.id"), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    barcode = db.Column(db.String(64))
    qty = db.Column(db.Float, default=0.0)
    accepted_qty = db.Column(db.Float)          # counted in at the other end
    product = db.relationship("Product")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(32), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    # Two different people, and the difference matters at payroll.
    # `cashier_id` is the login that rang the sale — who was on the till, and
    # therefore who answers for the drawer. `staff_id` is who served the customer,
    # identified by their code or ID card at the start of the sale, and that is
    # who the commission belongs to (see routes/staff.py). It is nullable because
    # every invoice raised before staff were identified has no answer for it.
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    invoice_date = db.Column(db.DateTime, default=datetime.utcnow)

    subtotal = db.Column(db.Float, default=0.0)     # sum of taxable amounts
    discount = db.Column(db.Float, default=0.0)
    cgst = db.Column(db.Float, default=0.0)
    sgst = db.Column(db.Float, default=0.0)
    igst = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)

    loyalty_earned = db.Column(db.Float, default=0.0)
    loyalty_redeemed = db.Column(db.Float, default=0.0)

    # How it was settled, in one word, for the screens and reports that want a
    # single answer: cash / card / upi, or "mixed" when it took more than one.
    # The real breakdown is in `payments` below — this stays because every
    # invoice raised before settlements existed has only this, and the invoice
    # list, the printed bill and the day's reports all read it.
    payment_method = db.Column(db.String(16), default="cash")  # cash/card/upi/mixed
    # Who billed it, from where, at which till. Nullable because every invoice
    # raised before there were counters has no answer, and inventing one would be
    # worse than leaving it blank. The company is the one that MATTERS: it is
    # whose GSTIN went on the bill.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)
    # Which storey it was billed from, and the three pieces its number was built
    # out of. The number itself is `invoice_number` above and stays the one thing
    # anybody quotes; these are kept beside it because a bill series has to be
    # auditable — "show me every bill on the TG series this financial year, in
    # sequence, and prove none is missing" cannot be answered by taking a string
    # apart with a regular expression, and would stop being answerable at all the
    # first time a floor's prefix was changed.
    floor_id = db.Column(db.Integer, db.ForeignKey("floors.id"), index=True)
    fin_year = db.Column(db.String(8), index=True)
    bill_prefix = db.Column(db.String(8), index=True)
    bill_seq = db.Column(db.Integer)
    company = db.relationship("Company")
    location = db.relationship("Location")
    counter = db.relationship("Counter")
    floor = db.relationship("Floor")
    payment_status = db.Column(db.String(16), default="paid")  # paid/pending/cancelled
    is_interstate = db.Column(db.Boolean, default=False)
    notes = db.Column(db.String(256))

    #: Cancellation. The row stays — its number is part of a statutory series and
    #: a gap in the series is the one thing an auditor cannot be shown — and is
    #: marked instead: `payment_status` becomes "cancelled" (which is also what the
    #: warehouse's own sales queries count by, so they drop it without being
    #: told), with who, when and why kept beside it. See app/cancellation.py.
    cancelled_at = db.Column(db.DateTime)
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancel_reason = db.Column(db.String(256))
    #: The part of `discount` that came off a coupon, so the bill can print the
    #: two apart. `discount` stays the whole reduction every total is built on.
    coupon_discount = db.Column(db.Float, default=0.0)
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_id])
    #: Who approved the discounts on this bill — the E.Code typed in the
    #: counter's discount card. Null when nobody was named.
    discount_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    discount_by = db.relationship("User", foreign_keys=[discount_by_id])
    #: The agreement code the bill was raised under (the discount card's P.Code),
    #: which a scheme's Agreement Code filter can ask for.
    agreement_code = db.Column(db.String(64))
    #: What On Settlement schemes took off when the bill was paid — part of
    #: `discount`, kept apart so the bill can say where it came from.
    scheme_discount = db.Column(db.Float, default=0.0)

    items = db.relationship("InvoiceItem", backref="invoice", lazy=True, cascade="all, delete-orphan")
    payments = db.relationship("InvoicePayment", backref="invoice", lazy=True,
                               cascade="all, delete-orphan",
                               order_by="InvoicePayment.id")

    @property
    def is_cancelled(self):
        return (self.payment_status or "") == "cancelled"

    @classmethod
    def live(cls):
        """The filter every sales figure uses: a cancelled bill sold nothing."""
        return db.or_(cls.payment_status.is_(None), cls.payment_status != "cancelled")

    @property
    def settled(self):
        """What was actually tendered against this bill, by method.

        Falls back to the whole total under `payment_method` for every invoice
        raised before settlements were recorded — and for the floor-sales path,
        which still bills a single method. Without that fallback the day's cash
        figure would drop to zero the moment this table arrived, on history that
        was perfectly well recorded.
        """
        if self.payments:
            out = {}
            for p in self.payments:
                out[p.method] = round(out.get(p.method, 0.0) + (p.amount or 0), 2)
            return out
        return {(self.payment_method or "cash"): round(self.total or 0, 2)}

    @property
    def change_given(self):
        """Cash handed back. Only cash is ever over-tendered."""
        return round(sum(max(0.0, (p.tendered or p.amount or 0) - (p.amount or 0))
                         for p in self.payments if p.method == "cash"), 2)

    @property
    def total_qty(self):
        """Pieces on this bill, which is what a delivery counter counts in."""
        return round(sum(i.quantity for i in self.items), 3)

    @property
    def delivered_qty(self):
        """Pieces the customer has actually carried out.

        Counted, not inferred from `total_qty - pending_qty`: that subtraction
        also picks up anything returned on a credit note, and would report goods
        the customer handed BACK as goods they collected.
        """
        return round(sum(i.delivered_qty for i in self.items), 3)

    @property
    def pending_qty(self):
        """Pieces still to be collected. 0 once the customer has everything."""
        return round(sum(i.pending_qty for i in self.items), 3)

    @property
    def delivery_status(self):
        """`pending` · `part` · `delivered` — what the counter still owes.

        Derived, never stored. A stored flag is a second answer to a question the
        delivery lines already answer, and the two would disagree the first time
        a credit note changed what was owed.
        """
        pending = self.pending_qty
        if pending <= 0:
            return "delivered"
        return "part" if any(i.delivered_qty for i in self.items) else "pending"


class InvoicePayment(db.Model):
    """One tender against one bill — ₹2,000 cash, ₹1,340 on a card.

    A bill is settled by however many of these add up to its total, which is what
    lets a customer put half on a card and hand over the rest. One column on the
    invoice could not say that; it could only say which method to blame the whole
    amount on, and the day's cash reconciliation would be wrong by whatever the
    other half was.

    `tendered` is what physically changed hands and is only ever different for
    cash: a customer pays 2,000 against 1,860 and takes 140 back. The bill is
    still settled for 1,860 — `amount` is what the invoice received, `tendered`
    is what the drawer saw — and keeping both is the only way the till and the
    invoice can each be right.
    """
    __tablename__ = "invoice_payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                           nullable=False, index=True)
    method = db.Column(db.String(16), nullable=False)      # cash | card | upi
    amount = db.Column(db.Float, nullable=False, default=0.0)
    # What was handed over. Equal to `amount` for everything but over-tendered cash.
    tendered = db.Column(db.Float)
    # The card's last four, a UPI reference, an approval code — whatever the
    # cashier has to be able to quote when a payment is queried later.
    reference = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def change(self):
        return round(max(0.0, (self.tendered or self.amount or 0) - (self.amount or 0)), 2)


#: The tenders a till accepts. One list, so the counter, the settlement check and
#: the reports cannot disagree about what a payment method is.
PAYMENT_METHODS = ("cash", "card", "upi")

#: Money the customer already paid the shop, spent against a bill. Each needs the
#: document it draws on as its `reference` — a credit note kept as store credit,
#: or an advance — and is checked against that document's balance at the till.
VOUCHER_METHODS = ("credit_note", "advance")

TENDER_LABELS = {"cash": "Cash", "card": "Card", "upi": "UPI",
                 "credit_note": "Credit note", "advance": "Advance"}


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)  # pre-tax
    #: The price before this line's own discount, when it had one. `unit_price`
    #: is what was actually charged — so GST, refunds and every report read the
    #: discounted figure without knowing discounts exist — and this is kept so
    #: the bill can still print the rate and the % taken off. Null: no discount.
    list_price = db.Column(db.Float)
    #: The sales scheme that took something off this line, and how much (%) —
    #: already inside `unit_price`; recorded so the bill and reports can say why.
    scheme_id = db.Column(db.Integer, db.ForeignKey("sales_schemes.id"), index=True)
    scheme_pct = db.Column(db.Float)
    scheme = db.relationship("SalesScheme")
    gst_rate = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, nullable=False)  # taxable amount (qty*price)
    tax_amount = db.Column(db.Float, default=0.0)
    product = db.relationship("Product")
    # Who sold THIS line. One bill can carry garments sold by different people,
    # so the counter scans the staff ID after each product and it lands here.
    # Nullable: lines billed before this existed, and free reward lines, have
    # only the bill's `staff_id` to go by.
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    staff = db.relationship("User", foreign_keys=[staff_id])

    # What a promotion did to this line. All null on an ordinary sale, which is
    # nearly every line — see app/promotions.py.
    #
    # The line stays a NORMAL invoice line either way: a free garment is a real
    # product leaving stock at a price of zero, not an annotation on the bill. So
    # the promotion is recorded ON it rather than in a parallel table of free
    # goods, and every screen that already reads invoice items — the print, the
    # delivery desk, returns, the stock ledger — keeps working without knowing
    # promotions exist.
    promo_application_id = db.Column(
        db.Integer, db.ForeignKey("promotion_applications.id"), index=True)
    #: `qualifying` — this line helped earn the promotion.
    #: `reward`     — this line IS the promotion.
    promo_role = db.Column(db.String(16))
    #: How much of `quantity` the promotion actually used. A cart of 5 chudithars
    #: earning one set of 3 has qualified 3 of them, and a report that counted the
    #: whole line would say the scheme moved goods it never touched.
    promo_qty = db.Column(db.Float, default=0.0)
    #: What was given away on this line, at the price it would have sold for.
    #: Stored rather than derived: `unit_price` is 0 on a free line and the
    #: product's selling price changes, so nothing else remembers what the
    #: customer was handed. 0 on a qualifying line — it was paid for in full.
    promo_value = db.Column(db.Float, default=0.0)

    @property
    def is_free_item(self):
        """A line the customer was not charged for, under a scheme."""
        return self.promo_role == "reward" and not (self.unit_price or 0)

    @property
    def returned_qty(self):
        """How much of this line has already come back on a credit note."""
        return round(sum(c.quantity for c in self.credit_lines), 3)

    @property
    def returnable_qty(self):
        """What is still left to return — nothing can come back twice.

        Nothing at all on a cancelled bill: cancelling already put the goods back.
        """
        if self.invoice is not None and self.invoice.is_cancelled:
            return 0.0
        return max(0.0, round(self.quantity - self.returned_qty, 3))

    @property
    def delivered_qty(self):
        """How much of this line the customer has physically collected."""
        return round(sum(d.quantity for d in self.delivery_lines), 3)

    @property
    def pending_qty(self):
        """What the shop still owes the customer on this line.

        Goods that came back on a credit note are subtracted, and that is a
        judgement rather than an identity: nothing records whether a returned
        piece had been collected first. A return of something already collected
        leaves this UNDER-stating what is owed by that amount; not subtracting
        would leave it over-stating on a return raised at the delivery counter
        before the goods ever moved.

        Under-stating is the one to live with. It shows up as a customer at the
        counter with a bill and a discrepancy a person then looks at; the other
        way round hands over goods that have already been refunded, silently.

        A cancelled bill owes nothing: its goods are back on the shelf.
        """
        if self.invoice is not None and self.invoice.is_cancelled:
            return 0.0
        return max(0.0, round(self.quantity - self.delivered_qty
                              - self.returned_qty, 3))


# ---------- Returns ----------
class CreditNote(db.Model):
    """Goods coming back, against the bill they went out on.

    Its own document rather than a negative invoice: a sale and a refund are
    different events, and blurring them means every sales figure, GST return and
    invoice list has to remember to exclude the negatives. This way a credit note
    is countable on its own and the original invoice can show what came back.

    `staff_id` is who handled the return. It is NOT who loses the commission —
    that is the staff member on the original sale, which is what
    routes/staff.py subtracts from.
    """
    __tablename__ = "credit_notes"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    subtotal = db.Column(db.Float, default=0.0)      # goods value returned
    discount = db.Column(db.Float, default=0.0)      # share of the bill's discount
    cgst = db.Column(db.Float, default=0.0)
    sgst = db.Column(db.Float, default=0.0)
    igst = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)         # what the customer gets back

    loyalty_reversed = db.Column(db.Float, default=0.0)
    #: Free goods the customer keeps but no longer qualifies for, charged back
    #: against this refund. 0 on every return that breaks no promotion, which is
    #: nearly all of them — see app/promotions.review_return.
    promo_clawback = db.Column(db.Float, default=0.0)
    #: cash · card · upi — money handed back — or `store_credit`: nothing handed
    #: back, and the note spent later as a tender (see app/vouchers.py).
    refund_method = db.Column(db.String(16), default="cash")
    reason = db.Column(db.String(256))
    #: Which till raised it, so a cash refund comes out of the right drawer.
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)

    invoice = db.relationship("Invoice", backref=db.backref("credit_notes", lazy=True))
    staff = db.relationship("User", foreign_keys=[staff_id])
    cashier = db.relationship("User", foreign_keys=[cashier_id])
    items = db.relationship("CreditNoteItem", backref="note", lazy=True,
                            cascade="all, delete-orphan")


class CreditNoteItem(db.Model):
    __tablename__ = "credit_note_items"
    id = db.Column(db.Integer, primary_key=True)
    credit_note_id = db.Column(db.Integer, db.ForeignKey("credit_notes.id"), nullable=False)
    # The exact line being returned, so a bill listing the same product twice at
    # different prices refunds the one actually handed back.
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    gst_rate = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, nullable=False)
    tax_amount = db.Column(db.Float, default=0.0)
    # resellable goes back on the shelf; damaged comes back and is written off,
    # so the movement log tells the truth and the sellable count stays honest.
    condition = db.Column(db.String(16), default="resellable")

    product = db.relationship("Product")
    invoice_item = db.relationship("InvoiceItem", backref="credit_lines")


# ---------- Physical stock audit ----------
#
# Counting a floor: walk it with a phone, scan every tag, say how many are
# actually there, and see where that disagrees with the books.
#
# THE COUNT AND THE BOOKS ARE KEPT APART. A line records what the system
# believed AT THE MOMENT IT WAS COUNTED and what the counter found; neither is
# derived from the other, and nothing here touches stock. Changing the ledger is
# a separate, later, deliberate act (`apply_adjustment`) that writes real
# movements against the audit's number — because a count that silently corrected
# the books would destroy the one record that is supposed to be independent of
# them, and nobody could ever say what the shelf had actually held.

#: Where an audit is in its life. `in_progress` is a floor being walked;
#: `completed` is the walking finished; `reviewed` and `approved` are two pairs
#: of eyes, which is the point of counting money-worth of stock; `cancelled` is
#: a count abandoned. Only an approved audit may move stock.
AUDIT_STATUSES = ("in_progress", "completed", "reviewed", "approved", "cancelled")

#: What a counted line turned out to be.
LINE_STATUSES = ("not_counted", "matched", "shortage", "excess")

#: The reason a stock movement carries when an audit's variance is applied. Its
#: own word, so the ledger can be asked what a count corrected without inferring
#: it from an ordinary adjustment somebody typed.
AUDIT_MOVEMENT_REASON = "audit"


class StockAudit(db.Model):
    """One physical count of one floor.

    Scoped to a floor rather than to the whole shop because that is how a count
    is actually done — one team, one storey, one afternoon — and because a
    variance is only meaningful against the place it was counted in. A shop-wide
    number would say stock is missing without saying where to go and look.
    """
    __tablename__ = "stock_audits"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    floor_id = db.Column(db.Integer, db.ForeignKey("floors.id"), index=True)
    status = db.Column(db.String(16), default="in_progress", index=True)
    note = db.Column(db.String(256))

    started_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    started_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    completed_at = db.Column(db.DateTime)
    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    #: When the variance was written into the ledger. Set once and never again —
    #: it is what makes applying an adjustment idempotent, so a double-clicked
    #: button cannot move the same stock twice.
    adjusted_at = db.Column(db.DateTime)
    adjusted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    location = db.relationship("Location")
    floor = db.relationship("Floor")
    started_by = db.relationship("User", foreign_keys=[started_by_id])
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    adjusted_by = db.relationship("User", foreign_keys=[adjusted_by_id])
    lines = db.relationship("StockAuditLine", backref="audit", lazy=True,
                            cascade="all, delete-orphan",
                            order_by="StockAuditLine.id")

    @property
    def counted_lines(self):
        return [l for l in self.lines if l.physical_qty is not None]

    @property
    def summary(self):
        """The figures at the head of the screen, worked out in one place.

        Derived rather than stored: a stored total is a second answer to a
        question the lines already answer, and the two would disagree the first
        time somebody re-counted a line.
        """
        counted = self.counted_lines
        return {
            "products": len(self.lines),
            "counted": len(counted),
            "not_counted": len(self.lines) - len(counted),
            "system_qty": round(sum(l.system_qty or 0 for l in self.lines), 3),
            "physical_qty": round(sum(l.physical_qty or 0 for l in counted), 3),
            "matched": sum(1 for l in counted if l.line_status == "matched"),
            "shortage_lines": sum(1 for l in counted if l.line_status == "shortage"),
            "excess_lines": sum(1 for l in counted if l.line_status == "excess"),
            # Quantities, not line counts — "35 pieces short" is the number
            # somebody acts on; "3 lines short" is not.
            "shortage": round(-sum(l.difference for l in counted
                                   if l.difference < 0), 3),
            "excess": round(sum(l.difference for l in counted
                                if l.difference > 0), 3),
            "value_variance": round(sum(l.value_variance for l in counted), 2),
        }

    @property
    def is_open(self):
        return self.status == "in_progress"

    @property
    def adjustable_lines(self):
        """Counted lines whose gap this count is entitled to write into stock.

        Excludes anything found off its own floor: that line is a finding about
        WHERE a garment was, not a census of how many the shop has, and applying
        it would write off every piece still sitting on the floor it belongs to.
        See audits.apply_adjustment.
        """
        return [l for l in self.counted_lines
                if l.difference and not l.found_off_floor]

    @property
    def can_adjust(self):
        """Approved, not yet applied, and with something it may apply."""
        return (self.status == "approved" and self.adjusted_at is None
                and bool(self.adjustable_lines))


class StockAuditLine(db.Model):
    """One product on one count — what the books said, and what was on the shelf.

    `system_qty`, `purchase_qty` and `sales_qty` are FROZEN when the line is
    created and never refreshed. Re-deriving them later would turn a count into a
    report on today's figures and destroy the only thing it was for: a variance
    is the gap between what was believed at a moment and what was there at that
    moment. `sku` and `name` are copied for the same reason — a product renamed
    afterwards must not rewrite what the counter saw.

    The live attributes (size, colour, material) are NOT copied. They are how you
    find the garment on the rack, they do not change, and reading them through
    the product keeps one description of an item rather than a stale second one.
    """
    __tablename__ = "stock_audit_lines"
    id = db.Column(db.Integer, primary_key=True)
    audit_id = db.Column(db.Integer, db.ForeignKey("stock_audits.id"),
                         nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"),
                           nullable=False, index=True)
    sku = db.Column(db.String(64))
    name = db.Column(db.String(128))

    #: What the shop's books said when this line was opened.
    system_qty = db.Column(db.Float, default=0.0)
    #: How the books got there — every piece in, and every piece out, off the
    #: movement ledger. They reconcile: purchase − sales IS system_qty, because
    #: the ledger is what stock is made of. Kept so the screen can show the
    #: working rather than asserting a figure.
    purchase_qty = db.Column(db.Float, default=0.0)
    sales_qty = db.Column(db.Float, default=0.0)

    #: What was actually on the floor. Null means nobody has counted it yet,
    #: which is a different thing from counting it and finding none.
    physical_qty = db.Column(db.Float)
    counted_at = db.Column(db.DateTime)
    counted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    #: How many times its tag was read. A second scan does not add a line — it
    #: says "you have already counted this", which is what stops a rack being
    #: walked twice and counted twice.
    scans = db.Column(db.Integer, default=0)
    note = db.Column(db.String(256))
    #: Set when the garment was counted here but belongs to another storey. The
    #: count still stands — it is where the piece IS — and this is the finding.
    found_off_floor = db.Column(db.Boolean, default=False)

    product = db.relationship("Product")
    counted_by = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("audit_id", "product_id",
                                          name="uq_audit_line_product"),)

    @property
    def difference(self):
        """Physical − system. 0 while uncounted, which is not a variance."""
        if self.physical_qty is None:
            return 0.0
        return round(self.physical_qty - (self.system_qty or 0), 3)

    @property
    def line_status(self):
        if self.physical_qty is None:
            return "not_counted"
        d = self.difference
        return "matched" if d == 0 else ("excess" if d > 0 else "shortage")

    @property
    def value_variance(self):
        """What the gap is worth, at what the shop sells the item for.

        Priced at the selling price rather than at cost because the question a
        shortage raises is what walked out of the door, and that is what it
        would have been sold for.
        """
        price = (self.product.selling_price if self.product else 0) or 0
        return round(self.difference * price, 2)


# ---------- Sales promotion schemes ----------
#
# Six tables where one would have done for "buy 3 chudithars, get a leggings",
# and that is the whole point. A scheme written as columns on one row can express
# exactly the offer somebody thought of first; the next one — two categories to
# qualify, a choice of three rewards, one branch only — needs a schema change and
# a new branch in the billing code. So the offer is decomposed into the things it
# is actually made of:
#
#     PromotionScheme          the offer, its validity, its limits
#       PromotionCondition     one group that has to be bought  ("any 3 of…")
#         PromotionConditionItem   a product or a category that satisfies it
#       PromotionReward        one thing that is then given     ("1 of…, free")
#         PromotionRewardItem      a product or a category it may be
#       PromotionPlace         where the offer runs (no rows = everywhere)
#
# and what happened is recorded separately from what was configured:
#
#     PromotionApplication     this scheme, on this bill, this many times
#     PromotionAudit           every promotion event, including the refusals
#
# The engine in app/promotions.py reads that structure and knows nothing about
# any particular offer. Adding "buy 2 get 1" is data.

#: What the customer gets. On the SCHEME rather than on each reward, because it
#: is the one word that describes the offer — a scheme does not hand out one item
#: free and another at 10% off.
SCHEME_TYPES = ("buy_get_free", "buy_get_percent", "buy_get_price")

#: How the reward item is picked when the scheme allows more than one.
REWARD_SELECTION = ("specific", "cheapest", "dearest", "choose")

#: What to do when the reward is not on the shelf (§ stock validation).
#: `block` — no free item is added and the till says why.
#: `substitute` — the next eligible reward that IS in stock is offered instead.
OUT_OF_STOCK_ACTIONS = ("block", "substitute")

#: What happens to the free goods when a qualifying item is returned.
RETURN_POLICIES = ("reclaim_value", "require_return", "keep")

#: Which side of the offer an invoice line was on.
PROMO_ROLES = ("qualifying", "reward")


class PromotionScheme(db.Model):
    """One offer the shop is running.

    `priority` decides who gets a cart first when several schemes could claim the
    same garments — lower runs earlier, as a numbered list reads. A scheme that
    claims an item takes it out of the pool, so the next scheme sees only what is
    left; that is what stops one purchase earning two rewards. `stackable` opts a
    scheme out of both halves of that: it reads the whole cart and reserves
    nothing, which is a deliberate second benefit rather than an accident.
    """
    __tablename__ = "promotion_schemes"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    scheme_type = db.Column(db.String(24), nullable=False, default="buy_get_free")

    #: Null at either end means open — a scheme with no end date runs until it is
    #: switched off, which is what a standing offer is.
    start_date = db.Column(db.Date, index=True)
    end_date = db.Column(db.Date, index=True)
    active = db.Column(db.Boolean, default=True, index=True)

    priority = db.Column(db.Integer, default=100, index=True)
    #: How many times one bill may earn this. Null = as often as it qualifies.
    max_applications = db.Column(db.Integer)
    stackable = db.Column(db.Boolean, default=False)

    reward_selection = db.Column(db.String(16), default="cheapest")
    out_of_stock = db.Column(db.String(16), default="block")
    return_policy = db.Column(db.String(16), default="reclaim_value")

    terms = db.Column(db.Text)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    conditions = db.relationship("PromotionCondition", backref="scheme", lazy=True,
                                 cascade="all, delete-orphan",
                                 order_by="PromotionCondition.sort_order")
    rewards = db.relationship("PromotionReward", backref="scheme", lazy=True,
                              cascade="all, delete-orphan",
                              order_by="PromotionReward.sort_order")
    places = db.relationship("PromotionPlace", backref="scheme", lazy=True,
                             cascade="all, delete-orphan")
    applications = db.relationship("PromotionApplication", backref="scheme",
                                   lazy=True)

    def runs_on(self, day):
        """Is `day` inside this scheme's validity? Open ends are always inside."""
        if self.start_date and day < self.start_date:
            return False
        if self.end_date and day > self.end_date:
            return False
        return True

    def status(self, today=None):
        """`active` · `scheduled` · `expired` · `inactive` — for the admin list.

        Derived, never stored: a scheme whose end date has passed is expired
        whether or not anybody remembered to untick Active, and a stored flag
        would have to be swept by something that runs.
        """
        today = today or date.today()
        if not self.active:
            return "inactive"
        if self.start_date and today < self.start_date:
            return "scheduled"
        if self.end_date and today > self.end_date:
            return "expired"
        return "active"


class PromotionCondition(db.Model):
    """One group of goods that has to be bought.

    A scheme with two conditions means BOTH — "2 chudithars AND 1 dupatta" — and
    each condition's own item rows mean ANY of these. That is the whole grammar,
    and it covers every example in the brief without a rule engine: quantity,
    product, category and mixed schemes are all conditions with different rows
    under them.
    """
    __tablename__ = "promotion_conditions"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("promotion_schemes.id"),
                          nullable=False, index=True)
    min_qty = db.Column(db.Float, nullable=False, default=1.0)
    #: What the cashier is shown when the till explains why a free item appeared.
    #: Left blank it is written from the rows themselves.
    label = db.Column(db.String(128))
    sort_order = db.Column(db.Integer, default=0)

    items = db.relationship("PromotionConditionItem", backref="condition",
                            lazy=True, cascade="all, delete-orphan")


class PromotionConditionItem(db.Model):
    """A product, or a whole category, that counts towards a condition."""
    __tablename__ = "promotion_condition_items"
    id = db.Column(db.Integer, primary_key=True)
    condition_id = db.Column(db.Integer, db.ForeignKey("promotion_conditions.id"),
                             nullable=False, index=True)
    # Exactly one of these is set. Pointing at the shop's own product and
    # category rows rather than copying names: the promotion has to mean the same
    # thing the stock ledger and the GRN mean, and a second spelling of
    # "LADIES CHUDITHAR" is how it stops meaning it.
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), index=True)

    product = db.relationship("Product")
    category = db.relationship("Category")

    @property
    def label(self):
        if self.product:
            return f"{self.product.name} ({self.product.sku})"
        if self.category:
            return f"any {self.category.name}"
        return "—"


class PromotionReward(db.Model):
    """One thing the customer gets each time the conditions are met."""
    __tablename__ = "promotion_rewards"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("promotion_schemes.id"),
                          nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False, default=1.0)
    #: The percentage off, or the fixed price, depending on the scheme's type.
    #: Ignored entirely by `buy_get_free`, where the answer is always zero.
    value = db.Column(db.Float, default=0.0)
    sort_order = db.Column(db.Integer, default=0)

    items = db.relationship("PromotionRewardItem", backref="reward", lazy=True,
                            cascade="all, delete-orphan")


class PromotionRewardItem(db.Model):
    """A product, or a category, this reward may be satisfied from."""
    __tablename__ = "promotion_reward_items"
    id = db.Column(db.Integer, primary_key=True)
    reward_id = db.Column(db.Integer, db.ForeignKey("promotion_rewards.id"),
                          nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), index=True)

    product = db.relationship("Product")
    category = db.relationship("Category")

    @property
    def label(self):
        if self.product:
            return f"{self.product.name} ({self.product.sku})"
        if self.category:
            return f"any {self.category.name}"
        return "—"


class PromotionPlace(db.Model):
    """Where a scheme runs. A scheme with NO rows runs everywhere.

    The four columns are the same four the till already answers with — the
    warehouse the frame was opened from, and the company / location / counter
    picked on it (see app/places.py). A row matches when every column it fills in
    matches the till; a row that fills in only `location_id` is "this branch, any
    counter". So one offer at one till and one offer across a whole company are
    the same shape of record, and neither needs its own flag on the scheme.
    """
    __tablename__ = "promotion_places"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("promotion_schemes.id"),
                          nullable=False, index=True)
    #: The WAREHOUSE the till was opened from. Not a foreign key — it names a row
    #: in the warehouse's database, which this shop only ever reads.
    warehouse_id = db.Column(db.Integer, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)

    company = db.relationship("Company")
    location = db.relationship("Location")
    counter = db.relationship("Counter")

    @property
    def label(self):
        bits = [self.company.name if self.company else None,
                self.location.name if self.location else None,
                self.counter.name if self.counter else None]
        if self.warehouse_id and not any(bits):
            return f"warehouse #{self.warehouse_id}"
        return " / ".join(b for b in bits if b) or "everywhere"


class PromotionApplication(db.Model):
    """One scheme, on one bill, applied this many times.

    The scheme's code and name are COPIED here, and that is not duplication: a
    scheme gets renamed, re-priced and switched off, and a bill from March has to
    keep saying what the customer was actually given. `scheme_id` is the live
    link for reporting; the two text columns are the receipt.

    Store, till, cashier and date are NOT copied — they are on the invoice, which
    is one row away and cannot disagree with itself.
    """
    __tablename__ = "promotion_applications"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                           nullable=False, index=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("promotion_schemes.id"),
                          index=True)
    scheme_code = db.Column(db.String(32))
    scheme_name = db.Column(db.String(128))

    times_applied = db.Column(db.Integer, default=1)
    qualifying_qty = db.Column(db.Float, default=0.0)
    reward_qty = db.Column(db.Float, default=0.0)
    #: What the reward goods were worth at the price they would have sold for.
    benefit_value = db.Column(db.Float, default=0.0)

    #: `applied` · `reduced` · `reversed` — what a later return did to it.
    status = db.Column(db.String(16), default="applied", index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    invoice = db.relationship("Invoice",
                              backref=db.backref("promotions", lazy=True,
                                                 cascade="all, delete-orphan"))
    lines = db.relationship("InvoiceItem", backref="promo_application", lazy=True)

    @property
    def reward_lines(self):
        return [l for l in self.lines if l.promo_role == "reward"]

    @property
    def qualifying_lines(self):
        return [l for l in self.lines if l.promo_role == "qualifying"]


class PromotionAudit(db.Model):
    """Every promotion event, including the ones where nothing was given away.

    Separate from the application because the interesting events have no
    application to hang off: a scheme that qualified and was refused for want of
    stock, or one whose reward was substituted, leaves no free line on any bill
    — and those are precisely the ones somebody asks about later. A log that only
    recorded successes could never answer "why did this customer not get theirs".
    """
    __tablename__ = "promotion_audit"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("promotion_schemes.id"),
                          index=True)
    scheme_code = db.Column(db.String(32))
    scheme_name = db.Column(db.String(128))
    #: Null for an event that never became a bill — a refusal at the counter.
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), index=True)
    application_id = db.Column(db.Integer,
                               db.ForeignKey("promotion_applications.id"), index=True)

    #: applied · blocked_no_stock · substituted · reduced · reversed
    event = db.Column(db.String(24), nullable=False, index=True)
    detail = db.Column(db.Text)

    times_applied = db.Column(db.Integer, default=0)
    qualifying_qty = db.Column(db.Float, default=0.0)
    reward_qty = db.Column(db.Float, default=0.0)
    benefit_value = db.Column(db.Float, default=0.0)

    # Where it happened. Copied here ONLY because a refusal has no invoice to
    # read them off — on an `applied` row they repeat the bill, harmlessly.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    invoice = db.relationship("Invoice")
    user = db.relationship("User")
    location = db.relationship("Location")
    counter = db.relationship("Counter")


# ---------- Delivery ----------
class Delivery(db.Model):
    """Goods physically handed to the customer, against the bills they paid on.

    Billing and collection are two counters and two moments. The till already
    took the money and took the stock off the shelf; what nothing recorded until
    now is whether the customer actually walked out with the garments. In a shop
    where a bill is paid at one desk and the pieces are packed at another, that
    gap is where goods go missing and where "I paid for three and got two" has no
    answer either way.

    So this touches NO stock and NO money — the sale did both. It records
    custody, exactly as an `Alteration` does: who handed over what, from which
    bills, verified against the tag on each garment.

    **One delivery, several bills.** A customer buying at two counters leaves
    with one bundle, and the handover is that bundle, not each bill separately.
    That is what `DeliveryBill` is for, and it is why the reference screen's
    left-hand table has a Total under it. This document IS the settlement the
    reference ERP scans — the shop has no separate settlement or voucher record,
    and inventing an empty one to hold a number would be a document that never
    says anything.
    """
    __tablename__ = "deliveries"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False)
    # Who it was handed to. Copied from the bills rather than typed, and null for
    # a walk-in, exactly as an invoice is.
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    # Two people again, and the same split the invoice makes: `staff_id` handed
    # the goods over and is who a query about this delivery goes to; `cashier_id`
    # is the login that was open at the delivery desk.
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # Where the handover happened. The delivery desk is not always the till that
    # billed it, so this is the desk's own choice and not copied off the invoice.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    notes = db.Column(db.String(256))

    customer = db.relationship("Customer")
    staff = db.relationship("User", foreign_keys=[staff_id])
    cashier = db.relationship("User", foreign_keys=[cashier_id])
    company = db.relationship("Company")
    location = db.relationship("Location")
    counter = db.relationship("Counter")
    bills = db.relationship("DeliveryBill", backref="delivery", lazy=True,
                            cascade="all, delete-orphan")
    lines = db.relationship("DeliveryLine", backref="delivery", lazy=True,
                            cascade="all, delete-orphan")

    @property
    def total_qty(self):
        """Pieces handed over on this delivery."""
        return round(sum(l.quantity for l in self.lines), 3)

    @property
    def scanned_qty(self):
        """How many of them were read off a tag rather than ticked by hand."""
        return round(sum(l.scanned for l in self.lines), 3)

    @property
    def overridden_qty(self):
        """Pieces a manager passed without a scan. 0 on an ordinary delivery."""
        return round(self.total_qty - self.scanned_qty, 3)

    @property
    def total_amount(self):
        """What the goods in this handover were billed at."""
        return round(sum(l.amount for l in self.lines), 2)


class DeliveryBill(db.Model):
    """One bill covered by one handover.

    A plain link row and nothing more: the quantities live on `DeliveryLine`,
    which points at the invoice LINE. Holding a per-bill quantity here as well
    would be the same figure in two places, free to drift the moment a delivery
    is corrected.
    """
    __tablename__ = "delivery_bills"
    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey("deliveries.id"),
                            nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                           nullable=False, index=True)
    invoice = db.relationship("Invoice",
                              backref=db.backref("delivery_bills", lazy=True))
    __table_args__ = (db.UniqueConstraint("delivery_id", "invoice_id",
                                          name="uq_delivery_bill"),)


class DeliveryLine(db.Model):
    """How much of one invoice line went out on one delivery.

    Keyed on the invoice ITEM, not the product, for the reason a credit note is:
    a bill carrying the same garment twice at two prices has two lines, and the
    customer collecting one of them has collected a specific one.

    `scanned` is how many of `quantity` were verified against a tag. It is a
    count rather than a flag because a line of five can be four scans and one
    piece whose label came off in a bag, and a delivery that could only say
    "verified" or "not" would have to lie about which.
    """
    __tablename__ = "delivery_lines"
    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey("deliveries.id"),
                            nullable=False, index=True)
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"),
                                nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    scanned = db.Column(db.Float, default=0.0)
    # Why anything on this line went out unscanned, and who allowed it. Both null
    # on a line that was scanned in full, which is the ordinary case.
    override_reason = db.Column(db.String(256))
    overridden_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    product = db.relationship("Product")
    overridden_by = db.relationship("User")
    invoice_item = db.relationship("InvoiceItem",
                                   backref=db.backref("delivery_lines", lazy=True))
    scans = db.relationship("DeliveryScan", backref="line", lazy=True,
                            cascade="all, delete-orphan")

    @property
    def unit_price(self):
        """What this was billed at. Read through, never copied.

        A posted invoice's rate cannot change, so a second copy here could only
        ever be the same number or a wrong one.
        """
        return (self.invoice_item.unit_price if self.invoice_item else 0.0) or 0.0

    @property
    def amount(self):
        return round(self.quantity * self.unit_price, 2)

    @property
    def overridden(self):
        """Pieces on this line that no tag was read for."""
        return round(self.quantity - (self.scanned or 0), 3)


class DeliveryScan(db.Model):
    """One tag actually read at the delivery desk.

    The evidence behind `DeliveryLine.scanned`, kept rather than counted away,
    because "which of these went out" is the whole reason the warehouse mints a
    code per garment (see the piece labels in the warehouse's ARCHITECTURE §7).
    A line that says 3 and three codes that say which three are different
    records, and only the second one settles a dispute.

    `piece_code` is filled only when the tag identified an individual garment —
    a `EU1|…` payload or a bare `ESSA-00002-007`. A plain SKU tag is printed
    identically on every piece of that item, so it carries no identity and
    scanning it three times for a line of three is correct. That distinction is
    what `delivery.piece_of()` decides, and it is the whole of the
    double-scan guard: a piece code may be read once, a SKU tag as often as the
    line has pieces.
    """
    __tablename__ = "delivery_scans"
    id = db.Column(db.Integer, primary_key=True)
    delivery_line_id = db.Column(db.Integer, db.ForeignKey("delivery_lines.id"),
                                 nullable=False, index=True)
    #: exactly what the reader produced, unparsed
    code = db.Column(db.Text)
    #: the individual garment, when the tag named one
    piece_code = db.Column(db.String(64), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------- Alterations ----------
class Tailor(db.Model):
    """Who does the stitching.

    Kept apart from staff on purpose: the tailor is usually an outside workshop
    that never logs in, has no shift and draws no commission. Giving them a login
    to appear in a dropdown would be the wrong shape.
    """
    __tablename__ = "tailors"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    phone = db.Column(db.String(32))
    notes = db.Column(db.String(256))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Alteration(db.Model):
    """A garment gone back for tailoring, against the bill it was sold on.

    The garment is the customer's already — it left stock when it was sold — so
    nothing here touches stock or the ledger. What it tracks is custody: who has
    the piece, what was asked for, when it was promised, and whether the customer
    has it back.

    `charge` is what the alteration costs, usually nothing. It is collected when
    the garment is handed over, not when it is taken in, because until the work
    is done there is nothing to charge for.
    """
    __tablename__ = "alterations"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    tailor_id = db.Column(db.Integer, db.ForeignKey("tailors.id"), index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"))      # took it in
    delivered_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    status = db.Column(db.String(16), default="pending", index=True)  # pending/ready/delivered
    promised_date = db.Column(db.Date, index=True)
    remarks = db.Column(db.String(256))

    charge = db.Column(db.Float, default=0.0)
    charge_method = db.Column(db.String(16))       # how it was settled, at handover

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ready_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)

    invoice = db.relationship("Invoice", backref=db.backref("alterations", lazy=True))
    tailor = db.relationship("Tailor")
    staff = db.relationship("User", foreign_keys=[staff_id])
    delivered_by = db.relationship("User", foreign_keys=[delivered_by_id])
    items = db.relationship("AlterationItem", backref="alteration", lazy=True,
                            cascade="all, delete-orphan")

    @property
    def total_qty(self):
        return round(sum(i.quantity for i in self.items), 3)

    @property
    def is_overdue(self):
        """Promised, still not ready, and the day has passed."""
        return bool(self.promised_date
                    and self.status == "pending"
                    and self.promised_date < date.today())


class AlterationItem(db.Model):
    __tablename__ = "alteration_items"
    id = db.Column(db.Integer, primary_key=True)
    alteration_id = db.Column(db.Integer, db.ForeignKey("alterations.id"), nullable=False)
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    # What the customer actually asked for — "shorten 2 inches", "take in waist".
    # The whole point of the ticket the tailor works from.
    instructions = db.Column(db.String(256))

    product = db.relationship("Product")
    invoice_item = db.relationship("InvoiceItem", backref="alteration_lines")


# ---------- Floor Sales (mobile salesperson + customer live view) ----------
class SaleSession(db.Model):
    """A draft cart shared between salesperson and customer via a short code."""
    __tablename__ = "sale_sessions"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    salesperson_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    # open, awaiting_approval, approved, rejected, completed, cancelled
    status = db.Column(db.String(20), default="open", nullable=False, index=True)
    discount = db.Column(db.Float, default=0.0)
    redeem_points = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(16), default="cash")
    notes = db.Column(db.String(256))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    salesperson = db.relationship("User", foreign_keys=[salesperson_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    items = db.relationship("SaleSessionItem", backref="session",
                            lazy=True, cascade="all, delete-orphan")

    def totals(self):
        # 1 point == ₹1 off (see config LOYALTY_POINT_VALUE)
        subtotal = sum(i.line_total for i in self.items)
        tax = sum(i.tax_amount for i in self.items)
        discount = min(self.discount or 0, subtotal)
        pre_loyalty = max(0, subtotal - discount + tax)
        # Cap redemption at customer balance AND at the bill amount so we
        # never over-redeem.
        requested = self.redeem_points or 0
        available = (self.customer.loyalty_points if self.customer else 0) or 0
        redeem = min(requested, available, pre_loyalty)
        total = round(pre_loyalty - redeem, 2)
        return {
            "subtotal": round(subtotal, 2),
            "tax": round(tax, 2),
            "discount": round(discount, 2),
            "redeem_requested": round(requested, 2),
            "redeem_available": round(available, 2),
            "redeem": round(redeem, 2),  # actually applied
            "total": total,
        }


class SaleSessionItem(db.Model):
    __tablename__ = "sale_session_items"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sale_sessions.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False)
    gst_rate = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, nullable=False)
    tax_amount = db.Column(db.Float, default=0.0)
    product = db.relationship("Product")

    def recompute(self):
        self.line_total = round(self.quantity * self.unit_price, 2)
        self.tax_amount = round(self.line_total * self.gst_rate / 100.0, 2)


# ---------- Customer advances ----------
class CustomerAdvance(db.Model):
    """Money a customer leaves with the shop before the bill exists.

    A deposit on an order, or a balance kept on account. It is NOT a sale — no
    goods, no tax — so it is its own document, and it is spent later as a tender
    (`InvoicePayment.method == "advance"`, `reference` = this number). What is
    left of it is always worked out from those payments, never stored, so the
    balance cannot drift from the bills it was spent on. See app/vouchers.py.
    """
    __tablename__ = "customer_advances"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(16), nullable=False, default="cash")   # cash/card/upi
    reference = db.Column(db.String(64))
    note = db.Column(db.String(256))
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    #: What was handed back of an unspent advance, and how.
    refunded = db.Column(db.Float, default=0.0)
    refund_method = db.Column(db.String(16))
    refunded_at = db.Column(db.DateTime)
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    customer = db.relationship("Customer", backref=db.backref("advances", lazy=True))
    cashier = db.relationship("User", foreign_keys=[cashier_id])
    refunded_by = db.relationship("User", foreign_keys=[refunded_by_id])
    location = db.relationship("Location")
    counter = db.relationship("Counter")


# ---------- Coupons ----------
COUPON_KINDS = ("amount", "percent")


class CouponCampaign(db.Model):
    """The rule a batch of coupons is cut from — what it is worth and when.

    `issue_at_settlement` makes the till hand one out by itself: a bill of at
    least `settlement_min_bill` gets a fresh coupon for the next visit, printed on
    the bill. Otherwise coupons are issued by hand from the Coupons screen.
    """
    __tablename__ = "coupon_campaigns"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    kind = db.Column(db.String(8), nullable=False, default="amount")   # amount/percent
    value = db.Column(db.Float, nullable=False, default=0.0)
    #: The bill has to come to at least this before the coupon applies.
    min_bill = db.Column(db.Float, default=0.0)
    #: A cap on a percentage coupon's rupees. Null = no cap.
    max_discount = db.Column(db.Float)
    #: How long each coupon cut from this is good for, from the day it is issued.
    valid_days = db.Column(db.Integer, default=30)
    #: Uses per coupon; null means as often as it is presented.
    uses_per_coupon = db.Column(db.Integer, default=1)
    issue_at_settlement = db.Column(db.Boolean, default=False)
    settlement_min_bill = db.Column(db.Float, default=0.0)
    active = db.Column(db.Boolean, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    coupons = db.relationship("Coupon", backref="campaign", lazy=True)

    @property
    def describe(self):
        worth = (f"₹{self.value:g} off" if self.kind == "amount"
                 else f"{self.value:g}% off" + (f" (up to ₹{self.max_discount:g})"
                                                if self.max_discount else ""))
        return worth + (f" on bills of ₹{self.min_bill:g}+" if self.min_bill else "")


class Coupon(db.Model):
    """One code a customer can present. Cut from a campaign."""
    __tablename__ = "coupons"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("coupon_campaigns.id"), nullable=False, index=True)
    #: Tied to one customer, or null for a code anyone may present.
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    valid_from = db.Column(db.Date)
    valid_to = db.Column(db.Date)
    uses_allowed = db.Column(db.Integer)
    #: manual · settlement
    issued_via = db.Column(db.String(16), default="manual", index=True)
    #: The bill that earned it, for a settlement coupon.
    issued_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), index=True)
    active = db.Column(db.Boolean, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship("Customer")
    issued_invoice = db.relationship("Invoice", foreign_keys=[issued_invoice_id])
    redemptions = db.relationship("CouponRedemption", backref="coupon", lazy=True)

    @property
    def uses(self):
        return sum(1 for r in self.redemptions if not r.voided_at)

    def state(self, today=None):
        """withdrawn · expired · used · valid — worked out, never stored."""
        today = today or date.today()
        if not self.active:
            return "withdrawn"
        if self.valid_to and self.valid_to < today:
            return "expired"
        if self.uses_allowed is not None and self.uses >= self.uses_allowed:
            return "used"
        return "valid"


class CouponRedemption(db.Model):
    """A coupon spent on a bill. Voided, not deleted, when that bill is cancelled."""
    __tablename__ = "coupon_redemptions"
    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer, db.ForeignKey("coupons.id"), nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    voided_at = db.Column(db.DateTime)

    invoice = db.relationship("Invoice", backref=db.backref("coupon_redemptions", lazy=True))


# ---------- Customer feedback ----------
class CustomerFeedback(db.Model):
    """How a customer said it went — a rating and, if they gave one, a comment."""
    __tablename__ = "customer_feedback"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), index=True)
    rating = db.Column(db.Integer, nullable=False)          # 1–5
    comments = db.Column(db.Text)
    #: counter — typed in by staff · link — sent by the customer from the bill
    source = db.Column(db.String(16), default="counter")
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship("Customer")
    invoice = db.relationship("Invoice")
    recorded_by = db.relationship("User")


# ---------- Scheduled messages ----------
MESSAGE_KINDS = ("birthday", "anniversary", "offer", "custom")


class ScheduledMessage(db.Model):
    """One message to one customer, due at a time — and what happened to it.

    The log IS the record. `logged` means it came due while no SMS or WhatsApp
    provider was configured, so nothing left the building; it stays in the log
    saying so rather than pretending to have been sent. See app/messaging.py.
    """
    __tablename__ = "scheduled_messages"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    phone = db.Column(db.String(32))
    kind = db.Column(db.String(16), default="custom", index=True)
    body = db.Column(db.Text, nullable=False)
    #: Shop wall-clock time, not UTC — see app/messaging.py.
    scheduled_for = db.Column(db.DateTime, default=datetime.now, index=True)
    #: queued · sent · failed · logged · cancelled
    status = db.Column(db.String(16), default="queued", index=True)
    attempts = db.Column(db.Integer, default=0)
    sent_at = db.Column(db.DateTime)
    error = db.Column(db.String(256))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")
    created_by = db.relationship("User")


# ---------- Cash drawer ----------
class DrawerSession(db.Model):
    """One till's drawer, from the float put in to the count taken out.

    `expected_cash` is written when the drawer is closed, from the cash that
    moved through this till in between — see app/drawer.py — so a closed session
    keeps saying what the books expected even if a bill is cancelled later.
    """
    __tablename__ = "drawer_sessions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    counter_id = db.Column(db.Integer, db.ForeignKey("counters.id"), index=True)
    opened_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    opening_float = db.Column(db.Float, nullable=False, default=0.0)
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_at = db.Column(db.DateTime, index=True)
    counted_cash = db.Column(db.Float)
    expected_cash = db.Column(db.Float)
    notes = db.Column(db.String(256))

    opened_by = db.relationship("User", foreign_keys=[opened_by_id])
    closed_by = db.relationship("User", foreign_keys=[closed_by_id])
    location = db.relationship("Location")
    counter = db.relationship("Counter")

    @property
    def difference(self):
        if self.counted_cash is None or self.expected_cash is None:
            return None
        return round(self.counted_cash - self.expected_cash, 2)


# ---------- Staff advances ----------
class StaffAdvance(db.Model):
    """Money paid to a member of staff ahead of salary."""
    __tablename__ = "staff_advances"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    given_on = db.Column(db.Date, default=date.today, index=True)
    method = db.Column(db.String(16), default="cash")
    note = db.Column(db.String(256))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id],
                           backref=db.backref("advances", lazy=True))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    recoveries = db.relationship("StaffAdvanceRecovery", backref="advance", lazy=True,
                                 cascade="all, delete-orphan",
                                 order_by="StaffAdvanceRecovery.id")

    @property
    def recovered(self):
        return round(sum(r.amount or 0 for r in self.recoveries), 2)

    @property
    def balance(self):
        return round((self.amount or 0) - self.recovered, 2)


class StaffAdvanceRecovery(db.Model):
    """Some of an advance paid back — off the salary, or in cash."""
    __tablename__ = "staff_advance_recoveries"
    id = db.Column(db.Integer, primary_key=True)
    advance_id = db.Column(db.Integer, db.ForeignKey("staff_advances.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    recovered_on = db.Column(db.Date, default=date.today)
    #: salary · cash
    method = db.Column(db.String(16), default="salary")
    note = db.Column(db.String(256))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Sales promotion schemes — the store's own offers, as the shop's billing
# software has always laid them out: a header (when, where, at which point in a
# sale), FILTERS saying which goods or bills qualify, and BENEFITS saying what
# they get. The engine that reads them is app/schemes.py; the screen that writes
# them is routes/promotions.py. The older buy-X-get-Y schemes (PromotionScheme
# above) still run alongside, unchanged.
# ---------------------------------------------------------------------------

#: When in a sale a scheme pays out. (code, label)
SCHEME_APPLICABLE = (
    ("same_bill", "On Same Bill"),
    ("next_bill", "On Next Bill"),
    ("next_day_bill", "On Next Day Bill"),
    ("settlement", "On Settlement"),
    ("addon", "On Bill (Addon)"),
    ("simulation", "On Simulation"),
)

#: What a scheme's filter rows can say. (code, label)
SCHEME_FILTER_MODES = (
    ("product_attr", "Product Attribute"),
    ("invoice", "Invoice"),
    ("barcode", "Barcode"),
    ("marker", "Marked Item"),
    ("ex_barcode", "Excluded Barcode"),
    ("ex_marker", "Excluded Marker"),
    ("ex_discount", "Excluded Discount"),
    ("ex_product_attr", "Excluded Product Attribute"),
    ("bill_value", "Bill/Bill Item Value"),
    ("agreement_code", "Agreement Code"),
    ("barcode_prefix", "Barcode Prefix"),
)

#: Where a scheme's condition is measured: the bill as a whole, or each item.
SCHEME_MODES = (
    ("bill_value", "On Bill Value"),
    ("bill_item", "On Bill Item"),
)

#: What a benefit row gives, in the order the screen offers them. (code, label)
SCHEME_BENEFIT_MODES = (
    ("discount_pct", "Discount In Percentage"),
    ("fixed", "Discount In Fixed Value"),
    ("points", "Reward Points"),
    ("free_item", "Discount In Qty"),
    ("combo", "Combo Offer"),
    ("discounted_item", "Discounted Item"),
    ("coupon", "Coupon for next bill"),
)
#: Older rows may still carry these; the engine reads them, the screen no longer offers them.
SCHEME_BENEFIT_LEGACY = (("flat", "Flat ₹ off"),)


class SalesScheme(db.Model):
    __tablename__ = "sales_schemes"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    #: Null = every company / every location.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), index=True)
    valid_from = db.Column(db.Date, index=True)
    valid_to = db.Column(db.Date, index=True)
    applicable = db.Column(db.String(16), nullable=False, default="same_bill")
    #: bill_value — the Rate condition is the qualifying goods' total;
    #: bill_item — it is each item's own price. See SCHEME_MODES.
    mode = db.Column(db.String(16), default="bill_item")
    active = db.Column(db.Boolean, default=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company")
    location = db.relationship("Location")
    filters = db.relationship("SalesSchemeFilter", backref="scheme", lazy=True,
                              cascade="all, delete-orphan",
                              order_by="SalesSchemeFilter.sort_order")
    benefits = db.relationship("SalesSchemeBenefit", backref="scheme", lazy=True,
                               cascade="all, delete-orphan",
                               order_by="SalesSchemeBenefit.sort_order")

    def runs_on(self, day):
        return ((self.valid_from is None or self.valid_from <= day)
                and (self.valid_to is None or day <= self.valid_to))

    def status(self, today=None):
        today = today or date.today()
        if not self.active:
            return "inactive"
        if self.valid_from and today < self.valid_from:
            return "scheduled"
        if self.valid_to and today > self.valid_to:
            return "expired"
        return "active"

    @property
    def applicable_label(self):
        return dict(SCHEME_APPLICABLE).get(self.applicable, self.applicable)

    @property
    def mode_label(self):
        return dict(SCHEME_MODES).get(self.mode or "bill_item", self.mode)


class SalesSchemeFilter(db.Model):
    """One FILTER row — which goods, or which bills, the scheme is for.

    `data` is JSON, its shape set by `mode` (see app/schemes.py): attribute
    values for Product Attribute, a list of codes for Barcode, two amounts for
    Bill Value, and so on.
    """
    __tablename__ = "sales_scheme_filters"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("sales_schemes.id"), nullable=False, index=True)
    mode = db.Column(db.String(24), nullable=False)
    data = db.Column(db.Text, default="{}")
    sort_order = db.Column(db.Integer, default=0)


class SalesSchemeBenefit(db.Model):
    """One BENEFIT row — what qualifying goods get, within a qty and price range.

    Ranges are inclusive and a 0 / empty upper end means "no limit". What
    `value` and `additional` mean depends on `mode` — see app/schemes.py.
    """
    __tablename__ = "sales_scheme_benefits"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("sales_schemes.id"), nullable=False, index=True)
    mode = db.Column(db.String(16), nullable=False)
    qty_from = db.Column(db.Float, default=0.0)
    qty_to = db.Column(db.Float, default=0.0)
    price_from = db.Column(db.Float, default=0.0)
    price_to = db.Column(db.Float, default=0.0)
    value = db.Column(db.Float, default=0.0)
    additional = db.Column(db.Float, default=0.0)
    #: Fixed Value / Combo Offer: the amount comes off ONCE for the bill rather
    #: than per piece / per set (the screen's "Flat Discount" tick).
    flat = db.Column(db.Boolean, default=False)
    #: Discounted Item: the barcode / SKU of the item that gets the discount.
    item_code = db.Column(db.String(64))
    #: The coupon campaign a Coupon benefit issues from, made when it is saved.
    campaign_id = db.Column(db.Integer, db.ForeignKey("coupon_campaigns.id"))
    sort_order = db.Column(db.Integer, default=0)
    campaign = db.relationship("CouponCampaign")


class SalesSchemeUse(db.Model):
    """What a scheme did on one bill — for the reports and for undoing it."""
    __tablename__ = "sales_scheme_uses"
    id = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer, db.ForeignKey("sales_schemes.id"), nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    #: bill · settlement · next_bill
    stage = db.Column(db.String(16), nullable=False)
    benefit_value = db.Column(db.Float, default=0.0)
    coupon_id = db.Column(db.Integer, db.ForeignKey("coupons.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    scheme = db.relationship("SalesScheme")
    coupon = db.relationship("Coupon")
