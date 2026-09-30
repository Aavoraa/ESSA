"""The retail shop (POS) module, served from inside this API at /pos.

The shop — "Textile Retail Shop" beside this backend — is a finished Flask app
with its own database, its own login and its own screens. It is not rewritten
into React here. It is mounted, as WSGI, under this same server, and the POS
button in the warehouse UI opens it in a frame.

Mounting rather than running it beside us on a second port is what makes the
button work at all: a Flask session cookie set by http://localhost:8100 is a
third-party cookie inside a frame served from :8000, and browsers drop it — the
POS would ask for a login it could never accept. Same origin, one port, one
process; the cookie is first-party and the whole thing behaves like a page of
this app.

The one wrinkle is that both codebases call their package `app`. Python has
room for exactly one `app` in sys.modules, and ours is already there. So the
shop is imported with ours lifted out of the way and put back afterwards (see
_load below) — which works because every `from app…` in the shop runs while its
package is the one installed. Two of them used to run later, at request time,
long after ours was back; those two imports were hoisted to the top of their
files (app/utils.py, app/routes/pos.py) so nothing reaches for `app` once the
swap is over.
"""

import importlib
import os
import sys
from pathlib import Path

# …/essa-intake/backend/app/pos_mount.py → …/essa-intake/Textile Retail Shop
POS_DIR = Path(__file__).resolve().parents[2] / "Textile Retail Shop"

_OURS = ("app", "config")          # module names the shop would otherwise shadow


def _is_ours(name: str) -> bool:
    return name in _OURS or name.startswith("app.")


def _seed_if_empty(pkg) -> None:
    """A shop with no users has no way in — nobody can log in to create the first
    one. Ship the same starting data `python run.py init` would have made."""
    from app.models import User          # the shop's models, mid-swap
    if User.query.first() is not None:
        return
    from app.seed import seed_all
    seed_all()


POS_SCHEMA = "shop"

#: The shop's report catalogue module (its app/retail_reports), kept from the
#: load below. Its `app` package is only importable during that swap, so it is
#: captured then — the warehouse's Reports screen lists these reports.
_retail_reports = None


def store_report_catalogue():
    """The shop's reports as plain data, in its own order: key, label, group,
    group label, whether it takes dates, and why it cannot run (or None).
    None when the shop is not loaded here."""
    rr = _retail_reports
    if rr is None:
        return None
    return [{"key": k, "label": s["label"], "group": s["group"],
             "group_label": s["group_label"], "dated": bool(s.get("dated")),
             "unavailable": s.get("unavailable") or None}
            for k, s in rr.REPORTS.items()]


def _single_store_name():
    """The store's name, when this company has exactly one — else None.

    The shop shows one `SHOP_NAME` for the whole mount, so it can only be
    answered honestly when there IS one store. With several, naming one of them
    on every store's login screen would be worse than the generic word: it would
    be wrong on all but one, and wrong in a way that reads as authoritative.
    Which entity a bill is actually raised as is decided at the till, per
    counter, by the shop's own places.py.

    Never raises. A store name is a nicety; a database that cannot be read at
    import time must not stop the shop mounting.
    """
    try:
        from .database import SessionLocal
        from . import models
        db = SessionLocal()
        try:
            rows = db.query(models.Store.name).filter(
                models.Store.active.is_(True)).limit(2).all()
            return rows[0][0] if len(rows) == 1 else None
        finally:
            db.close()
    except Exception:                            # noqa: BLE001 — a name, not a dependency
        return None


def _isolate_shop_schema() -> None:
    """Keep the shop's tables out of the warehouse's, on Postgres.

    The two codebases were written against separate SQLite FILES, and five of
    their table names are the same: categories, floors, products,
    stock_movements and users. A file each made that harmless. One Postgres
    database does not —
    whichever app runs `create_all` first wins the name, and the other then
    queries a table with its own name and the wrong columns. The symptom is a
    column that "does not exist" on a table that plainly does.

    `users` is the one that matters most: the shop's logins and the warehouse's
    accounts are different tables with the same name, and merging them would be
    a security problem rather than an error message.

    A Postgres schema per app is the standard separation. What matters is HOW the
    shop is told about it, and the first attempt got that wrong: it appended
    `options=-csearch_path=shop` to the connection URL, which puts the schema in
    the SESSION. That works on a direct connection and is silently dropped by a
    transaction-mode pooler — which is what the deployment connects through
    (Supabase's pooler, port 6543). The option went nowhere, every unqualified
    name resolved in `public`, and the shop read the warehouse's `categories`.

    So the schema is named on the shop's METADATA instead, through
    SHOP_DB_SCHEMA — it lands in the statement (`FROM shop.categories`) and needs
    nothing from the connection, which is what makes it survive any pooler. See
    the shop's app/__init__.

    On SQLite this does nothing at all, because two files were never the problem.
    """
    from .config import DATABASE_URL     # the same variable the warehouse chose
    url = (DATABASE_URL or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        return

    url = url.replace("postgres://", "postgresql://", 1)
    from sqlalchemy import create_engine, text
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{POS_SCHEMA}"'))
    finally:
        engine.dispose()

    # The shop reads the same candidates the warehouse does, so it will find this
    # on its own — but it is set here as well so that the URL the shop connects
    # with is provably the one the warehouse just created the schema in, rather
    # than a second variable that happens to be lying around.
    os.environ["DATABASE_URL"] = url
    os.environ["SHOP_DB_SCHEMA"] = POS_SCHEMA


def load_pos_app():
    """Import and return the shop's Flask application, or raise.

    Raises FileNotFoundError if the shop folder is not beside this project, and
    ImportError if its dependencies (Flask and friends) are not installed in
    this backend's environment — both are told to the user as a POS screen that
    explains itself rather than as a 500.
    """
    if not POS_DIR.is_dir():
        raise FileNotFoundError(f"POS module not found at {POS_DIR}")

    # Read BEFORE the package swap below: after it, `app` is the shop's package
    # and `from .models import Store` would reach into the wrong one. The same
    # trap app/places.py documents about its own hoisted imports.
    store_name = _single_store_name()

    _isolate_shop_schema()

    ours = {k: v for k, v in sys.modules.items() if _is_ours(k)}
    for name in ours:
        del sys.modules[name]
    sys.path.insert(0, str(POS_DIR))
    try:
        pkg = importlib.import_module("app")        # the shop's package
        flask_app = pkg.create_app()
        global _retail_reports
        _retail_reports = importlib.import_module("app.retail_reports")   # mid-swap: the shop's
        # The shop's own name, taken from the STORE the warehouse created rather
        # than from the shop's config default. Set before anything renders, so
        # the login screen greets people with the name they gave the place
        # instead of the brand that happened to be in the file.
        #
        # Only when the answer is unambiguous. `SHOP_NAME` is one value for the
        # whole mount, and a company with several stores has no single right
        # answer for it — naming one of them on every store's login screen would
        # be worse than the generic word. Per-store billing identity is settled
        # properly at the till, by the shop's own places.py.
        if store_name:
            flask_app.config["SHOP_NAME"] = store_name
        with flask_app.app_context():
            pkg.db.create_all()                     # first run: build the schema
            # The shop's categories are the warehouse master's, and its catalogue
            # is the warehouse's items — both kept in step on every start, and
            # both before the seed so a fresh shop seeds against them.
            from app.master_categories import sync_master_categories
            from app.warehouse_items import sync_warehouse_items
            from app.places import sync_locations
            from app.transfers import sync_transfers
            sync_master_categories()
            # The warehouse's state as of this sync, recorded so the shop's first
            # request does not run the very same full sync again through
            # sync_if_stale (its "last seen" stamp starts empty). Taken BEFORE the
            # sync, so a change made while it runs still triggers the next one.
            from app import warehouse_items as _wi
            seen = _wi._warehouse_signature()
            sync_warehouse_items()
            _wi._last_signature = seen
            # The branches this warehouse dispatches to are the branches the till
            # sells from, so the shop reads that list rather than keeping a second
            # one. See the shop's app/places.
            sync_locations()
            # Stock the warehouse dispatched to a branch becomes that branch's
            # stock. Last, because it needs the places and the items to exist.
            sync_transfers()
            _seed_if_empty(pkg)
        return flask_app
    finally:
        for name in [k for k in list(sys.modules) if _is_ours(k)]:
            del sys.modules[name]
        sys.modules.update(ours)                    # ours is `app` again
        try:
            sys.path.remove(str(POS_DIR))
        except ValueError:
            pass
