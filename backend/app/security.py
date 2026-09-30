"""
Who is allowed to call what.

The rule is one table, not a decorator on each of the ~130 routes. A decorator
per route is the usual shape, but here it would mean touching twenty router
files and, worse, it would mean that the answer to "what can a floor user
actually do?" is only obtainable by reading all twenty. POLICY below is that
answer, on one screen, and a route added tomorrow inherits the rule for its
prefix instead of being silently unprotected because someone forgot the
decorator.

The split, per module:

    user        LR, invoice entry, GRN, inventory, bundles, outward/inward,
                label printing, notifications, voice, dashboard
    admin       the above plus masters, suppliers, label design, reports,
                payments, returns, dead stock, asking questions, the audit trail
    superadmin  the above plus accounts, server settings and the Command Center
    superboss   everything a super admin has — the difference is in who may
                manage whom (services/users.may_manage), not in a path here

Reads and writes are ranked separately because the two do not follow each
other. Masters are an admin screen, but the floor cannot record a receipt
without reading the category and unit lists behind it — so masters read at
`user` and write at `admin`. Payments are admin at both ends: nothing on the
floor asks what a supplier is owed.
"""
import re

from fastapi import Request
from fastapi.responses import JSONResponse

from . import models
from .database import SessionLocal
from .services import audit
from .services import permissions
from .services import scope
from .services.users import ROLE_RANK, resolve_token

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# (regex on the path, role needed to read, role needed to write, screen). First
# match wins, so the specific entries come before the prefix they sit inside.
#
# The fourth column is which SCREEN the path belongs to, and it is here rather
# than in a table of its own for the same reason the roles are: a second table of
# path→screen kept beside this one is two tables that drift, and a drifted screen
# key is a permission that silently stops applying. `None` means no screen claims
# the path — the notification poll, the voice endpoint — and those are decided by
# the role alone. See services/permissions.
#: Screens that show ONE BUILDING'S work. A request for one of these from an
#: account that is allotted warehouses must say which — otherwise the scope
#: filters in services/scope have nothing to narrow on and hand back the whole
#: company. Screens absent from this set are company-wide by nature (suppliers,
#: masters, catalogues, users, the label designer) or are the very screens used
#: to choose a warehouse, and must stay reachable without one.
WAREHOUSE_SCOPED = {
    "dashboard", "lr", "documents", "purchases", "purchase_orders", "inventory",
    # A count is of ONE building's shelves — more literally than anything else on
    # this list, because somebody physically walked them. Two counts of two
    # warehouses appearing as one would be a count of nothing.
    "stock_audit",
    # The counted stocktake, for the same reason and rather more so: its variance
    # is measured against ONE building's balances, and a count that spanned two
    # would report each as short by whatever the other holds.
    "physical_audit",
    "labelprint", "outward", "inward", "returns", "deadstock", "reports",
}

POLICY = [
    # --- accounts and the server's own configuration ---
    (r"^/api/users", "superadmin", "superadmin", "users"),
    (r"^/api/settings", "superadmin", "superadmin", "users"),

    # --- the company's control center ---
    # Asking a question and tracing a document are an admin's tools as much as the
    # owner's: the answer is scoped to whatever buildings the account is allotted
    # (services/ask_anything), so nothing here reaches past what the account's own
    # screens already show. The Command Center dashboard itself — every warehouse,
    # every store, every user's activity on one screen — is the super admin's and
    # the Super Boss's.
    (r"^/api/command/(ask|trace|examples)", "admin", "admin", "ask"),
    (r"^/api/command", "superadmin", "superadmin", "command"),
    # Who did what. Admin may read it — confined to their own warehouses, and
    # without the account-management lines (routers/audit).
    (r"^/api/audit", "admin", "admin", "audit"),

    # --- setup the floor reads and only admin edits ---
    (r"^/api/masters", "user", "admin", "masters"),
    (r"^/api/master-data", "user", "admin", "masters"),
    # Where this company trades from. The floor READS these all day — a
    # dispatch has to name the branch it is going to — and only admin adds a
    # warehouse or closes a store, which is the same shape as the masters
    # above it.
    # The central dashboard's own read, matched off before the locations prefix
    # it sits inside: it is a company-wide stock VALUATION, which is admin, while
    # the location lists beneath it are read by the floor all day.
    (r"^/api/locations/overview", "admin", "admin", "central"),
    # Shop takings per branch. Admin for the same reason the overview is:
    # it is company revenue on one screen. Matched off ABOVE the general
    # locations row, or the floor would read it at `user` rank.
    (r"^/api/locations/sales", "admin", "admin", "central"),
    (r"^/api/locations", "user", "admin", "locations"),
    # What each warehouse trades in — its categories, its attributes, its option
    # lists. Read by the floor because the GRN screen has to know which
    # categories to offer; edited by admin, like every other master.
    (r"^/api/catalogues", "user", "admin", "catalogues"),
    (r"^/api/suppliers", "user", "admin", "suppliers"),
    # Label templates: the floor prints from them all day, admin lays them out.
    # /print and /templates/{id}/preview are GETs, so they fall to the read rank.
    # Printing is its OWN screen, though — the person putting goods away opens it
    # every day and never opens the designer — so it is matched off first.
    (r"^/api/labels/print", "user", "user", "labelprint"),
    (r"^/api/labels", "user", "admin", "labels"),

    # --- admin at both ends ---
    (r"^/api/reports", "admin", "admin", "reports"),
    (r"^/api/payments", "admin", "admin", "payments"),
    (r"^/api/returns", "admin", "admin", "returns"),
    (r"^/api/dead-stock", "admin", "admin", "deadstock"),

    # --- the floor ---
    # "Clear all" empties every transaction table and can take the masters with
    # it. It sits inside a user-level prefix but is not user-level work: it is
    # irreversible, global, and one mis-click from a screen the floor uses all
    # day. Listed above /api/documents so it is matched first.
    (r"^/api/documents/clear-all", "superadmin", "superadmin", "users"),
    (r"^/api/documents", "user", "user", "documents"),
    # Raising an order is floor work — the buying desk does it all day, exactly
    # as receiving is done on the GRN screen below. Its own screen key, so an
    # account can be given the order book without the receipt book.
    (r"^/api/purchase-orders", "user", "user", "purchase_orders"),
    (r"^/api/purchases", "user", "user", "purchases"),
    # Counting the shelf is floor work and its own screen: a stock auditor reads
    # every figure and changes none, which is exactly the split the permissions
    # module was written for. Its own prefix, so it is not governed by the
    # inventory screen's grants even though it reads through them.
    (r"^/api/stock-audit", "user", "user", "stock_audit"),
    # Correcting the books off a count is the one act in this module that moves
    # stock, and it is admin — matched off ABOVE the prefix it sits inside, or the
    # floor would inherit it. Everything else here is floor work: walking a rack,
    # typing what is on it, and saying the sheet is finished are all done by the
    # people who count, and none of them changes a balance.
    (r"^/api/physical-audit/[0-9]+/apply", "admin", "admin", "physical_audit"),
    (r"^/api/physical-audit", "user", "user", "physical_audit"),
    # What things sell for. READ by the floor — a price is on every screen that
    # quotes one — and CHANGED only by admin, because it is money and a bulk
    # change moves thousands of them at once. Not warehouse-scoped: a product is
    # company-wide and carries one price, however many buildings hold it.
    (r"^/api/pricing", "user", "admin", "pricing"),
    # The locator reads the whole account of one item and writes nothing. It is
    # its own screen and sits above /api/inventory, which it lives inside.
    (r"^/api/inventory/locate", "user", "user", "locator"),
    (r"^/api/inventory", "user", "user", "inventory"),
    (r"^/api/lr", "user", "user", "lr"),
    (r"^/api/bundles", "user", "user", "inventory"),
    # One router serves both ends of a transfer: dispatching it and accepting it.
    # They are two screens and two jobs — the warehouse packs, the shop receives —
    # so the receiving paths are matched off first.
    (r"^/api/outward/[0-9]+/receive", "user", "user", "inward"),
    (r"^/api/outward/inbox", "user", "user", "inward"),
    (r"^/api/outward", "user", "user", "outward"),
    (r"^/api/notifications", "user", "user", None),
    (r"^/api/voice", "user", "user", None),
    (r"^/api/dashboard", "user", "user", "dashboard"),
    # The search index's health — server administration, not business data.
    (r"^/api/search", "superadmin", "superadmin", None),
]

POLICY_RE = [(re.compile(p), r, w, s) for p, r, w, s in POLICY]

#: HTTP method -> what it is doing to the screen. PATCH and PUT are both
#: "change what is there"; POST is the only one that can mean either, and it
#: means create far more often than not — a POST that merely acts on an existing
#: record (posting a GRN, receiving a transfer) is covered because `create`
#: without `modify` is not a combination anybody grants.
METHOD_ACTION = {"GET": "view", "HEAD": "view", "OPTIONS": "view",
                 "POST": "create", "PUT": "modify", "PATCH": "modify",
                 "DELETE": "delete"}

#: …except when what it is doing is printing. A label, a QR, a barcode or a CSV
#: leaves the building on paper or on disk, which the reference system this
#: warehouse came from has always treated as its own permission — and rightly:
#: reading a cost on screen and walking out with it are different acts.
PRINT_RE = re.compile(r"(/print|/label|/qr\.(svg|png)|/barcode|\.csv$|[?&]format=csv)")

#: POSTs that only read — see required_access.
READ_POSTS_RE = re.compile(r"^/api/command/ask$")

# Reachable without a token. /api/auth is how you get one; /api/status is what
# the login screen probes before anyone has one; the mobile PWA shell and the
# built desktop bundle are static files whose own first call is the login.
PUBLIC = [
    re.compile(r"^/api/auth/"),
    re.compile(r"^/api/status$"),
    # Applying the schema to a deployment that skips it at start — see
    # main.admin_boot. Public HERE and authorised THERE, by ESSA_AUTH_SECRET in
    # a header, because the state it exists to repair includes a users table
    # with nothing in it: there is no super admin to sign in as yet, so a token
    # check would lock the fix behind the thing it fixes.
    re.compile(r"^/api/admin/boot$"),
    re.compile(r"^/docs"), re.compile(r"^/redoc"), re.compile(r"^/openapi.json$"),
]


def required_role(method: str, path: str):
    """The role this request needs, or None if the path is not policed here."""
    return required_access(method, path)[0]


def required_access(method: str, path: str):
    """(role, screen, action) for a request.

    Anything under /api that no entry matches gets the strictest answer rather
    than a free pass — a new router mounted without a POLICY line should be
    visibly locked, not quietly open.
    """
    up = method.upper()
    action = METHOD_ACTION.get(up, "modify")
    if action == "view" and PRINT_RE.search(path):
        action = "print"
    # A question is sent as a POST because it is a body, not because it changes
    # anything — an account granted only View on Ask Anything must still be able
    # to ask.
    if up == "POST" and READ_POSTS_RE.match(path):
        action = "view"
    for rx, read_role, write_role, screen in POLICY_RE:
        if rx.match(path):
            return (read_role if up in READ_METHODS else write_role), screen, action
    if path.startswith("/api/"):
        return "superadmin", None, action
    return None, None, action


def token_from(request: Request) -> str:
    """A token can arrive four ways, and all four are in use.

    The header is what the two apps send on their own calls. The cookie is for
    the requests the app does not make by hand — an <img> pointing at an invoice
    scan, or a report opened in a new tab — which cannot carry a header. The
    query parameter is the last resort for the same case, and for `curl`.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-essa-token")
            or request.cookies.get("essa_token")
            or request.query_params.get("token")
            or "")


def is_public(path: str) -> bool:
    return any(rx.match(path) for rx in PUBLIC)


def may_read(request: Request, path: str) -> bool:
    """Whether the account behind this (already admitted) request could GET
    `path` itself — the role, grant and allotment checks auth_middleware makes,
    in the same order.

    For an endpoint that answers several screens' questions in one call (the
    warehouse dashboard's summary): each part is given only to an account that
    could have asked for it directly, so combining the calls widens nothing."""
    need, screen, action = required_access("GET", path)
    if need is None or is_public(path):
        return True
    who = getattr(request.state, "user", None)
    if not who:
        return False
    if ROLE_RANK.get(who.get("role"), 0) < ROLE_RANK[need]:
        return False
    if not permissions.allows(who.get("permissions") or {}, screen, action):
        return False
    mine = getattr(request.state, "warehouses", None)
    if mine:
        wid = scope.from_request(request)
        if wid and int(wid) not in mine:
            return False
        if wid is None and screen in WAREHOUSE_SCOPED and len(mine) != 1:
            return False
    return True


async def auth_middleware(request: Request, call_next):
    """Rejects the call before it reaches the route.

    The two failures are kept distinct on purpose: 401 means "you are not signed
    in", which the apps answer by returning to the login screen, and 403 means
    "you are, but not as someone who may do this", which they answer by saying
    so and staying where they are. Collapsing them makes a permission error look
    like an expired session and sends the floor round a login loop.
    """
    path = request.url.path
    need, screen, action = required_access(request.method, path)
    if need is None or is_public(path):
        return await call_next(request)

    method = request.method.upper()
    # Whether this request goes on the audit trail: a change, and one that is an
    # act rather than a question or a preview. See services/audit.
    watch = method in audit.WRITE_METHODS and not audit.skipped(path)
    who = None
    before = None

    def refused(db, why):
        if watch and who:
            audit.record(db, user=who, method=method, path=path, screen=screen,
                         action=action, outcome="refused", status=403,
                         warehouse_id=scope.from_request(request),
                         summary=f"was refused: {audit.generic(screen, action, path)} — {why}")

    db = SessionLocal()
    try:
        user = resolve_token(db, token_from(request))
        if user is None:
            return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
        who = {"username": user.username, "role": user.role,
               "full_name": user.full_name or ""}
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK[need]:
            refused(db, f"needs {need}")
            return JSONResponse(
                {"detail": f"This needs {need} access — you are signed in as {user.role}."},
                status_code=403)
        # …and then, only for accounts that have actually been restricted, what
        # this one may do to THIS screen. The role above is still the ceiling: a
        # grant cannot reach past it, so this can only ever narrow.
        perms = permissions.normalise(user.permissions)
        if not permissions.allows(perms, screen, action):
            label = dict((k, l) for k, l, _, _ in permissions.SCREENS).get(screen, screen)
            verb = dict((k, l) for k, l, _ in permissions.ACTIONS).get(action, action)
            refused(db, f"no {verb} access to {label}")
            return JSONResponse(
                {"detail": f"You do not have {verb} access to {label}. "
                           "Ask a super admin to grant it in Users & Access."},
                status_code=403)
        # …and finally WHICH BUILDING. An account may be allotted a set of
        # warehouses; one that is confined to Erode must not read Karur's work,
        # however it asks.
        #
        # Two cases, and the second is the one that matters. Rejecting a wrong
        # explicit id is the obvious half. The dangerous half is the request that
        # names NO warehouse at all: services/scope returns every row unfiltered
        # when it has no id to filter on, so a restricted manager who simply
        # dropped the header would see the whole company. That is closed here,
        # in the one place every call passes through, rather than in each of the
        # dozen list endpoints — a leak that has to be plugged twelve times is a
        # leak that comes back.
        mine = permissions.allotted(perms)
        if mine:
            wid = scope.from_request(request)
            if wid and int(wid) not in mine:
                wh = db.get(models.Warehouse, int(wid))
                where = f"“{wh.name}”" if wh else f"warehouse #{wid}"
                refused(db, f"not allotted {where}")
                return JSONResponse(
                    {"detail": f"You are not allotted {where}. Ask a super admin "
                               f"to add it in Users & Access."}, status_code=403)
            if wid is None and screen in WAREHOUSE_SCOPED:
                if len(mine) == 1:
                    # Exactly one building: adopt it rather than refuse. Asking a
                    # manager with a single warehouse to say which one on every
                    # request is asking a question with one answer.
                    request.scope["headers"] = list(request.scope["headers"]) + [
                        (scope.HEADER.encode(), str(mine[0]).encode())]
                else:
                    refused(db, "no warehouse was open")
                    return JSONResponse(
                        {"detail": "Open one of your warehouses first — this "
                                   "screen shows one building's work."},
                        status_code=403)
        # Carried on the request so a route can attribute what it writes to the
        # person who did it without resolving the token a second time, so a
        # serialiser can withhold a figure this account is not shown, and so the
        # location reads can narrow themselves to the allotted buildings.
        request.state.user = {"username": user.username, "role": user.role,
                              "full_name": user.full_name or "", "id": user.id,
                              "permissions": perms}
        request.state.warehouses = mine
        # What the audit sentence will need that the route is about to destroy —
        # a deleted GRN's number, a warehouse's name before it was renamed.
        if watch:
            hook, _after, m = audit.match(method, path)
            if hook is not None:
                try:
                    before = hook(db, m)
                except Exception:                  # noqa: BLE001
                    before = None
    finally:
        db.close()

    response = await call_next(request)

    # The change is done; write it down. Only what succeeded — a 400 for a form
    # somebody filled in wrong is not an act — and never allowed to fail the
    # response that has already been produced.
    if watch and who and (response.status_code < 400 or response.status_code == 403):
        try:
            adb = SessionLocal()
            try:
                if response.status_code == 403:
                    # Refused by the route itself rather than by the table above —
                    # a super admin reaching for the Super Boss's password, say.
                    audit.record(adb, user=who, method=method, path=path, screen=screen,
                                 action=action, outcome="refused", status=403,
                                 warehouse_id=scope.from_request(request),
                                 summary=f"was refused: {audit.generic(screen, action, path)}")
                else:
                    said = audit.describe(adb, method, path, screen, action, before,
                                          who["username"],
                                          getattr(request.state, "audit", None))
                    audit.record(adb, user=who, method=method, path=path, screen=screen,
                                 action=said.get("action") or action, outcome="ok",
                                 status=response.status_code,
                                 warehouse_id=(said.get("warehouse_id")
                                               or scope.from_request(request)),
                                 summary=said.get("summary"), ref=said.get("ref"))
            finally:
                adb.close()
        except Exception:                          # noqa: BLE001
            pass
    return response
