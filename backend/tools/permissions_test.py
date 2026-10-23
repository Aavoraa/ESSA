"""Per-screen access: the mapping, the rules, and the middleware actually refusing.

    python backend/tools/permissions_test.py

This is the one part of the app where a bug is not a wrong number on a screen —
it is somebody seeing, or changing, what they were not meant to. So it is checked
from both ends: the table that says which screen a path belongs to, and the live
middleware refusing a real request.

Two properties matter more than the rest, and both are easy to break without
noticing:

  * an account with NOTHING recorded is unrestricted, not denied. Get that
    backwards on an upgrade and the warehouse cannot open its own app on Monday.
  * a grant can only ever NARROW. Ticking Reports for a floor user must not open
    Reports — the role is still the ceiling.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-perm-"), "test.db").replace(os.sep, "/")

from backend.app import models                                   # noqa: E402
from backend.app.database import engine                          # noqa: E402
from backend.app.security import required_access                 # noqa: E402
from backend.app.services import permissions as P                # noqa: E402
from backend.app.main import app                                 # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

models.Base.metadata.create_all(bind=engine)

bad = []


def eq(what, got, want):
    if got != want:
        bad.append(what)
        print("  FAIL  %s\n        got  %r\n        want %r" % (what, got, want))
    else:
        print("  ok    %s" % what)


def head(t):
    print("\n%s" % t)


# ===========================================================================
head("which screen a request belongs to, and what it is doing to it")
eq("reading the GRN list", required_access("GET", "/api/purchases"),
   ("user", "purchases", "view"))
eq("building one", required_access("POST", "/api/purchases/build/3"),
   ("user", "purchases", "create"))
eq("editing a line", required_access("PATCH", "/api/purchases/lines/7"),
   ("user", "purchases", "modify"))
eq("deleting it", required_access("DELETE", "/api/purchases/9"),
   ("user", "purchases", "delete"))

# printing is its own act: a cost read on screen and a cost carried out of the
# building on paper are not the same thing
eq("a label is a print", required_access("GET", "/api/inventory/products/3/label"),
   ("user", "inventory", "print"))
eq("a QR is a print", required_access("GET", "/api/inventory/products/3/qr.svg"),
   ("user", "inventory", "print"))
eq("a CSV export is a print", required_access("GET", "/api/reports/stock.csv"),
   ("admin", "reports", "print"))

# three screens live inside another screen's prefix and must be matched off first
eq("the locator is not Inventory", required_access("GET", "/api/inventory/locate"),
   ("user", "locator", "view"))
eq("label printing is not the designer", required_access("GET", "/api/labels/print"),
   ("user", "labelprint", "print"))
eq("receiving a transfer is Stock Inward",
   required_access("POST", "/api/outward/4/receive"), ("user", "inward", "create"))
eq("dispatching one is Stock Outward",
   required_access("POST", "/api/outward"), ("user", "outward", "create"))

eq("a path no screen claims", required_access("GET", "/api/notifications"),
   ("user", None, "view"))
eq("and an unrouted /api path is locked to superadmin rather than left open",
   required_access("GET", "/api/something-added-tomorrow")[0], "superadmin")

head("the two rules the whole feature turns on")
eq("nothing recorded means the ROLE decides", P.allows({}, "purchases", "delete"), True)
eq("…which is not the same as denied", P.has_map({}), False)
grant = P.normalise({"screens": {"purchases": ["view", "create"], "lr": ["view"]},
                     "data": ["hide_cost_price"], "locations": ["WAREHOUSE"]})
eq("granted", P.allows(grant, "purchases", "create"), True)
eq("not granted", P.allows(grant, "purchases", "delete"), False)
eq("view is implied by any other action", P.allows(grant, "lr", "view"), True)
eq("a screen absent from the map is closed", P.allows(grant, "reports", "view"), False)
eq("a path with no screen is never refused here", P.allows(grant, None, "delete"), True)
eq("junk is dropped rather than stored",
   P.normalise({"screens": {"nope": ["view"], "lr": ["fly", "view"]},
                "data": ["hide_everything"]}),
   {"screens": {"lr": ["view"]}})
eq("a data flag survives", P.hides(grant, "hide_cost_price"), True)
eq("one that was never set does not", P.hides(grant, "hide_supplier"), False)
eq("a user template covers the floor and stops there",
   sorted(P.template("user")["screens"]),
   sorted(k for k, _, _, m in P.SCREENS if not m))
eq("an admin template reaches the office", "reports" in P.template("admin")["screens"], True)
eq("and stops at the super admin's own screen",
   "users" in P.template("admin")["screens"], False)

head("and the middleware refusing a real request")
client = TestClient(app)


def login(u, pw):
    return client.post("/api/auth/login", headers={"X-Essa-Client": "native"}, json={"username": u, "password": pw}).json()


su = login("superadmin", "super@123")
eq("the super admin signs in", su.get("role"), "superadmin")
eq("carrying an empty map — nothing restricted yet", su.get("permissions"), {})
head_su = {"Authorization": "Bearer " + su["token"]}

cat = client.get("/api/users/catalog", headers=head_su).json()
eq("the catalog stands on its own", len(cat["screens"]), len(P.SCREENS))
eq("and names every action", [a["key"] for a in cat["actions"]], P.ACTION_KEYS)

users = client.get("/api/users", headers=head_su).json()
eq("the user list carries one too", len(users["catalog"]["screens"]), len(P.SCREENS))
floor = [u for u in users["users"] if u["username"] == "user"][0]
eq("and the floor account starts unrestricted", floor["permissions"], {})

u_tok = login("user", "user@123")
head_u = {"Authorization": "Bearer " + u_tok["token"]}
eq("so the floor reads the GRN screen as it always did",
   client.get("/api/purchases", headers=head_u).status_code, 200)
eq("and Reports is still refused by the role",
   client.get("/api/reports/stock", headers=head_u).status_code, 403)

r = client.put("/api/users/%d/permissions" % floor["id"], headers=head_su,
               json={"screens": {"purchases": ["view"], "lr": ["view", "create"]},
                     "data": ["hide_cost_price"]})
eq("the grant saves", r.status_code, 200)
eq("and reads back clean", r.json()["permissions"]["screens"],
   {"lr": ["view", "create"], "purchases": ["view"]})

eq("reading the GRN is still allowed",
   client.get("/api/purchases", headers=head_u).status_code, 200)
eq("but building one is not",
   client.post("/api/purchases/build/1", headers=head_u).status_code, 403)
eq("and the refusal names the screen and the act",
   "Create access to GRN" in client.post("/api/purchases/build/1",
                                         headers=head_u).json()["detail"], True)
eq("a screen left out of the map is closed",
   client.get("/api/inventory/products", headers=head_u).status_code, 403)
eq("one that is in it opens",
   client.get("/api/lr", headers=head_u).status_code, 200)
eq("paths no screen claims are unaffected",
   client.get("/api/notifications", headers=head_u).status_code, 200)

client.put("/api/users/%d/permissions" % floor["id"], headers=head_su,
           json={"screens": {"reports": ["view", "print"]}})
eq("granting Reports to a floor account saves…",
   client.get("/api/users", headers=head_su).status_code, 200)
eq("…and changes nothing, because the role is the ceiling",
   client.get("/api/reports/stock", headers=head_u).status_code, 403)

me = [u for u in users["users"] if u["username"] == "superadmin"][0]
eq("you may not restrict yourself out of the room",
   client.put("/api/users/%d/permissions" % me["id"], headers=head_su,
              json={"screens": {"lr": ["view"]}}).status_code, 400)

eq("clearing it puts the account back to role-only",
   client.put("/api/users/%d/permissions" % floor["id"], headers=head_su,
              json={}).json()["permissions"], {})
eq("and the floor has its role back",
   client.get("/api/inventory/products", headers=head_u).status_code, 200)

head("the account row's own two edits")
eq("a full name can be corrected",
   client.patch("/api/users/%d" % floor["id"], headers=head_su,
                json={"full_name": "Sharu Kumar"}).json()["full_name"], "Sharu Kumar")
eq("and cleared again",
   client.patch("/api/users/%d" % floor["id"], headers=head_su,
                json={"full_name": ""}).json()["full_name"], "")

head("Store access: Admin (the whole Store) or User (the floor's screens)")
eq("nothing recorded is the whole Store", P.store_access({}), "admin")
eq("user is kept", P.normalise({"store": "user"}), {"store": "user"})
eq("admin is what nothing means, so it is not stored",
   P.normalise({"store": "admin"}), {})
eq("junk is dropped rather than stored", P.normalise({"store": "cashier"}), {})
eq("the catalog offers both levels",
   [s["key"] for s in client.get("/api/users/catalog", headers=head_su)
    .json()["store_access"]], ["admin", "user"])

r = client.put("/api/users/%d/permissions" % floor["id"], headers=head_su,
               json={"store": "user"})
eq("a Store user saves", r.status_code, 200)
eq("and reads back on its own — no screens restricted with it",
   r.json()["permissions"], {"store": "user"})
eq("a misspelt level is refused, not saved as the whole Store",
   client.put("/api/users/%d/permissions" % floor["id"], headers=head_su,
              json={"store": "cashier"}).status_code, 400)
u_tok = login("user", "user@123")
eq("signing in carries it, so the menu can narrow",
   u_tok.get("permissions"), {"store": "user"})
eq("and it narrows nothing in the warehouse",
   client.get("/api/inventory/products",
              headers={"Authorization": "Bearer " + u_tok["token"]}).status_code, 200)

head("the /pos mount tells the shop — and only the mount can")
from backend.app.main import _with_store_access                  # noqa: E402


def shop_sees(path="/pos/pos/", token=None, extra=()):
    """The headers the shop would receive for a frame request."""
    h = [(b"host", b"essa")] + list(extra)
    if token:
        h.append((b"cookie", ("essa_token=%s" % token).encode()))
    out = _with_store_access({"type": "http", "method": "GET", "path": path,
                              "headers": h, "query_string": b""})
    return dict(out["headers"]).get(P.STORE_HEADER.encode())


eq("a Store user's cookie becomes the header", shop_sees(token=u_tok["token"]), b"user")
eq("a copy sent by the client is stripped, with no session",
   shop_sees(extra=[(b"x-essa-store-access", b"admin")]), None)
eq("…and replaced, with one",
   shop_sees(token=u_tok["token"], extra=[(b"x-essa-store-access", b"admin")]), b"user")
eq("the super admin is the whole Store", shop_sees(token=su["token"]), None)
eq("no Essa session adds nothing — the shop's own login decides", shop_sees(), None)
eq("static files are not looked up", shop_sees("/pos/static/css/app.css",
                                                token=u_tok["token"]), None)
client.put("/api/users/%d/permissions" % floor["id"], headers=head_su, json={})
eq("a User-role account stays a Store user even with nothing recorded",
   shop_sees(token=u_tok["token"]), b"user")
eq("…because the role decides it", P.store_access({}, "user"), "user")

# The Users & Access switch still narrows — and un-narrows — an admin.
adm_row = next(x for x in client.get("/api/users", headers=head_su).json()["users"]
               if x["username"] == "admin")
a_tok = login("admin", "essa@123")
eq("an admin is the whole Store", shop_sees(token=a_tok["token"]), None)
client.put("/api/users/%d/permissions" % adm_row["id"], headers=head_su,
           json={"store": "user"})
eq("narrowed in Users & Access, the admin is a Store user",
   shop_sees(token=a_tok["token"]), b"user")
client.put("/api/users/%d/permissions" % adm_row["id"], headers=head_su, json={})
eq("back to Admin, the header goes on the very next request",
   shop_sees(token=a_tok["token"]), None)

head("what a Store user may open in the shop")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "Textile Retail Shop"))
import importlib.util as _ilu                                         # noqa: E402
_spec = _ilu.spec_from_file_location("shop_modules", os.path.join(
    sys.path[0], "app", "modules.py"))
SM = _ilu.module_from_spec(_spec); _spec.loader.exec_module(SM)
for ep in ("pos.counter", "delivery.index", "inventory.list_products",
           "customers.list_customers", "returns.index", "alterations.index"):
    eq(f"allowed: {ep}", SM.store_user_allows(ep), True)
for ep in ("reports.index", "main.dashboard", "floor.index", "audits.index",
           "checker.index", "pos.invoice_list", "stores.index",
           "promotions.index", "staff.list_staff"):
    eq(f"refused: {ep}", SM.store_user_allows(ep), False)

print("\n%d FAILED" % len(bad) if bad else "\nall passing")
sys.exit(1 if bad else 0)
