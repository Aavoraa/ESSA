"""Changing what things sell for — one item, or a whole category at once.

    pick the products → choose what to do to them → SEE IT FIRST → apply

The preview is not a courtesy. A bulk price change is the easiest way in this
whole application to do a great deal of damage very quickly: "20% off" applied
to the wrong filter reprices ten thousand garments, and the only thing standing
between that and a shop selling stock below cost is somebody being shown the
before and after while they can still say no. So `preview` and `apply` run the
SAME calculation over the SAME selection, and the screen shows what apply will
do rather than a description of it.

WHAT IS AND IS NOT A PRICE HERE.

    mrp                the printed maximum. What the label says.
    sale_price         what the shop actually charges. What the till bills.
    sale_discount_pct  the lever between them, in percent.

    avg_cost           NOT changeable. It is what the goods cost, worked out by
                       weighted average from the GRNs that brought them in. Every
                       margin and every valuation in the app reads it, and typing
                       over it would turn arithmetic into an opinion.

TWO THINGS ARE REFUSED AND ONE IS WARNED ABOUT. A negative price is refused —
there is no such thing. A revert that would discard a later change is refused,
because putting back last month's price after somebody repriced yesterday would
silently undo their work. Selling ABOVE MRP is only warned about: it is illegal
to do, but MRP is blank on plenty of items and refusing would block honest
repricing on all of them. The warning names the products.
"""
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models
from ..models import PRICE_FIELDS, PRICE_OPERATIONS


class PricingError(Exception):
    """Something the price changer will not do, with the reason."""


# --------------------------------------------------------------------------
# picking the products
# --------------------------------------------------------------------------

#: The attribute columns a bulk change can be aimed with. Everything a person
#: says out loud when they describe a price change — "all the ladies chudithars",
#: "everything from this supplier", "the blue ones in medium".
#: No warehouse among them, deliberately: a product is company-wide and carries
#: ONE price. Its stock is split across buildings, its price is not, and offering
#: to reprice "the Coimbatore ones" would be offering something the data cannot
#: mean.
FILTERS = ("category", "category_section", "brand", "supplier_id",
           "color", "size", "material", "pattern", "fit", "product_type",
           "design_no", "style")


def find(db: Session, filters=None, text="", limit=None):
    """The products a change would land on.

    Only products that are real stock items — an archived one is not repriced,
    because it is not for sale.
    """
    q = _selection(db, filters, text)
    q = q.order_by(models.Product.description, models.Product.sku)
    return q.limit(limit).all() if limit else q.all()


def count(db: Session, filters=None, text=""):
    """How many products `find` would return with no limit — counted by the
    database rather than by loading every one of them."""
    return _selection(db, filters, text).count()


def _selection(db: Session, filters=None, text=""):
    """The filtered product query `find` and `count` share."""
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    q = db.query(models.Product)
    for field, value in filters.items():
        if field not in FILTERS:
            raise PricingError(f"“{field}” is not something a price change can "
                               f"be aimed at.")
        column = (models.Product.primary_supplier_id if field == "supplier_id"
                  else getattr(models.Product, field))
        q = q.filter(column == value)
    if text:
        like = f"%{text.strip()}%"
        q = q.filter(or_(models.Product.description.ilike(like),
                         models.Product.sku.ilike(like),
                         models.Product.barcode.ilike(like),
                         models.Product.design_no.ilike(like)))
    return q


def describe(filters=None, text="", ids=None):
    """The selection in words, for the revision's own record.

    Stored as TEXT rather than as the filter, because a saved query re-run next
    year returns a different set of products — and the one thing the record has
    to say is what was actually repriced at the time.
    """
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    bits = [f"{k.replace('_', ' ')} = {v}" for k, v in sorted(filters.items())]
    if text:
        bits.append(f"matching “{text}”")
    if ids:
        bits.append(f"{len(ids)} item(s) picked by hand")
    return "; ".join(bits) or "every product"


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def _round_to(value, step):
    """Round to the nearest `step` rupees. 487.63 at step 10 → 490.

    Retail prices are 499 and 1,290, never 487.63 — a percentage change without
    this produces figures nobody would ever print on a label.
    """
    if not step:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def new_value(product, field, operation, value, round_to=None):
    """What this product's `field` becomes, or None if it does not change.

    None also for a product the operation cannot be applied to — a percentage
    change needs something to be a percentage OF, so an item with no price yet
    is skipped rather than set to zero.
    """
    if field not in PRICE_FIELDS:
        raise PricingError(f"“{field}” is not a price this can change.")
    if operation not in PRICE_OPERATIONS:
        raise PricingError(f"“{operation}” is not something this can do.")

    current = getattr(product, field, None)
    if operation == "set":
        result = float(value)
    elif operation == "discount_off_mrp":
        # The lever between MRP and what is charged. Needs an MRP to work from;
        # without one there is nothing to discount and the item is left alone.
        if product.mrp in (None, "") or not float(product.mrp):
            return None
        result = float(product.mrp) * (1 - float(value) / 100.0)
    else:
        if current in (None, ""):
            return None                # nothing to move up or down from
        current = float(current)
        result = (current * (1 + float(value) / 100.0) if operation == "percent"
                  else current + float(value))

    # A percentage is a percentage, not a price, so rounding it to the nearest
    # ten rupees would be nonsense.
    result = (round(result, 2) if field == "sale_discount_pct"
              else _round_to(result, round_to))
    if result < 0:
        raise PricingError(f"That would make {product.sku or product.description} "
                           f"{result:g} — a price cannot be negative.")
    if field == "sale_discount_pct" and result > 100:
        raise PricingError("A discount cannot be more than 100%.")
    if current is not None and round(float(current or 0), 2) == result:
        return None                    # unchanged; not worth a row
    return result


def preview(db: Session, field, operation, value, filters=None, text="",
            ids=None, round_to=None, limit=None):
    """What applying this would do, without doing any of it.

    Returns {rows, total, changed, warnings}. `rows` is capped for the screen;
    `changed` is the real number, so a preview showing 200 lines can still say
    it is about to move 4,000 prices.
    """
    products = ([db.get(models.Product, i) for i in ids] if ids
                else find(db, filters, text))
    products = [p for p in products if p is not None]

    rows, warnings = [], []
    above_mrp = []
    for p in products:
        try:
            fresh = new_value(p, field, operation, value, round_to)
        except PricingError as exc:
            warnings.append(str(exc))
            continue
        if fresh is None:
            continue
        # Selling above the printed maximum is illegal, and worth saying out
        # loud — but MRP is blank on plenty of items, so this warns rather than
        # refuses. Refusing would block honest repricing on every item nobody
        # has recorded an MRP for.
        if field == "sale_price" and p.mrp and fresh > float(p.mrp) + 0.001:
            above_mrp.append(f"{p.sku or p.description} ({fresh:g} over MRP {float(p.mrp):g})")
        rows.append({"product_id": p.id, "sku": p.sku,
                     "description": p.description,
                     "category": p.category, "brand": p.brand,
                     "size": p.size, "color": p.color,
                     "stock_qty": p.stock_qty, "avg_cost": p.avg_cost,
                     "mrp": p.mrp, "sale_price": p.sale_price,
                     "sale_discount_pct": p.sale_discount_pct,
                     "old": getattr(p, field, None), "new": fresh})

    if above_mrp:
        warnings.append(f"{len(above_mrp)} item(s) would sell above their printed "
                        f"MRP, which is not allowed: "
                        + ", ".join(above_mrp[:5])
                        + (" …" if len(above_mrp) > 5 else ""))
    # Selling under cost is a decision, not a mistake — a clearance is exactly
    # that — so it is reported and never refused.
    under_cost = [r for r in rows if field == "sale_price" and r["avg_cost"]
                  and r["new"] < float(r["avg_cost"])]
    if under_cost:
        warnings.append(f"{len(under_cost)} item(s) would sell below what they "
                        f"cost. That is what a clearance is — but it should be "
                        f"on purpose.")

    return {"rows": rows[:limit] if limit else rows,
            "total": len(products), "changed": len(rows),
            "unchanged": len(products) - len(rows),
            "warnings": warnings}


# --------------------------------------------------------------------------
# doing it
# --------------------------------------------------------------------------

def apply(db: Session, field, operation, value, filters=None, text="", ids=None,
          round_to=None, note=None, user=None):
    """Write the change, and the record of it. Returns the PriceRevision.

    The same calculation the preview ran, over the same selection — not a second
    implementation that could disagree with what somebody was shown.
    """
    result = preview(db, field, operation, value, filters=filters, text=text,
                     ids=ids, round_to=round_to)
    if not result["rows"]:
        raise PricingError("Nothing would change — no product in that selection "
                           "moves to a different price.")

    # `is_taken` is not optional in spirit — see services/numbering. Without it
    # this is a bare counter, and a counter that has drifted low hands out a
    # number that already exists. Passing the lookup for the column the number
    # actually lands in is what makes the probe forward possible.
    from . import numbering
    taken = lambda c: db.query(models.PriceRevision).filter(       # noqa: E731
        models.PriceRevision.number == c).first() is not None
    number = numbering.next_number(db, "price_revision", is_taken=taken)

    rev = models.PriceRevision(
        number=number, field=field, operation=operation, value=float(value),
        round_to=float(round_to) if round_to else None,
        scope=describe(filters, text, ids), note=(note or "").strip() or None,
        product_count=len(result["rows"]), created_at=datetime.utcnow(),
        created_by=user)
    db.add(rev)
    db.flush()

    for row in result["rows"]:
        product = db.get(models.Product, row["product_id"])
        if product is None:
            continue
        db.add(models.PriceChange(
            revision_id=rev.id, product_id=product.id, sku=product.sku,
            description=product.description, field=field,
            old_value=row["old"], new_value=row["new"]))
        setattr(product, field, row["new"])

    db.commit()
    db.refresh(rev)
    return rev


def revert(db: Session, revision, user=None):
    """Put the prices back to what they were before this revision.

    REFUSED IF ANYTHING HAS MOVED SINCE. A later revision that touched the same
    product and the same field is somebody's more recent decision; putting this
    one back would silently discard it, and the person clicking revert would have
    no way of knowing. So the conflict is named and nothing is written.
    """
    if revision.reverted_at is not None:
        raise PricingError(f"{revision.number} was already put back on "
                           f"{revision.reverted_at.strftime('%d-%m-%Y %H:%M')}.")

    product_ids = [c.product_id for c in revision.changes]
    later = (db.query(models.PriceChange, models.PriceRevision)
             .join(models.PriceRevision,
                   models.PriceRevision.id == models.PriceChange.revision_id)
             .filter(models.PriceChange.product_id.in_(product_ids),
                     models.PriceChange.field == revision.field,
                     models.PriceRevision.id != revision.id,
                     models.PriceRevision.created_at > revision.created_at,
                     models.PriceRevision.reverted_at.is_(None))
             .all())
    if later:
        names = sorted({r.number for _c, r in later})
        raise PricingError(
            f"{revision.number} cannot be put back: "
            f"{', '.join(names[:3])}{' and others' if len(names) > 3 else ''} "
            f"changed the same prices afterwards, and reverting this would throw "
            f"that away. Reverse the later one first, or set the prices you want "
            f"directly.")

    for change in revision.changes:
        product = db.get(models.Product, change.product_id)
        if product is not None:
            setattr(product, revision.field, change.old_value)
    revision.reverted_at = datetime.utcnow()
    revision.reverted_by = user
    db.commit()
    return revision


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

def revision_out(rev, with_changes=False, limit=200):
    out = {"id": rev.id, "number": rev.number, "field": rev.field,
           "operation": rev.operation, "value": rev.value,
           "round_to": rev.round_to, "scope": rev.scope, "note": rev.note,
           "product_count": rev.product_count,
           "created_at": rev.created_at.isoformat() if rev.created_at else None,
           "created_by": rev.created_by,
           "reverted_at": rev.reverted_at.isoformat() if rev.reverted_at else None,
           "reverted_by": rev.reverted_by}
    if with_changes:
        out["changes"] = [{"product_id": c.product_id, "sku": c.sku,
                           "description": c.description,
                           "old": c.old_value, "new": c.new_value}
                          for c in rev.changes[:limit]]
        out["shown"] = min(len(rev.changes), limit)
    return out


def history_for(db: Session, product_id, limit=50):
    """Every price this product has been given, newest first.

    The question "why is this 400 when it was 500 last week" is asked of one
    item, so it is answerable from one item.
    """
    rows = (db.query(models.PriceChange, models.PriceRevision)
            .join(models.PriceRevision,
                  models.PriceRevision.id == models.PriceChange.revision_id)
            .filter(models.PriceChange.product_id == product_id)
            .order_by(models.PriceChange.id.desc()).limit(limit).all())
    return [{"number": r.number, "field": c.field, "old": c.old_value,
             "new": c.new_value, "scope": r.scope, "note": r.note,
             "at": r.created_at.isoformat() if r.created_at else None,
             "by": r.created_by,
             "reverted": r.reverted_at is not None}
            for c, r in rows]
