"""Working INSIDE one warehouse.

WHAT THIS IS FOR. Stock is split by warehouse (services/stock_locations), but
the SCREENS were not: Erode's receiving clerk opened the GRN list and saw
Karur's drafts, the invoice queue held every branch's bills, and the transport
register was one long list nobody could find their own lorry in. Splitting the
ledger made the numbers right; this makes the day's work right.

HOW THE WAREHOUSE TRAVELS. As a request header, `X-Essa-Warehouse`, set once by
the client for every call it makes (see frontend/src/api.js). Not as a parameter
threaded through fifty call sites, because the failure mode there is one screen
that forgets — and a screen that forgets does not look broken, it looks like
another warehouse's work has appeared in yours. A header cannot be forgotten by
one screen at a time.

`?warehouse_id=` is still honoured, for a link somebody pastes and for the
central dashboard, which asks about a warehouse it is not inside.

WHAT IS AND IS NOT SCOPED. Three kinds of record:

  * **Owns a warehouse** — Purchase, StockOutward, Document, LREntry. Filtered on
    their own column.
  * **Derives one** — a debit note belongs to the warehouse its GRN received
    into; a carton belongs to the receipt that made it; a product is "here" if
    this warehouse holds any of it. Filtered through the row that knows.
  * **Company-wide** — suppliers, masters, catalogues, label templates, users,
    and SUPPLIER PAYMENTS. A payment is deliberately not scoped: one cheque
    settles bills from two warehouses, and splitting the ledger by building
    would make the supplier's account impossible to reconcile.

UNASSIGNED ROWS STAY VISIBLE. Anything written before workspaces existed has no
warehouse, and it is shown inside every one rather than being hidden until
somebody claims it. An invoice that has vanished from all queues is an invoice
that goes unpaid; a duplicate on two screens is merely untidy, and it stops as
soon as it is filed.
"""
from typing import Optional

from fastapi import Request

from .. import models

#: The header the apps send on every call once somebody is inside a warehouse.
HEADER = "x-essa-warehouse"


def from_request(request: Request) -> Optional[int]:
    """Which warehouse this call is being made inside, or None for the company.

    An explicit `?warehouse_id=` beats the header: a caller that names one is
    asking about that warehouse specifically — the central dashboard does, from
    outside any of them — and the ambient context must not override the
    question that was actually asked.
    """
    for raw in (request.query_params.get("warehouse_id"),
                request.headers.get(HEADER)):
        text = str(raw or "").strip()
        if text.isdigit():
            return int(text)
    return None


def current(request: Request) -> Optional[int]:
    """FastAPI dependency form — `wid: Optional[int] = Depends(scope.current)`."""
    return from_request(request)


# ---------------------------------------------------------------------------
#  filters
# ---------------------------------------------------------------------------
def own(query, column, warehouse_id, include_unassigned=True):
    """Narrow to rows whose own warehouse column matches.

    `include_unassigned` keeps rows that have no warehouse at all — see the
    module note. Turned off only where a blank genuinely means "not here".
    """
    if not warehouse_id:
        return query
    if include_unassigned:
        return query.filter((column == warehouse_id) | (column.is_(None)))
    return query.filter(column == warehouse_id)


def documents(query, warehouse_id):
    return own(query, models.Document.warehouse_id, warehouse_id)


def lr_entries(query, warehouse_id):
    return own(query, models.LREntry.warehouse_id, warehouse_id)


def audit_sessions(query, warehouse_id):
    """Counts run in this building.

    `include_unassigned=False`, unlike almost everything else here. A count is
    physical — somebody walked THIS warehouse's racks — so a session with no
    warehouse is not "might be ours", it is a session that predates workspaces or
    was raised outside a building, and showing it inside every warehouse would
    let two counts of two different buildings appear to be one.
    """
    return own(query, models.AuditSession.warehouse_id, warehouse_id,
               include_unassigned=False)


def physical_audits(query, warehouse_id):
    """Counted stocktakes of this building. Scoped exactly as `audit_sessions`
    is, and for the same reason: somebody walked THESE racks, so a count with no
    warehouse is not "might be ours" — and one warehouse's count appearing inside
    another's screen would be measured against the wrong shelves."""
    return own(query, models.PhysicalAudit.warehouse_id, warehouse_id,
               include_unassigned=False)


def purchase_orders(query, warehouse_id):
    """Orders raised here.

    Scoped on its own column, like the GRN and the LR entry it sits in front of:
    a buyer at one branch reading another branch's order book is the same failure
    this module was written to stop, one document earlier in the chain.
    """
    return own(query, models.PurchaseOrder.warehouse_id, warehouse_id)


def purchases(query, warehouse_id):
    """GRNs received here.

    A DRAFT with no warehouse yet is included: it is a receipt somebody has
    started and not yet said where, and the warehouse it is being prepared at is
    the one whose screen it should be on.
    """
    return own(query, models.Purchase.warehouse_id, warehouse_id)


def outwards(query, warehouse_id):
    """Dispatches this warehouse is either end of.

    Both ends, because an incoming transfer is this warehouse's business too —
    somebody here has to count it in. Filtering to `from` alone is what would
    make an arriving consignment invisible at the branch expecting it.
    """
    if not warehouse_id:
        return query
    return query.filter((models.StockOutward.from_warehouse_id == warehouse_id)
                        | (models.StockOutward.to_warehouse_id == warehouse_id)
                        | (models.StockOutward.from_warehouse_id.is_(None)))


def returns(query, warehouse_id):
    """Debit notes against GRNs received here — joined, because a return has no
    warehouse of its own and inventing one could disagree with its receipt."""
    if not warehouse_id:
        return query
    return (query.join(models.Purchase,
                       models.PurchaseReturn.purchase_id == models.Purchase.id)
                 .filter((models.Purchase.warehouse_id == warehouse_id)
                         | (models.Purchase.warehouse_id.is_(None))))


def bundles(query, warehouse_id):
    """Cartons made by a receipt into this warehouse."""
    if not warehouse_id:
        return query
    return (query.join(models.Purchase,
                       models.Bundle.purchase_id == models.Purchase.id)
                 .filter((models.Purchase.warehouse_id == warehouse_id)
                         | (models.Purchase.warehouse_id.is_(None))))


def product_ids_here(db, warehouse_id, include_zero=False):
    """The products this warehouse holds — the set every stock screen narrows to.

    Returns None when there is no warehouse, meaning "no narrowing", so callers
    can pass the answer straight through without a second branch.

    `include_zero` keeps items whose balance has fallen to nothing HERE. They
    are this warehouse's items — it has stocked them, it will again — and a
    dead-stock or re-order screen that dropped them the moment they sold out
    would be hiding exactly what it exists to show.
    """
    if not warehouse_id:
        return None
    q = db.query(models.StockBalance.product_id).filter(
        models.StockBalance.warehouse_id == warehouse_id)
    if not include_zero:
        q = q.filter(models.StockBalance.qty > 0)
    return {row[0] for row in q.all()}


def products(db, query, warehouse_id, include_zero=True, as_subquery=False):
    """Narrow a Product query to what this warehouse has anything to do with.

    `as_subquery` narrows by the balances query itself (`id IN (SELECT …)`)
    rather than reading the ids out and sending them back as a literal list —
    the same products, without shipping hundreds of thousands of ids each way.
    Opt-in, for callers whose ORDER BY decides the order completely (or that
    want a set): a different plan may return rows that tie in a different order."""
    if as_subquery:
        if not warehouse_id:
            return query
        q = db.query(models.StockBalance.product_id).filter(
            models.StockBalance.warehouse_id == warehouse_id)
        if not include_zero:
            q = q.filter(models.StockBalance.qty > 0)
        # an empty subquery matches nothing — the same answer as the explicit
        # `id IS NULL` below gives a warehouse with nothing in it yet
        return query.filter(models.Product.id.in_(q.scalar_subquery()))
    ids = product_ids_here(db, warehouse_id, include_zero=include_zero)
    if ids is None:
        return query
    if not ids:
        # Nothing here yet. An empty IN () is asked for explicitly rather than
        # left unfiltered — a new warehouse showing the whole company's stock
        # would be the exact failure this module exists to prevent.
        return query.filter(models.Product.id.is_(None))
    return query.filter(models.Product.id.in_(ids))


# ---------------------------------------------------------------------------
#  stamping
# ---------------------------------------------------------------------------
def stamp(row, warehouse_id, field="warehouse_id"):
    """Put the current warehouse on a row being created, if it hasn't got one.

    Never overwrites: a caller that named a warehouse explicitly has said
    something more specific than the ambient context.
    """
    if warehouse_id and getattr(row, field, None) is None:
        setattr(row, field, warehouse_id)
    return row


def backfill(db) -> dict:
    """Nothing to move. Recorded as a deliberate no-op, not an oversight.

    Documents and LR entries written before workspaces have no warehouse, and
    they are LEFT that way: assigning them to the default warehouse would be a
    guess, and a wrong guess hides an invoice from the branch that is actually
    waiting for it. Unassigned rows appear inside every warehouse (see `own`),
    which is the safe direction to be wrong in, and they file themselves as soon
    as anyone touches them.
    """
    return {
        "documents_unassigned": db.query(models.Document).filter(
            models.Document.warehouse_id.is_(None)).count(),
        "lr_unassigned": db.query(models.LREntry).filter(
            models.LREntry.warehouse_id.is_(None)).count(),
    }
