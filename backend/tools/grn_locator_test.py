"""What a line says about itself, and the whole account of one item afterwards.

    python backend/tools/grn_locator_test.py

Two things that were both silently wrong once, and would be again:

  * BRAND, DESIGN AND SIZE crossing from the invoice to the product. They used to
    stop at the GRN — and worse, matching ignored them, so FROCK/16, FROCK/18,
    FROCK/20 and FROCK/22 all scored 100 against an existing "FROCK" and collapsed
    into the one stock item that splitting them existed to separate. Keyed,
    carried, and then discarded at the last step.

  * THE ITEM LOCATOR'S FIGURES. The consignment behind a receipt, how long the
    goods have stood, the margin, and the stock figure taken apart. None of it is
    checkable by looking at the screen: every number there is derived, and a
    plausible wrong one looks exactly like a right one.

Runs against a throwaway SQLite file, with the till deliberately absent so the
"shop not installed" path is the one exercised.
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-grn-"), "test.db").replace(os.sep, "/")
os.environ["ESSA_POS_DB"] = "/nonexistent-so-the-till-is-absent.db"

from backend.app import models                                   # noqa: E402
from backend.app.database import engine, SessionLocal            # noqa: E402
from backend.app.services import inventory as inv                # noqa: E402
from backend.app.services import locator as loc                  # noqa: E402
from backend.app.main import app                                 # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

models.Base.metadata.create_all(bind=engine)
db = SessionLocal()

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
head("what a line names about itself")


def named(**kw):
    a = inv.named_attrs(**kw)
    return None if a is None else {k: v for k, v in a.items() if v}


eq("a line that names nothing", named(), None)
eq("blanks are nothing", named(size="  ", brand="", design_no=None), None)
eq("one size", named(size="16"), {"size": "16"})
eq("the Frock line", named(size="16", brand="Fashion Gear", design_no="4313"),
   {"size": "16", "brand": "Fashion Gear", "design_no": "4313"})
eq("a brand alone is enough", named(brand="Essa"), {"brand": "Essa"})
# a size cell holding a RUN is four garments hiding in a line, not a size — the
# answer to it is the breakdown, not a product whose size is "30:2, 32:4"
eq("a size run is not a size", named(size="30:2, 32:4, 34:4, 36:2"), None)
eq("…even beside a brand", named(size="30:2, 32:4", brand="Essa"), {"brand": "Essa"})
eq("but a measurement IS one", named(size="16*22"), {"size": "16*22"})
eq("and the tuple is the server's own shape",
   sorted(inv.named_attrs(size="16")), sorted(inv.SPLIT_ATTRS))

head("so four sizes are four stock items, not one")
sup = models.Supplier(name="ROHIT FASHION")
db.add(sup)
db.flush()


def product(desc, **attrs):
    p = models.Product(sku=inv._next_sku(db), description=desc, hsn="620469",
                       primary_supplier_id=sup.id, **attrs)
    db.add(p)
    db.flush()
    return p


frock16 = product("FROCK", size="16", brand="Fashion Gear", design_no="4313")
plain = product("KURTA")


def match(desc, **kw):
    m = inv.match_product(db, None, desc, "620469", sup.id, attrs=inv.named_attrs(**kw))
    return m.sku if m else None


eq("a true re-buy merges",
   match("FROCK", size="16", brand="Fashion Gear", design_no="4313"), frock16.sku)
eq("another size does not", match("FROCK", size="18", brand="Fashion Gear",
                                  design_no="4313"), None)
eq("another brand does not", match("FROCK", size="16", brand="Yuva",
                                   design_no="4313"), None)
eq("another design does not", match("FROCK", size="16", brand="Fashion Gear",
                                    design_no="4444"), None)
eq("a bundle line still matches on its description alone", match("KURTA"), plain.sku)
eq("and a named line does not fold into a blank product", match("KURTA", size="XL"), None)

head("and the invoice's own columns reach the product")
doc = models.Document(filename="inv.jpg", stored_path="/tmp/inv.jpg",
                      supplier_id=sup.id, status="confirmed")
db.add(doc)
db.flush()
db.add(models.Extraction(document_id=doc.id, provider="human", data={
    "invoice": {"number": "T-1", "date": "2026-08-21"},
    "totals": {"taxable_total": 5400, "tax_total": 0, "grand_total": 5400},
    "line_items": [
        {"description": "FROCK", "brand": "Fashion Gear", "design": "4313",
         "size": z, "hsn": "620469", "qty": 1, "uom": "PCS", "rate": 1350,
         "amount": 1350, "taxable_value": 1350}
        for z in ("16", "18", "20", "22")
    ],
}))
db.flush()
grn = inv.build_grn_from_document(db, doc)
db.flush()
lines = sorted(grn.lines, key=lambda x: x.id)
eq("four lines on the GRN", len(lines), 4)
eq("each carrying its size", [x.size for x in lines], ["16", "18", "20", "22"])
eq("the brand", {x.brand for x in lines}, {"Fashion Gear"})
eq("and the design", {x.design_no for x in lines}, {"4313"})
eq("the size that already existed matched; the other three are new",
   [x.product_id is not None for x in lines], [True, False, False, False])
for x in lines:
    if x.product_id:
        continue
    p = inv._create_product(db, grn, x, unit=("PCS", 1.0))
    eq("the product born for size %s carries all three" % x.size,
       (p.description, p.size, p.brand, p.design_no),
       ("FROCK", x.size, "Fashion Gear", "4313"))

# ===========================================================================
head("the locator: the lorry, the age, the money, the count")
sup2 = models.Supplier(name="R-WINGS FASHION HOUSE (MUMBAI)")
db.add(sup2)
db.flush()
doc2 = models.Document(filename="i2.jpg", stored_path="/tmp/i2.jpg",
                       supplier_id=sup2.id, status="posted")
db.add(doc2)
db.flush()
db.add(models.LREntry(
    invoice_document_id=doc2.id, lr_entry_no="GRN15326", lr_entry_date="2026-08-18",
    lr_no="AC/5420", lr_date="2026-08-18", lr_mode="Transport",
    transport="MATOSHREE AGENCY", recv_date="2026-08-18",
    supplier_name="R-WINGS FASHION HOUSE (MUMBAI)", agent="MATOSHREE AGENCY",
    agent_commission=3.0, inv_no="7136", inv_date="2026-08-11",
    qty=50, amount=26500, purchase_manager="ESSA", stock_holding_days=30,
    auto_transfer_location="TAQUA SILKS, TIRUPUR", paid_topay="TOPAY"))
pur = models.Purchase(document_id=doc2.id, supplier_id=sup2.id,
                      grn_no="GRN-2026-00007", invoice_number="7136",
                      invoice_date="2026-08-11", taxable_total=26500,
                      tax_total=1325, grand_total=27825, status="posted",
                      posted_at=dt.datetime(2026, 8, 18, 10, 0))
db.add(pur)
db.flush()
shirt = models.Product(sku="ESSA-SHIRT", barcode="TQ1334194", description="MENS-SHIRT",
                       hsn="610910", uom="PCS", primary_supplier_id=sup2.id,
                       brand="R-WINGS", pattern="PLAIN", style="CASUAL",
                       material="COTTON", sleeve="FULL", size="S",
                       design_no="01 F/S PL", mrp=960, sale_price=770,
                       stock_qty=30, avg_cost=530, last_rate=530)
db.add(shirt)
db.flush()
sline = models.PurchaseLine(purchase_id=pur.id, product_id=shirt.id,
                            description="MENS-SHIRT", hsn="610910", qty=50,
                            uom="PCS", rate=530, amount=26500, size="S",
                            brand="R-WINGS", design_no="01 F/S PL")
db.add(sline)
db.flush()
db.add(models.GrnShortage(purchase_id=pur.id, line_id=sline.id, kind="short",
                          qty=3, reason="not in box"))
db.add(models.GrnShortage(purchase_id=pur.id, line_id=sline.id, kind="damaged",
                          qty=2, reason="torn"))
for kind, delta, bal in (("inward", 45, 45), ("outward", -12, 33),
                         ("return", -2, 31), ("adjustment", -1, 30)):
    db.add(models.StockMovement(product_id=shirt.id, qty_delta=delta, kind=kind,
                                balance_after=bal, rate=530,
                                ref_type="purchase", ref_id=pur.id))
ow = models.StockOutward(code="TF-001", date="2026-08-19", from_location="WAREHOUSE",
                         to_destination="TAQUA SILKS, TIRUPUR", packed_by="RAJA",
                         received_by="SHAJAHAN", received_date="2026-08-19",
                         status="received", posted_at=dt.datetime(2026, 8, 19))
db.add(ow)
db.flush()
db.add(models.StockOutwardLine(outward_id=ow.id, product_id=shirt.id,
                               description="MENS-SHIRT", qty=12, accepted_qty=11,
                               rate=530))
db.flush()

con = loc.consignment_of(db, pur)
eq("the transport register row is found", con["lr_entry_no"], "GRN15326")
eq("with its LR and date", (con["lr_no"], con["lr_date"]), ("AC/5420", "2026-08-18"))
eq("the agent and his cut", (con["agent"], con["agent_commission"]),
   ("MATOSHREE AGENCY", 3.0))
eq("no purchase, no consignment", loc.consignment_of(db, None), None)

age = loc.stock_age(pur, con, today=dt.date(2026, 8, 20))
eq("aged from the day it was RECEIVED, not billed", age["received_on"], "2026-08-18")
eq("two days standing", age["days"], 2)
eq("against the period it was bought for", age["holding_days"], 30)
eq("and not late", age["overdue_by"], 0)
late = loc.stock_age(pur, con, today=dt.date(2026, 10, 1))
eq("44 days later it is 14 over", (late["days"], late["overdue_by"]), (44, 14))
eq("nothing to date it by", loc.stock_age(None, None, today=dt.date(2026, 8, 20)), None)

pr = loc.pricing_of(shirt, sline, pur)
eq("cost", pr["cost"], 530.0)
eq("the invoice was taxed at 5%", pr["purchase_tax_pct"], 5.0)
eq("so the cost with tax on it", pr["net_cost"], 556.5)
eq("MRP less the discount is the sell price",
   (pr["mrp"], pr["discount"], pr["sale_price"]), (960.0, 190.0, 770.0))
eq("margin over the SELL price", (pr["margin"], pr["margin_pct"]), (240.0, 31.17))
eq("net of the purchase tax", (pr["net_margin"], pr["net_margin_pct"]), (213.5, 27.73))
eq("and mark-up over COST, which is the other number", pr["markup_pct"], 45.28)

ws = loc.warehouse_stock(db, shirt)
eq("purchased", ws["purchased"], 45.0)
eq("transferred out", ws["transferred"], 12.0)
eq("returned to the supplier", ws["returned"], 2.0)
eq("counted away", ws["adjusted"], -1.0)
eq("short on the bill", ws["short"], 3.0)
eq("damaged at the dock", ws["damaged"], 2.0)
eq("stock", ws["stock"], 30.0)
eq("and the ledger adds up to it",
   round(sum(k["signed"] for k in ws["kinds"]), 3), ws["stock"])

locs = loc.locations_of(db, shirt, sold=4.0)
eq("one destination", [r["location"] for r in locs], ["TAQUA SILKS, TIRUPUR"])
eq("sent and accepted are kept apart",
   (locs[0]["sent"], locs[0]["accepted"]), (12.0, 11.0))
eq("so the shortfall stays visible", locs[0]["short_by"], 1.0)
eq("and the till's count lands on the one shop", locs[0]["sold"], 4.0)
ow2 = models.StockOutward(code="TF-002", date="2026-08-20",
                          to_destination="ELSEWHERE", status="draft")
db.add(ow2)
db.flush()
db.add(models.StockOutwardLine(outward_id=ow2.id, product_id=shirt.id, qty=5))
db.flush()
eq("a draft dispatch has sent nothing anywhere",
   [r["location"] for r in loc.locations_of(db, shirt)], ["TAQUA SILKS, TIRUPUR"])

tr = [t for t in loc.transfers_of(db, shirt) if t["code"] == "TF-001"][0]
eq("from and to", (tr["from"], tr["to"]), ("WAREHOUSE", "TAQUA SILKS, TIRUPUR"))
eq("packed", (tr["packed_on"], tr["packed_qty"]), ("2026-08-19", 12.0))
eq("received", (tr["received_on"], tr["received_qty"]), ("2026-08-19", 11.0))
eq("one short", tr["short_by"], 1.0)
db.commit()

# ===========================================================================
head("…and all of it over HTTP, in the shape the screen consumes")
client = TestClient(app)
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"}, json={"username": "admin", "password": "essa@123"})
client.headers["Authorization"] = "Bearer " + tok.json()["token"]

r = client.get("/api/inventory/locate", params={"code": "TQ1334194"})
eq("the scan is answered", r.status_code, 200)
d = r.json()
eq("every block the screen reads is present", sorted(d), sorted([
    "kind", "code", "product", "unit", "unit_counts", "receipts", "cartons",
    "dispatches", "movements", "consignment", "age", "pricing",
    "warehouse_stock", "locations", "transfers", "sales", "printing"]))
eq("identified", d["product"]["sku"], "ESSA-SHIRT")
eq("the consignment came through", d["consignment"]["lr_no"], "AC/5420")
eq("the margin", d["pricing"]["margin_pct"], 31.17)
eq("the count reconciles",
   round(sum(k["signed"] for k in d["warehouse_stock"]["kinds"]), 3),
   d["warehouse_stock"]["stock"])
eq("the till is absent and says so rather than erroring",
   (d["sales"]["available"], d["sales"]["rows"]), (False, []))
eq("printing is answered", sorted(d["printing"]), ["can_print", "why"])
# serialisable, 30 in stock, no piece codes: the sheet would be wrong before it
# left the printer, so the locator shows the reason instead of the button
eq("a piece-code mismatch is refused", d["printing"]["can_print"], False)
eq("and says why", "does not match the number of QR records" in d["printing"]["why"], True)
eq("an unknown tag is a 404, not a 500",
   client.get("/api/inventory/locate", params={"code": "NOT-A-TAG"}).status_code, 404)

head("the stock holding period the age is measured against")
# Goods are booked in against a confirmed purchase order — the default policy
# since orders exist (config.REQUIRE_PO_FOR_LR). One is raised here so these
# rows are created the way the form now creates them; the holding period is what
# is actually under test below.
_po = client.post("/api/purchase-orders", json={
    "supplier_name": "KRISHA", "po_date": "2026-08-18",
    "lines": [{"particulars": "MBJ frock", "qty": 23, "rate": 2140}]}).json()
client.post("/api/purchase-orders/%d/status" % _po["id"], json={"status": "confirmed"})
FULL = {"lr_mode": "Transport", "lr_date": "2026-08-18", "lr_entry_date": "2026-08-18",
        "supplier_name": "KRISHA", "agent": "GOLDEN TRANSPORT SERVICE",
        "bundle": 1, "boxes": 1, "qty": 23, "amount": 49224,
        "purchase_order_id": _po["id"]}
made = client.post("/api/lr", json=dict(FULL, lr_no="MBJ-1")).json()
eq("a new consignment carries ninety days without anybody typing it",
   made.get("stock_holding_days"), 90)
eq("one that states its own keeps it",
   client.post("/api/lr", json=dict(FULL, lr_no="MBJ-2",
                                    stock_holding_days=30)).json().get("stock_holding_days"), 30)
eq("and it is editable afterwards",
   client.patch("/api/lr/%d" % made["id"],
                json={"stock_holding_days": 45}).json().get("stock_holding_days"), 45)
eq("…or clearable, for goods bought against no period at all",
   client.patch("/api/lr/%d" % made["id"],
                json={"stock_holding_days": ""}).json().get("stock_holding_days"), None)

db.close()
print("\n%d FAILED" % len(bad) if bad else "\nall passing")
sys.exit(1 if bad else 0)
