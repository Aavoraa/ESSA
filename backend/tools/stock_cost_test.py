"""Weighted-average cost when stock has gone below zero.

    python backend/tools/stock_cost_test.py

The migrated history often issues goods before their receipt was keyed, so a
product's balance dips below zero and comes back. Blending that negative
quantity into the average once divided a large figure by the few units left:
a product at −143 that received 144 was valued at crores per piece, and the
dashboard's stock value read ₹2.6 lakh crore (2026-11-25).

The rule now: blend only with stock ON HAND; from zero or below, the receipt's
own rate is the average. Checked on the pure function and on a real replay.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.pop("ESSA_DATABASE_URL", None)
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'stockcost.db'}"

from app.services.stock_locations import rolled_average  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = abs(got - want) < 1e-6
    fails += not ok
    print(("ok   " if ok else "FAIL ") + f"{name}: {got} (want {want})")


# the ordinary case: blend what is on hand with what came in
check("blend on hand", rolled_average(10, 100, 10, 200), 150)
# from empty: the receipt's rate
check("from zero", rolled_average(0, 999, 5, 120), 120)
# THE bug: −143 on hand, 144 arrive at 185 → 1 left, worth 185, not crores
check("from negative", rolled_average(-143, 250, 144, 185), 185)
# still negative after the receipt: nothing on hand, so the rate
check("stays negative", rolled_average(-10, 250, 4, 300), 300)
# a negative average can never be produced
check("never negative", rolled_average(-1, 110227, 2, 120), 120)

# the same rule through the real ledger replay
from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402
from app.services import stock_locations as sl  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()
wh = models.Warehouse(name="TEST", code="TEST", active=True)
db.add(wh)
db.flush()
p = models.Product(sku="T-1", description="Test item", stock_qty=0, avg_cost=0)
db.add(p)
db.flush()
for kind, qty, rate in [("inward", 80, 250), ("outward", -223, 250),      # down to −143
                        ("inward", 144, 185),                              # back to 1
                        ("inward", 5, 205)]:                               # 6 on hand
    sl.apply(db, p, wh.id, qty, kind=kind, ref_type="test", rate=rate)
row = db.query(models.StockBalance).filter_by(product_id=p.id, warehouse_id=wh.id).one()
check("posting: qty", row.qty, 6)
check("posting: avg", row.avg_cost, round((1 * 185 + 5 * 205) / 6, 4))
sl.rebuild(db, p)
row = db.query(models.StockBalance).filter_by(product_id=p.id, warehouse_id=wh.id).one()
check("replay agrees: avg", row.avg_cost, round((1 * 185 + 5 * 205) / 6, 4))
check("product avg", p.avg_cost, round((1 * 185 + 5 * 205) / 6, 4))
db.rollback()
db.close()

print("\n" + ("All checks passed." if not fails else f"{fails} check(s) FAILED."))
sys.exit(1 if fails else 0)
