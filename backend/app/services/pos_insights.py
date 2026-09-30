"""The till's figures, cut the ways an owner asks for them.

services/pos_store_sales answers "what did each branch take". The Command Center
and its question box ask more than that — which FLOOR sold most, who gave the
biggest discount, how much of "ladies shirts" went this month, what the margin
was — and the till is the only place those answers live.

Same wall as pos_sales.py and the same rules: read-only, every query degrades to
"nothing known" rather than raising, and returns are netted off in the window
they happened in.

WINDOWS ARE UTC INSTANTS. Callers turn a business day into one with
services/business_day; the till stores `invoice_date` as naive UTC, and a string
'YYYY-MM-DD HH:MM:SS' compares correctly against it on SQLite and Postgres both.

WHERE: every sales function takes `at`, a dict narrowing the bills —
{"location_ids": [...], "floor_ids": [...], "counter_ids": [...]}. A key that is
absent or None does not narrow; an EMPTY list is a real answer of nothing (a
warehouse whose stores the till does not recognise sold nothing we can show).
"""
import re

from . import pos_sales

#: A bill the till cancelled sold nothing (see pos_sales.LIVE_BILL).
LIVE = " AND COALESCE(i.payment_status, 'paid') <> 'cancelled'"

#: What each breakdown groups by: (label SQL, joins it needs).
_BY = {
    "store": ("COALESCE(l.name, '(no branch)')",
              " LEFT JOIN {locations} l ON l.id = i.location_id"),
    "floor": ("COALESCE(f.name, '(no floor)')",
              " LEFT JOIN {floors} f ON f.id = i.floor_id"),
    "counter": ("COALESCE(c.name, '(no counter)') || ' · ' || COALESCE(l.name, '')",
                " LEFT JOIN {counters} c ON c.id = i.counter_id"
                " LEFT JOIN {locations} l ON l.id = i.location_id"),
    "cashier": ("COALESCE(u.full_name, u.username, '(unknown)')",
                " LEFT JOIN {users} u ON u.id = i.cashier_id"),
    "staff": ("COALESCE(s.full_name, s.username, '(not recorded)')",
              " LEFT JOIN {users} s ON s.id = i.staff_id"),
    "payment": ("COALESCE(i.payment_method, 'cash')", ""),
}
#: Groupings that live on the LINE rather than the bill.
_ITEM_BY = {
    "category": "COALESCE(cat.name, '(uncategorised)')",
    "product": "p.name",
}
BY_KEYS = tuple(_BY) + tuple(_ITEM_BY)

_STOP = {"the", "a", "an", "of", "for", "in", "on", "and", "all", "items", "item",
         "products", "product", "sales", "sale", "sold", "stock", "show", "me",
         "how", "much", "many", "what", "which", "this", "that", "month", "week",
         "today", "yesterday", "year", "last", "total", "value", "qty", "quantity",
         "pieces", "pcs", "units", "did", "we", "sell", "our", "is", "are", "there",
         "available", "left", "worth", "by", "from", "to", "with", "any"}
_SECTIONS = {"ladies": "LADIES", "lady": "LADIES", "women": "LADIES", "womens": "LADIES",
             "mens": "MENS", "men": "MENS", "gents": "MENS", "kids": "KIDS",
             "kid": "KIDS", "children": "KIDS", "boys": "KIDS", "girls": "KIDS"}


def available():
    return pos_sales.available()


def _t(table):
    return pos_sales.q(table)


def _ts(when):
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _where(at, alias="i"):
    """(sql, params) for the bills `at` narrows to — see the module note."""
    at = at or {}
    sql, params = "", []
    for key, col in (("location_ids", "location_id"), ("floor_ids", "floor_id"),
                     ("counter_ids", "counter_id")):
        ids = at.get(key)
        if ids is None:
            continue
        if not ids:
            return " AND 1=0", []
        sql += f" AND {alias}.{col} IN (" + ",".join("?" for _ in ids) + ")"
        params += [int(x) for x in ids]
    return sql, params


def words_of(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def _stem(w):
    if w.endswith("es") and len(w) > 4 and w[-3] in "sxz":
        return w[:-2]
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def item_filter(text):
    """(sql, params, words) matching products by what somebody CALLED them.

    "ladies shirts" is not a product name or a category code. It is two ideas: a
    section (LADIES) and a kind of garment (shirt). Each word must match
    something about the product — its name, its category, the category's
    section or its type — so the words narrow together rather than any one of
    them letting everything through. A trailing plural is dropped, because
    nobody's product master says "SHIRTS".
    """
    words = words_of(text)
    if not words:
        return "", [], []
    sql, params = "", []
    for w in words:
        section = _SECTIONS.get(w)
        if section:
            sql += " AND (UPPER(COALESCE(cat.section,'')) = ? OR LOWER(p.name) LIKE ?)"
            params += [section, f"%{w}%"]
            continue
        like = f"%{_stem(w)}%"
        sql += (" AND (LOWER(p.name) LIKE ? OR LOWER(COALESCE(cat.name,'')) LIKE ?"
                " OR LOWER(COALESCE(p.product_type,'')) LIKE ? OR LOWER(COALESCE(p.sku,'')) LIKE ?)")
        params += [like, like, like, like]
    return sql, params, words


def _item_joins():
    return (" JOIN " + _t("products") + " p ON p.id = ii.product_id"
            " LEFT JOIN " + _t("categories") + " cat ON cat.id = p.category_id")


def _tables():
    return {k: _t(k) for k in ("locations", "floors", "counters", "users")}


# ---------------------------------------------------------------------------
#  Totals
# ---------------------------------------------------------------------------
def totals(start, end, at=None, item=None):
    """Sales over [start, end): bills, gross, discount, tax, returns, net, units.

    With `item`, the figures are the matching LINES — gross is their value with
    tax, and a bill-level discount cannot be split onto them (`line_level`)."""
    if not available():
        return {"available": False}
    w_sql, w_params = _where(at)
    win = [_ts(start), _ts(end)]
    if item:
        f_sql, f_params, words = item_filter(item)
        rows = pos_sales._rows(
            "SELECT COUNT(DISTINCT i.id), SUM(ii.line_total + COALESCE(ii.tax_amount,0)), "
            "       SUM(ii.quantity), SUM(ii.line_total), COUNT(DISTINCT p.id) "
            "FROM " + _t("invoice_items") + " ii "
            "JOIN " + _t("invoices") + " i ON i.id = ii.invoice_id" + _item_joins() +
            " WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql + f_sql,
            win + w_params + f_params)
        bills, gross, units, taxable, products = rows[0] if rows else (0, 0, 0, 0, 0)
        ret = pos_sales._rows(
            "SELECT SUM(ci.line_total + COALESCE(ci.tax_amount,0)), SUM(ci.quantity) "
            "FROM " + _t("credit_note_items") + " ci "
            "JOIN " + _t("credit_notes") + " cn ON cn.id = ci.credit_note_id "
            "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id "
            "JOIN " + _t("products") + " p ON p.id = ci.product_id "
            "LEFT JOIN " + _t("categories") + " cat ON cat.id = p.category_id "
            "WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql + f_sql,
            win + w_params + f_params)
        r_amount, r_qty = ret[0] if ret else (0, 0)
        return {"available": True, "line_level": True, "words": words,
                "bills": int(bills or 0), "gross": round(_num(gross), 2),
                "taxable": round(_num(taxable), 2), "discount": 0.0, "tax": None,
                "returns": round(_num(r_amount), 2), "products": int(products or 0),
                "units": round(_num(units) - _num(r_qty), 3),
                "net": round(_num(gross) - _num(r_amount), 2)}

    rows = pos_sales._rows(
        "SELECT COUNT(DISTINCT i.id), SUM(i.total), SUM(COALESCE(i.subtotal,0)), "
        "       SUM(COALESCE(i.discount,0)), "
        "       SUM(COALESCE(i.cgst,0)+COALESCE(i.sgst,0)+COALESCE(i.igst,0)), "
        "       COUNT(DISTINCT i.location_id), COUNT(DISTINCT i.counter_id) "
        "FROM " + _t("invoices") + " i "
        "WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql,
        win + w_params)
    bills, gross, subtotal, discount, tax, branches, counters_ = rows[0] if rows else (0,) * 7
    units = pos_sales._rows(
        "SELECT SUM(ii.quantity) FROM " + _t("invoice_items") + " ii "
        "JOIN " + _t("invoices") + " i ON i.id = ii.invoice_id "
        "WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql,
        win + w_params)
    # Credit notes summed on their OWN row — joining their items first counts a
    # three-line note three times.
    ret = pos_sales._rows(
        "SELECT SUM(COALESCE(cn.total,0)), COUNT(cn.id) FROM " + _t("credit_notes") + " cn "
        "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id "
        "WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql, win + w_params)
    r_qty = pos_sales._rows(
        "SELECT SUM(ci.quantity) FROM " + _t("credit_note_items") + " ci "
        "JOIN " + _t("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id "
        "WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql, win + w_params)
    r_amount, r_notes = ret[0] if ret else (0, 0)
    return {"available": True, "line_level": False,
            "bills": int(bills or 0), "gross": round(_num(gross), 2),
            "taxable": round(_num(subtotal) - _num(discount), 2),
            "discount": round(_num(discount), 2), "tax": round(_num(tax), 2),
            "returns": round(_num(r_amount), 2), "return_notes": int(r_notes or 0),
            "units": round(_num((units[0] if units else (0,))[0])
                           - _num((r_qty[0] if r_qty else (0,))[0]), 3),
            "net": round(_num(gross) - _num(r_amount), 2),
            "branches": int(branches or 0), "counters": int(counters_ or 0)}


def breakdown(start, end, by, at=None, item=None, limit=None):
    """[{label, bills, amount, qty}] biggest first. `by` is one of BY_KEYS."""
    if not available():
        return []
    w_sql, w_params = _where(at)
    win = [_ts(start), _ts(end)]
    f_sql, f_params, _w = item_filter(item) if item else ("", [], [])
    if by in _ITEM_BY or item:
        label = _ITEM_BY.get(by)
        joins = ""
        if label is None:
            label, joins = _BY.get(by, _BY["store"])
            joins = joins.format(**_tables())
        rows = pos_sales._rows(
            f"SELECT {label}, COUNT(DISTINCT i.id), "
            "       SUM(ii.line_total + COALESCE(ii.tax_amount,0)), SUM(ii.quantity) "
            "FROM " + _t("invoice_items") + " ii "
            "JOIN " + _t("invoices") + " i ON i.id = ii.invoice_id" + _item_joins() + joins +
            " WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql + f_sql +
            f" GROUP BY {label}", win + w_params + f_params)
    else:
        label, joins = _BY.get(by, _BY["store"])
        rows = pos_sales._rows(
            f"SELECT {label}, COUNT(DISTINCT i.id), SUM(i.total), NULL "
            "FROM " + _t("invoices") + " i" + joins.format(**_tables()) +
            " WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql +
            f" GROUP BY {label}", win + w_params)
    out = [{"label": str(r[0] if r[0] is not None else "—").strip().strip("·").strip(),
            "bills": int(r[1] or 0), "amount": round(_num(r[2]), 2),
            "qty": None if r[3] is None else round(_num(r[3]), 3)} for r in rows]
    out.sort(key=lambda x: -x["amount"])
    return out[:limit] if limit else out


def returns_by(start, end, by="store", at=None):
    """Credit notes grouped by the branch, floor or counter of the bill they came
    off: [{label, notes, amount}].

    Grouped on the NOTE, never on its lines — a three-line credit note joined to
    its items is counted three times, which is what makes a returns column
    disagree with the till's own day-end figure.
    """
    if not available():
        return []
    w_sql, w_params = _where(at)
    label, joins = _BY.get(by, _BY["store"])
    rows = pos_sales._rows(
        f"SELECT {label}, COUNT(cn.id), SUM(COALESCE(cn.total,0)) "
        "FROM " + _t("credit_notes") + " cn "
        "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id" + joins.format(**_tables()) +
        " WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql + f" GROUP BY {label}",
        [_ts(start), _ts(end)] + w_params)
    return [{"label": str(r[0] or "—").strip().strip("·").strip(), "notes": int(r[1] or 0),
             "amount": round(_num(r[2]), 2)} for r in rows]


def top_products(start, end, at=None, limit=6, item=None):
    """Best sellers, with the SKU — so a row can be tracked, not just read.

    `breakdown(by="product")` groups on the name, which is what a chart labels
    itself with and is useless for following an item: two designs can share a
    name and nothing can be traced from one. This carries the SKU and the
    warehouse product id with it.
    """
    if not available():
        return []
    w_sql, w_params = _where(at)
    f_sql, f_params, _w = item_filter(item) if item else ("", [], [])
    rows = pos_sales._rows(
        "SELECT p.sku, p.name, p.warehouse_id, COUNT(DISTINCT i.id), "
        "       SUM(ii.line_total + COALESCE(ii.tax_amount,0)), SUM(ii.quantity) "
        "FROM " + _t("invoice_items") + " ii "
        "JOIN " + _t("invoices") + " i ON i.id = ii.invoice_id" + _item_joins() +
        " WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql + f_sql +
        " GROUP BY p.sku, p.name, p.warehouse_id", win_params(start, end) + w_params + f_params)
    out = [{"sku": r[0], "label": r[1], "warehouse_product_id": r[2], "bills": int(r[3] or 0),
            "amount": round(_num(r[4]), 2), "qty": round(_num(r[5]), 3)} for r in rows]
    out.sort(key=lambda r: -r["amount"])
    return out[:limit] if limit else out


def win_params(start, end):
    return [_ts(start), _ts(end)]


def discounts(start, end, by="cashier", at=None):
    """Who gave how much off: [{label, bills, discount, coupon, gross}]."""
    if not available():
        return []
    w_sql, w_params = _where(at)
    label, joins = _BY.get(by, _BY["cashier"])
    rows = pos_sales._rows(
        f"SELECT {label}, COUNT(DISTINCT i.id), SUM(COALESCE(i.discount,0)), "
        "       SUM(COALESCE(i.coupon_discount,0)), SUM(i.total) "
        "FROM " + _t("invoices") + " i" + joins.format(**_tables()) +
        " WHERE i.invoice_date >= ? AND i.invoice_date < ? AND COALESCE(i.discount,0) > 0"
        + LIVE + w_sql + f" GROUP BY {label}", [_ts(start), _ts(end)] + w_params)
    out = [{"label": str(r[0] or "—").strip().strip("·").strip(), "bills": int(r[1] or 0),
            "discount": round(_num(r[2]), 2), "coupon": round(_num(r[3]), 2),
            "gross": round(_num(r[4]), 2)} for r in rows]
    out.sort(key=lambda x: -x["discount"])
    return out


def margin(start, end, at=None, item=None):
    """Revenue before tax, what those goods cost, and the difference.

    Cost is the till's `cost_price`, which the shop copies from the warehouse's
    weighted-average cost — so this is margin at CURRENT cost, and says so. A
    bill discount comes off revenue at bill level; with an item filter it cannot
    be apportioned to lines and is left out."""
    if not available():
        return {"available": False}
    w_sql, w_params = _where(at)
    win = [_ts(start), _ts(end)]
    f_sql, f_params, _w = item_filter(item) if item else ("", [], [])
    cost = pos_sales._rows(
        "SELECT SUM(ii.quantity * COALESCE(p.cost_price,0)), SUM(ii.line_total), "
        "       SUM(CASE WHEN COALESCE(p.cost_price,0) > 0 THEN 0 ELSE ii.line_total END) "
        "FROM " + _t("invoice_items") + " ii "
        "JOIN " + _t("invoices") + " i ON i.id = ii.invoice_id" + _item_joins() +
        " WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql + f_sql,
        win + w_params + f_params)
    c_cost, c_lines, uncosted = cost[0] if cost else (0, 0, 0)
    back = pos_sales._rows(
        "SELECT SUM(ci.quantity * COALESCE(p.cost_price,0)), SUM(ci.line_total) "
        "FROM " + _t("credit_note_items") + " ci "
        "JOIN " + _t("credit_notes") + " cn ON cn.id = ci.credit_note_id "
        "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id "
        "JOIN " + _t("products") + " p ON p.id = ci.product_id "
        "LEFT JOIN " + _t("categories") + " cat ON cat.id = p.category_id "
        "WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql + f_sql,
        win + w_params + f_params)
    b_cost, b_lines = back[0] if back else (0, 0)
    if item:
        revenue = _num(c_lines) - _num(b_lines)
    else:
        bill = pos_sales._rows(
            "SELECT SUM(COALESCE(i.subtotal,0) - COALESCE(i.discount,0)) FROM "
            + _t("invoices") + " i WHERE i.invoice_date >= ? AND i.invoice_date < ?"
            + LIVE + w_sql, win + w_params)
        revenue = _num((bill[0] if bill else (0,))[0]) - _num(b_lines)
    cost_total = _num(c_cost) - _num(b_cost)
    profit = revenue - cost_total
    return {"available": True, "revenue": round(revenue, 2), "cost": round(cost_total, 2),
            "profit": round(profit, 2),
            "margin_pct": round(profit / revenue * 100, 1) if revenue > 0 else None,
            "uncosted_revenue": round(_num(uncosted), 2)}


def invoice_times(start, end, at=None):
    """[(invoice_date, total)] for bucketing into business days in Python —
    the portable way to group by a day that is not UTC's."""
    if not available():
        return []
    w_sql, w_params = _where(at)
    return pos_sales._rows(
        "SELECT i.invoice_date, i.total FROM " + _t("invoices") + " i "
        "WHERE i.invoice_date >= ? AND i.invoice_date < ?" + LIVE + w_sql,
        [_ts(start), _ts(end)] + w_params)


def return_times(start, end, at=None):
    if not available():
        return []
    w_sql, w_params = _where(at)
    return pos_sales._rows(
        "SELECT cn.created_at, cn.total FROM " + _t("credit_notes") + " cn "
        "LEFT JOIN " + _t("invoices") + " i ON i.id = cn.invoice_id "
        "WHERE cn.created_at >= ? AND cn.created_at < ?" + w_sql,
        [_ts(start), _ts(end)] + w_params)


def cancelled(start, end, at=None):
    if not available():
        return {"bills": 0, "amount": 0.0}
    w_sql, w_params = _where(at)
    rows = pos_sales._rows(
        "SELECT COUNT(i.id), SUM(i.total) FROM " + _t("invoices") + " i "
        "WHERE i.cancelled_at >= ? AND i.cancelled_at < ? "
        "AND COALESCE(i.payment_status,'') = 'cancelled'" + w_sql,
        [_ts(start), _ts(end)] + w_params)
    n, amount = rows[0] if rows else (0, 0)
    return {"bills": int(n or 0), "amount": round(_num(amount), 2)}


# ---------------------------------------------------------------------------
#  Stock the stores hold
# ---------------------------------------------------------------------------
def low_stock(limit=50, item=None):
    """Active store products at or below their reorder level, emptiest first."""
    if not available():
        return {"available": False, "count": 0, "rows": []}
    f_sql, f_params, _w = item_filter(item) if item else ("", [], [])
    source = (
        "FROM " + _t("products") + " p "
        "LEFT JOIN " + _t("categories") + " cat ON cat.id = p.category_id "
        "LEFT JOIN " + _t("floors") + " f ON f.id = p.floor_id "
        # `active IS NULL OR active = ?` rather than COALESCE(active, 1): the
        # column is an integer on SQLite and a boolean on Postgres, and COALESCE of
        # a boolean with 1 is a type error there.
        "WHERE (p.active IS NULL OR p.active = ?) "
        "AND COALESCE(p.stock_qty,0) <= COALESCE(p.reorder_level,0)" + f_sql)
    select = ("SELECT p.sku, p.name, COALESCE(cat.name,''), p.stock_qty, p.reorder_level, "
              "       COALESCE(f.name,'') " + source + " ORDER BY p.stock_qty ASC, p.name")
    params = [True] + f_params

    def shape(rows):
        return [{"sku": r[0], "product": r[1], "category": r[2], "stock": _num(r[3]),
                 "reorder_level": _num(r[4]), "floor": r[5] or None} for r in rows]

    if limit:
        # The count and the first `limit` rows, asked for as exactly that. A shop
        # whose reorder levels are unset has nearly every product "low", and
        # fetching all of them to show eight was most of the dashboard's wait.
        counted = pos_sales._rows("SELECT COUNT(*) " + source, params)
        if counted:
            rows = pos_sales._rows(select + f" LIMIT {int(limit)}", params)
            return {"available": True, "count": int(counted[0][0] or 0), "rows": shape(rows)}
    out = shape(pos_sales._rows(select, params))
    return {"available": True, "count": len(out), "rows": out[:limit] if limit else out}


def stock(floor_ids=None, item=None):
    """What the stores hold: units, value at cost, items — optionally on some
    floors and/or matching an item description."""
    if not available():
        return {"available": False}
    f_sql, f_params, _w = item_filter(item) if item else ("", [], [])
    where = "WHERE (p.active IS NULL OR p.active = ?) AND COALESCE(p.stock_qty,0) > 0"
    params = [True]
    if floor_ids is not None:
        if not floor_ids:
            return {"available": True, "units": 0.0, "value": 0.0, "items": 0, "rows": []}
        where += " AND p.floor_id IN (" + ",".join("?" for _ in floor_ids) + ")"
        params += [int(x) for x in floor_ids]
    rows = pos_sales._rows(
        "SELECT p.sku, p.name, COALESCE(cat.name,''), p.stock_qty, COALESCE(p.cost_price,0), "
        "       COALESCE(f.name,'') "
        "FROM " + _t("products") + " p "
        "LEFT JOIN " + _t("categories") + " cat ON cat.id = p.category_id "
        "LEFT JOIN " + _t("floors") + " f ON f.id = p.floor_id "
        + where + f_sql, params + f_params)
    out = [{"sku": r[0], "product": r[1], "category": r[2], "qty": _num(r[3]),
            "cost": _num(r[4]), "value": round(_num(r[3]) * _num(r[4]), 2),
            "floor": r[5] or None} for r in rows]
    out.sort(key=lambda r: -r["value"])
    return {"available": True, "units": round(sum(r["qty"] for r in out), 3),
            "value": round(sum(r["value"] for r in out), 2), "items": len(out),
            "rows": out}


def store_stock_of(warehouse_product_id):
    """How many of one warehouse item the stores still hold."""
    rows = pos_sales._rows(
        "SELECT SUM(COALESCE(stock_qty,0)) FROM " + _t("products") +
        " WHERE warehouse_id = ?", [int(warehouse_product_id)])
    return round(_num((rows[0] if rows else (0,))[0]), 3)


# ---------------------------------------------------------------------------
#  Names, for matching what somebody said
# ---------------------------------------------------------------------------
def floors():
    """[{id, name, prefix, branch, location_id}] — the till's storeys."""
    return [{"id": r[0], "name": r[1], "prefix": r[2], "branch": r[3], "location_id": r[4]}
            for r in pos_sales._rows(
                "SELECT f.id, f.name, f.prefix, l.name, f.location_id FROM " + _t("floors") + " f "
                "LEFT JOIN " + _t("locations") + " l ON l.id = f.location_id", [])]


def counters():
    return [{"id": r[0], "name": r[1], "branch": r[2], "location_id": r[3]}
            for r in pos_sales._rows(
                "SELECT c.id, c.name, l.name, c.location_id FROM " + _t("counters") + " c "
                "LEFT JOIN " + _t("locations") + " l ON l.id = c.location_id", [])]


def locations():
    return [{"id": r[0], "name": r[1]}
            for r in pos_sales._rows("SELECT id, name FROM " + _t("locations"), [])]


def people():
    return [{"id": r[0], "username": r[1], "name": r[2] or r[1], "role": r[3]}
            for r in pos_sales._rows(
                "SELECT id, username, full_name, role FROM " + _t("users"), [])]


def _norm(name):
    return " ".join(str(name or "").split()).lower()


def location_ids_for_stores(stores):
    """The till's branch ids for these Store rows, matched by name — the only key
    the two databases share (see pos_store_sales)."""
    if not available():
        return []
    wanted = {_norm(s.name) for s in stores}
    return [int(r["id"]) for r in locations() if _norm(r["name"]) in wanted]


def location_ids_for_warehouses(db, warehouse_ids):
    """The till's branch ids for the stores these warehouses supply, or None for
    "do not narrow" when no warehouses were named."""
    if not warehouse_ids:
        return None
    from .. import models
    return location_ids_for_stores(db.query(models.Store).filter(
        models.Store.warehouse_id.in_([int(x) for x in warehouse_ids])).all())


def branch_warehouses(db):
    """{till location name (normalised): (warehouse_id, warehouse name, store name)}."""
    from .. import models
    out = {}
    for s in db.query(models.Store).all():
        wh = s.warehouse
        out[_norm(s.name)] = (s.warehouse_id, wh.name if wh else None, s.name)
    return out


# ---------------------------------------------------------------------------
#  Recent bills and one bill in full
# ---------------------------------------------------------------------------
def recent_bills(limit=10, at=None, since=None):
    if not available():
        return []
    w_sql, w_params = _where(at)
    where, params = "WHERE 1=1", []
    if since is not None:
        where += " AND i.invoice_date >= ?"
        params.append(_ts(since))
    rows = pos_sales._rows(
        "SELECT i.invoice_number, i.invoice_date, i.total, i.payment_status, "
        "       COALESCE(c.name,''), COALESCE(l.name,''), COALESCE(u.full_name, u.username, ''), "
        "       COALESCE(f.name,'') "
        "FROM " + _t("invoices") + " i "
        "LEFT JOIN " + _t("counters") + " c ON c.id = i.counter_id "
        "LEFT JOIN " + _t("locations") + " l ON l.id = i.location_id "
        "LEFT JOIN " + _t("users") + " u ON u.id = i.cashier_id "
        "LEFT JOIN " + _t("floors") + " f ON f.id = i.floor_id "
        + where + w_sql + " ORDER BY i.invoice_date DESC, i.id DESC LIMIT ?",
        params + w_params + [int(limit)])
    return [{"bill_no": r[0], "at": str(r[1] or ""), "total": round(_num(r[2]), 2),
             "status": r[3] or "paid", "counter": r[4] or None, "branch": r[5] or None,
             "cashier": r[6] or None, "floor": r[7] or None} for r in rows]


def find_bill(number):
    """One bill by its number, with its lines, tenders and returns — or None."""
    if not available() or not (number or "").strip():
        return None
    rows = pos_sales._rows(
        "SELECT i.id, i.invoice_number, i.invoice_date, i.subtotal, i.discount, i.total, "
        "       i.payment_status, i.payment_method, COALESCE(l.name,''), COALESCE(f.name,''), "
        "       COALESCE(c.name,''), COALESCE(u.full_name,u.username,''), "
        "       COALESCE(s.full_name,s.username,''), COALESCE(cu.name,''), COALESCE(cu.phone,''), "
        "       i.cancelled_at, i.cancel_reason, i.location_id "
        "FROM " + _t("invoices") + " i "
        "LEFT JOIN " + _t("locations") + " l ON l.id = i.location_id "
        "LEFT JOIN " + _t("floors") + " f ON f.id = i.floor_id "
        "LEFT JOIN " + _t("counters") + " c ON c.id = i.counter_id "
        "LEFT JOIN " + _t("users") + " u ON u.id = i.cashier_id "
        "LEFT JOIN " + _t("users") + " s ON s.id = i.staff_id "
        "LEFT JOIN " + _t("customers") + " cu ON cu.id = i.customer_id "
        "WHERE UPPER(i.invoice_number) = UPPER(?)", [number.strip()])
    if not rows:
        return None
    r = rows[0]
    iid = r[0]
    lines = [{"sku": x[0], "product": x[1], "qty": _num(x[2]), "rate": _num(x[3]),
              "amount": round(_num(x[4]) + _num(x[5]), 2), "warehouse_product_id": x[6]}
             for x in pos_sales._rows(
                 "SELECT p.sku, p.name, ii.quantity, ii.unit_price, ii.line_total, "
                 "       ii.tax_amount, p.warehouse_id "
                 "FROM " + _t("invoice_items") + " ii "
                 "JOIN " + _t("products") + " p ON p.id = ii.product_id "
                 "WHERE ii.invoice_id = ?", [iid])]
    tenders = [{"method": x[0], "amount": _num(x[1])} for x in pos_sales._rows(
        "SELECT method, amount FROM " + _t("invoice_payments") + " WHERE invoice_id = ?", [iid])]
    notes = [{"number": x[0], "at": str(x[1] or ""), "total": _num(x[2])}
             for x in pos_sales._rows(
                 "SELECT number, created_at, total FROM " + _t("credit_notes") +
                 " WHERE invoice_id = ? ORDER BY created_at", [iid])]
    return {"id": iid, "bill_no": r[1], "at": str(r[2] or ""), "subtotal": _num(r[3]),
            "discount": _num(r[4]), "total": _num(r[5]), "status": r[6] or "paid",
            "method": r[7], "branch": r[8] or None, "floor": r[9] or None,
            "counter": r[10] or None, "cashier": r[11] or None, "staff": r[12] or None,
            "customer": r[13] or None, "phone": r[14] or None,
            "cancelled_at": str(r[15]) if r[15] else None, "cancel_reason": r[16],
            "location_id": r[17], "lines": lines, "tenders": tenders, "returns": notes}
