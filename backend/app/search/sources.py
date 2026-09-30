"""PostgreSQL → documents. The only place that decides what a document holds.

Each document is the same join and filter `pos_sales._sales_by_product` uses,
grouped one level finer (by bill, not only by product), so summing documents
reproduces its figures: Σ qty, Σ line_total, one document per bill (so the
document count IS `COUNT(DISTINCT invoice)`), MAX(bill date), live flag.

Read in id ranges — never the whole table at once — through the warehouse's
own engine: on PostgreSQL the shop's tables are a schema of the same database.
"""
from sqlalchemy import text

from ..services import pos_sales

LIVE = "COALESCE(i.payment_status, 'paid') <> 'cancelled'"


def _iso(v):
    return v.isoformat() if v is not None else None


def _sales_sql(where):
    q = pos_sales.q
    return text(
        "SELECT i.id, p.warehouse_id, SUM(ii.quantity), SUM(ii.line_total), "
        f"       i.invoice_date, i.location_id, ({LIVE}) AS live "
        "FROM " + q("invoice_items") + " ii "
        "JOIN " + q("invoices") + " i ON i.id = ii.invoice_id "
        "JOIN " + q("products") + " p ON p.id = ii.product_id "
        "WHERE p.warehouse_id IS NOT NULL AND " + where + " "
        "GROUP BY i.id, p.warehouse_id, i.invoice_date, i.location_id, i.payment_status")


def _returns_sql(where):
    q = pos_sales.q
    return text(
        "SELECT cn.id, p.warehouse_id, SUM(ci.quantity), SUM(ci.line_total), cn.created_at "
        "FROM " + q("credit_note_items") + " ci "
        "JOIN " + q("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "JOIN " + q("products") + " p ON p.id = ci.product_id "
        "WHERE p.warehouse_id IS NOT NULL AND " + where + " "
        "GROUP BY cn.id, p.warehouse_id, cn.created_at")


def _sale_doc(r):
    iid, wid, qty, amount, when, loc, live = r
    return f"{iid}:{wid}", {"invoice_id": iid, "wid": wid,
                            "qty": float(qty) if qty is not None else None,
                            "amount": float(amount) if amount is not None else None,
                            "invoice_date": _iso(when), "location_id": loc, "live": bool(live)}


def _return_doc(r):
    cid, wid, qty, amount, when = r
    return f"{cid}:{wid}", {"credit_note_id": cid, "wid": wid,
                            "qty": float(qty) if qty is not None else None,
                            "amount": float(amount) if amount is not None else None,
                            "created_at": _iso(when)}


def sales_docs_between(conn, lo, hi):
    """Documents for bills with lo <= id <= hi."""
    return [_sale_doc(r) for r in conn.execute(_sales_sql("i.id BETWEEN :lo AND :hi"),
                                               {"lo": lo, "hi": hi})]


def sales_docs_for(conn, invoice_ids):
    ids = sorted({int(i) for i in invoice_ids})
    if not ids:
        return []
    return [_sale_doc(r) for r in conn.execute(_sales_sql("i.id = ANY(:ids)"), {"ids": ids})]


def returns_docs_between(conn, lo, hi):
    return [_return_doc(r) for r in conn.execute(_returns_sql("cn.id BETWEEN :lo AND :hi"),
                                                 {"lo": lo, "hi": hi})]


def returns_docs_for(conn, note_ids):
    ids = sorted({int(i) for i in note_ids})
    if not ids:
        return []
    return [_return_doc(r) for r in conn.execute(_returns_sql("cn.id = ANY(:ids)"), {"ids": ids})]


def max_ids(conn):
    q = pos_sales.q
    return (conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM " + q("invoices"))).scalar(),
            conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM " + q("credit_notes"))).scalar())


def cancelled_since(conn, since):
    """Bills cancelled at or after `since` (a naive timestamp), with the newest time."""
    q = pos_sales.q
    rows = conn.execute(text("SELECT id, cancelled_at FROM " + q("invoices") + " i "
                             "WHERE i.cancelled_at >= :s AND COALESCE(i.payment_status,'paid') = 'cancelled'"),
                        {"s": since}).fetchall()
    return [r[0] for r in rows], max((r[1] for r in rows), default=None)


def product_links(conn):
    """{shop product id: warehouse product id or None} — what documents are keyed by."""
    q = pos_sales.q
    return dict(conn.execute(text("SELECT id, warehouse_id FROM " + q("products"))).fetchall())


def invoices_with_products(conn, shop_product_ids):
    q = pos_sales.q
    ids = sorted({int(i) for i in shop_product_ids})
    if not ids:
        return [], []
    inv = [r[0] for r in conn.execute(text("SELECT DISTINCT invoice_id FROM " + q("invoice_items")
                                           + " WHERE product_id = ANY(:ids)"), {"ids": ids})]
    cn = [r[0] for r in conn.execute(text("SELECT DISTINCT credit_note_id FROM " + q("credit_note_items")
                                          + " WHERE product_id = ANY(:ids)"), {"ids": ids})]
    return inv, cn
