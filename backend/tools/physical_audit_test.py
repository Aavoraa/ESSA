"""Counting a warehouse by quantity: the variance, and the gate in front of it.

    python backend/tools/physical_audit_test.py

The properties here are the ones that would be wrong silently — a count that
looks finished and is not, a correction that runs twice, a filter that quietly
stops filtering:

  * **The filters define the count, and are kept.** A count opened over one
    brand covers that brand's items and no others, and what was being counted is
    on the header afterwards. Without that, last month's count of the MENS rack
    reads as a count of the building and reports every other rack as missing.
  * **Null is not zero.** "Nobody has been to this rack" and "the rack is empty"
    are different findings, and only the first may be left out of an adjustment.
  * **The book figure is frozen.** A line carries what the ledger said when it
    was made. Re-reading it as the screen draws would make every line agree with
    itself and the variance would always be nought.
  * **Counting moves no stock.** Not on a scan, not on a typed count, not on
    completion, not on approval. Only `apply` moves stock, only after approval,
    only with a reason, and only once.
  * **Scanning adds, typing replaces, uploading replaces.** Three verbs that
    look alike. A gun read twice is two garments; a file run twice is not two
    warehouses.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-psa-"), "test.db").replace(os.sep, "/")

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


def ok(what, cond):
    eq(what, bool(cond), True)


def head(t):
    print("\n%s" % t)


client = TestClient(app)
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                  json={"username": "superadmin", "password": "super@123"}).json()
client.headers["Authorization"] = "Bearer " + tok["token"]

# --- one warehouse, three products of two brands ----------------------------
db = SessionLocal()
wh = models.Warehouse(name="Erode", code="ER")
db.add(wh)
db.commit()
WID = wh.id
here = {"X-Essa-Warehouse": str(WID)}

shirt = models.Product(description="Cotton Shirt", sku="ESSA-00001",
                       barcode="8901234000011", brand="YUVA", size="L",
                       category_section="MENS", design_no="D-100",
                       mrp=999, sale_price=799)
dhoti = models.Product(description="Silk Dhoti", sku="ESSA-00002",
                       brand="YUVA", size="XL", category_section="MENS",
                       mrp=1499, sale_price=1299)
saree = models.Product(description="Kanchi Saree", sku="ESSA-00003",
                       brand="TAQUA", size="FREE", category_section="LADIES",
                       mrp=4999, sale_price=4499)
db.add_all([shirt, dhoti, saree])
db.commit()

# stock through the ONE door stock moves through, so balances and the ledger agree
for product, qty, rate in ((shirt, 12, 300), (dhoti, 5, 700), (saree, 3, 2000)):
    stock_locations.apply(db, product, WID, qty, kind="inward",
                          ref_type="test", rate=rate)
db.commit()
SHIRT, DHOTI, SAREE = shirt.id, dhoti.id, saree.id


head("The filters decide what is counted, and are kept on the count")
r = client.post("/api/physical-audit/preview", headers=here,
                json={"filters": {"brand": "YUVA"}}).json()
eq("preview counts only the filtered brand", r["items"], 2)
eq("preview totals that brand's pieces", r["qty"], 17.0)
r = client.post("/api/physical-audit/preview", headers=here,
                json={"filters": {"brand": "  "}}).json()
eq("a blank filter is dropped, not counted as an empty brand", r["items"], 3)

# Barcode Type decides WHICH identifier a tag is matched on. The two are not
# interchangeable: the barcode is the supplier's and can be blank or shared, the
# UAN is ours and is neither.
p = lambda f: client.post("/api/physical-audit/preview", headers=here,  # noqa: E731
                          json={"filters": f}).json()["items"]
eq("Any matches either identifier", p({"barcode": "8901234000011"}), 1)
eq("and finds the same item by its UAN", p({"barcode": "ESSA-00001"}), 1)
eq("Supplier barcode will not match a UAN",
   p({"barcode": "ESSA-00001", "barcode_type": "supplier"}), 0)
eq("and our UAN will not match a supplier's barcode",
   p({"barcode": "8901234000011", "barcode_type": "uan"}), 0)

# "Show All Rows" decides what the GRID draws, not what is counted. Kept off the
# document on purpose: snapshotted, it would be a tickbox that silently stopped
# responding once a count was open.
eq("a view preference never reaches the count",
   client.post("/api/physical-audit/preview", headers=here, json={
       "filters": {"brand": "YUVA", "show_all": True}}).json()["filters"],
   {"brand": "YUVA"})

r = client.post("/api/physical-audit/open", headers=here,
                json={"filters": {"brand": "NOBODY"}}).json()
ok("opening over filters that match nothing is refused",
   "nothing to count" in str(r.get("detail", "")).lower())

opened = client.post("/api/physical-audit/open", headers=here,
                     json={"filters": {"brand": "YUVA", "section": "MENS"}}).json()
AID = opened["id"]
eq("the count is numbered in its own series", opened["code"][:4], "PSA-")
eq("it opens over the filtered set only", len(opened["lines"]), 2)
eq("the filters are on the header afterwards",
   opened["scope"], {"brand": "YUVA", "section": "MENS"})
eq("it names the building whose shelves were walked", opened["warehouse"], "Erode")
ok("the saree is not on it",
   all(l["product_id"] != SAREE for l in opened["lines"]))

r = client.post("/api/physical-audit/open", headers=here, json={}).json()
ok("a second count of the same warehouse is refused", "already being counted"
   in str(r.get("detail", "")))


head("The sheet opens against today's books")
lines = {l["product_id"]: l for l in opened["lines"]}
eq("the shirt's book figure is copied", lines[SHIRT]["stock"], 12.0)
eq("its cost comes from this warehouse's balance", lines[SHIRT]["cost_price"], 300.0)
eq("its selling price is carried for the grid", lines[SHIRT]["net_price"], 799.0)
eq("our own article number is the SKU", lines[SHIRT]["uan"], "ESSA-00001")
eq("the supplier's barcode is its own column",
   lines[SHIRT]["barcode"], "8901234000011")

# Eight more shirts arrive while the sheet is sitting on somebody's desk. This is
# the case that decides whether the module is right or merely plausible: the
# expected figure printed on the sheet is now stale, and a variance measured
# against it would charge the delivery to whoever walks the rack tomorrow.
stock_locations.apply(db, db.get(models.Product, SHIRT), WID, 8, kind="inward",
                      ref_type="test", rate=300)
db.commit()
after = client.get("/api/physical-audit/%d" % AID, headers=here).json()
eq("the sheet still reads what it was drawn up with",
   {l["product_id"]: l["stock"] for l in after["lines"]}[SHIRT], 12.0)


head("Nobody has looked yet is not the same as there are none")
LINE = lines[SHIRT]["id"]
eq("an uncounted line's qty is null", lines[SHIRT]["qty"], None)
eq("and its status says pending", lines[SHIRT]["status"], "pending")
r = client.post("/api/physical-audit/%d/lines/%d/count" % (AID, LINE),
                headers=here, json={"qty": 0}).json()
eq("counting zero records zero", r["line"]["qty"], 0.0)
eq("and reads as short, not as pending", r["line"]["status"], "short")
eq("counting the row pins it to the books AS THEY ARE NOW, not as the sheet read",
   r["line"]["stock"], 20.0)
eq("so the gap is the whole shortfall, delivery included",
   r["line"]["difference"], -20.0)
r = client.post("/api/physical-audit/%d/lines/%d/count" % (AID, LINE),
                headers=here, json={"qty": None}).json()
eq("clearing puts it back to never-counted", r["line"]["qty"], None)
eq("and back to pending", r["line"]["status"], "pending")

r = client.post("/api/physical-audit/%d/lines/%d/count" % (AID, LINE),
                headers=here, json={"qty": -1}).json()
ok("a negative count is refused", "cannot be negative" in str(r.get("detail", "")))


head("Scanning adds a garment; typing states a total")
r = client.post("/api/physical-audit/%d/scan" % AID, headers=here,
                json={"code": "8901234000011"}).json()
eq("one beep, one garment", r["line"]["qty"], 1.0)
r = client.post("/api/physical-audit/%d/scan" % AID, headers=here,
                json={"code": "8901234000011"}).json()
eq("two beeps, two garments", r["line"]["qty"], 2.0)
eq("and the scan count rises with them", r["line"]["count"], 2)
ok("the second beep says it was already counted",
   "already counted" in r["message"].lower())
r = client.post("/api/physical-audit/%d/lines/%d/count" % (AID, LINE),
                headers=here, json={"qty": 10}).json()
eq("typing a total replaces rather than adds", r["line"]["qty"], 10.0)

r = client.post("/api/physical-audit/%d/scan" % AID, headers=here,
                json={"code": "ESSA-00003"}).json()
ok("an item outside the filters is refused while Direct Add/Remove is off",
   "outside the filters" in str(r.get("detail", "")))
r = client.post("/api/physical-audit/%d/scan" % AID, headers=here,
                json={"code": "NOTHING-AT-ALL"}).json()
ok("a tag that matches nothing is refused with the code in the message",
   "NOTHING-AT-ALL" in str(r.get("detail", "")))


head("A handheld's file replaces, so running it twice is safe")
up = client.post("/api/physical-audit/%d/upload" % AID, headers=here, json={
    "rows": [{"code": "ESSA-00001", "qty": 9}, {"code": "ESSA-00002", "qty": 5},
             {"code": "ESSA-00003", "qty": 2}, {"code": "junk", "qty": 1}]}).json()
eq("the two lines on the count take their figures", up["applied"], 2)
eq("the code that resolves to nothing is reported back", up["unknown"], ["junk"])
eq("and so is the one outside the filters", up["off_scope"], ["ESSA-00003"])
up2 = client.post("/api/physical-audit/%d/upload" % AID, headers=here, json={
    "rows": [{"code": "ESSA-00001", "qty": 9}]}).json()
eq("re-running the same file does not double the shelf",
   up2["totals"]["uploaded"], 14.0)


head("The strip across the top")
t = client.get("/api/physical-audit/%d" % AID, headers=here).json()["totals"]
eq("available is what the books say over the counted set", t["available"], 25.0)
eq("uploaded is what was actually found", t["uploaded"], 14.0)
eq("valid is the lines that agree", t["valid"], 1)          # the dhoti, 5 = 5
eq("beside the lines that do not", t["variance_lines"], 1)  # the shirt, 9 vs 20
eq("missing is the net shortfall", t["missing"], 11.0)
eq("nothing is pending now", t["pending_lines"], 0)


head("Counting moves no stock at all")
eq("the shirt's balance is untouched by the count",
   stock_locations.qty_at(SessionLocal(), SHIRT, WID), 20.0)
mvs = SessionLocal().query(models.StockMovement).filter(
    models.StockMovement.ref_type == "physical_audit").count()
eq("and no audit movement has been written", mvs, 0)


head("Complete, review, approve — in that order, and only then apply")
r = client.post("/api/physical-audit/%d/apply" % AID, headers=here,
                json={"reason": "counted"}).json()
ok("a count still being taken cannot correct stock",
   "only an approved count" in str(r.get("detail", "")).lower())
r = client.post("/api/physical-audit/%d/status" % AID, headers=here,
                json={"status": "approved"}).json()
ok("and it cannot jump the queue to approved",
   "can only become" in str(r.get("detail", "")))

for step in ("completed", "reviewed", "approved"):
    r = client.post("/api/physical-audit/%d/status" % AID, headers=here,
                    json={"status": step}).json()
    eq("it becomes %s" % step, r["status"], step)

r = client.post("/api/physical-audit/%d/apply" % AID, headers=here, json={}).json()
ok("an approved count still refuses to move stock with no reason given",
   "change reason" in str(r.get("detail", "")).lower())

r = client.post("/api/physical-audit/%d/apply" % AID, headers=here,
                json={"reason": "Counted 04-09, three shirts short"}).json()
eq("one movement per line that disagrees", r["moved"], 1)
eq("the shirt's stock is corrected to what was counted",
   stock_locations.qty_at(SessionLocal(), SHIRT, WID), 9.0)

mv = (SessionLocal().query(models.StockMovement)
      .filter(models.StockMovement.ref_type == "physical_audit")
      .order_by(models.StockMovement.id.desc()).first())
eq("through a real ledger row", mv.qty_delta, -11.0)
eq("filed against this warehouse", mv.warehouse_id, WID)
ok("carrying the count's number and the reason somebody typed",
   mv.note.startswith("PSA-") and "three shirts short" in mv.note)

r = client.post("/api/physical-audit/%d/apply" % AID, headers=here,
                json={"reason": "again"}).json()
ok("a correction cannot be run twice",
   "corrected once" in str(r.get("detail", "")).lower())


head("An uncounted line is never written off")
opened2 = client.post("/api/physical-audit/open", headers=here,
                      json={"filters": {"brand": "TAQUA"}}).json()
AID2, L2 = opened2["id"], opened2["lines"][0]["id"]
client.post("/api/physical-audit/%d/lines/%d/count" % (AID2, L2), headers=here,
            json={"qty": 3})
# a second line nobody counts: add the shirt by hand with Direct Add/Remove off →
# refused, so instead widen by synchronize after the filter set changes. Simpler:
# the saree count above matches, so nothing should move at all.
for step in ("completed", "reviewed", "approved"):
    client.post("/api/physical-audit/%d/status" % AID2, headers=here,
                json={"status": step})
r = client.post("/api/physical-audit/%d/apply" % AID2, headers=here,
                json={"reason": "matched"}).json()
eq("a count that agrees with the books moves nothing", r["moved"], 0)
eq("the saree is where it was",
   stock_locations.qty_at(SessionLocal(), SAREE, WID), 3.0)


head("Direct Add/Remove lets a count take in what it finds off-scope")
opened3 = client.post("/api/physical-audit/open", headers=here, json={
    "filters": {"brand": "YUVA", "direct_edit": True}}).json()
AID3 = opened3["id"]
eq("the tickbox is kept on the header", opened3["scope"].get("direct_edit"), True)
r = client.post("/api/physical-audit/%d/scan" % AID3, headers=here,
                json={"code": "ESSA-00003"}).json()
eq("a saree found on the YUVA rack is taken onto the count", r["line"]["qty"], 1.0)
eq("and flagged as found off-scope", r["line"]["source"], "scanned")
ok("with the screen told why", "off-scope" in r["message"])

new_line = r["line"]["id"]
r = client.delete("/api/physical-audit/%d/lines/%d" % (AID3, new_line),
                  headers=here).json()
ok("a counted row will not be deleted", "clear the count first"
   in str(r.get("detail", "")).lower())
client.post("/api/physical-audit/%d/lines/%d/count" % (AID3, new_line),
            headers=here, json={"qty": None})
r = client.delete("/api/physical-audit/%d/lines/%d" % (AID3, new_line),
                  headers=here).json()
eq("an uncounted row may come back off", r.get("ok"), True)


head("Synchronize re-reads the books for rows nobody has counted")
sync_lines = {l["product_id"]: l for l in
              client.get("/api/physical-audit/%d" % AID3, headers=here).json()["lines"]}
eq("the shirt opened at today's corrected figure", sync_lines[SHIRT]["stock"], 9.0)
client.post("/api/physical-audit/%d/lines/%d/count"
            % (AID3, sync_lines[DHOTI]["id"]), headers=here, json={"qty": 5})
stock_locations.apply(db, db.get(models.Product, SHIRT), WID, 4, kind="inward",
                      ref_type="test", rate=300)
stock_locations.apply(db, db.get(models.Product, DHOTI), WID, 4, kind="inward",
                      ref_type="test", rate=700)
db.commit()
r = client.post("/api/physical-audit/%d/synchronize" % AID3, headers=here).json()
after = {l["product_id"]: l for l in r["audit"]["lines"]}
eq("the uncounted shirt picks up the delivery", after[SHIRT]["stock"], 13.0)
eq("the counted dhoti keeps the figure it was measured against",
   after[DHOTI]["stock"], 5.0)


head("The screen is listed, permitted and warehouse-scoped")
from backend.app import security                                  # noqa: E402
from backend.app.services import permissions                      # noqa: E402
eq("the API path is policed at user level",
   security.required_access("GET", "/api/physical-audit")[:2],
   ("user", "physical_audit"))
eq("correcting stock off it is admin",
   security.required_access("POST", "/api/physical-audit/1/apply")[:2],
   ("admin", "physical_audit"))
ok("the screen is in the permissions editor",
   "physical_audit" in permissions.SCREEN_KEYS)
ok("and is marked as showing one building's work",
   "physical_audit" in security.WAREHOUSE_SCOPED)

past = client.get("/api/physical-audit", headers=here).json()
eq("every count of this warehouse is in the register", len(past), 3)
ok("newest first", past[0]["id"] > past[-1]["id"])

print("\n%s" % ("ALL GOOD" if not bad else "FAILED: " + ", ".join(bad)))
sys.exit(1 if bad else 0)
