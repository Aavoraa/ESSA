"""The shop's modules, in one list.

The dashboard's cards and the header's menu are the same thing shown two ways —
a grid when you arrive, a dropdown once you are working — so they are built from
this rather than each carrying its own copy. Adding a screen here puts it in
both, and neither can quietly fall behind the other.

`owns` is how a screen knows which module it belongs to. Endpoints do not map to
modules one-for-one: the `pos` blueprint holds both the billing counter and the
invoice register, and an invoice being viewed or printed is still Invoices. So a
module claims endpoint prefixes rather than a blueprint.
"""
MODULES = [
    {"key": "floor", "endpoint": "floor.index", "icon": "bi-phone",
     "label": "Floor Sales", "owns": ["floor."], "manager": False,
     "blurb": "Build a sale on the phone while walking the floor"},

    {"key": "counter", "endpoint": "pos.counter", "icon": "bi-cart-check",
     "label": "Billing Counter", "owns": ["pos.counter", "pos.checkout",
                                         "pos.last_invoice", "drawer."], "manager": False,
     "blurb": "Scan and bill, and print the delivery copy"},

    {"key": "settlement", "endpoint": "pos.settle", "icon": "bi-cash-coin",
     "label": "Settlement", "owns": ["pos.settle"], "manager": False,
     "blurb": "Take payment for billed bills and print the customer copy"},

    {"key": "delivery", "endpoint": "delivery.index", "icon": "bi-bag-check",
     "label": "Delivery", "owns": ["delivery."], "manager": False,
     "blurb": "Scan the bill, scan each garment, hand the goods over"},

    {"key": "inventory", "endpoint": "inventory.list_products", "icon": "bi-box-seam",
     "label": "Inventory", "owns": ["inventory."], "manager": False,
     "blurb": "What the shop holds, with the warehouse QR on every item"},

    {"key": "stock_inward", "endpoint": "storestock.inward", "icon": "bi-box-arrow-in-down",
     "label": "Stock Inward", "owns": ["storestock.inward"], "manager": False,
     "blurb": "Count in what the warehouse or another branch sent here"},

    {"key": "transfer_goods", "endpoint": "storestock.transfer", "icon": "bi-box-arrow-up-right",
     "label": "Transfer Goods", "owns": ["storestock.transfer"], "manager": False,
     "blurb": "Send goods back to a warehouse, or across to another branch"},

    {"key": "audits", "endpoint": "audits.index", "icon": "bi-clipboard-check",
     "label": "Stock audit", "owns": ["audits."], "manager": False,
     "blurb": "Count a floor, item by item, and see where the books disagree"},

    {"key": "checker", "endpoint": "checker.index", "icon": "bi-search",
     "label": "Stock check", "owns": ["checker."], "manager": False,
     "blurb": "Scan or filter to find an item and where it is"},

    {"key": "customers", "endpoint": "customers.list_customers", "icon": "bi-people",
     "label": "Customers", "owns": ["customers."], "manager": False,
     "blurb": "Customer master, loyalty points and history"},

    {"key": "invoices", "endpoint": "pos.invoice_list", "icon": "bi-receipt",
     "label": "Invoices", "owns": ["pos.invoice", "pos.view_invoice", "pos.print_invoice"],
     "manager": False,
     "blurb": "Every bill raised, searchable and reprintable"},

    {"key": "returns", "endpoint": "returns.index", "icon": "bi-arrow-return-left",
     "label": "Returns", "owns": ["returns."], "manager": False,
     "blurb": "Take goods back against a bill and raise a credit note"},

    {"key": "alterations", "endpoint": "alterations.index", "icon": "bi-scissors",
     "label": "Alteration", "owns": ["alterations."], "manager": False,
     "blurb": "Garments out for tailoring, and what each tailor is holding"},

    {"key": "stores", "endpoint": "stores.index", "icon": "bi-building",
     "label": "Floors & tills", "owns": ["stores."], "manager": True,
     "blurb": "Which storey each till bills from, and what its bills are called"},

    {"key": "promotions", "endpoint": "promotions.index", "icon": "bi-gift",
     "label": "Promotions", "owns": ["promotions.", "coupons."], "manager": True,
     "blurb": "Offers the till applies by itself, and what they have given away"},

    {"key": "staff", "endpoint": "staff.list_staff", "icon": "bi-person-badge",
     "label": "Staff", "owns": ["staff."], "manager": True,
     "blurb": "Attendance, roles, ID cards and sales commission"},

    {"key": "reports", "endpoint": "reports.index", "icon": "bi-graph-up",
     "label": "Reports", "owns": ["reports."], "manager": True,
     "blurb": "Every register — or just ask a question in plain words"},
]



# ---------- Store access: Admin, or User (billing and reports) ----------
#
# Set per account in the Essa warehouse's Users & Access, and handed to this shop
# on each request by the /pos mount as a header (backend/app/main.py). It can only
# NARROW: a request without it is governed by this shop's own login exactly as
# before, and one with it still needs that login to allow a screen — Reports
# stays a manager's screen here. That is also why trusting a header is safe: a
# client that sends one itself has only taken access away from itself.
STORE_HEADER = "X-Essa-Store-Access"

#: The modules a Store user sees: the floor's day — billing, handing over,
#: looking up stock, the customer, taking goods back, alterations. Nothing that
#: reports on the store or configures it.
STORE_USER_MODULES = ("counter", "settlement", "delivery", "inventory", "customers", "returns",
                      "alterations")

#: …and what billing needs that belongs to no menu entry of its own: signing in
#: and out, choosing the till, the counter's own lookups, the customer search it
#: borrows from Floor Sales, and the bill it opens to print once it is paid. The
#: customer's feedback page is public and reached from the bill's QR.
STORE_USER_ALSO = ("auth.", "static", "feedback.", "pos.place", "pos.api_",
                   "pos.view_invoice", "pos.print_invoice",
                   "floor.customer_lookup", "floor.customer_new")


def store_user(req):
    """Whether this request comes from an account narrowed to billing and reports."""
    try:
        return (req.headers.get(STORE_HEADER) or "").strip().lower() == "user"
    except (AttributeError, RuntimeError):     # no request — a startup task
        return False


def store_user_allows(endpoint):
    """May a Store user reach this endpoint?"""
    if not endpoint:
        return True                            # nothing matched; the 404 answers
    m = current(endpoint)
    if m is not None and m["key"] in STORE_USER_MODULES:
        return True
    return any(endpoint == c or endpoint.startswith(c) for c in STORE_USER_ALSO)


def visible(user, restricted=False):
    """The modules this person may open."""
    is_manager = bool(getattr(user, "is_manager", False))
    return [m for m in MODULES if (is_manager or not m["manager"])
            and (not restricted or m["key"] in STORE_USER_MODULES)]


def current(endpoint):
    """The module a given endpoint belongs to, or None.

    Longest claim wins, so `pos.invoice_list` goes to Invoices rather than to
    whichever module happened to claim `pos.` first.
    """
    if not endpoint:
        return None
    best, best_len = None, -1
    for m in MODULES:
        for claim in m["owns"]:
            if endpoint.startswith(claim) and len(claim) > best_len:
                best, best_len = m, len(claim)
    return best
