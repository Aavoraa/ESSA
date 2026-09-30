"""65 · Build the shop (POS) side with the shop's OWN code.

Exactly what backend/app/pos_mount.load_pos_app does when /pos is first opened —
the `shop` schema on Postgres, create_all, then the shop reading the warehouse:

  * sync_master_categories  — the category master
  * sync_warehouse_items    — every warehouse product becomes a shop product
                              (linked by products.warehouse_id, with its QR)
  * sync_locations          — stores → locations, floors → floors, tills → counters

Two parts of that mount are deliberately NOT run here, and step 70 does their
job with the legacy data instead:

  * sync_transfers — it would take every migrated dispatch into shop stock one
    ORM row at a time; step 70 writes the same TransferReceipt rows in bulk and
    then sets each store's stock to the legacy closing figure.
  * _seed_if_empty — it creates demo users/products/customers when the shop
    has no users. Step 70 creates the real (legacy) users, so it never fires.
"""
import os
import sys
from pathlib import Path

POS_DIR = Path(__file__).resolve().parents[3] / "Textile Retail Shop"
POS_SCHEMA = "shop"


def main():
    url = os.environ["ESSA_DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    if not url.startswith("postgresql://"):
        sys.exit("the migration targets Postgres; ESSA_DATABASE_URL must be a postgresql:// URL")

    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{POS_SCHEMA}"'))
    eng.dispose()
    # pos_mount._isolate_shop_schema sets exactly these two
    os.environ["DATABASE_URL"] = url
    os.environ["SHOP_DB_SCHEMA"] = POS_SCHEMA

    # the shop's package is also called `app`: it must be the one found
    sys.path = [p for p in sys.path if Path(p or ".").resolve() != POS_DIR.parent / "backend"]
    sys.path.insert(0, str(POS_DIR))
    for name in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
        del sys.modules[name]

    import app as shop
    flask_app = shop.create_app()
    with flask_app.app_context():
        shop.db.create_all()
        from app.dbpatch import apply_all
        apply_all()
        from app.master_categories import sync_master_categories
        from app.warehouse_items import sync_warehouse_items
        from app.places import sync_locations
        print("    categories:", sync_master_categories())
        print("    products:  ", sync_warehouse_items())
        print("    places:    ", sync_locations())


if __name__ == "__main__":
    main()
