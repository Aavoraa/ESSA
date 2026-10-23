"""A location's own identity: address, contact and registration, at all three levels.

    python backend/tools/locations_test.py

A warehouse, a store and a counter each PRINT. A GRN is received at one, a
transfer note is addressed to the next, a retail bill comes off the third — and
what has to appear at the head of each is that place's own address and its own
GST number, because a branch registered separately has a different one from the
warehouse supplying it. So all three carry the same field set (see
models.LocationProfile) and this checks that they really all do, rather than the
warehouse carrying it and the other two quietly dropping it on save.

Four things are easy to break here and are checked directly:

  * a field that saves on one level and is dropped on another. One writer feeds
    all three from services.locations.PROFILE_FIELDS; a level that stopped using
    it would still return 200 and simply lose what was typed.
  * the GST state code disagreeing with the GSTIN. It is DERIVED from the first
    two digits rather than typed, because two fields that must agree and are
    both typed by hand disagree eventually — and this one decides whether a sale
    is inter-state.
  * a partial PATCH wiping what it did not mention. The close-a-branch button
    sends {name, active} and nothing else. If "not mentioned" were read as
    "clear it", closing a store for the weekend would erase its address.
  * an empty string NOT clearing. It is the other half of the same rule: the
    form sends "" for a box somebody deliberately emptied, and that has to
    reach the column.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-loc-"), "test.db").replace(os.sep, "/")

from backend.app import models                                   # noqa: E402
from backend.app.database import engine                          # noqa: E402
from backend.app.services import locations as svc                # noqa: E402
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


client = TestClient(app)
su = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                 json={"username": "superadmin", "password": "super@123"}).json()
H = {"Authorization": "Bearer " + su["token"]}


# ===========================================================================
head("the same columns really are on all three levels")
for m in (models.Warehouse, models.Store, models.PosTerminal):
    have = {c.name for c in m.__table__.columns}
    eq("%s carries the whole profile" % m.__tablename__,
       sorted(set(svc.PROFILE_FIELDS) - have), [])
    eq("%s can be filed under a business" % m.__tablename__,
       "business_id" in have, True)

# Every one of them nullable, which is what lets main._add_missing_columns add
# them to an existing database with a plain ALTER TABLE — a NOT NULL column is
# skipped there and reported as needing a hand-written migration instead.
notnull = [(m.__tablename__, c.name)
           for m in (models.Warehouse, models.Store, models.PosTerminal)
           for c in m.__table__.columns
           if c.name in svc.PROFILE_FIELDS and not c.nullable]
eq("and every one of them is nullable, so the migration is automatic", notnull, [])


# ===========================================================================
head("a warehouse keeps everything it was given")
FULL = dict(
    loc_type="Silks",
    address="12/4 Avinashi Road", address2="Peelamedu", city="Coimbatore",
    district="Coimbatore", state="Tamil Nadu", country="India", pincode="641004",
    contact_person="R Manikandan", phone="+91 98430 11223",
    email="cbe@essa.example", gstin="33AADCE6591N1Z7",
    cin="U17111TZ2011PTC017890",
)
w = client.post("/api/locations/warehouses", headers=H,
                json=dict(FULL, name="Coimbatore Warehouse", code="CBWH1")).json()
for k, v in FULL.items():
    eq("warehouse.%s" % k, w.get(k), v)
eq("its GST state code is the GSTIN's first two digits", w.get("state_code"), "33")
eq("and it is filed under a business without being asked", bool(w.get("business_id")), True)


head("so does a store")
s = client.post("/api/locations/stores", headers=H,
                json=dict(FULL, name="ESSA - PEELAMEDU", warehouse_id=w["id"],
                          loc_type="Franchise")).json()
for k, v in FULL.items():
    if k == "loc_type":
        continue
    eq("store.%s" % k, s.get(k), v)
eq("store.loc_type", s.get("loc_type"), "Franchise")
eq("its state code is derived too", s.get("state_code"), "33")
# A store with no business named is the supplying warehouse's, not the install
# default: the branch of a Silks warehouse is a Silks branch. A franchise is
# exactly the case where somebody says otherwise.
plain = client.post("/api/locations/stores", headers=H,
                    json={"name": "ESSA - GANDHIPURAM",
                          "warehouse_id": w["id"]}).json()
eq("and one that names no business takes its warehouse's",
   plain.get("business_id"), w.get("business_id"))


head("and so does a counter — a till prints, so it has an identity too")
t = client.post("/api/locations/terminals", headers=H,
                json=dict(FULL, name="MAIN COUNTER", store_id=s["id"],
                          loc_type="Franchise")).json()
for k, v in FULL.items():
    if k == "loc_type":
        continue
    eq("terminal.%s" % k, t.get(k), v)
eq("a counter that names no business takes its store's",
   client.post("/api/locations/terminals", headers=H,
               json={"name": "COUNTER 2", "store_id": s["id"]}
               ).json().get("business_id"), s.get("business_id"))


# ===========================================================================
head("closing a branch does not erase it")
# EXACTLY what the ⊘ button on the Locations screen sends, and nothing else.
closed = client.patch("/api/locations/stores/%d" % s["id"], headers=H,
                      json={"name": s["name"], "active": False}).json()
eq("it is closed", closed.get("active"), False)
eq("its GSTIN survived", closed.get("gstin"), FULL["gstin"])
eq("its address survived", closed.get("address"), FULL["address"])
eq("its contact survived", closed.get("phone"), FULL["phone"])
eq("its type survived", closed.get("loc_type"), "Franchise")
reopened = client.patch("/api/locations/stores/%d" % s["id"], headers=H,
                        json={"name": s["name"], "active": True}).json()
eq("and reopening it changes nothing else either",
   [reopened.get(k) for k in ("gstin", "city", "pincode", "email")],
   [FULL["gstin"], FULL["city"], FULL["pincode"], FULL["email"]])


head("but an empty box does clear the column")
cleared = client.patch("/api/locations/stores/%d" % s["id"], headers=H,
                       json={"name": s["name"], "district": "", "cin": ""}).json()
eq("a district somebody deleted is gone", cleared.get("district"), None)
eq("so is the CIN", cleared.get("cin"), None)
eq("and the fields beside them are untouched", cleared.get("city"), FULL["city"])

# 0 rather than null for "no company of its own": null means "not mentioned",
# so it could never clear one that had already been set.
eq("business_id 0 hands the store back to its warehouse's company",
   client.patch("/api/locations/stores/%d" % s["id"], headers=H,
                json={"name": s["name"], "business_id": 0}
                ).json().get("business_id"), None)


# ===========================================================================
head("what the server refuses to store")


def refused(path, body):
    r = client.post(path, headers=H, json=body)
    return r.status_code, (r.json().get("detail") if r.status_code != 200 else None)


code, msg = refused("/api/locations/warehouses",
                    {"name": "Bad GST", "gstin": "33AADCE6591N1Z"})
eq("a GSTIN one character short", code, 400)
eq("and it says what one looks like", "15 characters" in (msg or ""), True)
eq("a PIN that is not six digits",
   refused("/api/locations/warehouses", {"name": "Bad PIN", "pincode": "6410"})[0], 400)
eq("an email with no @",
   refused("/api/locations/warehouses", {"name": "Bad Mail", "email": "nope"})[0], 400)
eq("a type outside the three",
   refused("/api/locations/warehouses", {"name": "Bad Type", "loc_type": "Hardware"})[0], 400)
# Foreign keys are enforced (database.py), so an id that does not exist would
# otherwise surface as a bare 500 instead of a sentence.
eq("a business that does not exist",
   refused("/api/locations/warehouses", {"name": "Bad Biz", "business_id": 9999})[0], 404)
eq("nothing was created by any of those",
   len([x for x in client.get("/api/locations/warehouses", headers=H).json()
        if x["name"].startswith("Bad ")]), 0)


# ===========================================================================
head("the form's own vocabulary comes from here, not from the UI")
opts = client.get("/api/locations/form-options", headers=H).json()
eq("the three types", opts.get("types"), ["Garments", "Silks", "Franchise"])
eq("and a company to file a place under", len(opts.get("businesses") or []) >= 1, True)
eq("each one named well enough to pick from a list",
   sorted(set(opts["businesses"][0]) & {"id", "name", "code", "gstin"}),
   ["code", "gstin", "id", "name"])


# ===========================================================================
head("and the tree the screen draws carries it all through")
tree = client.get("/api/locations", headers=H).json()["warehouses"]
node = [x for x in tree if x.get("code") == "CBWH1"][0]
eq("the warehouse's identity is on the tree", node.get("gstin"), FULL["gstin"])
eq("its type is too", node.get("loc_type"), "Silks")
store = [x for x in node["stores"] if x["name"] == "ESSA - PEELAMEDU"][0]
eq("the store's is on its own node", store.get("city"), FULL["city"])
till = [x for x in store["terminals"] if x["name"] == "MAIN COUNTER"][0]
eq("and the counter's on its", till.get("gstin"), FULL["gstin"])
eq("every profile field reaches the tree, none dropped in the serializer",
   sorted(set(svc.PROFILE_FIELDS) - set(till)), [])


# ===========================================================================
head("floors sit between a store and its tills, and carry the bill prefix")
# The level the retail app reads to number a bill: a till on the ground floor
# bills TG26-001, one on the first bills TF26-001. It lives HERE, in the one
# location master, rather than in the shop — a second list of storeys could be
# asked what a counter's bills are called and answer differently from the system
# that issued the counter.
store_id = store["id"]
made = {}
for i, (nm, px) in enumerate([("Ground Floor", "TG"), ("First Floor", "TF")]):
    r = client.post("/api/locations/floors", headers=H,
                    json={"name": nm, "store_id": store_id, "prefix": px,
                          "sort_order": i})
    eq(f"{nm} created", r.status_code, 200)
    made[nm] = r.json()
eq("the prefix comes back as it will be used", made["Ground Floor"]["prefix"], "TG")

eq("a prefix that would not read back is refused",
   client.post("/api/locations/floors", headers=H,
               json={"name": "Bad", "store_id": store_id,
                     "prefix": "T-G"}).status_code, 400)
eq("and two floors of one store cannot share a name",
   client.post("/api/locations/floors", headers=H,
               json={"name": "Ground Floor", "store_id": store_id,
                     "prefix": "TZ"}).status_code, 409)

# A till stands on a floor of ITS OWN store, never another building's.
other_store = [x for x in tree for x in x["stores"]
               if x["id"] != store_id]
r = client.post("/api/locations/terminals", headers=H,
                json={"name": "GF TILL", "store_id": store_id,
                      "floor_id": made["Ground Floor"]["id"]})
eq("a till can be put straight on a floor", r.status_code, 200)
eq("…and reports the series its bills will carry", r.json().get("bill_prefix"), "TG")
if other_store:
    eq("a till cannot stand on another store's floor",
       client.post("/api/locations/terminals", headers=H,
                   json={"name": "WRONG", "store_id": other_store[0]["id"],
                         "floor_id": made["Ground Floor"]["id"]}).status_code, 400)

tree2 = client.get("/api/locations", headers=H).json()["warehouses"]
s2 = [x for w in tree2 for x in w["stores"] if x["id"] == store_id][0]
eq("the tree shows the floors in order, ground first",
   [f["name"] for f in s2["floors"]], ["Ground Floor", "First Floor"])
gf = [f for f in s2["floors"] if f["name"] == "Ground Floor"][0]
eq("with the till standing on it", [t["name"] for t in gf["terminals"]], ["GF TILL"])
eq("and the tills on NO floor still listed at the store, not lost",
   "MAIN COUNTER" in [t["name"] for t in s2["terminals"]], True)

eq("a floor with tills on it cannot be deleted",
   client.delete(f"/api/locations/floors/{gf['id']}", headers=H).status_code, 409)
eq("an empty one can",
   client.delete(f"/api/locations/floors/{made['First Floor']['id']}",
                 headers=H).status_code, 200)


# ===========================================================================
head("a code somebody already has is refused, not crashed on")
# `code` is unique on all three levels and nothing used to check it: the insert
# reached the database, the constraint refused it, and the person got an
# Internal Server Error with a SQLAlchemy traceback behind it. On a form whose
# Code placeholder used to read "POS-01" — a real, already-taken code — that was
# a typo away for anybody.
held = client.post("/api/locations/terminals", headers=H,
                   json={"name": "CODED TILL", "store_id": store_id}).json()
eq("a till created blank gets a code of its own", bool(held.get("code")), True)

clash = client.post("/api/locations/terminals", headers=H,
                    json={"name": "ANOTHER", "store_id": store_id,
                          "code": held["code"]})
eq("a second till cannot take it", clash.status_code, 409)
eq("…and is told who has it", "CODED TILL" in (clash.json().get("detail") or ""), True)

eq("nor can a store take a store code that is used",
   client.post("/api/locations/stores", headers=H,
               json={"name": "CLASHING STORE", "code": store["code"]}).status_code, 409)
eq("nor a warehouse", client.post("/api/locations/warehouses", headers=H,
                                  json={"name": "CLASHING WH",
                                        "code": node["code"]}).status_code, 409)

neighbour = client.post("/api/locations/terminals", headers=H,
                        json={"name": "NEIGHBOUR TILL", "store_id": store_id,
                              "code": "POS-ZZ"}).json()
eq("a code nobody holds is accepted as given", neighbour.get("code"), "POS-ZZ")
eq("a row may keep its own code when it is edited",
   client.patch(f"/api/locations/terminals/{held['id']}", headers=H,
                json={"name": "CODED TILL RENAMED",
                      "code": held["code"]}).status_code, 200)
eq("but may not take its neighbour's",
   client.patch(f"/api/locations/terminals/{held['id']}", headers=H,
                json={"name": "CODED TILL RENAMED", "code": "POS-ZZ"}).status_code, 409)


print("\n%s" % ("all passing" if not bad else "FAILED: " + ", ".join(bad)))
sys.exit(1 if bad else 0)
