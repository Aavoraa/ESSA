"""Purchase orders: the arithmetic, the lifecycle, and the LR guard.

    python backend/tools/purchase_orders_test.py

Three things here are worth more than the rest, and all three are the kind that
break quietly:

  * **A confirmed order cannot be edited.** The whole point of the LR guard is
    that "confirmed" means something. If a confirmed order can still be amended
    then the guard is checking a document that no longer says what it said when
    somebody agreed to it.
  * **The guard is on the MANUAL route only.** A register page names no purchase
    order, so requiring one on the import path would reject twenty good rows off
    a perfectly good photograph. The asymmetry is deliberate and is easy to
    "tidy up" into a bug.
  * **Cancelling is refused once goods are booked in against the order.** The
    consignment on the transport register points at it, and a cancelled order
    would leave that row citing a document that says it never happened.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-po-"), "test.db").replace(os.sep, "/")

from backend.app import models                                    # noqa: E402
from backend.app.database import engine                           # noqa: E402
from backend.app.services import purchase_orders as po_svc        # noqa: E402
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
H = {"Authorization": "Bearer " + tok["token"]}


def mk(**kw):
    """Raise an order over HTTP, returning the row."""
    body = {"supplier_name": "AMS Garments", "po_date": "2026-07-31",
            "lines": [{"particulars": "Cotton shirt", "qty": 10, "rate": 250}]}
    body.update(kw)
    return client.post("/api/purchase-orders", headers=H, json=body).json()


# ===========================================================================
head("the arithmetic")

po = mk(discount_pct=10,
        lines=[{"particulars": "Cotton shirt", "qty": 10, "rate": 250},
               {"particulars": "Dhoti", "qty": 4, "rate": 500}])
eq("a line's amount is derived from qty x rate",
   [l["amount"] for l in po["lines"]], [2500.0, 2000.0])
eq("the subtotal adds the lines up", po["subtotal"], 4500.0)
eq("the header discount comes off the subtotal", po["discount_amount"], 450.0)
eq("and the total is what is left", po["total"], 4050.0)

stated = mk(lines=[{"particulars": "Agreed lot", "qty": 3, "rate": 100,
                    "amount": 250}])
eq("an amount the buyer actually typed is kept, not recomputed",
   stated["lines"][0]["amount"], 250.0)

blank = mk(lines=[{"particulars": "Real row", "qty": 1, "rate": 5},
                  {"particulars": "", "qty": None, "rate": None}])
eq("the grid's empty spare row is dropped", len(blank["lines"]), 1)

# ===========================================================================
head("numbering")

eq("orders number from the sequence", mk()["po_no"].startswith("PO-"), True)
a, b = mk()["po_no"], mk()["po_no"]
eq("and never twice", a != b, True)

# ===========================================================================
head("the lifecycle")

po = mk()
eq("an order starts as a draft", po["status"], "draft")
eq("and says it can be edited", po["editable"], True)
eq("but not received against yet", po["can_receive"], False)


def move(po_id, status, **kw):
    return client.post("/api/purchase-orders/%d/status" % po_id, headers=H,
                       json={"status": status, **kw})


eq("a draft can be sent to the supplier",
   move(po["id"], "pending").json()["status"], "pending")
eq("and then confirmed",
   move(po["id"], "confirmed").json()["status"], "confirmed")
eq("a confirmed order may be received against",
   client.get("/api/purchase-orders/%d" % po["id"], headers=H).json()["can_receive"],
   True)

r = client.patch("/api/purchase-orders/%d" % po["id"], headers=H,
                 json={"item": "changed after the fact"})
eq("a confirmed order refuses to be edited", r.status_code, 400)
eq("and says what to do instead", "cancel it and raise a replacement" in
   r.json()["detail"], True)

eq("a confirmed order cannot go back to draft",
   move(po["id"], "draft").status_code, 400)

nolines = mk(lines=[])
eq("an order with no lines cannot be confirmed",
   move(nolines["id"], "confirmed").status_code, 400)
eq("and the blocker is named in advance",
   "at least one line" in nolines["blockers"], True)

nosup = mk(supplier_name="", lines=[{"particulars": "x", "qty": 1, "rate": 1}])
eq("nor can one with no supplier", move(nosup["id"], "confirmed").status_code, 400)

cancelled = mk()
move(cancelled["id"], "cancelled", reason="supplier out of stock")
eq("a cancelled order stays cancelled",
   move(cancelled["id"], "confirmed").status_code, 400)

eq("a draft can be deleted outright",
   client.delete("/api/purchase-orders/%d" % mk()["id"], headers=H).status_code, 200)
committed = mk()
move(committed["id"], "confirmed")
eq("a confirmed one cannot — it is cancelled, not erased",
   client.delete("/api/purchase-orders/%d" % committed["id"],
                 headers=H).status_code, 400)
sent = mk()
move(sent["id"], "pending")
eq("nor can a pending one — the supplier holds it",
   client.delete("/api/purchase-orders/%d" % sent["id"], headers=H).status_code, 400)
called_off = mk()
move(called_off["id"], "cancelled", reason="raised twice")
eq("once cancelled, it can be cleared off the register",
   client.delete("/api/purchase-orders/%d" % called_off["id"], headers=H).status_code, 200)
eq("and it is really gone",
   client.get("/api/purchase-orders/%d" % called_off["id"], headers=H).status_code, 404)

# ===========================================================================
head("only confirmed orders are offered for receiving")

open_ids = {r["id"] for r in client.get("/api/purchase-orders/open",
                                        headers=H).json()}
eq("the confirmed order is offered", po["id"] in open_ids, True)
eq("the draft is not", mk()["id"] in open_ids, False)
eq("and neither is the cancelled one", cancelled["id"] in open_ids, False)

# ===========================================================================
head("the LR guard — manual entry")

LR = {"lr_mode": "Transport", "lr_no": "GT-9912", "lr_date": "2026-07-31",
      "supplier_name": "AMS Garments", "agent": "Ravi", "bundle": 3,
      "lr_entry_date": "2026-07-31", "boxes": 2, "qty": 120}

r = client.post("/api/lr", headers=H, json=dict(LR))
eq("keying a consignment with no order is refused", r.status_code, 400)
eq("and the message says why", "confirmed Purchase Order" in r.json()["detail"], True)

draft = mk()
r = client.post("/api/lr", headers=H, json=dict(LR, purchase_order_id=draft["id"]))
eq("citing a DRAFT order is refused too", r.status_code, 400)
eq("naming the state it is in", "is draft" in r.json()["detail"], True)

r = client.post("/api/lr", headers=H, json=dict(LR, purchase_order_id=999999))
eq("citing an order that does not exist is refused", r.status_code, 400)

r = client.post("/api/lr", headers=H, json=dict(LR, purchase_order_id=po["id"]))
eq("citing a CONFIRMED order goes through", r.status_code, 200)
entry = r.json()
eq("and the entry carries the order's number", entry["po_no"], po["po_no"])
eq("and is not flagged as missing one", entry["po_missing"], False)

listed = {r["id"]: r for r in client.get("/api/purchase-orders", headers=H).json()}
eq("the list says how many consignments cite an order, so the screen can hide "
   "a Cancel that would be refused",
   listed[po["id"]]["linked_lr_count"], 1)
eq("and reports none for an order nothing has arrived against",
   listed[draft["id"]]["linked_lr_count"], 0)

eq("an order goods are booked in against cannot be cancelled",
   move(po["id"], "cancelled").status_code, 400)
eq("and says how many consignments hold it back",
   "1 transport entry" in move(po["id"], "cancelled").json()["detail"], True)

# ===========================================================================
head("the LR guard stops at the manual route — a register page names no order")

r = client.post("/api/lr/save", headers=H, json={
    "rows": [{"lr_no": "REG-1", "supplier_name": "Matoshree", "qty": 40},
             {"lr_no": "REG-2", "supplier_name": "Mehak Fashion", "qty": 12}]})
eq("a photographed register page still imports with no order", r.status_code, 200)
saved = r.json()
eq("both rows land", saved.get("saved"), 2)

rows = client.get("/api/lr", headers=H).json()
imported = [e for e in rows if e["lr_no"] in ("REG-1", "REG-2")]
eq("and they are flagged as needing an order rather than refused",
   [e["po_missing"] for e in imported], [True, True])

# an imported row is tied to its order afterwards, from the grid
r = client.patch("/api/lr/%d" % imported[0]["id"], headers=H,
                 json={"purchase_order_id": po["id"]})
eq("an imported row can be tied to a confirmed order later", r.status_code, 200)
eq("and stops being flagged", r.json()["po_missing"], False)

r = client.patch("/api/lr/%d" % imported[1]["id"], headers=H,
                 json={"purchase_order_id": draft["id"]})
eq("but not to an unconfirmed one, even from the grid", r.status_code, 400)

# ===========================================================================
head("a supplier named on an order joins the master, once")

mk(supplier_name="Kamatchi Textiles")


def suppliers_named(name):
    return [s for s in client.get("/api/suppliers", headers=H).json()
            if (s.get("name") or "").lower() == name.lower()]


eq("a new supplier is filed from the order", len(suppliers_named("Kamatchi Textiles")), 1)
mk(supplier_name="Kamatchi Textiles")
eq("and not filed twice", len(suppliers_named("Kamatchi Textiles")), 1)
# Voice hands back what it heard in lower case (see frontend voicefill.js), so
# this is the spelling a dictated order actually arrives with.
mk(supplier_name="kamatchi textiles")
eq("nor again under a different case — one vendor, one row",
   len(suppliers_named("Kamatchi Textiles")), 1)

# ===========================================================================
head("reading a photographed order — and NOT saving it")

from backend.app.services import po_extract                       # noqa: E402

# The reading itself needs a vision key and a real page, so what is checked here
# is everything around it: the shape it hands back, the flags it raises, and the
# property the whole design turns on — that nothing is written until a human
# presses Save.
clean = po_extract._clean({
    "supplier_name": "Matoshree", "po_date": "31/07/2026", "discount_pct": "10",
    "junk_field": "ignored",
    "lines": [{"particulars": "Cotton shirt", "qty": "10", "rate": "250", "amount": "2500"}]})
eq("a date in the page's own form is normalised to ISO", clean["po_date"], "2026-07-31")
eq("numbers arrive as numbers", (clean["discount_pct"], clean["lines"][0]["qty"]), (10.0, 10.0))
eq("a key nobody asked for is dropped", "junk_field" in clean, False)
eq("a clean reading raises nothing", po_extract.validate(clean)["field_flags"], {})
eq("and scores full confidence", po_extract.validate(clean)["confidence"], 1.0)

wrong = po_extract._clean({
    "supplier_name": "Matoshree", "po_date": "2026-07-31",
    "lines": [{"particulars": "Shirt", "qty": 10, "rate": 250, "amount": 9999}]})
v = po_extract.validate(wrong)
eq("a line that does not multiply out is flagged on the amount",
   v["field_flags"], {"lines.0.amount": True})
eq("and the warning shows the arithmetic",
   "10 x 250 is 2500, but the page says 9999" in v["warnings"][0], True)
eq("which costs confidence", v["confidence"] < 1.0, True)

blank = po_extract._clean({})
eq("a page that read as nothing flags the fields that matter",
   sorted(po_extract.validate(blank)["field_flags"]), ["po_date", "supplier_name"])

before = len(client.get("/api/purchase-orders", headers=H).json())
r = client.post("/api/purchase-orders/extract", headers=H,
                files={"file": ("order.jpg", b"not-a-real-image", "image/jpeg")})
eq("an unreadable page is answered, not an error", r.status_code, 200)
body = r.json()
eq("the page is still stored, so the order can be checked against it",
   isinstance(body.get("document_id"), int), True)
eq("a draft comes back to review", isinstance(body.get("draft"), dict), True)
eq("marked as an import rather than typed", body.get("entry_source"), "import")
eq("NOTHING was written to the order book",
   len(client.get("/api/purchase-orders", headers=H).json()), before)

doc_id = body["document_id"]
made = client.post("/api/purchase-orders", headers=H, json={
    "supplier_name": "Matoshree", "po_date": "2026-07-31", "document_id": doc_id,
    "entry_source": "import",
    "lines": [{"particulars": "Cotton shirt", "qty": 10, "rate": 250}]}).json()
eq("and only the human's Save writes one", made["status"], "draft")
eq("pinned to the page it was read off", made["document_id"], doc_id)
eq("recorded as having come in by import", made["entry_source"], "import")

eq("the screen can ask whether reading is possible at all",
   sorted(client.get("/api/purchase-orders/extract/status", headers=H).json()),
   ["available"])

# ===========================================================================
head("buying against orders is a policy, not a fact")

from backend.app import runtime                                   # noqa: E402

runtime.set_many(require_po_for_lr=False)
try:
    r = client.post("/api/lr", headers=H, json=dict(LR, lr_no="NO-PO-1"))
    eq("a business that raises no orders can still key a consignment",
       r.status_code, 200)
    eq("and the row simply says it has no order", r.json()["po_missing"], True)
    r = client.post("/api/lr", headers=H,
                    json=dict(LR, lr_no="NO-PO-2", purchase_order_id=draft["id"]))
    eq("but naming a draft order is still wrong, policy or not", r.status_code, 400)
finally:
    runtime.set_many(require_po_for_lr=True)

eq("and switching it back on restores the guard",
   client.post("/api/lr", headers=H, json=dict(LR, lr_no="BACK-ON")).status_code,
   400)

# ===========================================================================
head("warehouse scoping")

with __import__("backend.app.database", fromlist=["SessionLocal"]).SessionLocal() as db:
    wh = models.Warehouse(name="Erode", code="ER")
    db.add(wh)
    db.commit()
    wid = wh.id

mine = client.post("/api/purchase-orders",
                   headers={**H, "X-Essa-Warehouse": str(wid)},
                   json={"supplier_name": "Local", "po_date": "2026-08-01",
                         "lines": [{"particulars": "y", "qty": 1, "rate": 2}]}).json()
eq("an order is stamped with the warehouse it was raised in",
   mine["warehouse_id"], wid)
seen = {r["id"] for r in client.get(
    "/api/purchase-orders", headers={**H, "X-Essa-Warehouse": str(wid)}).json()}
eq("which sees its own order", mine["id"] in seen, True)
eq("and unassigned ones stay visible, as everywhere else", po["id"] in seen, True)

# ===========================================================================
print("\n" + "=" * 68)
if bad:
    print("%d FAILED:" % len(bad))
    for b in bad:
        print("  - %s" % b)
    sys.exit(1)
print("all purchase-order checks passing")
