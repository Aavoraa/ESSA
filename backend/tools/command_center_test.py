"""The Command Center: the role above super admin, the audit trail, the figures,
the questions and the end-to-end trace.

    python backend/tools/command_center_test.py

Runs against a throwaway warehouse database AND a throwaway till database, both
built here, because half of what this feature answers — sales, discounts, floors,
margins — lives in the shop's own tables and the other half in ours. A test with
only one of them would check the arithmetic of a system nobody runs.

The questions asked below are the ones in the brief, and each is checked for the
ONE LINE it answers with, because that line is the feature: a number in a
sentence, which somebody reads and acts on without opening a report.

Nothing here touches real data: both databases are fresh files in a temp folder,
and the questions run in keyword mode (no API key), which is the harder path.
"""
import datetime as dt
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHOP = ROOT / "Textile Retail Shop"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

bad = []


def eq(what, got, want):
    if got != want:
        bad.append(what)
        print("  FAIL  %s\n        got  %r\n        want %r" % (what, got, want))
    else:
        print("  ok    %s" % what)


def has(what, text, wanted):
    ok = wanted.lower() in str(text).lower()
    if not ok:
        bad.append(what)
        print("  FAIL  %s\n        %r\n        does not contain %r" % (what, text, wanted))
    else:
        print("  ok    %s" % what)


def head(t):
    print("\n%s" % t)


tmp = tempfile.mkdtemp(prefix="essa-cc-")
WH_DB = os.path.join(tmp, "warehouse.db").replace(os.sep, "/")
SHOP_DB = os.path.join(tmp, "shop.db").replace(os.sep, "/")
for leak in ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL",
             "POSTGRES_URL_NON_POOLING", "SHOP_DB_SCHEMA", "ANTHROPIC_API_KEY",
             "ESSA_SUPERBOSS_PASSWORD"):
    os.environ.pop(leak, None)
os.environ["ESSA_STATE_DIR"] = tmp
os.environ["ESSA_POS_DB"] = SHOP_DB
os.environ["ESSA_WAREHOUSE_DB"] = os.path.join(tmp, "no-warehouse-here.db")

# The business day, worked out the same way the app does, so the fixtures land
# inside "today" whatever o'clock this is run at.
OFFSET = dt.timedelta(minutes=int(os.environ.get("ESSA_UTC_OFFSET_MINUTES", "330")))
TODAY = (dt.datetime.utcnow() + OFFSET).date()
YESTERDAY = TODAY - dt.timedelta(days=1)


def at(day, hour):
    """A UTC timestamp `hour` hours into that business day."""
    return dt.datetime.combine(day, dt.time()) - OFFSET + dt.timedelta(hours=hour)


# ===========================================================================
head("a till with two floors, two counters and a day's bills")
os.environ["DATABASE_URL"] = "sqlite:///" + SHOP_DB
sys.path.insert(0, str(SHOP))
import app as shop_pkg                                              # noqa: E402
from app import db as shop_db                                       # noqa: E402
from app.models import (Category as SCategory, Company as SCompany,  # noqa: E402
                        Counter, CreditNote, CreditNoteItem, Floor as SFloor,
                        Invoice, InvoiceItem, Location as SLocation,
                        Product as SProduct, User as SUser)

shop_app = shop_pkg.create_app()
used = shop_app.config["SQLALCHEMY_DATABASE_URI"].replace("\\", "/")
if SHOP_DB not in used:
    print("REFUSING TO RUN — the shop opened %s instead of the scratch file" % used)
    sys.exit(2)
ctx = shop_app.app_context()
ctx.push()
shop_db.create_all()

co = SCompany(name="TAQUA SILKS", is_default=True, active=True)
ravi = SUser(username="ravi", full_name="Ravi Kumar", role="cashier")
ravi.set_password("x")
meena = SUser(username="meena", full_name="Meena Selvam", role="cashier")
meena.set_password("x")
shirts = SCategory(name="SHIRT", section="LADIES")
frocks = SCategory(name="FROCK", section="KIDS")
shop_db.session.add_all([co, ravi, meena, shirts, frocks])
shop_db.session.flush()
branch = SLocation(name="TAQUA TIRUPUR", company_id=co.id, active=True)
shop_db.session.add(branch)
shop_db.session.flush()
ground = SFloor(location_id=branch.id, name="Ground Floor", prefix="TG", active=True)
first = SFloor(location_id=branch.id, name="First Floor", prefix="TF", active=True)
shop_db.session.add_all([ground, first])
shop_db.session.flush()
till1 = Counter(name="Counter 1", location_id=branch.id, floor_id=ground.id)
till2 = Counter(name="Counter 2", location_id=branch.id, floor_id=first.id)
shirt = SProduct(sku="ESSA-00001", name="LADIES SHIRT BLUE", category_id=shirts.id,
                 cost_price=400.0, selling_price=1000.0, gst_rate=5.0, stock_qty=3,
                 reorder_level=5, floor_id=ground.id)
frock = SProduct(sku="ESSA-00002", name="KIDS FROCK", category_id=frocks.id,
                 cost_price=300.0, selling_price=500.0, gst_rate=5.0, stock_qty=20,
                 reorder_level=5, floor_id=first.id)
shop_db.session.add_all([till1, till2, shirt, frock])
shop_db.session.flush()


def bill(number, day, hour, floor, till, cashier, product, qty, price, tax, discount=0.0):
    inv = Invoice(invoice_number=number, cashier_id=cashier.id, staff_id=cashier.id,
                  invoice_date=at(day, hour), subtotal=qty * price, discount=discount,
                  cgst=tax / 2, sgst=tax / 2, total=qty * price - discount + tax,
                  company_id=co.id, location_id=branch.id, counter_id=till.id,
                  floor_id=floor.id, bill_prefix=floor.prefix, fin_year="26",
                  payment_status="paid", payment_method="cash")
    shop_db.session.add(inv)
    shop_db.session.flush()
    shop_db.session.add(InvoiceItem(invoice_id=inv.id, product_id=product.id, quantity=qty,
                                    unit_price=price, gst_rate=5.0, line_total=qty * price,
                                    tax_amount=tax))
    return inv


inv_a = bill("TG26-001", TODAY, 2, ground, till1, ravi, shirt, 2, 1000.0, 100.0, discount=200.0)
inv_b = bill("TF26-001", TODAY, 3, first, till2, meena, frock, 1, 500.0, 25.0)
bill("TG26-000", YESTERDAY, 5, ground, till1, ravi, frock, 2, 500.0, 50.0)
shop_db.session.flush()
note = CreditNote(number="CN-0001", invoice_id=inv_a.id, cashier_id=ravi.id,
                  created_at=at(TODAY, 4), subtotal=1000.0, cgst=25.0, sgst=25.0,
                  total=950.0, refund_method="cash", counter_id=till1.id)
shop_db.session.add(note)
shop_db.session.flush()
shop_db.session.add(CreditNoteItem(credit_note_id=note.id,
                                   invoice_item_id=inv_a.items[0].id, product_id=shirt.id,
                                   quantity=1, unit_price=1000.0, gst_rate=5.0,
                                   line_total=1000.0, tax_amount=50.0))
shop_db.session.commit()
shirt_shop_id = shirt.id
print("  ok    the till has %d bills and one credit note" % Invoice.query.count())
ctx.pop()

# The shop's package goes back where it was, exactly as pos_mount does, so the
# name `app` is free for nothing and `backend.app` can import cleanly.
for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.") or k == "config"]:
    del sys.modules[name]
sys.path.remove(str(SHOP))

# ===========================================================================
head("a warehouse with stock, a posted GRN, a draft and a part payment")
os.environ["DATABASE_URL"] = "sqlite:///" + WH_DB
sys.path.insert(0, str(ROOT))

from backend.app import models                                      # noqa: E402
from backend.app.database import SessionLocal, engine               # noqa: E402
from backend.app.main import app                                    # noqa: E402
from backend.app.services import business_day, pos_sales            # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

models.Base.metadata.create_all(bind=engine)
db = SessionLocal()

main_wh = models.Warehouse(name="Main Warehouse", code="MW", active=True)
erode = models.Warehouse(name="Erode", code="ER", active=True)
supplier = models.Supplier(name="AMS Garments", gstin="33AAACA1234A1Z2")
db.add_all([main_wh, erode, supplier])
db.commit()
store = models.Store(name="TAQUA TIRUPUR", code="TT", warehouse_id=main_wh.id, active=True)
db.add(store)
db.commit()
db.add_all([models.PosTerminal(store_id=store.id, name="Counter 1", code="C1", active=True),
            models.PosTerminal(store_id=store.id, name="Counter 2", code="C2", active=True)])
p1 = models.Product(sku="ESSA-00001", description="LADIES SHIRT BLUE", category="SHIRT",
                    category_section="LADIES", stock_qty=40, avg_cost=400.0, mrp=1200.0)
p2 = models.Product(sku="ESSA-00002", description="KIDS FROCK", category="FROCK",
                    category_section="KIDS", stock_qty=10, avg_cost=300.0, mrp=600.0)
db.add_all([p1, p2])
db.commit()
db.add_all([models.StockBalance(product_id=p1.id, warehouse_id=main_wh.id, qty=40, avg_cost=400.0),
            models.StockBalance(product_id=p2.id, warehouse_id=erode.id, qty=10, avg_cost=300.0)])
grn = models.Purchase(supplier_id=supplier.id, warehouse_id=main_wh.id,
                      grn_no="GRN-2026-00001", invoice_number="INV-1029",
                      invoice_date=TODAY.isoformat(), taxable_total=20000.0,
                      tax_total=1000.0, grand_total=21000.0, status="posted",
                      posted_at=at(TODAY, 1))
draft = models.Purchase(supplier_id=supplier.id, warehouse_id=main_wh.id,
                        grn_no="GRN-2026-00002", invoice_number="INV-1030",
                        invoice_date=TODAY.isoformat(), grand_total=5000.0, status="draft",
                        created_at=at(TODAY, 1))
db.add_all([grn, draft])
db.commit()
db.add(models.PurchaseLine(purchase_id=grn.id, product_id=p1.id, description="LADIES SHIRT BLUE",
                           qty=50, rate=400.0, amount=20000.0))
db.add(models.StockMovement(product_id=p1.id, warehouse_id=main_wh.id, qty_delta=50,
                            kind="inward", ref_type="purchase", ref_id=grn.id, rate=400.0,
                            balance_after=40, created_at=at(TODAY, 1)))
db.add(models.Document(filename="AMS-1029.jpg", stored_path="x/AMS-1029.jpg",
                       supplier_id=supplier.id, warehouse_id=main_wh.id,
                       status="needs_review", uploaded_at=at(TODAY, 1)))
db.add(models.LREntry(warehouse_id=main_wh.id, lr_entry_no="LRE-00001", lr_no="GT-4471",
                      supplier_name="AMS Garments", transport="Golden Transport",
                      qty=120, amount=21000.0, created_at=at(TODAY, 1)))
pay = models.Payment(receipt_no="ESP00001", supplier_id=supplier.id, date=TODAY.isoformat(),
                     mode="NEFT", paid_amount=10000.0, gross_amount=21000.0,
                     created_at=at(TODAY, 2))
db.add(pay)
db.commit()
db.add(models.PaymentAllocation(payment_id=pay.id, purchase_id=grn.id,
                                invoice_number="INV-1029", invoice_total=21000.0,
                                settled=10000.0))
db.commit()

# the shop's product is this warehouse item — the join every trace turns on
os.environ["DATABASE_URL"] = "sqlite:///" + SHOP_DB
import sqlite3                                                      # noqa: E402
con = sqlite3.connect(SHOP_DB)
con.execute("UPDATE products SET warehouse_id = ? WHERE id = ?", (p1.id, shirt_shop_id))
con.commit()
con.close()
os.environ["DATABASE_URL"] = "sqlite:///" + WH_DB
eq("the warehouse can read the till", pos_sales.available(), True)

client = TestClient(app)


def login(u, pw):
    return client.post("/api/auth/login", headers={"X-Essa-Client": "native"}, json={"username": u, "password": pw}).json()


def H(tok):
    return {"Authorization": "Bearer " + tok}


su = login("superadmin", "super@123")
head_su = H(su["token"])

# ===========================================================================
head("Super Boss — the rank above super admin")
listed = client.get("/api/users", headers=head_su).json()
eq("with nobody in the seat, a super admin may appoint one",
   "superboss" in [r["value"] for r in listed["roles"]], True)
r = client.post("/api/users", headers=head_su,
                json={"username": "boss", "password": "boss@123", "role": "superboss",
                      "full_name": "The Owner"})
eq("the appointment saves", r.status_code, 200)
eq("and the account holds the rank", r.json()["role_label"], "Super Boss")

listed = client.get("/api/users", headers=head_su).json()
eq("…and now the super admin cannot hand it out again",
   "superboss" in [r["value"] for r in listed["roles"]], False)
boss_row = [u for u in listed["users"] if u["username"] == "boss"][0]
eq("the screen is told the row is not theirs to change", boss_row["manageable"], False)

eq("a second Super Boss is refused",
   client.post("/api/users", headers=head_su,
               json={"username": "boss2", "password": "boss@123", "role": "superboss"}).status_code, 403)
eq("resetting the Super Boss's password is refused",
   client.post("/api/users/%d/password" % boss_row["id"], headers=head_su,
               json={"new_password": "hijack1"}).status_code, 403)
eq("…and so is deactivating them",
   client.patch("/api/users/%d" % boss_row["id"], headers=head_su,
                json={"active": False}).status_code, 403)
eq("…and restricting their access",
   client.put("/api/users/%d/permissions" % boss_row["id"], headers=head_su,
              json={"screens": {"lr": ["view"]}}).status_code, 403)

boss = login("boss", "boss@123")
eq("the Super Boss signs in", boss.get("role"), "superboss")
eq("carrying the flags the shell reads", boss["can"],
   {"manage_users": True, "admin": True, "command": True, "boss": True})
head_boss = H(boss["token"])
eq("and may manage the super admin", client.get("/api/users", headers=head_boss).status_code, 200)
su_row = [u for u in client.get("/api/users", headers=head_boss).json()["users"]
          if u["username"] == "superadmin"][0]
eq("…including the Super Boss's own seat being grantable again",
   "superboss" in [r["value"] for r in client.get("/api/users", headers=head_boss).json()["roles"]], True)
eq("a super admin's row is theirs to change", su_row["manageable"], True)

# an admin to ask questions as, allotted to Erode only
r = client.post("/api/users", headers=head_su,
                json={"username": "kumar", "password": "kumar@123", "role": "admin",
                      "full_name": "Kumar"})
kumar_id = r.json()["id"]
client.put("/api/users/%d/permissions" % kumar_id, headers=head_su,
           json={"warehouses": [erode.id]})
kumar = login("kumar", "kumar@123")
head_k = H(kumar["token"])

# ===========================================================================
head("the audit trail writes itself")
r = client.post("/api/locations/warehouses", headers=head_su,
                json={"name": "Karur", "code": "KR"})
eq("a warehouse is created", r.status_code, 200)
client.post("/api/auth/login", headers={"X-Essa-Client": "native"}, json={"username": "boss", "password": "wrong"})
trail = client.get("/api/audit", headers=head_su).json()
lines = [e["summary"] for e in trail["events"]]
eq("the new warehouse is on the trail", any("added warehouse Karur" in s for s in lines), True)
eq("so is the account that was created",
   any("created account boss as Super Boss" in s for s in lines), True)
eq("…and what the super admin was refused",
   any("was refused" in s and "account" in s for s in lines), True)
eq("a failed sign-in is recorded, with the name tried",
   any(e["summary"].startswith("failed to sign in") and e["username"] == "boss"
       for e in trail["events"]), True)
eq("a successful one too", any(e["summary"] == "signed in" for e in trail["events"]), True)
eq("every line says who, when and what",
   sorted(k for k in trail["events"][0] if k in ("who", "local", "summary", "module")),
   ["local", "module", "summary", "who"])
k_trail = client.get("/api/audit", headers=head_k).json()
eq("an admin is not shown account management",
   any(e["screen"] == "users" for e in k_trail["events"]), False)
eq("nor sign-ins", any(e["screen"] == "session" for e in k_trail["events"]), False)
eq("a floor account cannot read the trail at all",
   client.get("/api/audit", headers=H(login("user", "user@123")["token"])).status_code, 403)

# ===========================================================================
head("the Command Center, in one call")
eq("an admin cannot open it",
   client.get("/api/command/overview", headers=head_k).status_code, 403)
ov = client.get("/api/command/overview", headers=head_boss).json()
k = ov["kpis"]
eq("today's sales are net of the credit note", k["sales"]["value"], 1475.0)
eq("on two bills", k["sales"]["bills"], 2)
eq("today's GRN receipts", (k["purchases"]["grns"], k["purchases"]["value"],
                            k["purchases"]["units"]), (1, 21000.0, 50.0))
eq("today's payments", k["payments"]["value"], 10000.0)
eq("stock value is every warehouse's own average cost", k["stock_value"]["value"], 19000.0)
eq("profit is revenue less what it cost", k["profit"]["value"], 600.0)
eq("…and its margin", k["profit"]["margin_pct"], 46.2)
eq("discounts given today", k["discounts"]["value"], 200.0)
eq("returns are the two kinds added up", k["returns"]["value"], 950.0)
eq("the draft GRN is pending", (k["pending_grns"]["count"], k["pending_grns"]["value"]),
   (1, 5000.0))
eq("and the bill is part paid", k["pending_payments"]["value"], 11000.0)
eq("low stock comes from the stores", k["low_stock"]["count"], 1)
eq("the places are counted", (ov["counts"]["warehouses"], ov["counts"]["stores"],
                              ov["counts"]["counters"]), (3, 1, 2))
eq("the busiest floor today", ov["top_floors"][0]["label"], "Ground Floor")
eq("the series covers a fortnight of business days", len(ov["series"]["labels"]), 14)
eq("…and today's takings are its last point", ov["series"]["sales"][-1], 2425.0)
eq("the activity stream carries the till's bills",
   any(a["kind"] == "bill" and "TG26-001" in (a["ref"] or "") for a in ov["activity"]), True)
eq("…and the warehouse's own changes",
   any(a["kind"] == "event" for a in ov["activity"]), True)
has("the headline says the day in one line", ov["headline"], "store sales")

# ===========================================================================
head("…and the drill-down: one warehouse, its stores, its tills")
pl = ov["places"]
eq("every warehouse has a row", sorted(w["name"] for w in pl["warehouses"]),
   ["Erode", "Karur", "Main Warehouse"])
main = [w for w in pl["warehouses"] if w["name"] == "Main Warehouse"][0]
eq("carrying what it holds and what its shops took",
   (main["value"], main["stores"], main["sales"], main["bills"], main["purchases"]),
   (16000.0, 1, 2425.0, 2, 21000.0))
eq("a warehouse with no stores still reads zero rather than going missing",
   [w["sales"] for w in pl["warehouses"] if w["name"] == "Erode"], [0.0])
eq("every store has a row", [s["name"] for s in pl["stores"]], ["TAQUA TIRUPUR"])
store_row = pl["stores"][0]
eq("with its tills, bills, takings and returns",
   (store_row["terminals"], store_row["bills"], store_row["sales"], store_row["returns"]),
   (2, 2, 2425.0, 950.0))
eq("and it is matched to the till's own branch name", store_row["matched"], True)
eq("both counters are listed", len(pl["counters"]), 2)
eq("the busiest first", pl["counters"][0]["label"].startswith("Counter 1"), True)
eq("with who billed them", sorted(c["label"] for c in pl["cashiers"]),
   ["Meena Selvam", "Ravi Kumar"])
eq("the top sellers carry a SKU, so a row can be tracked",
   sorted(p["sku"] for p in ov["top_products"]), ["ESSA-00001", "ESSA-00002"])

scoped = client.get("/api/command/overview", headers=head_boss,
                    params={"warehouse_id": erode.id}).json()
eq("scoping to Erode narrows the stock to Erode's", scoped["kpis"]["stock_value"]["value"], 3000.0)
eq("…and its GRNs to none", scoped["kpis"]["purchases"]["grns"], 0)
eq("…and its stores' takings to nothing, because it supplies none",
   scoped["kpis"]["sales"]["value"], 0.0)
eq("the screen says which building it is showing", scoped["scope"]["warehouse"], "Erode")
eq("but the picker still lists every one, or there is no way back",
   sorted(w["name"] for w in scoped["places"]["picker"]),
   ["Erode", "Karur", "Main Warehouse"])
eq("an admin cannot scope to a warehouse they are not allotted",
   client.get("/api/command/overview", headers=head_k,
              params={"warehouse_id": main_wh.id}).status_code, 403)

# ===========================================================================
head("ask anything — the questions from the brief, in keyword mode")


def ask(q, tok=None):
    return client.post("/api/command/ask", headers=tok or head_boss, json={"q": q}).json()


a = ask("Today's total sales")
eq("routed to sales", a["intent"], "sales")
eq("keyword mode says so", a["interpretation"]["engine"], "keywords")
has("the one line", a["line"], "Today's Sales: ₹1,475")
has("…and what it is across", a["line"], "1 warehouse, 1 store and 2 POS counters")
eq("with the rows behind it", a["columns"], ["Store", "Warehouse", "Bills", "Sales"])
has("and something to say out loud", a["speak"], "1,475 rupees")

a = ask("Which floor has the highest sales?")
eq("routed to a ranking", a["intent"], "sales_rank")
has("naming the floor and the money", a["line"], "Ground Floor — ₹1,900")

a = ask("How much stock is available in Ground Floor?")
eq("routed to stock", a["intent"], "stock")
has("answered for that floor", a["line"], "Stock on Ground Floor: 3 pieces")

a = ask("Show sales of ladies shirts this month")
eq("routed to sales of a thing", a["intent"], "sales")
has("with the thing in the title", a["title"], "Ladies Shirts")
has("and its own figure", a["line"], "₹1,050")

a = ask("Which products are low in stock?")
eq("routed to low stock", a["intent"], "low_stock")
has("counted", a["line"], "Low Stock: 1 product")
has("naming the emptiest", a["line"], "LADIES SHIRT BLUE")

a = ask("Show products with no sales for 90 days")
eq("routed to dead stock", a["intent"], "dead_stock")
has("with the age asked for", a["title"], "90+ days")

a = ask("Who gave the highest discount today?")
eq("routed to discounts", a["intent"], "discounts")
has("naming the cashier and the amount", a["line"], "Ravi Kumar — ₹200")

a = ask("Show pending supplier payments")
eq("routed to what is owed", a["intent"], "pending_payments")
has("with the total and the supplier", a["line"], "₹11,000")

a = ask("How many products were received through GRN today?")
eq("routed to receipts", a["intent"], "purchases")
has("units, products and GRNs", a["line"], "50 units of 1 product on 1 GRN")

head("…and the ones asked out loud at the counter")
a = ask("today total invoices")
eq("routed to supplier invoices, not to till bills", a["intent"], "invoices")
has("counted", a["line"], "1 supplier invoice")
has("…with the tills' own invoices said beside it", a["line"], "customer invoice")

a = ask("Today Total LR entries")
eq("routed to the transport register", a["intent"], "lr")
has("counted", a["line"], "1 consignment")
has("with the pieces on it", a["line"], "120 pieces")
has("and what is not in yet", a["line"], "not received")

a = ask("Today Total Billing")
eq("billing is the tills", a["intent"], "sales")
has("and answers in money", a["line"], "₹1,475")

a = ask("Which warehouse has the highest stock value?")
eq("routed to a stock ranking", a["intent"], "stock_rank")
has("naming the warehouse", a["line"], "Main Warehouse — ₹16,000")

a = ask("Today's profit")
eq("routed to profit", a["intent"], "profit")
has("with the margin", a["line"], "46.2% margin")

a = ask("How much did Main Warehouse sell yesterday?")
has("a named warehouse and a named day", a["title"], "Yesterday's Sales at Main Warehouse")
has("…answered from that day's bills", a["line"], "₹1,050")

a = ask("What did people do today?")
eq("routed to the trail", a["intent"], "activity")
has("counted and attributed", a["line"], "actions by")

a = ask("How many warehouses and stores do we have?")
eq("routed to counts", a["intent"], "counts")
has("answered", a["line"], "3 warehouses, 1 store and 2 POS counters")

a = ask("what is the weather in chennai")
eq("an unanswerable question is not forced into a report", a["ok"], False)
eq("…and it offers examples instead", len(a["suggestions"]) > 0, True)

head("…and the same questions inside one warehouse's allotment")
a = ask("Today's total sales", head_k)
has("an admin allotted Erode is answered for Erode", a["line"], "₹0")
a = ask("Today's sales at Main Warehouse", head_k)
eq("and a warehouse they are not allotted is refused, not widened", a["ok"], False)
has("saying so", a["line"], "not allotted")
eq("a floor account cannot ask at all",
   client.post("/api/command/ask", headers=H(login("user", "user@123")["token"]),
               json={"q": "today's sales"}).status_code, 403)

# ===========================================================================
head("track anything, end to end")
a = ask("GRN-2026-00001")
eq("a GRN number is traced, not read as a question", a["intent"], "trace")
eq("the chain is the business's own order",
   [s["stage"] for s in a["chain"]][:5],
   ["Supplier", "LR Entry", "Invoice", "GRN", "Stock Inward"])
has("the line says what it was", a["line"], "50 units from AMS Garments")
has("…and what is still owed", a["line"], "₹11,000 still owed")
eq("the payment step is on it", any(s["stage"] == "Payment" for s in a["chain"]), True)

a = ask("INV-1029")
eq("a supplier invoice number reaches the same GRN", a["kind"], "grn")

a = ask("TG26-001")
eq("a till bill is traced to the counter that raised it", a["kind"], "bill")
eq("with the store's chain", [s["stage"] for s in a["chain"]][:4],
   ["Store", "POS", "Sale", "Customer"])
has("…and the return against it", a["line"], "₹950 returned")

a = ask("ESSA-00001")
eq("a SKU is traced as the item", a["kind"], "product")
stages = [s["stage"] for s in a["chain"]]
eq("through receipt, warehouse, sale and what is left",
   [s for s in ("GRN", "Warehouse", "Sold", "Current Stock") if s in stages],
   ["GRN", "Warehouse", "Sold", "Current Stock"])
has("the line adds up to the stock on hand", a["line"], "current stock")

a = ask("NOTHING-9999")
eq("a code that names nothing says so", a["ok"], False)
has("…in words", a["line"], "Nothing matches")

r = client.get("/api/command/trace", headers=head_k, params={"code": "GRN-2026-00001"})
eq("a GRN in a warehouse an admin is not allotted is refused", r.json()["ok"], False)

print("\n%d FAILED" % len(bad) if bad else "\nall passing")
sys.exit(1 if bad else 0)
