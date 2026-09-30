"""
Inventory integrity — what is allowed to be counted, shown and printed.

Stock is only ever created by posting a GRN. So every inventory record should be
traceable back to a posted GRN, and anything that isn't is not stock — it is
debris, and the danger is that it does not *look* like debris. A stale piece code
carries a real-looking QR, prints on a real label and scans to a real product. The
warehouse has no way to tell it from a live one.

That is not hypothetical. A bulk `clear-all` once emptied `products` without
touching `product_units` or `bundles`; ids then restarted, and the next product to
land on id 1 silently adopted a hundred dead piece codes belonging to a product
that no longer existed. The screen read "110 pieces of 10 Pcs in stock" and offered
to print all 110. Nothing was corrupt at the row level — every row was individually
valid — which is exactly why nothing caught it. `clear-all` now deletes those
children explicitly; this module is the standing check that does not depend on
every future delete path remembering to.

THREE STATES, and only the first is stock
-----------------------------------------
* **posted** — traceable to a GRN that is posted. Real stock.
* **unposted** — traceable to a GRN that exists but has been unposted. Its stock
  was reversed, so it is already zero; the record is kept deliberately (see
  `inventory.unpost_grn`) because it carries history the GRN never created —
  phone-recorded detailing, a dispatch, a return. Excluded from stock and from
  labels; **never** deleted, because deleting it would destroy that history.
* **orphan** — traceable to nothing at all: no purchase line, no breakdown row, no
  stock movement. Nothing that could have created it survives. This is debris,
  and it is what `repair()` removes.

The distinction between the last two is the whole reason repair is not simply
"delete anything without a posted GRN". Both are excluded from stock; only one is
safe to delete.

The middle state applies to **products only**. A piece code or a carton label has
no history of its own — it is a pointer to a garment or a box that a receipt put
somewhere — so if its GRN is not posted there is nothing for it to point at, and
`unpost_grn` already deletes both on that reasoning. One still naming an unposted
GRN is a row that escaped a cleanup, not a record worth preserving.

WHAT COUNTS AS "the right number of pieces"
-------------------------------------------
Two different questions, deliberately answered against two different figures:

* **Integrity** compares live piece codes against the quantity this SKU has
  actually *received* from GRNs (`expected_units`). Piece codes are minted at
  receipt and nothing consumes them yet — dispatching by unit is a later step — so
  a garment that has been sent out still has its identity. Comparing against
  current stock would flag every dispatch as corruption.
* **Printing** compares them against **stock on hand**, because that is the
  physical question: you cannot tag garments you do not have. Ten in stock and a
  hundred codes is the failure this module exists for; ten codes and seven in
  stock means three have already gone out wearing their labels.
"""
from sqlalchemy import func

from .. import models

#: movement rows a GRN writes — the inwards and the reversals of earlier unposts
GRN_REF_TYPES = ("purchase", "purchase_unpost")

QTY_TOLERANCE = 0.001

POSTED, UNPOSTED, ORPHAN = "posted", "unposted", "orphan"


def _round(n):
    return round(float(n or 0), 3)


# ---------------------------------------------------------------------------
#  Provenance
# ---------------------------------------------------------------------------
def _purchase_status(db):
    """{purchase_id: status} — one query, so nothing below does it per row."""
    return {pid: st for pid, st in db.query(models.Purchase.id, models.Purchase.status)}


def _in_scope(q, col, scope):
    """Restrict a per-product read to `scope` (ids, or a select of ids); None is
    every product."""
    return q if scope is None else q.filter(col.in_(scope))


def _product_links(db, scope=None):
    """{product_id: {purchase_id, …}} from GRN lines, breakdown rows and movements.

    All three, because a product can be reachable by any of them: a plain line
    holds `product_id`, a broken-down bundle holds it on the variant instead, and
    the ledger records it even when a later edit clears both."""
    links = {}
    def add(pid, purchase_id):
        if pid:
            links.setdefault(pid, set()).add(purchase_id)

    # DISTINCT: the result is a set per product, and a SKU received on one GRN
    # as fifty ledger rows needs that pair sent back once, not fifty times
    for pid, purchase_id in _in_scope(db.query(
            models.PurchaseLine.product_id, models.PurchaseLine.purchase_id),
            models.PurchaseLine.product_id, scope).distinct():
        add(pid, purchase_id)
    for pid, purchase_id in _in_scope(db.query(
            models.PurchaseLineSplit.product_id, models.PurchaseLine.purchase_id).join(
            models.PurchaseLine, models.PurchaseLineSplit.line_id == models.PurchaseLine.id),
            models.PurchaseLineSplit.product_id, scope).distinct():
        add(pid, purchase_id)
    for pid, ref_id in _in_scope(db.query(
            models.StockMovement.product_id, models.StockMovement.ref_id).filter(
            models.StockMovement.ref_type.in_(GRN_REF_TYPES)),
            models.StockMovement.product_id, scope).distinct():
        add(pid, ref_id)
    return links


def _movement_count(db, scope=None):
    """{product_id: how many ledger rows} — a product with movements has history
    of its own and is never treated as debris, whatever created it.

    Counted by the database: the ledger is a million rows and only the count per
    product is wanted."""
    M = models.StockMovement
    return {pid: int(n) for pid, n in
            _in_scope(db.query(M.product_id, func.count(M.id)), M.product_id, scope)
            .group_by(M.product_id)}


def _received_qty(db, scope=None):
    """{product_id: net quantity received from GRNs}.

    Inwards less the reversals of any unpost — i.e. what the receipts still say
    this SKU took in, which is the number of piece identities that should exist.
    Outward, return and adjustment rows are deliberately not counted: they move
    stock, they do not un-receive it."""
    M = models.StockMovement
    rows = (_in_scope(db.query(M.product_id, func.sum(func.coalesce(M.qty_delta, 0)))
                        .filter(M.ref_type.in_(GRN_REF_TYPES)), M.product_id, scope)
            .group_by(M.product_id))
    return {k: _round(v) for k, v in rows}


def _state(purchase_ids, statuses, has_history):
    """Which of the three states a record's provenance puts it in."""
    live = [p for p in purchase_ids if statuses.get(p) == "posted"]
    if live:
        return POSTED
    known = [p for p in purchase_ids if p in statuses]
    if known or has_history:
        return UNPOSTED
    return ORPHAN


class Context:
    """Everything the checks need, gathered in a handful of queries so a scan of
    the whole inventory stays a handful of queries."""

    def __init__(self, db, product_ids=None):
        """`product_ids` (a list of ids, or a SELECT of them) limits what is read
        to those products — a screen judging a few thousand products need not read
        the provenance of every product ever received. A scoped context must only
        be asked about products inside its scope: any other reads as having no
        receipt, no history and no codes."""
        self.db = db
        self.scope = product_ids
        self.statuses = _purchase_status(db)
        self.links = _product_links(db, product_ids)
        self.history = _movement_count(db, product_ids)
        self.received = _received_qty(db, product_ids)
        self._adopted = {}
        self._units = None          # {product_id: [unit rows by seq]} once preloaded
        self._countable = {}        # uom → countable, asked once per pass

    def preload_units(self, product_ids=None):
        """Read every piece code once, for a pass over the whole inventory.

        Only the four columns the checks read, as plain rows, ordered as
        `_adopted_ids` needs them. Without this a whole-inventory scan asked for
        each product's codes separately — hundreds of thousands of queries on a
        real catalogue — and loaded every code as a full ORM object besides.

        `product_ids` limits the read to those products — a list screen showing
        a few thousand items has no use for millions of codes. Only those
        products may then be asked about: any other would read as having none."""
        U = models.ProductUnit
        by = {}
        q = self.db.query(U.id, U.product_id, U.purchase_id, U.seq)
        if product_ids is not None:
            ids = sorted({int(i) for i in product_ids})
            if not ids:
                self._units = by
                return
            q = q.filter(U.product_id.in_(ids))
        for row in q.order_by(U.product_id, U.seq):
            by.setdefault(row.product_id, []).append(row)
        self._units = by

    def units_for(self, product_id):
        """This product's piece codes, ordered by seq."""
        if self._units is not None:
            return self._units.get(product_id, [])
        return self.db.query(models.ProductUnit).filter(
            models.ProductUnit.product_id == product_id).order_by(
            models.ProductUnit.seq).all()

    def can_serialise(self, uom, qty):
        """units.can_serialise, with the unit-type lookup asked once per unit.

        The lookup reads the whole unit-type master, and a scan asks it once per
        product. The fast form (no db) is used only where it gives the same
        answer — a unit the built-in list and the master both call countable;
        anything else takes the exact, database-backed path."""
        from . import units as unit_svc
        key = (uom or "PCS").strip().upper()
        if key not in self._countable:
            self._countable[key] = unit_svc.is_countable(key, self.db)
        if self._countable[key] and key in unit_svc.COUNTABLE_UOM:
            return unit_svc.can_serialise(uom, qty)
        return unit_svc.can_serialise(uom, qty, self.db)

    def product_state(self, product):
        return _state(self.links.get(product.id, set()), self.statuses,
                      bool(self.history.get(product.id)))

    def _adopted_ids(self, product_id):
        """Piece codes with no receipt recorded against them that are nonetheless
        real, identified by counting rather than by their own field.

        Some codes legitimately carry no `purchase_id`: `units.backfill` minted
        them for stock received before receipts were tracked per piece. Those
        cannot be told apart from debris by inspection — both are simply a null
        column — so they are told apart by *arithmetic*. A SKU that received 10 and
        has 4 codes naming their receipt is short 6, and the 6 oldest nameless
        codes are covering that shortfall; they are real, and deleting them would
        strip labels off garments that exist. Anything past the shortfall is
        surplus no receipt can account for, and that is the debris.

        Ordered by seq so the ones adopted are the earliest — the same ones a
        human reading the list would take to be the original set."""
        if product_id in self._adopted:
            return self._adopted[product_id]
        rows = self.units_for(product_id)
        named_live = sum(1 for u in rows if self.statuses.get(u.purchase_id) == "posted")
        shortfall = int(round(self.received.get(product_id, 0.0))) - named_live
        nameless = [u for u in rows if u.purchase_id is None]
        keep = {u.id for u in nameless[:max(0, shortfall)]}
        self._adopted[product_id] = keep
        return keep

    def unit_state(self, unit):
        """A piece code is live only if a posted receipt accounts for it.

        There is no middle state here, and that is the difference from a product.
        A product kept after an unpost is worth keeping because it carries things
        the GRN never created — hand-recorded detailing, a dispatch, a ledger. A
        piece code carries none of that. It is a pointer to one garment, and if the
        receipt that brought that garment in is not posted, the garment is not on
        the floor. `unpost_grn` deletes these for exactly that reason, so a code
        still naming an unposted GRN is a row that escaped a cleanup — debris that
        happens to name something."""
        if self.statuses.get(unit.purchase_id) == "posted":
            return POSTED
        if unit.purchase_id is None:
            return POSTED if unit.id in self._adopted_ids(unit.product_id) else ORPHAN
        return ORPHAN

    def bundle_state(self, bundle):
        """Same reasoning as a piece code: a carton label describes goods a receipt
        put on a rack. No posted receipt, no goods, no carton."""
        return POSTED if self.statuses.get(bundle.purchase_id) == "posted" else ORPHAN


# ---------------------------------------------------------------------------
#  One product
# ---------------------------------------------------------------------------
def product_report(db, product, ctx=None):
    """Provenance, piece-code arithmetic and print eligibility for one product."""
    from . import units as unit_svc
    # alone, only this product's provenance is needed — not every product's
    ctx = ctx or Context(db, product_ids=[product.id])
    state = ctx.product_state(product)
    rows = ctx.units_for(product.id)
    live = [u for u in rows if ctx.unit_state(u) == POSTED]
    orphaned = [u for u in rows if ctx.unit_state(u) == ORPHAN]
    stock = _round(product.stock_qty)
    expected = ctx.received.get(product.id, 0.0)
    serialisable, why = ctx.can_serialise(product.uom, expected or stock)

    d = {
        "product_id": product.id, "sku": product.sku,
        "state": state, "in_stock_scope": state == POSTED,
        "stock_qty": stock, "expected_units": _round(expected),
        "unit_count": len(rows), "live_units": len(live),
        "orphan_units": len(orphaned),
        "serialisable": serialisable,
        "units_match": True, "issues": [],
    }
    if serialisable:
        d["units_match"] = abs(len(live) - expected) <= QTY_TOLERANCE
    if state == ORPHAN:
        d["issues"].append("no posted GRN, no stock movement — this record is not stock")
    elif state == UNPOSTED:
        d["issues"].append("its GRN has been unposted — kept for its history, not counted as stock")
    if orphaned:
        d["issues"].append(f"{len(orphaned)} piece code(s) belong to no posted GRN")
    if serialisable and not d["units_match"]:
        d["issues"].append(f"{len(live)} live piece code(s) against {_round(expected):g} received")
    d["can_print"], d["print_block"] = _print_eligibility(d)
    return d


def _print_eligibility(d):
    """(ok, reason) for printing this SKU's labels.

    Checked against **stock on hand**, not against what was received: a label is
    stuck on a garment, so the question is how many garments are here now."""
    if d["state"] != POSTED:
        return False, ("This product does not belong to a posted GRN, so it is not stock. "
                       "Run Inventory Repair, or post its GRN first.")
    if d["stock_qty"] <= 0:
        return False, (f"Stock is {d['stock_qty']:g} — there are no garments to label.")
    if not d["serialisable"]:
        return True, ""           # measured goods print a SKU label, not piece labels
    if abs(d["live_units"] - d["stock_qty"]) > QTY_TOLERANCE:
        return False, (f"Product quantity ({d['stock_qty']:g}) does not match the number of "
                       f"QR records ({d['live_units']}). Please resolve the inventory "
                       f"mismatch before printing labels.")
    return True, ""


def can_print(db, product, ctx=None):
    d = product_report(db, product, ctx)
    return d["can_print"], d["print_block"]


# ---------------------------------------------------------------------------
#  Whole-inventory scan
# ---------------------------------------------------------------------------
def scan(db):
    """Everything that fails a check, with enough detail to act on it.

    Read-only — this is what the Inventory Repair screen shows *before* anyone
    agrees to delete anything."""
    ctx = Context(db)
    ctx.preload_units()
    products = db.query(models.Product).order_by(models.Product.id).all()

    orphan_products, unposted_products, unit_mismatches = [], [], []
    for p in products:
        state = ctx.product_state(p)
        row = {"product_id": p.id, "sku": p.sku, "description": p.description,
               "stock_qty": _round(p.stock_qty), "detailed": bool(p.detailed)}
        if state == ORPHAN:
            orphan_products.append(row)
        elif state == UNPOSTED:
            unposted_products.append(row)
        rep = product_report(db, p, ctx)
        if rep["serialisable"] and not rep["units_match"]:
            unit_mismatches.append({**row, "live_units": rep["live_units"],
                                    "expected_units": rep["expected_units"],
                                    "orphan_units": rep["orphan_units"]})

    product_ids = {p.id for p in products}
    orphan_units = []
    U = models.ProductUnit
    for u in db.query(U.id, U.code, U.product_id, U.purchase_id).order_by(U.id):
        if u.product_id not in product_ids:
            orphan_units.append({"id": u.id, "code": u.code,
                                 "why": "its product no longer exists"})
        elif ctx.unit_state(u) == ORPHAN:
            orphan_units.append({"id": u.id, "code": u.code,
                                 "why": "no posted GRN behind it"})

    line_ids = {i for i, in db.query(models.PurchaseLine.id)}
    orphan_bundles = []
    for b in db.query(models.Bundle).order_by(models.Bundle.id).all():
        if ctx.bundle_state(b) == ORPHAN:
            orphan_bundles.append({"id": b.id, "code": b.code, "qty": b.qty,
                                   "why": "no posted GRN behind it"})
        elif b.line_id not in line_ids:
            orphan_bundles.append({"id": b.id, "code": b.code, "qty": b.qty,
                                   "why": "its GRN line no longer exists"})

    removable = len(orphan_products) + len(orphan_units) + len(orphan_bundles)
    return {
        "clean": removable == 0 and not unit_mismatches,
        "orphan_products": orphan_products,
        "orphan_units": orphan_units,
        "orphan_bundles": orphan_bundles,
        "unit_mismatches": unit_mismatches,
        # kept on purpose — listed so it is visible, never offered for deletion
        "unposted_products": unposted_products,
        "counts": {
            "products": len(products),
            "orphan_products": len(orphan_products),
            "orphan_units": len(orphan_units),
            "orphan_bundles": len(orphan_bundles),
            "unit_mismatches": len(unit_mismatches),
            "unposted_products": len(unposted_products),
            "removable": removable,
        },
    }


def repair(db, dry_run=False):
    """Delete the debris a scan found. Returns what went (or would go).

    Only ORPHAN records — nothing traceable to a GRN, posted or unposted, and
    nothing carrying a ledger entry of its own. A product kept at zero stock after
    an unpost is reported but never touched: it holds detailing the warehouse
    recorded by hand, and no cleanup is worth silently destroying that."""
    report = scan(db)
    removed = {"products": [r["sku"] for r in report["orphan_products"]],
               "units": [r["code"] for r in report["orphan_units"]],
               "bundles": [r["code"] for r in report["orphan_bundles"]]}
    if dry_run:
        return {"ok": True, "dry_run": True, "would_remove": removed,
                "counts": report["counts"]}

    # units first, then bundles, then the products themselves — a product delete
    # cascades its remaining units, but doing it in this order keeps the counts
    # honest about what was removed for which reason
    for r in report["orphan_units"]:
        u = db.get(models.ProductUnit, r["id"])
        if u:
            db.delete(u)
    for r in report["orphan_bundles"]:
        b = db.get(models.Bundle, r["id"])
        if b:
            db.delete(b)
    db.flush()
    for r in report["orphan_products"]:
        p = db.get(models.Product, r["product_id"])
        if p:
            db.delete(p)
    db.flush()
    return {"ok": True, "dry_run": False, "removed": removed,
            "counts": {k: len(v) for k, v in removed.items()},
            "kept_unposted": len(report["unposted_products"])}


def stock_scope_ids(db, ctx=None):
    """The product ids that count as stock — everything else is hidden from the
    Inventory screen, the valuation and the label printer."""
    ctx = ctx or Context(db)
    return {p.id for p in db.query(models.Product).all()
            if ctx.product_state(p) == POSTED}
