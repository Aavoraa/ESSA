"""What the shop has sold, read from the shop's own database.

The till is a separate Flask application with its own SQLite file (see
pos_mount.py). It already opens OUR database `mode=ro` to build its catalogue —
this is the mirror of that arrangement and obeys the same rule: read-only, never
written. The shop is the system of record for what was sold, exactly as the
warehouse is for what exists, and a report is not a reason to reach into another
application's tables with a pen.

`products.warehouse_id` in the shop is the join. Everything the shop sells came
from a warehouse item and carries our product id (the shop's
app/warehouse_items.py puts it there); anything the shop added on its own has
none and is invisible here — correctly, because the warehouse holds no stock of
it.

Returns are netted off. A kurti sold and brought back on a credit note is not a
kurti that sold, and counting it as one would keep a genuinely dead line looking
alive on the strength of a sale that was undone.

Every failure degrades to "no sales known" rather than raising: the shop may not
be installed beside us, may never have been opened, or may be locked mid-write.
A dead-stock report that refuses to open because the till is switched off is
worse than one that answers from the warehouse's own ledger and says so — which
is why `available()` is part of the answer and every caller reports it.
"""
import os
import sqlite3
import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from ..request_memo import memo, peek, remember
import threading as _threading
import time as _time

# ---------------------------------------------------------------------------
#  The till's two slowest facts, kept for up to a minute
# ---------------------------------------------------------------------------
#  `sales_of_stocked` and `status` each read the till's 2.1M bill lines (0.6 s
#  each, more when the database is busy), and every dashboard load, the bell
#  and the Command Center need both. They are kept here between requests —
#  never older than TILL_MAX_AGE seconds, the same bound the Elasticsearch copy
#  of these sales is held to — and refreshed in the background once they are
#  TILL_SOFT_AGE old, so a screen gets the kept answer at once instead of
#  waiting for the read. Figures from our own ledger (stock, GRNs, payables) are
#  not kept: those are read live on every load. A product that came into stock
#  after the till was read is fetched fresh (dead_stock._product_rows), as it
#  always was.
TILL_MAX_AGE = float(os.environ.get("ESSA_TILL_MAX_AGE", "60"))
TILL_SOFT_AGE = min(TILL_MAX_AGE, float(os.environ.get("ESSA_TILL_SOFT_AGE", "30")))
_till = {}                      # name -> (computed_at, value)
_till_lock = _threading.Lock()
_till_refreshing = set()


_till_computes = {}             # name -> how to read it again
_till_last_read = [0.0]
_till_keeper = [None]
TILL_KEEP_WHILE_USED = 600.0    # refresh in the background while read in the last 10 min


def _till_keep_warm():
    """While the dashboards are in use, read the till facts again every
    TILL_SOFT_AGE seconds in the background, so a screen never waits for them.
    Idle for TILL_KEEP_WHILE_USED, it stops asking the database anything."""
    def loop():
        while True:
            _time.sleep(max(5.0, TILL_SOFT_AGE - 5.0))
            if _time.time() - _till_last_read[0] > TILL_KEEP_WHILE_USED:
                continue
            for name, compute in list(_till_computes.items()):
                try:
                    t = _time.time()
                    value = compute()
                    with _till_lock:
                        _till[name] = (t, value)
                except Exception:                       # noqa: BLE001 — the next read computes
                    pass
    with _till_lock:
        if _till_keeper[0] is None:
            _till_keeper[0] = _threading.Thread(target=loop, name="till-keeper", daemon=True)
            _till_keeper[0].start()


def warm_till():
    """Read the till facts now, in the background — at server start, so the
    first dashboard finds them ready."""
    def run():
        for fn in (sales_of_stocked, status):
            try:
                fn()
            except Exception:                           # noqa: BLE001 — the first read computes
                pass
    _threading.Thread(target=run, name="till-warm", daemon=True).start()


def _till_kept(name, compute):
    now = _time.time()
    _till_last_read[0] = now
    if TILL_MAX_AGE > 0 and name not in _till_computes:
        _till_computes[name] = compute
        _till_keep_warm()
    with _till_lock:
        hit = _till.get(name)
    if hit is not None:
        age = now - hit[0]
        if age < TILL_SOFT_AGE:
            return hit[1]
        if age < TILL_MAX_AGE:
            _till_refresh_later(name, compute)
            return hit[1]
    value = compute()
    with _till_lock:
        _till[name] = (now, value)
    return value


def _till_refresh_later(name, compute):
    with _till_lock:
        if name in _till_refreshing:
            return
        _till_refreshing.add(name)

    def run():
        try:
            t = _time.time()
            value = compute()
            with _till_lock:
                _till[name] = (t, value)
        except Exception:                               # noqa: BLE001 — the next read computes
            pass
        finally:
            with _till_lock:
                _till_refreshing.discard(name)
    _threading.Thread(target=run, name="till-refresh-" + name, daemon=True).start()


def forget_till():
    """Drop the kept facts — tests, and anything that must see the till now."""
    with _till_lock:
        _till.clear()

# …/backend/app/services/pos_sales.py → …/essa-intake/Textile Retail Shop
_SHOP_DIR = Path(__file__).resolve().parents[3] / "Textile Retail Shop"
_DB_NAME = "textile_shop.db"

# ---------------------------------------------------------------------------
#  WHERE THE SHOP'S TABLES ACTUALLY ARE
# ---------------------------------------------------------------------------
#  This used to be a file path and nothing else: sqlite3 opened
#  "Textile Retail Shop/textile_shop.db" read-only, because both halves lived in
#  one folder on one PC. Deployed, they do not. The shop is mounted INTO this
#  process and put in the `shop` SCHEMA of the same Postgres database (see
#  app/pos_mount._isolate_shop_schema) — there is no file at all. So `available()`
#  answered False, and Dead Stock and the Item Locator reported "the shop is not
#  installed" while the till was billing in the same process.
#
#  Worse than blind: WRONG. A machine pointed at Postgres through DATABASE_URL
#  usually still has the old local textile_shop.db lying beside the code, so this
#  module read it and presented months-old local sales as live figures. A screen
#  that says nothing is recoverable; a screen that says the wrong number is not.
#
#  So the target is chosen the way the shop chooses ITS target when reading back
#  the other way (Textile Retail Shop/app/warehouse_items.warehouse_url): ask the
#  configured database first, and fall back to the file only when this really is
#  a SQLite install.
# ---------------------------------------------------------------------------
_sqlite_engine = None


def db_path():
    """The shop's SQLite file, or None. Only meaningful on a SQLite install."""
    env = os.environ.get("ESSA_POS_DB")
    if env:
        return Path(env) if os.path.exists(env) else None
    p = _SHOP_DIR / _DB_NAME
    return p if p.exists() else None


def _target():
    """(engine, table_prefix, description) for the shop's tables, or None.

    On Postgres the warehouse's OWN engine is reused — same database, different
    schema — rather than opening a second pool. That matters under NullPool
    (see database.py): a second engine per request would be a second connection
    per request to the same server.
    """
    from ..database import IS_SQLITE, engine as wh_engine

    if not IS_SQLITE:
        from ..pos_mount import POS_SCHEMA
        return wh_engine, f'"{POS_SCHEMA}".', f"postgres:{POS_SCHEMA}"

    global _sqlite_engine
    p = db_path()
    if not p:
        return None
    url = f"sqlite:///{p.as_posix()}"
    if _sqlite_engine is None or str(_sqlite_engine.url) != url:
        try:
            _sqlite_engine = create_engine(url)
        except SQLAlchemyError:
            return None
    return _sqlite_engine, "", str(p)


def available() -> bool:
    return _target() is not None


def source() -> str:
    """Where the figures are being read from, for a screen to say out loud."""
    t = _target()
    return t[2] if t else ""


def _connect():
    """Kept for callers that still want a raw SQLite handle. SQLite only."""
    p = db_path()
    if not p:
        return None
    try:
        # uri=ro rather than a plain path: opening read-write would create an
        # empty database where a missing one was, and the shop would then start
        # against a file with no schema
        return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _day_end(d):
    """An end date the till's timestamps compare against.

    invoice_date is stored as ISO text with a time on it, so `<= '2026-08-17'`
    would exclude everything sold ON the 17th — the last day of a campaign would
    silently count nothing."""
    return f"{d} 23:59:59.999999"


def q(table: str) -> str:
    """A shop table named so it resolves on whichever engine is connected."""
    t = _target()
    return f"{t[1]}{table}" if t else table


def _rows(sql, params):
    """Run one read against the shop. [] when it cannot be read at all.

    The queries here were written with sqlite3's positional `?`; those are
    translated to named parameters so the same SQL runs on Postgres. Table names
    are NOT translated here — callers wrap them in q() — because only the caller
    knows which identifiers in its SQL are tables.
    """
    t = _target()
    if not t:
        return []
    engine, _, _ = t
    named = {}
    if params:
        for i, v in enumerate(params):
            named[f"p{i}"] = v
            sql = sql.replace("?", f":p{i}", 1)
    try:
        with engine.connect() as con:
            return [tuple(r) for r in con.execute(text(sql), named)]
    except SQLAlchemyError:
        # a shop database older than the columns asked for here, or locked, or
        # a schema that does not exist yet. All mean "nothing known", not "the
        # warehouse cannot show dead stock"
        return []


#: A bill the till cancelled sold nothing. `payment_status` is on every shop
#: database there has ever been, so this needs no column check; COALESCE keeps a
#: row with no status counted, as it always was.
LIVE_BILL = " AND COALESCE(i.payment_status, 'paid') <> 'cancelled'"


def _window(start, end, col):
    where, params = "", []
    if start:
        where += f" AND {col} >= ?"
        params.append(str(start))
    if end:
        where += f" AND {col} <= ?"
        params.append(_day_end(end))
    return where, params


def sales_by_product(start=None, end=None):
    """Net sales per warehouse product (see _sales_by_product).

    Memoised for the length of one GET: the Command Centre reaches this through
    the dead-stock count, the dead-stock alerts and the notification rules, and
    each would otherwise re-run the same aggregation over every till line."""
    return memo(("pos_sales.sales_by_product", str(start), str(end)),
                lambda: _sales_by_product(start, end))


def _sales_by_product(start=None, end=None):
    """Net sales per WAREHOUSE product id, optionally inside a date window.

    Returns {product_id: {"qty", "amount", "bills", "last_sold"}} where amount is
    the taxable value the customer was billed (line_total), net of returns, and
    `last_sold` is an ISO date string.

    Sales and returns are counted in the window each HAPPENED in, not the window
    the original bill fell in: a campaign that sold nine kurtis and took two back
    within its own dates realised seven, and a return keyed in after the campaign
    closed does not reach back and change what that campaign achieved.
    """
    sold_where, sold_params = _window(start, end, "i.invoice_date")
    sold = _rows(
        "SELECT p.warehouse_id, SUM(ii.quantity), SUM(ii.line_total),"
        "       COUNT(DISTINCT i.id), MAX(i.invoice_date) "
        "FROM " + q("invoice_items") + " ii "
        "JOIN " + q("invoices") + " i ON i.id = ii.invoice_id "
        "JOIN " + q("products") + " p ON p.id = ii.product_id "
        "WHERE p.warehouse_id IS NOT NULL" + LIVE_BILL + sold_where +
        " GROUP BY p.warehouse_id", sold_params)
    if not start and not end:
        # Unwindowed, these groups are exactly what status() counts: the same
        # join and filter, one group per linked product, each carrying its
        # latest bill. Left for status() so it need not run the join again.
        lasts = [r[4] for r in sold if r[4] is not None]
        remember(("pos_sales.live_sale_facts",), (max(lasts) if lasts else None, len(sold)))

    ret_where, ret_params = _window(start, end, "cn.created_at")
    returned = _rows(
        "SELECT p.warehouse_id, SUM(ci.quantity), SUM(ci.line_total) "
        "FROM " + q("credit_note_items") + " ci "
        "JOIN " + q("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "JOIN " + q("products") + " p ON p.id = ci.product_id "
        "WHERE p.warehouse_id IS NOT NULL" + ret_where +
        " GROUP BY p.warehouse_id", ret_params)
    return _merge(sold, returned)


def _merge(sold, returned):
    """{product_id: {qty, amount, bills, last_sold}} from the sold and returned
    groups — sales less returns, as `_sales_by_product` describes."""
    out = {}
    for pid, qty, amount, bills, last in sold:
        if pid is None:
            continue
        out[int(pid)] = {"qty": float(qty or 0), "amount": float(amount or 0),
                         "bills": int(bills or 0),
                         # str(): SQLite hands back ISO text, Postgres a datetime
                         "last_sold": str(last or "")[:10] or None}
    for pid, qty, amount in returned:
        if pid is None:
            continue
        row = out.setdefault(int(pid), {"qty": 0.0, "amount": 0.0, "bills": 0,
                                        "last_sold": None})
        row["qty"] = round(row["qty"] - float(qty or 0), 3)
        row["amount"] = round(row["amount"] - float(amount or 0), 2)
    return out


def sales_of_stocked():
    """`sales_by_product()`, only for the warehouse products holding stock, and
    which of those it covers: `({product_id: {...}}, {covered ids})`.

    Dead stock reads the till only for products on the shelf (~6K), but the full
    aggregation groups every line ever billed (~340K products) and ships them all
    back — 1.5 s where this is 0.45 s. The groups are the same groups, so the
    figures are the same figures.

    The in-stock set is read inside the same statement as the sales (one
    statement, one snapshot) and returned as `covered`, so a product that came
    into stock between this read and the caller's own product list is known to
    be missing rather than silently read as never sold — see
    dead_stock._product_rows, which fetches those with `sales_of`.

    Only where the shop lives in the warehouse's own Postgres database; None
    elsewhere (a SQLite till cannot see the warehouse's products table), and the
    caller then uses `sales_by_product()` exactly as before. Memoised per GET."""
    return memo(("pos_sales.sales_of_stocked",),
                lambda: _till_kept("sales_of_stocked", _sales_of_stocked))


def _shares_warehouse_db():
    from ..database import IS_SQLITE
    return not IS_SQLITE and _target() is not None


def _sales_of_stocked():
    if not _shares_warehouse_db():
        return None
    from .. import models
    stocked = f"SELECT id FROM {models.Product.__tablename__} WHERE stock_qty > 0"
    # The search index, when it is fresh: the same figures (see app/search),
    # without aggregating the till's bill lines here. The stocked set is still
    # read from PostgreSQL, and is what `covered` reports. Anything the index
    # cannot answer — disabled, down, behind — falls through to the SQL below.
    from ..search import sales as es_sales, config as es_config
    if es_config.ENABLED:
        from ..database import engine as wh_engine
        with wh_engine.connect() as con:
            ids = [r[0] for r in con.execute(text(stocked))]
        got = es_sales.sales_of(ids) if ids else None
        if got is not None:
            return got, set(ids)
    # the in-stock set twice in ONE statement: the outer list says which products
    # were covered, the inner filter keeps the aggregation to them — the same
    # snapshot, so the two cannot disagree
    sold = _rows(
        "SELECT s.id, a.wid, a.qty, a.amount, a.bills, a.last "
        f"FROM {models.Product.__tablename__} s LEFT JOIN ("
        " SELECT p.warehouse_id AS wid, SUM(ii.quantity) AS qty, SUM(ii.line_total) AS amount,"
        "        COUNT(DISTINCT i.id) AS bills, MAX(i.invoice_date) AS last"
        " FROM " + q("invoice_items") + " ii"
        " JOIN " + q("invoices") + " i ON i.id = ii.invoice_id"
        " JOIN " + q("products") + " p ON p.id = ii.product_id"
        f" WHERE p.warehouse_id IN ({stocked})" + LIVE_BILL +
        " GROUP BY p.warehouse_id) a ON a.wid = s.id WHERE s.stock_qty > 0", [])
    returned = _rows(
        "SELECT s.id, a.wid, a.qty, a.amount "
        f"FROM {models.Product.__tablename__} s LEFT JOIN ("
        " SELECT p.warehouse_id AS wid, SUM(ci.quantity) AS qty, SUM(ci.line_total) AS amount"
        " FROM " + q("credit_note_items") + " ci"
        " JOIN " + q("credit_notes") + " cn ON cn.id = ci.credit_note_id"
        " JOIN " + q("products") + " p ON p.id = ci.product_id"
        f" WHERE p.warehouse_id IN ({stocked})"
        " GROUP BY p.warehouse_id) a ON a.wid = s.id WHERE s.stock_qty > 0", [])
    if not sold:
        # nothing in stock, or the read failed (`_rows` answers [] for both):
        # either way the full aggregation is the safe answer
        return None
    covered = {int(r[0]) for r in sold} & {int(r[0]) for r in returned}
    # a.wid is set exactly where the product had a group — as the full
    # aggregation has one row per product that was billed / returned
    out = _merge([r[1:] for r in sold if r[1] is not None],
                 [r[1:] for r in returned if r[1] is not None])
    return out, covered


def sales_of(product_ids):
    """`sales_by_product()` for these warehouse product ids only."""
    ids = sorted({int(i) for i in product_ids})
    if not ids:
        return {}
    listed = ", ".join(str(i) for i in ids)          # ints, so nothing to escape
    sold = _rows(
        "SELECT p.warehouse_id, SUM(ii.quantity), SUM(ii.line_total),"
        "       COUNT(DISTINCT i.id), MAX(i.invoice_date) "
        "FROM " + q("invoice_items") + " ii "
        "JOIN " + q("invoices") + " i ON i.id = ii.invoice_id "
        "JOIN " + q("products") + " p ON p.id = ii.product_id "
        f"WHERE p.warehouse_id IN ({listed})" + LIVE_BILL +
        " GROUP BY p.warehouse_id", [])
    returned = _rows(
        "SELECT p.warehouse_id, SUM(ci.quantity), SUM(ci.line_total) "
        "FROM " + q("credit_note_items") + " ci "
        "JOIN " + q("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "JOIN " + q("products") + " p ON p.id = ci.product_id "
        f"WHERE p.warehouse_id IN ({listed})"
        " GROUP BY p.warehouse_id", [])
    return _merge(sold, returned)


def bills_for_product(product_id, limit=50):
    """Every till bill one warehouse item appears on, newest first.

    The Item Locator's last question. Somebody holding a garment that is no
    longer in the warehouse wants to know whether it SOLD or whether it is lost,
    and those two answers look identical from this side of the wall: stock zero,
    nothing on the shelf. The shop knows, so the shop is asked.

    Returns are listed too, as negative quantities against their own credit note,
    because a piece sold and brought back is not a piece that sold — and a row
    saying so is the only way the totals here agree with the ones on the
    dead-stock report, which nets them off the same way.
    """
    if not available():
        return {"available": False, "rows": [], "qty": 0.0, "amount": 0.0}
    sold = _rows(
        "SELECT i.invoice_number, i.invoice_date, ii.quantity, ii.unit_price, "
        "       ii.gst_rate, ii.tax_amount, ii.line_total, c.name, c.phone "
        "FROM " + q("invoice_items") + " ii "
        "JOIN " + q("invoices") + " i ON i.id = ii.invoice_id "
        "JOIN " + q("products") + " p ON p.id = ii.product_id "
        "LEFT JOIN " + q("customers") + " c ON c.id = i.customer_id "
        "WHERE p.warehouse_id = ?" + LIVE_BILL + " "
        "ORDER BY i.invoice_date DESC, i.id DESC LIMIT ?", (int(product_id), int(limit)))
    rows = [{
        "kind": "sale", "bill_no": r[0], "date": str(r[1] or "")[:10] or None,
        "qty": float(r[2] or 0), "rate": float(r[3] or 0),
        "gst_rate": r[4], "tax": float(r[5] or 0), "amount": float(r[6] or 0),
        "customer": r[7], "phone": r[8],
    } for r in sold]

    returned = _rows(
        "SELECT cn.number, cn.created_at, ci.quantity, ci.unit_price, "
        "       ci.gst_rate, ci.tax_amount, ci.line_total, ci.condition "
        "FROM " + q("credit_note_items") + " ci "
        "JOIN " + q("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "JOIN " + q("products") + " p ON p.id = ci.product_id "
        "WHERE p.warehouse_id = ? "
        "ORDER BY cn.created_at DESC LIMIT ?", (int(product_id), int(limit)))
    rows += [{
        "kind": "return", "bill_no": r[0], "date": str(r[1] or "")[:10] or None,
        "qty": -float(r[2] or 0), "rate": float(r[3] or 0),
        "gst_rate": r[4], "tax": -float(r[5] or 0), "amount": -float(r[6] or 0),
        "customer": r[7], "phone": None,
    } for r in returned]

    rows.sort(key=lambda x: (x["date"] or "", x["bill_no"] or ""), reverse=True)
    return {
        "available": True, "rows": rows,
        "qty": round(sum(r["qty"] for r in rows), 3),
        "amount": round(sum(r["amount"] for r in rows), 2),
        "bills": len({r["bill_no"] for r in rows if r["kind"] == "sale"}),
        "last_sold": next((r["date"] for r in rows if r["kind"] == "sale" and r["date"]), None),
    }


def last_sold_index():
    """{product_id: ISO date} — the most recent till sale of each warehouse item.

    Kept separate from `sales_by_product` because this is the one fact the dead
    stock check turns on, and it is wanted for every product ever sold, whatever
    window a campaign happens to be looking at."""
    return {pid: row["last_sold"] for pid, row in sales_by_product().items()
            if row.get("last_sold")}


def status():
    """What the register should say about where its sales figures came from."""
    return memo(("pos_sales.status",), lambda: _till_kept("status", _status))


def _status():
    t = _target()
    if not t:
        return {"available": False, "reason": "The shop (POS) module is not installed here",
                "path": None, "source": None, "last_sale": None,
                "linked_products": 0}
    p = t[2]
    known = peek(("pos_sales.live_sale_facts",))
    if known is not None:
        last, linked = known
    elif _shares_warehouse_db():
        # The same two facts as the join below, asked the way the indexes can
        # answer: the newest live bill carrying a linked item (invoice_date index,
        # stops at the first), and the linked products with any live sale. The
        # full aggregation used to hand these over for free; dead stock now reads
        # only the stocked products' sales (sales_of_stocked), so they are asked
        # for here — 0.6 s where the join is 0.8 s.
        lr = _rows("SELECT i.invoice_date FROM " + q("invoices") + " i "
                   "WHERE i.invoice_date IS NOT NULL" + LIVE_BILL +
                   " AND EXISTS (SELECT 1 FROM " + q("invoice_items") + " ii "
                   "JOIN " + q("products") + " p ON p.id = ii.product_id "
                   "WHERE ii.invoice_id = i.id AND p.warehouse_id IS NOT NULL) "
                   "ORDER BY i.invoice_date DESC LIMIT 1", [])
        lk = _rows("SELECT COUNT(DISTINCT p.warehouse_id) FROM " + q("products") + " p "
                   "WHERE p.warehouse_id IS NOT NULL AND EXISTS (SELECT 1 FROM "
                   + q("invoice_items") + " ii JOIN " + q("invoices") + " i ON i.id = ii.invoice_id "
                   "WHERE ii.product_id = p.id" + LIVE_BILL + ")", [])
        last = lr[0][0] if lr else None
        linked = lk[0][0] if lk else 0
    else:
        rows = _rows("SELECT MAX(i.invoice_date), COUNT(DISTINCT p.warehouse_id) "
                     "FROM " + q("invoices") + " i "
                     "JOIN " + q("invoice_items") + " ii ON ii.invoice_id = i.id "
                     "JOIN " + q("products") + " p ON p.id = ii.product_id "
                     "WHERE p.warehouse_id IS NOT NULL" + LIVE_BILL, [])
        last, linked = (rows[0] if rows else (None, 0))
    return {"available": True, "reason": None, "path": str(p), "source": str(p),
            "last_sale": str(last or "")[:10] or None,
            "linked_products": int(linked or 0)}


def today():
    return dt.date.today()
