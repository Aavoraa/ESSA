"""Checks bill cancellation, store credit, advances, coupons, the cash drawer,
customer feedback, scheduled messages and salary advances.

    python test_counter_extras.py

No pytest — the shop has no test dependency and this needs none. Runs against a
throwaway database with no warehouse attached, so it never touches
textile_shop.db.

Everything here is money a customer already has with the shop, money the shop
gives away, or money that ought to be in a drawer — so the checks drive the real
routes and then hold the balances to figures worked out by hand. The one that
matters most is cancellation: a cancelled bill has to hand back EVERYTHING it
took — stock, points, the coupon, the store credit, the advance — and stop
counting in the drawer and the reports, or the books quietly disagree with the
shelf.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'counter_extras_test.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
os.environ.pop("MESSAGE_WEBHOOK_URL", None)
sys.path.insert(0, str(SHOP_DIR))

from itsdangerous import URLSafeSerializer                          # noqa: E402

from app import create_app, db, drawer, messaging, vouchers         # noqa: E402
from app import cancellation, retail_reports as rr                  # noqa: E402
from app.models import (Category, Company, Counter, Coupon,          # noqa: E402
                        CouponCampaign, CreditNote, Customer, CustomerAdvance,
                        CustomerFeedback, DrawerSession, Invoice, Location, Product,
                        ScheduledMessage, StaffAdvance, User)

app = create_app()
failures = []


def check(name, got, want):
    ok_ = got == want
    print(f"{'PASS' if ok_ else 'FAIL'}  {name}")
    if not ok_:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
        failures.append(name)


def ok(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(name)


today = date.today()
try:
    born = today.replace(year=today.year - 30)
except ValueError:                                  # 29 February
    born = today.replace(year=today.year - 30, day=28)

with app.app_context():
    db.create_all()
    boss = User(username="boss", full_name="M Anand", role="manager")
    boss.set_password("x")
    till = User(username="till", full_name="Till User", role="cashier")
    till.set_password("x")
    priya = User(username="priya", full_name="Priya S", role="cashier", salary=15000)
    priya.set_password("x")
    cat = Category(name="MENS-SHIRT", section="MENS")
    kumar = Customer(name="R Kumar", phone="9876500001", dob=born)
    co = Company(name="Taqua Silks", gstin="33AAAAA0000A1Z5")
    db.session.add_all([boss, till, priya, cat, kumar, co])
    db.session.flush()
    branch = Location(name="TAQUA TIRUPUR", company_id=co.id)
    db.session.add(branch)
    db.session.flush()
    counter = Counter(name="G-BILL1", location_id=branch.id)
    shirt = Product(sku="ESSA-00002", name="MENS COTTON SHIRT", category_id=cat.id,
                    selling_price=1000.0, cost_price=500.0, gst_rate=5.0, stock_qty=20)
    db.session.add_all([counter, shirt])
    db.session.commit()
    IDS = {"boss": boss.id, "till": till.id, "priya": priya.id, "kumar": kumar.id,
           "branch": branch.id, "counter": counter.id, "shirt": shirt.id, "co": co.id}

client = app.test_client()


def login(uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def sell(qty, payments, customer=True, coupon=None):
    return client.post("/pos/checkout", json={
        "staff_code": "till", "customer_id": IDS["kumar"] if customer else None,
        "coupon_code": coupon, "payments": payments,
        "items": [{"product_id": IDS["shirt"], "quantity": qty, "unit_price": 1000.0}]})


login(IDS["boss"])
client.post("/pos/place", json={"company_id": IDS["co"], "location_id": IDS["branch"],
                                "floor_id": None, "counter_id": IDS["counter"]})

# ---- the drawer opens ---------------------------------------------------------
print("\n-- the drawer is opened with a float")
client.post("/drawer/open", data={"opening_float": "500"})
with app.app_context():
    s = drawer.current(IDS["counter"])
    ok("a session is open on this till", s is not None)
    check("with its float", s.opening_float if s else None, 500.0)
client.post("/drawer/open", data={"opening_float": "100"})
with app.app_context():
    check("a second open on the same till is refused",
          DrawerSession.query.filter_by(counter_id=IDS["counter"]).count(), 1)

# ---- coupon campaigns ------------------------------------------------------------
print("\n-- coupons are cut from campaigns")
client.post("/coupons/campaigns", data={"name": "Flat 100", "kind": "amount", "value": "100",
                                        "min_bill": "500", "valid_days": "30",
                                        "uses_per_coupon": "1"})
client.post("/coupons/campaigns", data={"name": "Next visit", "kind": "percent", "value": "10",
                                        "max_discount": "200", "valid_days": "15",
                                        "uses_per_coupon": "1", "issue_at_settlement": "on",
                                        "settlement_min_bill": "2000"})
with app.app_context():
    flat = CouponCampaign.query.filter_by(name="Flat 100").first()
    FLAT = flat.id
    check("described in words", flat.describe, "₹100 off on bills of ₹500+")
client.post("/coupons/issue", data={"campaign_id": FLAT, "count": "1", "code": "welcome100"})
client.post("/coupons/issue", data={"campaign_id": FLAT, "count": "1", "code": "WELCOME100"})
with app.app_context():
    check("a chosen code is kept, upper-cased, and only once",
          [c.code for c in Coupon.query.all()], ["WELCOME100"])

r = client.get("/pos/api/coupon?code=welcome100&amount=300&room=300").get_json()
check("short of the minimum, the code is still good — just not yet",
      (r["ok"], r["below_min"], r["amount"]), (True, True, 0.0))
r = client.get("/pos/api/coupon?code=WELCOME100&amount=2100&room=2000").get_json()
check("over the minimum it takes its 100 off", (r["ok"], r["amount"]), (True, 100.0))
r = client.get("/pos/api/coupon?code=NOPE").get_json()
check("an unknown code is refused", (r["ok"], r["error"]), (False, "No such coupon"))

# ---- an advance -----------------------------------------------------------------
print("\n-- the customer leaves an advance")
r = client.post(f"/customers/{IDS['kumar']}/advance", data={"amount": "1500", "method": "cash",
                                                          "note": "order deposit"})
check("it goes to its receipt", r.status_code, 302)
with app.app_context():
    adv = CustomerAdvance.query.first()
    ADV, ADV_NO = adv.id, adv.number
    check("taken at this till", adv.counter_id, IDS["counter"])
    check("balance is the whole advance", vouchers.advance_balance(adv), 1500.0)

# ---- a plain bill, then store credit ------------------------------------------------
print("\n-- a first bill, and a credit note kept as store credit")
r = sell(1, [{"method": "cash", "amount": 1050.0, "tendered": 1050.0}])
ok("bill 1 goes through", r.status_code == 200 and r.get_json().get("success"), r.get_json())
BILL1 = r.get_json()["invoice_id"]
with app.app_context():
    db.session.add(CreditNote(number="CN-T0001", invoice_id=BILL1, cashier_id=IDS["till"],
                              subtotal=1000.0, cgst=25.0, sgst=25.0, total=1050.0,
                              refund_method="store_credit", reason="exchange"))
    db.session.commit()
r = client.get("/pos/api/credit-note?code=cn-t0001").get_json()
check("the note is found and has all of itself left", (r["ok"], r["balance"]), (True, 1050.0))

# ---- the bill that uses everything ------------------------------------------------------
print("\n-- bill 2: coupon off, then store credit + advance + cash")
# 2 shirts = 2,000 + 100 GST = 2,100; coupon -100 → 2,000
r = sell(2, [{"method": "credit_note", "amount": 1050.0, "reference": "cn-t0001"},
             {"method": "advance", "amount": 500.0},
             {"method": "cash", "amount": 450.0, "tendered": 500.0}], coupon="welcome100")
body = r.get_json()
ok("bill 2 goes through", r.status_code == 200 and body.get("success"), body)
BILL2 = body.get("invoice_id")
check("it earned a coupon for next time (bill of 2,000 on a 2,000 minimum)",
      len(body.get("coupons_issued", [])), 1)
with app.app_context():
    inv = db.session.get(Invoice, BILL2)
    check("coupon is in the discount, and named", (inv.discount, inv.coupon_discount), (100.0, 100.0))
    check("total", inv.total, 2000.0)
    check("tenders pinned to their documents",
          sorted((p.method, p.amount, p.reference) for p in inv.payments),
          [("advance", 500.0, ADV_NO), ("cash", 450.0, None), ("credit_note", 1050.0, "CN-T0001")])
    check("payment method", inv.payment_method, "mixed")
    check("store credit spent", vouchers.credit_note_balance(CreditNote.query.first()), 0.0)
    check("advance left", vouchers.advance_balance(db.session.get(CustomerAdvance, ADV)), 1000.0)
    check("the coupon is used up", Coupon.query.filter_by(code="WELCOME100").first().state(), "used")
    check("stock came off", db.session.get(Product, IDS["shirt"]).stock_qty, 17.0)
    check("points: 1% of 1,050 and of 2,000",
          db.session.get(Customer, IDS["kumar"]).loyalty_points, 30.5)
    SETTLE_CODE = Coupon.query.filter_by(issued_via="settlement").first().code

print("\n-- what the till refuses")
r = sell(1, [{"method": "cash", "amount": 950.0, "tendered": 950.0}], coupon="WELCOME100")
check("a used coupon", (r.status_code, "already been used" in r.get_json()["error"]), (400, True))
r = sell(1, [{"method": "credit_note", "amount": 1050.0, "reference": "CN-T0001"}])
check("spent store credit", (r.status_code, "left" in r.get_json()["error"]), (400, True))
r = sell(1, [{"method": "credit_note", "amount": 1050.0}])
check("store credit with no note number", r.status_code, 400)
r = sell(1, [{"method": "advance", "amount": 1050.0}], customer=False)
check("an advance with no customer", (r.status_code, "Attach the customer" in r.get_json()["error"]),
      (400, True))
r = sell(1, [{"method": "advance", "amount": 1050.0}])
check("more than the advance holds", (r.status_code, "short" in r.get_json()["error"]), (400, True))
r = sell(1, [{"method": "advance", "amount": 1050.0, "tendered": 1100.0}])
check("an advance cannot be over-tendered", r.status_code, 400)
with app.app_context():
    check("no refused sale took stock", db.session.get(Product, IDS["shirt"]).stock_qty, 17.0)

# ---- the drawer knows --------------------------------------------------------------------
print("\n-- what should be in the drawer")
with app.app_context():
    expected, parts = drawer.breakdown(drawer.current(IDS["counter"]))
    check("cash on bills: 1,050 + 450 (not the store credit, not the advance spent)",
          parts["Cash on bills"], 1500.0)
    check("advance taken in cash", parts["Advances taken in cash"], 1500.0)
    check("expected: 500 float + 1,500 + 1,500", expected, 3500.0)

# ---- cancellation ---------------------------------------------------------------------------
print("\n-- who may cancel, and which bills")
with app.app_context():
    inv2 = db.session.get(Invoice, BILL2)
    check("not a cashier", cancellation.why_not(inv2, db.session.get(User, IDS["till"])),
          "Only a manager can cancel a bill.")
    ok("a manager, today, on a clean bill", cancellation.why_not(inv2, db.session.get(User, IDS["boss"])) is None)
    ok("not a bill from another day",
       "today" in cancellation.why_not(inv2, db.session.get(User, IDS["boss"]),
                                       today=inv2.invoice_date.date() + timedelta(days=1)))
    ok("not a bill with goods returned against it",
       "returned" in cancellation.why_not(db.session.get(Invoice, BILL1), db.session.get(User, IDS["boss"])))

login(IDS["till"])
r = client.post(f"/pos/invoice/{BILL2}/cancel", data={"reason": "test"})
check("a cashier posting the cancel is turned away", r.status_code, 403)
login(IDS["boss"])
r = client.post(f"/pos/invoice/{BILL2}/cancel", data={"reason": ""})
with app.app_context():
    ok("a cancel with no reason does nothing", not db.session.get(Invoice, BILL2).is_cancelled)

print("\n-- bill 2 cancelled: everything comes back")
r = client.post(f"/pos/invoice/{BILL2}/cancel", data={"reason": "billed to the wrong customer"},
                follow_redirects=True)
html = r.get_data(as_text=True)
ok("the bill page says so", "CANCELLED" in html and "billed to the wrong customer" in html)
ok("and says what to hand back", "CASH ₹450.00" in html)
with app.app_context():
    inv2 = db.session.get(Invoice, BILL2)
    check("marked cancelled, by whom", (inv2.payment_status, inv2.cancelled_by_id),
          ("cancelled", IDS["boss"]))
    check("stock back on the shelf", db.session.get(Product, IDS["shirt"]).stock_qty, 19.0)
    check("store credit back", vouchers.credit_note_balance(CreditNote.query.first()), 1050.0)
    check("advance back", vouchers.advance_balance(db.session.get(CustomerAdvance, ADV)), 1500.0)
    check("the coupon can be used again",
          Coupon.query.filter_by(code="WELCOME100").first().state(), "valid")
    check("the coupon it earned is withdrawn",
          Coupon.query.filter_by(code=SETTLE_CODE).first().state(), "withdrawn")
    kumar = db.session.get(Customer, IDS["kumar"])
    check("points it earned taken back", kumar.loyalty_points, 10.5)
    check("total spent is bill 1 only", kumar.total_spent, 1050.0)
    check("the drawer no longer expects its cash",
          drawer.breakdown(drawer.current(IDS["counter"]))[0], 3050.0)
    check("a second cancel is refused", cancellation.why_not(inv2, db.session.get(User, IDS["boss"])),
          "This bill is already cancelled.")

print("\n-- a cancelled bill stays in the register and out of the totals")
page = client.get("/pos/invoices").get_data(as_text=True)
with app.app_context():
    n2 = db.session.get(Invoice, BILL2).invoice_number
    n1 = db.session.get(Invoice, BILL1).invoice_number
ok("hidden from the live list", n2 not in page and n1 in page)
ok("listed when asked for", n2 in client.get("/pos/invoices?status=cancelled").get_data(as_text=True))
with app.test_request_context():
    check("sales report: bill 1 alone", rr.run("sales_report", today - timedelta(days=1),
                                               today + timedelta(days=1))["totals"]["Bills"], 1)
    check("cancelled report: bill 2 alone",
          [r[0] for r in rr.run("sales_cancelled", today - timedelta(days=1),
                                today + timedelta(days=1))["rows"]], [n2])
    check("coupon consumption: nothing, the only use was cancelled",
          rr.run("coupon_consumption", today - timedelta(days=1), today + timedelta(days=1))["rows"], [])

print("\n-- a return refuses a cancelled bill")
r = client.get(f"/returns/?q={n2}", follow_redirects=True)
html = r.get_data(as_text=True)
ok("the return screen says it was cancelled", "was cancelled" in html)
ok("and offers no return form for it", 'name="invoice_id"' not in html)

# ---- refund an advance, then close the drawer --------------------------------------------------
print("\n-- part of the advance handed back, then the drawer counted")
client.post(f"/customers/advances/{ADV}/refund", data={"amount": "200", "method": "cash"})
client.post(f"/customers/advances/{ADV}/refund", data={"amount": "5000", "method": "cash"})
with app.app_context():
    a = db.session.get(CustomerAdvance, ADV)
    check("refunded once, the over-large one refused", (a.refunded, vouchers.advance_balance(a)),
          (200.0, 1300.0))
    SID = drawer.current(IDS["counter"]).id
client.post(f"/drawer/{SID}/close", data={"counted_cash": "2800"})
with app.app_context():
    s = db.session.get(DrawerSession, SID)
    check("expected fixed at close: 3,050 − 200 refunded", s.expected_cash, 2850.0)
    check("short by 50", s.difference, -50.0)
    ok("and the till has no open drawer", drawer.current(IDS["counter"]) is None)

# ---- feedback through the bill's own link ------------------------------------------------------
print("\n-- feedback from the QR on the bill")
with app.app_context():
    signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="feedback")
    token1, token2 = signer.dumps(BILL1), signer.dumps(BILL2)
public = app.test_client()
check("the page opens without a login", public.get(f"/feedback/{token1}").status_code, 200)
check("a forged token is a 404", public.get(f"/feedback/{token1}x").status_code, 404)
check("a cancelled bill's link is a 404", public.get(f"/feedback/{token2}").status_code, 404)
public.post(f"/feedback/{token1}", data={"rating": "9"})
public.post(f"/feedback/{token1}", data={"rating": "4", "comments": "Good fit"})
public.post(f"/feedback/{token1}", data={"rating": "1", "comments": "second go"})
with app.app_context():
    rows = CustomerFeedback.query.all()
    check("one answer per bill, the out-of-range rating refused",
          [(f.rating, f.comments, f.source, f.customer_id) for f in rows],
          [(4, "Good fit", "link", IDS["kumar"])])
client.post("/customers/feedback", data={"rating": "5", "invoice": n1, "comments": "at the till"})
with app.app_context():
    check("staff can add one at the counter too", CustomerFeedback.query.count(), 2)

# ---- scheduled messages --------------------------------------------------------------------------
print("\n-- birthday wishes are queued once, and logged when there is no provider")
with app.app_context():
    check("the customer's birthday is today", [c.id for c, _ in messaging.due_wishes("birthday", 0)],
          [IDS["kumar"]])
    check("queued", messaging.queue_wishes("birthday", 7), 1)
    check("not twice", messaging.queue_wishes("birthday", 7), 0)
    db.session.commit()
    m = ScheduledMessage.query.first()
    ok("with the name filled in", "R Kumar" in m.body, m.body)
    counts = messaging.send_due(now=datetime.now() + timedelta(days=1))
    db.session.commit()
    check("no provider: logged, not sent", counts, {"sent": 0, "failed": 0, "logged": 1})
    check("and the log says why", ScheduledMessage.query.first().error,
          "No SMS/WhatsApp provider configured — not sent")
    check("a leap-day birthday falls on 28 February",
          messaging.next_occurrence(date(2000, 2, 29), date(2027, 1, 1)), date(2027, 2, 28))
client.post("/customers/messages/new", data={"body": "Hi {name}, sale on Friday", "to": "one",
                                             "phone": "0000", "kind": "offer"})
with app.app_context():
    check("a message to a phone nobody has is refused", ScheduledMessage.query.count(), 1)

# ---- salary advances ------------------------------------------------------------------------------
print("\n-- a salary advance and its recovery")
client.post("/staff/advances/new", data={"user_id": IDS["priya"], "amount": "3000",
                                         "given_on": today.isoformat(), "method": "cash"})
with app.app_context():
    SA = StaffAdvance.query.first().id
client.post(f"/staff/advances/{SA}/recover", data={"amount": "1000", "method": "salary",
                                                    "recovered_on": today.isoformat()})
client.post(f"/staff/advances/{SA}/recover", data={"amount": "5000", "method": "salary"})
with app.app_context():
    sa = db.session.get(StaffAdvance, SA)
    check("recovered 1,000, the over-recovery refused", (sa.recovered, sa.balance), (1000.0, 2000.0))

# ---- the reports that were waiting for these records -------------------------------------------------
print("\n-- the reports now have something to say")
with app.test_request_context():
    span = (today - timedelta(days=1), today + timedelta(days=1))
    check("advance collection", rr.run("customer_advance", *span)["totals"]["Balance now"], 1300.0)
    check("opening/closing", rr.run("opening_closing", *span)["totals"]["Difference"], -50.0)
    check("employee advance pending", rr.run("employee_advance", *span)["totals"]["Pending"], 2000.0)
    check("coupons issued: by hand and at settlement",
          rr.run("coupon_issue", *span)["totals"]["Coupons"], 2)
    check("settlement coupon", [r[9] for r in rr.run("settlement_coupon_issue", *span)["rows"]],
          ["Withdrawn"])
    check("credit note consumption: none left standing",
          rr.run("gv_cn_consumption", *span)["rows"], [])
    check("birthday", [r[0] for r in rr.run("birthday", *span)["rows"]], ["R Kumar"])
    check("anniversary: nobody recorded", rr.run("anniversary", *span)["rows"], [])
    check("feedback average", rr.run("feedback", *span)["totals"]["Average rating"], 4.5)
    check("message log", rr.run("scheduled_messages", *span)["totals"]["Logged"], 1)

# ---- every new screen opens -------------------------------------------------------------------------
print("\n-- every new screen opens")
for url, needle in [(f"/customers/{IDS['kumar']}", "Advance in hand"),
                    ("/customers/new", 'name="dob"'),
                    ("/customers/advances", ADV_NO),
                    (f"/customers/advances/{ADV}", "ADVANCE RECEIPT"),
                    ("/customers/feedback", "Good fit"),
                    ("/customers/messages", "No SMS or WhatsApp provider"),
                    ("/coupons/", "WELCOME100"),
                    ("/drawer/", "Open the drawer"),
                    ("/staff/advances", "Priya S"),
                    (f"/pos/invoice/{BILL1}", "Rate your visit"),
                    # the coupon is entered as the discount card's P.Code
                    ("/pos/", 'id="dcPCode"')]:
    r = client.get(url)
    ok(f"{url} opens", r.status_code == 200 and needle in r.get_data(as_text=True),
       f"HTTP {r.status_code}")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All counter extras checks passing.")
