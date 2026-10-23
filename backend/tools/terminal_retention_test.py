"""A till is closed, then deleted a year later — not deleted on the day.

    python backend/tools/terminal_retention_test.py

A POS terminal is the one place in this hierarchy where deletion cannot be
decided by looking for rows that point at it. Nothing in THIS database does: the
sales belong to the shop, in the shop's own file, and they name the counter by
name. So the server would happily delete a till that billed all last week, and
nothing here would notice — the damage appears a year later, on a screen in a
different application, when a bill is read back and the counter that raised it
cannot be named.

The rule is therefore a date, and this checks the four ways it has to hold:

  * an OPEN till is never deletable, however old it is. This is the case a
    "one year after it was created" rule would have got wrong, and the reason
    the clock starts at closing rather than at creation.
  * a till closed today is refused, and the refusal says WHEN it may go.
  * a till closed over a year ago can be deleted.
  * reopening clears the clock. A counter that came back into use has not been
    retired at all, and must not stay deletable because it happened to be shut
    this time last year.

The screen is fed from the same place — `can_delete` decides whether the cross
is drawn at all — so what it offers and what the server allows cannot drift.

Runs against a throwaway SQLite file; nothing here touches the real database.
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-term-"), "test.db").replace(os.sep, "/")

from backend.app import models                                   # noqa: E402
from backend.app.database import engine, SessionLocal            # noqa: E402
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

w = client.post("/api/locations/warehouses", headers=H,
                json={"name": "Retention Warehouse", "code": "RTWH"}).json()
s = client.post("/api/locations/stores", headers=H,
                json={"name": "RETENTION STORE", "warehouse_id": w["id"]}).json()


def new_till(name):
    return client.post("/api/locations/terminals", headers=H,
                       json={"name": name, "store_id": s["id"]}).json()


def close(till, active=False):
    """Exactly what the Locations screen sends: {name, active} and nothing else."""
    return client.patch("/api/locations/terminals/%d" % till["id"], headers=H,
                        json={"name": till["name"], "active": active}).json()


def backdate(tid, days):
    """Put the closing date `days` into the past, the way time would have."""
    db = SessionLocal()
    row = db.get(models.PosTerminal, tid)
    row.deactivated_at = dt.datetime.utcnow() - dt.timedelta(days=days)
    db.commit()
    db.close()


# ===========================================================================
head("an open till is never deletable")
t = new_till("OPEN COUNTER")
eq("a new till is open", t["active"], True)
eq("it carries no closing date", t["deactivated_at"], None)
eq("the screen is told not to draw the cross", t["can_delete"], False)
eq("and there is no date to show yet", t["deletable_on"], None)

r = client.delete("/api/locations/terminals/%d" % t["id"], headers=H)
eq("deleting it is refused", r.status_code, 409)
eq("and the refusal says to close it first",
   "close this till first" in r.json()["detail"], True)

# The case a created_at rule gets wrong: old, but still billing.
backdate_open = new_till("OLD BUT OPEN")
db = SessionLocal()
row = db.get(models.PosTerminal, backdate_open["id"])
row.created_at = dt.datetime.utcnow() - dt.timedelta(days=900)
db.commit()
db.close()
r = client.delete("/api/locations/terminals/%d" % backdate_open["id"], headers=H)
eq("a till open for 900 days is still refused", r.status_code, 409)


# ===========================================================================
head("closing it starts the clock")
t2 = new_till("CLOSED TODAY")
closed = close(t2)
eq("it is closed", closed["active"], False)
eq("and the day it closed is recorded", bool(closed["deactivated_at"]), True)
eq("it is not deletable yet", closed["can_delete"], False)
eq("but the screen can say when", bool(closed["deletable_on"]), True)

on = dt.datetime.fromisoformat(closed["deletable_on"])
off = dt.datetime.fromisoformat(closed["deactivated_at"])
eq("and that date is a year out", (on - off).days, 365)

r = client.delete("/api/locations/terminals/%d" % t2["id"], headers=H)
eq("deleting it today is refused", r.status_code, 409)
eq("and the refusal names the date it may go",
   "can be deleted from" in r.json()["detail"], True)


# ===========================================================================
head("a year later it can go")
t3 = new_till("CLOSED LAST YEAR")
close(t3)
backdate(t3["id"], 366)
again = client.get("/api/locations/terminals", headers=H).json()
row = [x for x in again if x["id"] == t3["id"]][0]
eq("the screen is now told to draw the cross", row["can_delete"], True)

r = client.delete("/api/locations/terminals/%d" % t3["id"], headers=H)
eq("and the server allows it", r.status_code, 200)
db = SessionLocal()
eq("the row is really gone", db.get(models.PosTerminal, t3["id"]), None)
db.close()

# The boundary: a day short is still a day short.
t4 = new_till("CLOSED ALMOST A YEAR AGO")
close(t4)
backdate(t4["id"], 364)
r = client.delete("/api/locations/terminals/%d" % t4["id"], headers=H)
eq("364 days is refused", r.status_code, 409)


# ===========================================================================
head("reopening clears the clock")
t5 = new_till("BACK IN USE")
close(t5)
backdate(t5["id"], 400)
reopened = close(t5, active=True)
eq("it is open again", reopened["active"], True)
eq("and the old closing date is gone", reopened["deactivated_at"], None)
eq("so it is not deletable", reopened["can_delete"], False)
r = client.delete("/api/locations/terminals/%d" % t5["id"], headers=H)
eq("the server refuses it too", r.status_code, 409)

# Saving an edit on an already-closed till must not restart the year.
t6 = new_till("EDITED WHILE CLOSED")
close(t6)
backdate(t6["id"], 380)
edited = client.patch("/api/locations/terminals/%d" % t6["id"], headers=H,
                      json={"name": "RENAMED WHILE CLOSED", "active": False}).json()
eq("editing a closed till leaves its date alone", edited["can_delete"], True)


# ===========================================================================
print("\n" + "=" * 72)
if bad:
    print("%d FAILING" % len(bad))
    for b in bad:
        print("  - %s" % b)
    sys.exit(1)
print("all passing")
