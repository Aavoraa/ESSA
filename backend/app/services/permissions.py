"""Per-screen access: what one account may VIEW, CREATE, MODIFY, DELETE and PRINT.

Roles answer "how much of this app is yours" in three steps, and three steps is
not enough for a warehouse. A receiving clerk needs the GRN screen but must not
post a return; a stock auditor needs to read every screen and change none of
them. Both of those are inside one role today, so the only way to express them
was to hand out the whole role.

So a user may carry a GRANT MAP on top of their role: screen → the actions they
have on it. Two rules make it safe to add to a system that has been running
without it:

  * **The role is still the ceiling.** A grant cannot reach past it. Ticking
    Reports for a floor user does not open Reports — `security.POLICY` still
    says Reports is admin, and it is still the thing that refuses. This keeps
    one answer to "what is dangerous", and it means a mis-tick cannot escalate
    anybody.
  * **No map means no narrowing.** A user with nothing recorded behaves exactly
    as they did before — their role decides, as it always has. Existing accounts
    are therefore untouched until somebody deliberately restricts one, and an
    empty map is never mistaken for "denied everything", which would lock the
    warehouse out of its own app on the morning of an upgrade.

The screens are the app's own modules, and the paths that serve them are already
enumerated once, in `security.POLICY`. They are not enumerated a second time
here: a table of path→screen kept beside a table of path→role is two tables that
drift, and the drift is silent and is a security hole. POLICY carries the screen.
"""

#: What can be done to a screen. `print` is separate from `view` because reading
#: a cost on screen and walking out with it on paper are different acts, and the
#: reference system this warehouse came from has always distinguished them.
ACTIONS = [
    ("view", "View", "open the screen and read it"),
    ("create", "Create", "add new records on it"),
    ("modify", "Modify", "change records that are already there"),
    ("delete", "Delete", "remove records"),
    ("print", "Print", "print labels, or export the register to a file"),
]
ACTION_KEYS = [a for a, _, _ in ACTIONS]

#: The screens, in the order the menu shows them. `group` is the heading they sit
#: under; `min` repeats the role floor from security.POLICY so the editor can say
#: WHY a box it is showing will not take effect for this user.
SCREENS = [
    # The owner's screen: the whole business, every warehouse, store, till and
    # person on one page. Super admin and above.
    ("command",    "Command Center", "Company", "superadmin"),
    # The one-line answer box and the end-to-end trace behind it. Admin, and
    # answered within the account's allotted warehouses.
    ("ask",        "Ask Anything & Track", "Company", "admin"),
    # Who did what, when and where.
    ("audit",      "Audit Trail", "Company", "admin"),
    # The company-wide view. Admin, because it puts every warehouse's stock
    # valuation on one screen — which is a different thing from the floor
    # dashboard beside it, and a different audience.
    ("central",    "Central Dashboard", "Company", "admin"),
    ("dashboard",  "Warehouse Dashboard", "Warehouse", None),
    # The order comes first in the business chain, and the menu follows the chain:
    # order the goods, book the lorry in, read the invoice, receive against it.
    ("purchase_orders", "Purchase Orders", "Warehouse", None),
    ("lr",         "Transport / LR Entry", "Warehouse", None),
    ("documents",  "Invoice Entry", "Warehouse", None),
    ("purchases",  "GRN — Receive Goods", "Warehouse", None),
    ("inventory",  "Inventory", "Warehouse", None),
    ("locator",    "Item Locator", "Warehouse", None),
    ("stock_audit", "Warehouse Stock Audit", "Warehouse", None),
    ("physical_audit", "Physical Stock Audit", "Warehouse", None),
    ("labelprint", "QR / Label Printing", "Warehouse", None),
    ("outward",    "Stock Outward", "Warehouse", None),
    ("inward",     "Stock Inward", "Warehouse", None),
    ("deadstock",  "Dead Stock & Clearance", "Office", "admin"),
    ("returns",    "Returns / Debit Notes", "Office", "admin"),
    ("payments",   "Payments", "Office", "admin"),
    ("reports",    "Reports", "Office", "admin"),
    ("suppliers",  "Suppliers", "Setup", "admin"),
    ("masters",    "Masters", "Setup", "admin"),
    ("catalogues", "Catalogues", "Setup", "admin"),
    ("locations",  "Locations", "Setup", "admin"),
    ("labels",     "Label Designer", "Setup", "admin"),
    ("users",      "Users & Access", "Setup", "superadmin"),
]
SCREEN_KEYS = [k for k, _, _, _ in SCREENS]

#: Figures a screen may be told to withhold. Only these four, because these four
#: are the ones this app can actually enforce — they are stripped in
#: `stock_view.product_card` and `inventory._product_out`, which is every path a
#: price reaches a screen by. A checkbox that hides nothing is worse than no
#: checkbox: it is a promise the software does not keep.
DATA_PERMISSIONS = [
    ("hide_cost_price", "Hide cost price",
     "the GRN cost and weighted-average cost, everywhere they are shown"),
    ("hide_selling_price", "Hide selling price",
     "the shelf price and its discount"),
    ("hide_mrp", "Hide MRP", "the printed retail price"),
    ("hide_supplier", "Hide supplier",
     "who the goods were bought from, on stock and locator screens"),
]
DATA_KEYS = [k for k, _, _ in DATA_PERMISSIONS]

#: How much of the STORE (the shop mounted at /pos) this account may open. Two
#: levels, because the question at a shop is simpler than at a warehouse: the
#: person at the till either runs the store or bills in it.
#:
#: `admin` is what nothing recorded means — every Store screen, exactly as before
#: this existed — so it is never stored; only `user` is. The shop's own sign-in is
#: still underneath it and still the ceiling: this can hide and refuse screens,
#: never open one the shop login could not.
STORE_ACCESS = [
    ("admin", "Admin", "every Store screen"),
    ("user", "User", "Billing, Delivery, Store Stock, Customers, Returns and Alteration only"),
]
STORE_KEYS = [k for k, _, _ in STORE_ACCESS]

#: The request header the /pos mount hands the shop when an account is a Store
#: user. It can only ever NARROW what the shop allows, so a copy sent by a client
#: to a standalone shop costs that client access and gains it nothing — and the
#: mount strips any incoming copy before setting its own regardless.
STORE_HEADER = "x-essa-store-access"


def normalise(raw):
    """A stored permission blob, cleaned of anything this app does not know.

    Keys that are not screens, actions that are not actions and data flags that
    are not flags are dropped rather than kept: a permission nothing enforces is
    a promise the software does not keep, and the way one gets in is a rename.
    """
    raw = raw if isinstance(raw, dict) else {}
    screens = {}
    for key, acts in (raw.get("screens") or {}).items():
        if key not in SCREEN_KEYS:
            continue
        keep = sorted({a for a in (acts or []) if a in ACTION_KEYS},
                      key=ACTION_KEYS.index)
        if keep:
            screens[key] = keep
    data = sorted({d for d in (raw.get("data") or []) if d in DATA_KEYS},
                  key=DATA_KEYS.index)
    # Which BUILDINGS this account may work inside. Ids, not names: a warehouse
    # can be renamed, and an allotment that pointed at a string would silently
    # come loose the moment somebody corrected a spelling.
    #
    # This replaces an earlier `locations` key that held NAMES and that nothing
    # ever enforced. It is a new key rather than a reused one because the old
    # word already means something else here — `locations` is a SCREEN key in
    # SCREENS above — and one word meaning two things in one blob is how the
    # wrong check gets written later.
    warehouses = sorted({int(x) for x in (raw.get("warehouses") or [])
                         if str(x).strip().lstrip("-").isdigit() and int(x) > 0})
    out = {}
    if screens:
        out["screens"] = screens
    if data:
        out["data"] = data
    if warehouses:
        out["warehouses"] = warehouses
    # Only the narrowed level is kept — see STORE_ACCESS. Anything else,
    # "admin" included, is the default and is left out.
    if raw.get("store") == "user":
        out["store"] = "user"
    return out


def store_access(perms, role=None):
    """`admin` (every Store screen) or `user` (the floor's screens — billing,
    delivery, store stock, customers, returns, alteration).

    An account whose ROLE is `user` is always the latter, whatever its map says:
    the Store's reports, promotions, staff and till setup are not floor work. An
    admin or above is narrowed only when Users & Access says so."""
    if role == "user":
        return "user"
    return "user" if (perms or {}).get("store") == "user" else "admin"


def allotted(perms):
    """The warehouse ids this account is confined to, or [] for all of them.

    EMPTY MEANS EVERY WAREHOUSE, exactly as an empty screen map means every
    screen. That is what keeps every account that predates allotments working:
    nobody is restricted until somebody deliberately restricts them, and an
    empty list is never read as "denied everywhere" — which would lock the whole
    company out of its own app on the morning of an upgrade.
    """
    return list((perms or {}).get("warehouses") or [])


def may_enter(perms, warehouse_id):
    """Whether this account may work inside that warehouse.

    True when nothing is allotted (see `allotted`), and true when no warehouse
    was named at all — deciding what an unscoped request may see is a separate
    question, answered in security.auth_middleware, because the answer depends
    on which screen is being asked for.
    """
    ids = allotted(perms)
    if not ids or not warehouse_id:
        return True
    return int(warehouse_id) in ids


def has_map(perms):
    """Whether this account has been restricted at all.

    The distinction the whole feature turns on: **nothing recorded** means the
    role decides, exactly as before. It does NOT mean denied.
    """
    return bool((perms or {}).get("screens"))


def allows(perms, screen, action):
    """May this account do `action` on `screen`?

    True when nothing has been recorded — see has_map. `view` is implied by every
    other action: an account that may create a GRN can obviously open the GRN
    screen, and making somebody tick both is a way to produce accounts that can
    write to a screen they cannot read.
    """
    if not has_map(perms):
        return True
    if screen is None:
        return True                     # a path no screen claims; the role decides
    granted = (perms.get("screens") or {}).get(screen) or []
    if action in granted:
        return True
    return action == "view" and bool(granted)


def hides(perms, flag):
    """Whether a figure is withheld from this account."""
    return flag in ((perms or {}).get("data") or [])


def template(role):
    """The grant map a role starts from — everything that role can reach anyway.

    Offered in the editor as a starting point, because the useful restriction is
    almost always "all of this except two things", and building that up from
    seventeen empty rows is how the job gets abandoned half-done.
    """
    from .users import ROLE_RANK
    rank = ROLE_RANK.get(role, 0)
    screens = {}
    for key, _, _, need in SCREENS:
        if need and rank < ROLE_RANK.get(need, 99):
            continue
        screens[key] = list(ACTION_KEYS)
    return {"screens": screens}


def catalog():
    """Everything the editor needs to draw itself, named the same on both sides."""
    return {
        "actions": [{"key": k, "label": l, "why": w} for k, l, w in ACTIONS],
        "screens": [{"key": k, "label": l, "group": g, "min": m}
                    for k, l, g, m in SCREENS],
        "data_permissions": [{"key": k, "label": l, "why": w}
                             for k, l, w in DATA_PERMISSIONS],
        "store_access": [{"key": k, "label": l, "why": w}
                         for k, l, w in STORE_ACCESS],
    }
