"""Stage 4 — compare the legacy data with what ESSA now holds, and write the reports.

Writes into migration/tqa/output/:

  migration_summary.csv     per entity: legacy rows, migrated, excluded, and the
                            business totals (amounts, quantities) on both sides
  migration_errors.csv      every exception logged by the migration steps
                            (excluded / unmatched / review), with its reason
  migration_duplicates.csv  records sharing a business identifier, and what was done
  migration_unmatched.csv   the unmatched subset of the exceptions, for follow-up
  id_mapping.csv            legacy id → ESSA id for every mapped entity
  stock_reconciliation.csv  per product: documented movements vs legacy closing stock

Exit code 1 if any HARD check fails (a control total that must match exactly).

    ESSA_DATABASE_URL=postgresql://postgres:***@localhost:5432/essa \
        python validate.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import psycopg2

OUT = Path(__file__).resolve().parent / "output"

#: (entity, legacy query, ESSA query, hard) — each query returns (rows, amount, qty).
#: `hard` checks must agree exactly; the others are reported with the reason they differ.
CHECKS = [
    ("Businesses", "select count(*), null, null from legacy.company",
     "select count(*), null, null from businesses", False),
    ("Stores + warehouse", "select count(*), null, null from legacy.referencelist where type='LOCATION'",
     "select (select count(*) from stores) + (select count(*) from warehouses), null, null", True),
    ("Suppliers", "select count(*), null, null from legacy.supplier",
     "select count(*), null, null from suppliers", True),
    ("Brands (master)", "select count(*), null, null from legacy.brand",
     "select count(*), null, null from master_records where master='brand'", False),
    ("Product groups", "select count(*), null, null from legacy.products",
     "select count(*), null, null from master_records where master='product'", False),
    ("Employees", "select count(*), null, null from legacy.employee",
     "select count(*), null, null from master_records where master='employee'", False),
    ("Customers", "select count(*), null, null from legacy.customer",
     "select count(*), null, null from shop.customers where id in (select new_id from migration.map_customer)", True),
    ("Users (shop logins)", "select count(*), null, null from legacy.users",
     "select count(*), null, null from shop.users where id in (select shop_user_id from migration.map_user where old_id > 0)", True),
    ("Products (item x design with pieces)",
     "select count(*), null, null from (select distinct migration.int(itemid), coalesce(migration.txt(designid), '') "
     "from legacy.stock where migration.int(itemid) is not null) x",
     "select count(*), null, null from products", True),
    ("Purchase orders", "select count(*), null, null from legacy.purchaseorder",
     "select count(*), null, null from purchase_orders", True),
    ("LR entries (active)", "select count(*), null, null from legacy.lrentry where isactive='t'",
     "select count(*), null, null from lr_entries", True),
    ("GRNs (active purchase invoices)",
     "select count(*), sum(migration.num(totalamount)), null from legacy.lrinvoice where isactive='t'",
     "select count(*), sum(grand_total), null from purchases", True),
    ("Supplier returns (active)",
     "select count(*), sum(migration.num(total)), null from legacy.lrinvoicereturn where isactive='t'",
     "select count(*), sum(total), null from purchase_returns", True),
    ("Supplier payments (live)",
     "select count(*), sum(migration.num(paidamount)), null from legacy.supplierpayment "
     "where isactive='t' and hascancel='f'",
     "select count(*), sum(paid_amount), null from payments", True),
    ("Piece codes (barcoded pieces received)",
     "select count(*), null, sum(qty) from migration.piece "
     "where active and qty > 0 and purchase_id is not null",
     "select count(*), null, null from product_units", False),
    ("Warehouse closing stock",
     "select null, null, sum(greatest(qty - transferred - returned, 0)) from migration.piece "
     "where active and has_stock and qty > 0 and purchase_id is not null",
     "select null, null, sum(stock_qty) from products", True),
    ("Warehouse stock value (at cost)",
     "select null, sum(greatest(qty - transferred - returned, 0) * coalesce(cost, 0)), null from migration.piece "
     "where active and has_stock and qty > 0 and purchase_id is not null",
     "select null, sum(stock_qty * avg_cost), null from products", False),
    ("Store closing stock",
     "select null, null, sum(s.held) from migration.store_piece s join migration.map_location m "
     "on m.old_id = s.location_id and m.kind = 'store'",
     "select null, null, sum(qty) from shop.location_stock", True),
    ("Dispatches warehouse -> store (active notes)",
     "select count(*), null, null from legacy.bundles where isactive='t' and fromlocationid='2' "
     "and tolocationid in (select old_id::text from migration.map_location where kind='store')",
     "select count(distinct o.id), null, sum(l.qty) from stock_outwards o join stock_outward_lines l on l.outward_id = o.id", False),
    ("Sales bills (live)",
     "select count(*), sum(migration.num(receivable)), sum(migration.num(qty)) from legacy.bill b "
     "join migration.bill_class c on c.id = migration.int(b.id) where c.cls='sale' and b.iscancel='f'",
     "select count(*), sum(total), null from shop.invoices where payment_status='paid'", True),
    ("Sales bills (cancelled)",
     "select count(*), sum(migration.num(receivable)), null from legacy.bill b "
     "join migration.bill_class c on c.id = migration.int(b.id) where c.cls='sale' and b.iscancel='t'",
     "select count(*), sum(total), null from shop.invoices where payment_status='cancelled'", True),
    ("Sales bill lines",
     "select count(*), null, sum(l.qty) from migration.bill_line l join migration.bill_class c "
     "on c.id = l.bill_id where c.cls='sale'",
     "select count(*), null, sum(quantity) from shop.invoice_items", True),
    ("Tenders (active settlements)",
     "select count(*), sum(migration.num(paid_cash)+migration.num(paid_card)+migration.num(paid_upi)), null "
     "from legacy.billsettlementmaster where isactive='t'",
     "select count(*), sum(amount), null from shop.invoice_payments", False),
    ("Customer returns",
     "select count(*), -sum(migration.num(receivable)), null from legacy.bill b join migration.bill_class c "
     "on c.id = migration.int(b.id) where c.cls='return'",
     "select count(*), sum(total), null from shop.credit_notes", False),
]


def _one(cur, q):
    cur.execute(q)
    return cur.fetchone()


def main() -> int:
    url = os.environ.get("ESSA_DATABASE_URL")
    if not url:
        print(__doc__)
        return 2
    OUT.mkdir(exist_ok=True)
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True)
    cur = conn.cursor()

    failed = 0
    with open(OUT / "migration_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entity", "legacy_rows", "essa_rows", "row_diff", "legacy_amount", "essa_amount",
                    "amount_diff", "legacy_qty", "essa_qty", "qty_diff", "check", "result"])
        for name, lq, eq, hard in CHECKS:
            lr, la, lqty = _one(cur, lq)
            er, ea, eqty = _one(cur, eq)

            def diff(a, b):
                return None if a is None or b is None else round(float(b) - float(a), 2)
            ok = all(d in (None, 0) or abs(d) < 0.01 for d in
                     (diff(lr, er), diff(la, ea) if la is not None else None,
                      diff(lqty, eqty) if lqty is not None and eqty is not None else None))
            result = "match" if ok else ("MISMATCH" if hard else "differs (see migration_errors.csv)")
            failed += (not ok) and hard
            w.writerow([name, lr, er, diff(lr, er), la and round(float(la), 2), ea and round(float(ea), 2),
                        diff(la, ea), lqty and round(float(lqty), 3), eqty and round(float(eqty), 3),
                        diff(lqty, eqty), "hard" if hard else "reported", result])
            print(f"  {name:45s} {result}")

        # how the exceptions add up, by entity and kind
        w.writerow([])
        w.writerow(["exceptions by entity", "kind", "count"])
        cur.execute("select entity, kind, count(*) from migration.exceptions group by 1, 2 order by 1, 2")
        for row in cur.fetchall():
            w.writerow(row)

    def dump(name, q):
        cur.execute(q)
        with open(OUT / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur.fetchall())

    dump("migration_errors.csv",
         "select id, entity, old_id, kind, reason, detail::text detail from migration.exceptions order by id")
    dump("migration_unmatched.csv",
         "select id, entity, old_id, reason, detail::text detail from migration.exceptions "
         "where kind = 'unmatched' order by id")
    dump("migration_duplicates.csv",
         "select entity, match_key, array_to_string(old_ids, ' | ') legacy_ids, "
         "array_to_string(new_ids, ' | ') essa_ids, resolution from migration.duplicates order by entity, match_key")
    dump("id_mapping.csv", """
        select 'company' entity, old_id::text legacy_id, 'businesses' essa_table, new_id essa_id from migration.map_company
        union all select 'location:' || kind, old_id::text, kind || 's', new_id from migration.map_location
        union all select 'floor', old_id::text, 'floors', new_id from migration.map_floor
        union all select 'counter', old_id::text, 'pos_terminals', new_id from migration.map_counter
        union all select 'product_group', old_id::text, 'categories:' || name, null from migration.map_category
        union all select 'tax', old_id::text, 'master_records', new_id from migration.map_tax
        union all select 'brand', old_id::text, 'master_records', new_id from migration.map_brand
        union all select 'supplier', old_id::text, 'suppliers', new_id from migration.map_supplier
        union all select 'agent', old_id::text, 'agents', new_id from migration.map_agent
        union all select 'transport', old_id::text, 'transports', new_id from migration.map_transport
        union all select 'employee', old_id::text, 'master_records', new_id from migration.map_employee
        union all select 'user', old_id::text, 'users (ESSA)', essa_user_id from migration.map_user where essa_user_id is not null
        union all select 'user', old_id::text, 'shop.users', shop_user_id from migration.map_user where shop_user_id is not null
        union all select 'item+design', item_id || '|' || design, 'products:' || sku, new_id from migration.map_product
        union all select 'purchaseorder', old_id::text, 'purchase_orders', new_id from migration.map_po
        union all select 'lrentry', old_id::text, 'lr_entries', new_id from migration.map_lr
        union all select 'lrinvoice', old_id::text, 'purchases:' || grn_no, new_id from migration.map_grn
        union all select 'lrinvoiceitem', old_id::text, 'purchase_lines', new_id from migration.map_grn_line
        union all select 'lrinvoicereturn', old_id::text, 'purchase_returns', new_id from migration.map_purchase_return
        union all select 'bundle', old_id::text, 'stock_outwards', new_id from migration.map_outward
        union all select 'supplierpayment', old_id::text, 'payments', new_id from migration.map_payment
        union all select 'customer', old_id::text, 'shop.customers', new_id from migration.map_customer
        union all select 'bill', old_id::text, 'shop.invoices', invoice_id from migration.map_bill
        union all select 'bill(return)', old_id::text, 'shop.credit_notes', credit_note_id from migration.map_credit_note
        union all select 'stock(piece)', p.id::text, 'product_units:' || p.barcode, u.id
          from migration.piece p join product_units u on u.code = p.barcode
        order by 1, 2""")
    dump("stock_reconciliation.csv", """
        select 'warehouse' side, p.sku, p.description, r.documented, r.legacy_closing, r.adjustment
          from migration.reconciliation r join products p on p.id = r.product_id where abs(r.adjustment) >= 0.001
        union all
        select 'stores', sp.sku, sp.name, r.documented, r.legacy_closing, r.adjustment
          from migration.store_reconciliation r join shop.products sp on sp.id = r.product_id
         where abs(r.adjustment) >= 0.001
        order by 1, 2""")

    conn.close()
    print(f"reports written to {OUT}")
    if failed:
        print(f"{failed} HARD CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
