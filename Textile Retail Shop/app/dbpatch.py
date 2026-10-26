"""Adding a column to a database that predates it.

The shop carries no migration tool, and adopting one for the handful of columns
that postdate the original schema would be heavier than the problem. A
single-column ALTER is cheap and standard, so each module that introduces a
column asks for it here at startup and moves on.

Postgres is included, and it did not use to be. The reasoning then was that on
another engine "the schema is someone else's job" — true when the shop's only
Postgres would have been somebody else's server. It is not true now: mounted
inside the warehouse the shop creates and owns its own schema (see
backend/app/pos_mount), and `create_all` builds only missing TABLES. A column
added to a model after that schema exists is simply absent, and the first query
that mentions it fails — which is exactly the class of fault this file exists to
prevent. The warehouse solves the same problem the same way, in
main._add_missing_columns.
"""
from sqlalchemy import inspect, text

from app import db


def table_columns(table):
    """The column names of `table`, or an empty set if it doesn't exist yet.

    Asked through the inspector rather than with a PRAGMA, so the answer comes
    from whichever engine is connected — and, on Postgres, from the schema the
    shop's metadata is bound to rather than from whatever `public` happens to
    hold under the same name. The warehouse has a `products` table too; reading
    its columns here and concluding nothing needs adding is the precise failure
    this whole schema separation exists to avoid.
    """
    try:
        cols = inspect(db.engine).get_columns(table, schema=db.metadata.schema)
    except Exception:                      # noqa: BLE001 — no such table yet
        return set()
    return {c["name"] for c in cols}


def _qualified(table):
    """`shop.invoices` where there is a schema, `invoices` where there is not."""
    return f"{db.metadata.schema}.{table}" if db.metadata.schema else table


def ensure_column(table, column, ddl_type):
    """Add `column` to `table` if it's missing. True if it was added.

    No-op on a table that doesn't exist — create_all is what builds those, and it
    runs before any of this.
    """
    cols = table_columns(table)
    if not cols or column in cols:
        return False
    db.session.execute(
        text(f"ALTER TABLE {_qualified(table)} ADD COLUMN {column} {ddl_type}"))
    db.session.commit()
    return True


# Every column that postdates the original schema, patched as one step.
COLUMNS = [
    ("categories", "section", "VARCHAR(16)"),      # app/master_categories.py
    ("products", "warehouse_id", "INTEGER"),       # app/warehouse_items.py
    ("products", "warehouse_qr", "TEXT"),          # app/warehouse_items.py
    ("products", "mrp", "FLOAT"),                  # the warehouse attribute tuple,
    ("products", "product_type", "VARCHAR(64)"),   # copied so the shop can show a
    ("products", "pattern", "VARCHAR(64)"),        # product in full on its own
    ("products", "fit", "VARCHAR(64)"),
    ("products", "design_no", "VARCHAR(64)"),
    ("invoices", "staff_id", "INTEGER"),           # who served the sale
    ("invoice_items", "staff_id", "INTEGER"),      # …and who sold each line
    ("invoice_items", "list_price", "FLOAT"),      # price before the line's discount
    # Sales promotion schemes — see app/schemes.py.
    ("invoice_items", "scheme_id", "INTEGER"),
    ("invoice_items", "scheme_pct", "FLOAT"),
    ("invoices", "agreement_code", "VARCHAR(64)"),
    ("invoices", "scheme_discount", "FLOAT"),
    ("products", "brand", "VARCHAR(64)"),
    ("products", "style", "VARCHAR(64)"),
    ("products", "sleeve", "VARCHAR(64)"),
    ("products", "supplier", "VARCHAR(128)"),
    ("products", "purchase_invoice", "VARCHAR(64)"),
    ("products", "purchase_date", "DATE"),
    ("products", "marker", "VARCHAR(64)"),
    ("sales_schemes", "mode", "VARCHAR(16)"),
    ("sales_scheme_benefits", "flat", "BOOLEAN"),
    ("sales_scheme_benefits", "item_code", "VARCHAR(64)"),
    # Which entity billed it, from where, and at which till — see app/places.py.
    # Nullable: every invoice raised before there were counters has no answer,
    # and inventing one would be worse than leaving it blank.
    ("invoices", "company_id", "INTEGER"),
    ("invoices", "location_id", "INTEGER"),
    ("invoices", "counter_id", "INTEGER"),
    # What a promotion did to a bill line — see app/promotions.py. The promotion
    # TABLES are new and create_all builds them; these four sit on a table that
    # already existed, which is exactly what this file is for.
    ("invoice_items", "promo_application_id", "INTEGER"),
    ("invoice_items", "promo_role", "VARCHAR(16)"),
    ("invoice_items", "promo_qty", "FLOAT"),
    ("invoice_items", "promo_value", "FLOAT"),
    # Free goods kept after the purchase that earned them was returned.
    ("credit_notes", "promo_clawback", "FLOAT"),
    # Which storey a till stands on, and how a bill's number was built — see
    # app/billing_numbers.py. Nullable throughout: every till and every bill that
    # predates floors has no answer, and the plain INV- series they were numbered
    # on is still a series.
    ("counters", "floor_id", "INTEGER"),
    # Which warehouse row a mirrored floor or till came from, and whether the
    # shop made it itself — see app/places.sync_places. On the `floors` table as
    # well as `counters` because a shop that ran an earlier build already has
    # `floors` without them, and create_all never revisits a table it has made.
    ("counters", "wh_id", "INTEGER"),
    # Which storey an item is held on — where you go to find it. See
    # models.Product.floor_id; it is a place, not a quantity.
    ("products", "floor_id", "INTEGER"),
    ("floors", "wh_id", "INTEGER"),
    ("floors", "local", "BOOLEAN"),
    ("invoices", "floor_id", "INTEGER"),
    ("invoices", "fin_year", "VARCHAR(8)"),
    ("invoices", "bill_prefix", "VARCHAR(8)"),
    ("invoices", "bill_seq", "INTEGER"),
    # Birthday and anniversary wishes — see the customer form.
    ("customers", "dob", "DATE"),
    ("customers", "anniversary", "DATE"),
    # Bill cancellation — see app/cancellation.py. The bill is marked, not removed.
    ("invoices", "cancelled_at", "TIMESTAMP"),
    ("invoices", "cancelled_by_id", "INTEGER"),
    ("invoices", "cancel_reason", "VARCHAR(256)"),
    # The coupon's share of a bill's discount — see app/vouchers.py.
    ("invoices", "coupon_discount", "FLOAT"),
    # Who approved a bill's discounts — the discount card's E.Code.
    ("invoices", "discount_by_id", "INTEGER"),
    # Which till raised a credit note, so a cash refund comes out of its drawer.
    ("credit_notes", "counter_id", "INTEGER"),
]


_applied = False


def apply_all(force=False):
    """Patch the whole list before anything queries the models that declare it.

    All of it, not just the table a caller cares about: the models are a graph,
    and a query on one loads another. Syncing categories walks `Category.products`
    to see what may be pruned, which selects every products column SQLAlchemy
    knows about — so an unpatched `products` breaks a categories-only sync. Doing
    them together removes the ordering trap rather than documenting it.

    Runs its PRAGMAs once per process — it is called from before_request, and a
    schema does not change under a running shop. `force` re-checks anyway.
    """
    global _applied
    if _applied and not force:
        return []
    added = [f"{t}.{c}" for t, c, ddl in COLUMNS if ensure_column(t, c, ddl)]
    _applied = True
    return added
