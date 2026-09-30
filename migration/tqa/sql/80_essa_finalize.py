"""80 · Hand the migrated database back to ESSA's own services.

* locations.sync — mirrors every store into the `auto_transfer_location` option
  list the LR form and the shop read (what ESSA does after any location edit).
* numbering.upsert — starts each ESSA document series AFTER the numbers the
  migration issued. ESSA probes at most MAX_PROBES (5000) taken numbers before
  giving up, so with 400K+ SKUs from ESSA-00001 onwards, the first GRN posted
  after migration would otherwise stall on ESSA-05001 and fail the unique SKU.
  The same applies to purchase orders (PO-#####) and debit notes (PR-#####).
"""
import re

from app.database import SessionLocal
from app import models
from app.services import locations, numbering


def _next(db, column, prefix):
    top = 0
    for (v,) in db.query(column).filter(column.like(prefix + "%")):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", v or "")
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def main():
    db = SessionLocal()
    try:
        print("    locations:", locations.sync(db))
        for doc, column, prefix in (("sku", models.Product.sku, "ESSA-"),
                                    ("purchase_order", models.PurchaseOrder.po_no, "PO-"),
                                    ("debit_note", models.PurchaseReturn.code, "PR-")):
            start = _next(db, column, prefix)
            numbering.upsert(db, doc, start=start)
            print(f"    {doc:15s} next number {start}")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
