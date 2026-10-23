"""Counting the shelf: what a scan concludes, and what it refuses to conclude.

    python backend/tools/stock_audit_test.py

Four properties, each of which would be wrong in a way nobody would notice:

  * **Three results, not two.** A code that resolves to nothing is a master-data
    problem; a product with no stock is a shelf problem. The screen shows two
    colours, but collapsing the two findings in the DATA would send somebody to
    look for stock that never existed.
  * **A re-scan does not double-count.** One row per code per session. The
    register quietly growing a second row for one garment is the failure a stock
    count exists to prevent.
  * **The figures are frozen at scan time.** `stock_qty` is what the books said
    when the shelf was looked at. Re-deriving it later turns the count into a
    report on today, and destroys the only thing it was for.
  * **Counting never moves stock.** The ledger is changed deliberately, elsewhere,
    with a reason on it — never as a side effect of a phone scanning a label.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-audit-"), "test.db").replace(os.sep, "/")

from backend.app import models                                    # noqa: E402
from backend.app.database import engine, SessionLocal             # noqa: E402
from backend.app.services import stock_locations                  # noqa: E402
from backend.app.main import app                                  # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

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
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                  json={"username": "superadmin", "password": "super@123"}).json()
client.headers["Authorization"] = "Bearer " + tok["token"]

# --- a warehouse with two products: one on the shelf, one sold out -----------
db = SessionLocal()
wh = models.Warehouse(name="Erode", code="ER")
db.add(wh)
db.commit()
WID = wh.id
here = {"X-Essa-Warehouse": str(WID)}

onshelf = models.Product(description="Cotton Shirt", sku="ESSA-00001", stock_qty=12)
soldout = models.Product(description="Silk Dhoti", sku="ESSA-00002", stock_qty=0)
db.add_all([onshelf, soldout])
db.commit()
stock_locations.apply(db, onshelf, WID, 12, kind="inward", ref_type="test", ref_id=1)
db.commit()
ON_ID, OUT_ID = onshelf.id, soldout.id
db.close()

# ===========================================================================
head("a count belongs to a building")

# With ONE warehouse on file, a caller that names none is not guessing — the
# phone sends no warehouse header and there is only one building it could mean.
solo = client.post("/api/stock-audit/open", json={})
eq("with one warehouse, a headerless caller gets it", solo.status_code, 200)
eq("and the count says which building it is of", solo.json()["warehouse"], "Erode")
client.post("/api/stock-audit/%d/close" % solo.json()["id"], headers=here)

eq("nothing in progress yet",
   client.get("/api/stock-audit/current", headers=here).json(), None)

s = client.post("/api/stock-audit/open", headers=here, json={}).json()
eq("a count opens", s["status"], "open")
eq("numbered from the sequence", s["code"].startswith("AUD-"), True)
eq("attributed to whoever is signed in", s["started_by"], "superadmin")
eq("with nothing counted", s["totals"], {"available": 0, "not_available": 0,
                                         "unknown": 0, "scanned": 0})

again = client.post("/api/stock-audit/open", headers=here, json={}).json()
eq("re-opening resumes the count in progress rather than starting a second",
   again["id"], s["id"])
eq("which is what the phone finds when it is picked up again",
   client.get("/api/stock-audit/current", headers=here).json()["id"], s["id"])

SID = s["id"]


def scan(code):
    return client.post(f"/api/stock-audit/{SID}/scan", headers=here,
                       json={"code": code}).json()


# ===========================================================================
head("three findings, not two")

r = scan("ESSA-00001")
eq("a product with stock here is AVAILABLE", r["scan"]["result"], "available")
eq("and says so in words, not only in colour", r["scan"]["label"], "AVAILABLE")
eq("carrying what the books said at that moment", r["scan"]["stock_qty"], 12.0)
eq("and which product it was", r["scan"]["sku"], "ESSA-00001")
eq("named, so a later rename cannot rewrite the count",
   r["scan"]["product_name"], "Cotton Shirt")

r = scan("ESSA-00002")
eq("a product with none here is NOT AVAILABLE", r["scan"]["result"], "not_available")
eq("said in words too", r["scan"]["label"], "NOT AVAILABLE")
eq("with the figure that made it so", r["scan"]["stock_qty"], 0.0)

r = scan("SOME-OTHER-SYSTEMS-TAG")
eq("a tag that resolves to nothing is NOT FOUND, not 'no stock'",
   r["scan"]["result"], "unknown")
eq("which is a different sentence", r["scan"]["label"], "NOT FOUND")
eq("and pins no product", r["scan"]["product_id"], None)

eq("the tally counts all three separately", r["totals"],
   {"available": 1, "not_available": 1, "unknown": 1, "scanned": 3})

# ===========================================================================
head("scanning the same label twice")

before = r["totals"]["scanned"]
again = scan("ESSA-00001")
eq("is reported as already counted", again["duplicate"], True)
eq("does NOT add a second row", again["totals"]["scanned"], before)
eq("but records that it was seen again", again["scan"]["times_seen"], 2)
eq("and does not rewrite what was found the first time",
   again["scan"]["stock_qty"], 12.0)

# ===========================================================================
head("the figures are frozen at the moment of the scan")

db = SessionLocal()
p = db.get(models.Product, ON_ID)
stock_locations.apply(db, p, WID, -12, kind="outward", ref_type="test", ref_id=2)
p.stock_qty = 0
db.commit()
db.close()

kept = next(x for x in client.get(f"/api/stock-audit/{SID}").json()["scans"]
            if x["sku"] == "ESSA-00001")
eq("stock emptied afterwards does not rewrite the count", kept["stock_qty"], 12.0)
eq("nor the finding", kept["result"], "available")

# ===========================================================================
head("counting never moves stock")

db = SessionLocal()
moves = db.query(models.StockMovement).filter(
    models.StockMovement.ref_type == "audit").count()
db.close()
eq("no movement is written by a scan", moves, 0)
eq("the sold-out product still reads zero, untouched by having been counted",
   client.get(f"/api/inventory/products/{OUT_ID}", headers=here).json()["stock_qty"], 0)

# ===========================================================================
head("correcting a miss-scan, and closing")

wrong = scan("ANOTHER-STRAY-TAG")["scan"]
n = client.get(f"/api/stock-audit/{SID}").json()["totals"]["scanned"]
d = client.delete(f"/api/stock-audit/{SID}/scans/{wrong['id']}", headers=here)
eq("a reading can be taken back out while the count is open", d.status_code, 200)
eq("and the tally follows", d.json()["totals"]["scanned"], n - 1)

closed = client.post(f"/api/stock-audit/{SID}/close", headers=here).json()
eq("the count closes", closed["status"], "closed")
eq("recording who closed it", closed["closed_by"], "superadmin")
eq("a closed count takes no more scans",
   client.post(f"/api/stock-audit/{SID}/scan", headers=here,
               json={"code": "ESSA-00001"}).status_code, 400)
eq("and its readings stand",
   client.delete(f"/api/stock-audit/{SID}/scans/{wrong['id']}",
                 headers=here).status_code, 400)
eq("closing frees the warehouse for the next count",
   client.get("/api/stock-audit/current", headers=here).json(), None)

# ===========================================================================
head("a count is one building's work")

other = models.Warehouse(name="Karur", code="KR")
db = SessionLocal()
db.add(other)
db.commit()
OTHER = {"X-Essa-Warehouse": str(other.id)}
db.close()

o = client.post("/api/stock-audit/open", headers=OTHER, json={}).json()
eq("another warehouse opens its own count", o["id"] != SID, True)
eq("named as itself", o["warehouse"], "Karur")
mine = {x["id"] for x in client.get("/api/stock-audit", headers=here).json()}
eq("which does not appear in the first warehouse's history", o["id"] in mine, False)
eq("while its own count does", SID in mine, True)

# Now there are two buildings, so a caller naming none IS guessing — and the
# refusal is the whole point: filing Erode's count against Karur is the kind of
# wrong that is found months later, if ever.
r = client.post("/api/stock-audit/open", json={})
eq("with two warehouses, a headerless caller is refused", r.status_code, 400)
eq("and told to say which", "Say which warehouse" in r.json()["detail"], True)

# ===========================================================================
print("\n" + "=" * 68)
if bad:
    print("%d FAILED:" % len(bad))
    for b in bad:
        print("  - %s" % b)
    sys.exit(1)
print("all stock-audit checks passing")
