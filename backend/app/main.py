import hmac
import os
import re
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from .database import Base, engine
from . import models  # noqa: F401  (register tables)
from .routers import (documents, suppliers, purchases, purchase_orders,
                      inventory, outward, pricing, stock_audit, physical_audit,
                      payments, returns, reports, settings, auth, masters, lr,
                      bundles, dashboard, search, master_data, labels, dead_stock,
                      notifications, voice, users, locations, catalogues,
                      command, audit)
from .extraction.engine import provider_status
from .security import auth_middleware
from .config import COMPANY_NAME, COMPANY_GSTIN, UPLOAD_DIR
from . import runtime
from .services import storage as storage_svc

# Why the schema build is allowed to fail here
# ---------------------------------------------
# On the warehouse PC this is a SQLite file that is always reachable, and a
# failure means something is badly wrong and crashing is the honest response.
# Deployed, the database is across a network and behind a connection string
# somebody typed. A raise at import time there does not produce a message
# anybody sees — the whole module fails to import, and the platform answers
# every request with an opaque FUNCTION_INVOCATION_FAILED. The reason is in a
# log the person debugging has to know to go and find.
#
# So the failure is recorded instead of raised, and /api/status reports it. The
# deployment is just as unusable either way; the difference is that this one
# says what is wrong with it.
STARTUP_ERROR = None


def _scrub(text: str) -> str:
    """Connection errors quote the URL back, and the URL carries the password.
    /api/status is reachable by anyone, so the credentials come out first."""
    return re.sub(r"://([^:/@\s]+):([^@/\s]+)@", r"://\1:***@", str(text))


def _database_status(url: str) -> dict:
    """What /api/status says about the database.

    `ok` is not simply "did it start". A serverless deployment that falls back
    to SQLite starts perfectly, answers every request, and writes to /tmp —
    which is wiped between invocations. Every GRN posted against it is lost, and
    nothing anywhere says so. That is a worse failure than not starting, because
    the warehouse would find out days later, so it is reported as NOT ok with
    the reason spelled out.
    """
    dialect = url.split("://")[0] if "://" in url else "?"
    host = _scrub(url).split("@")[-1].split("/")[0] if "@" in url else "local file"
    ephemeral = dialect.startswith("sqlite") and os.environ.get("VERCEL")
    warning = None
    if ephemeral:
        warning = ("Running on a temporary SQLite file, because no usable Postgres "
                   "connection string was found. This works, and then LOSES "
                   "EVERYTHING when the instance recycles. Set ESSA_DATABASE_URL "
                   "to a real Postgres URL — a value still containing < or > is a "
                   "template and is ignored.")
    from .config import database_url_report
    return {
        "ok": STARTUP_ERROR is None and not ephemeral,
        # The driver and host, never the password — see _scrub.
        "dialect": dialect,
        "host": host,
        "persistent": not ephemeral,
        "error": STARTUP_ERROR,
        "warning": warning,
        # What the last start had to add. Normally empty; non-empty right after
        # a deploy that introduced a column, and the place to look when a query
        # complains about a column that the models plainly declare.
        "columns_added": SCHEMA_ADDED,
        # Only shown when there is a problem: when it is working, the list of
        # variables that were not needed is noise on an endpoint the app polls.
        "checked": None if not ephemeral else database_url_report(),
    }


# The variable each host sets by itself, so the deployment can be named in the
# warning below rather than described. Only used to make the message concrete —
# the test that actually fires it is the mismatch, not this list, because a host
# that is not on it is exactly the case a VERCEL-only check already got wrong.
_PLATFORM_ENV = [
    ("VERCEL", "Vercel"), ("RENDER", "Render"),
    ("RAILWAY_ENVIRONMENT", "Railway"), ("FLY_APP_NAME", "Fly.io"),
    ("DYNO", "Heroku"), ("K_SERVICE", "Google Cloud Run"),
    ("WEBSITE_INSTANCE_ID", "Azure App Service"), ("KOYEB_APP_NAME", "Koyeb"),
    ("CONTAINER_APP_NAME", "Azure Container Apps"),
]


def _platform_name():
    return next((n for var, n in _PLATFORM_ENV if os.environ.get(var)), None)


def _storage_status(db_url: str) -> dict:
    """What /api/status says about where uploaded invoices are being PUT.

    The counterpart of the database warning above, and it was the missing half
    of it. Uploaded invoices written to a container's own filesystem work
    perfectly and then vanish: the upload succeeds, and the FIRST reading
    succeeds too because it happens in the same request while the file is still
    there — the document is confirmed at 100%. Only afterwards is it gone, and
    what surfaces is a 404 on the image and a FileNotFoundError from inside the
    vision provider. That reads as an extraction fault, which is a long way from
    the fix and the wrong place to go looking.

    What fires it is a MISMATCH, not a platform. This asked `os.environ["VERCEL"]`
    first, which was the mistake it was written to catch: the deployment that
    prompted it turned out to be Neon Postgres somewhere else entirely, so the
    check stayed silent on precisely the installation it was for.

    The durable signal is that the two halves of this app's state are being kept
    in places with different lifetimes. A warehouse PC is consistent — SQLite
    beside the uploads, both on the same disk, both surviving a reboot. A
    deployment with a managed Postgres has already decided its data must outlive
    the instance; invoice images on the instance's own filesystem contradict
    that decision, and the images are the half that loses.
    """
    local_uploads = not storage_svc.using_blob()
    remote_db = not db_url.startswith("sqlite")
    platform = _platform_name()
    # A managed database is the strong signal; a known host is the weaker one,
    # and either is enough. Blob storage settles it — that is durable wherever
    # it runs from, being someone else's HTTP API rather than this disk.
    ephemeral = local_uploads and (remote_db or platform is not None)

    warning = None
    if ephemeral:
        where = f"this {platform} instance's" if platform else "this server's"
        why = ("the database is a managed Postgres, so it already outlives the "
               "instance — the invoice images do not"
               if remote_db else
               "a hosted instance's filesystem is replaced on the next deploy "
               "or restart")
        warning = (
            f"Uploaded invoices are being written to {where} own filesystem "
            f"({UPLOAD_DIR}), and {why}. The upload works and the FIRST reading "
            f"works — both happen while the file is still there — and then the "
            f"file is gone: the invoice image 404s, re-reading it fails with "
            f"FileNotFoundError, and pages cannot be merged. Fix it by setting "
            f"BLOB_READ_WRITE_TOKEN to a Vercel Blob store (it is an HTTP API "
            f"and works from any host), or by mounting a persistent disk and "
            f"pointing ESSA_UPLOAD_DIR at it. If this path IS already a mounted "
            f"disk, this warning is wrong and can be ignored.")
    return {
        "ok": not ephemeral,
        "backend": storage_svc.backend_name(),
        "persistent": not ephemeral,
        "platform": platform,
        "upload_dir": None if storage_svc.using_blob() else UPLOAD_DIR,
        "warning": warning,
    }


def _record_startup_failure(exc: BaseException, during: str) -> None:
    global STARTUP_ERROR
    if STARTUP_ERROR is None:
        STARTUP_ERROR = f"{during}: {type(exc).__name__}: {_scrub(exc)}"


def _add_missing_columns():
    """Add any column the models declare and the database has not got.

    `create_all` creates missing TABLES and never touches an existing one, so a
    column added to a model after the database was built simply is not there —
    and the first insert fails with "column X does not exist" on a table that
    plainly does. On SQLite that was handled by the hand-written ALTERs below;
    on Postgres nothing handled it, because _migrate returns early there. A new
    column therefore worked on the warehouse PC and broke the deployment, which
    is the worst possible split.

    Driven from the models rather than from a list, so the next column added
    needs no entry anywhere. ADD COLUMN is standard SQL and the type is compiled
    for whichever dialect is connected, so this is not a second SQLite-shaped
    migration.

    Deliberately narrow: it only ADDS. Dropping, renaming and retyping all
    depend on what the existing data means, and guessing at that unattended is
    how a migration loses a column somebody needed. Those stay hand-written.
    """
    from sqlalchemy import inspect
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue                       # create_all just made it, in full
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            # A NOT NULL column with no default cannot be added to a table that
            # already has rows — there is no value to give them. Left alone and
            # reported rather than guessed at.
            if not col.nullable and col.server_default is None and col.default is None:
                added.append(f"!{table.name}.{col.name} (not-null, needs a manual migration)")
                continue
            typ = col.type.compile(engine.dialect)
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {typ}'))
                added.append(f"{table.name}.{col.name}")
            except Exception as exc:       # noqa: BLE001 — reported, not raised
                added.append(f"!{table.name}.{col.name} ({type(exc).__name__})")
    return added


def _migrate():
    """Add columns introduced after a DB was first created (SQLite), so existing
    installs pick up new Product attributes without losing data.

    SQLite only, and that is not a limitation — it is the whole point. Every
    statement below patches a database that already holds Essa's data and was
    created before the column existed. There is exactly one such database and it
    is the SQLite file on the warehouse PC. A Postgres deployment is created by
    `create_all` above, from today's models, with every one of these columns
    already present — so running these against it would at best be a long series
    of no-ops and at worst fail on syntax SQLite accepts and Postgres does not
    (`ALTER TABLE … DROP COLUMN` inside a try/except being the clearest case).
    """
    from sqlalchemy import inspect, text
    if engine.dialect.name != "sqlite":
        return
    insp = inspect(engine)
    if "products" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("products")}
    adds = {"color": "VARCHAR", "size": "VARCHAR", "pattern": "VARCHAR", "fit": "VARCHAR",
            "product_type": "VARCHAR", "material": "VARCHAR", "design_no": "VARCHAR",
            "sale_price": "FLOAT", "sale_discount_pct": "FLOAT", "detailed": "BOOLEAN",
            "detailed_at": "DATETIME", "detailed_by": "VARCHAR",
            "category": "VARCHAR", "category_section": "VARCHAR",
            # the unit this product is counted in, and how many individual items
            # are in one of it (piece = 1, pair = 2). Left NULL on existing rows
            # on purpose: a product received before unit types existed keeps
            # counting the way it did until a receipt pins it — see
            # services/inventory.line_unit_type.
            "unit_type": "VARCHAR", "pieces_per_unit": "FLOAT",
            # the rest of the stock master's attribute set
            "brand": "VARCHAR", "style": "VARCHAR", "sleeve": "VARCHAR"}
    missing = {k: v for k, v in adds.items() if k not in have}
    if missing:
        with engine.begin() as conn:
            for name, typ in missing.items():
                conn.execute(text(f"ALTER TABLE products ADD COLUMN {name} {typ}"))
    # margin over cost gave way to discount off MRP — see Product.sale_discount_pct
    if "margin_pct" in have:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE products DROP COLUMN margin_pct"))
        except Exception:
            pass
    # documents: document_type (invoice | lr_register | purchase_order)
    if "documents" in insp.get_table_names():
        dcols = {c["name"] for c in insp.get_columns("documents")}
        if "document_type" not in dcols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE documents ADD COLUMN document_type VARCHAR"))
        # every page of a multi-page invoice; NULL on rows that predate it, which
        # the readers treat as "just stored_path"
        if "pages" not in dcols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE documents ADD COLUMN pages JSON"))
    # lr_entries: invoice linkage columns (cross-fill from invoice), then the
    # full Transport Entry field set (company, routing, packages, charges, …)
    if "lr_entries" in insp.get_table_names():
        lcols = {c["name"] for c in insp.get_columns("lr_entries")}
        ladds = {
            "entry_source": "VARCHAR", "lr_entry_no": "VARCHAR", "lr_entry_date": "VARCHAR",
            "lr_mode": "VARCHAR", "boxes": "FLOAT",
            "agent": "VARCHAR", "agent_commission": "FLOAT",
            "auto_transfer_location": "VARCHAR", "purchase_manager": "VARCHAR",
            "stock_holding_days": "FLOAT", "additional_margin": "FLOAT",
            "freight_applicable": "BOOLEAN",
            # a page read in Tamil, and what it actually said
            "source_language": "VARCHAR", "original_values": "JSON",
            # the transporter's G. TOTAL and the charge lines under it — freight
            # alone was never the whole bill
            "freight_total": "FLOAT", "freight_charges": "JSON",
        }
        # Fields Essa never used — no consignment ever carried one, so they were
        # ten empty columns in every view. Dropped rather than left unmapped:
        # SQLite has supported DROP COLUMN since 3.35, and an unmapped column is
        # a trap for the next person reading the schema. If the drop fails (an
        # older SQLite, or the column is indexed) the column simply stays behind,
        # unmapped and unread — which is what the legacy `place` / `purchaser`
        # columns already do.
        ldrops = ["due_date", "pay_mode", "package_slip_no", "slip_date",
                  "actual_weight", "charged_weight", "from_city", "receiving_city",
                  "loading_charge", "loading_applicable", "cash_cheque",
                  "company", "bundle_rack", "section", "remark"]
        with engine.begin() as conn:
            if "invoice_document_id" not in lcols:
                conn.execute(text("ALTER TABLE lr_entries ADD COLUMN invoice_document_id INTEGER"))
            if "matched" not in lcols:
                conn.execute(text("ALTER TABLE lr_entries ADD COLUMN matched BOOLEAN DEFAULT 0"))
            if "mismatches" not in lcols:
                conn.execute(text("ALTER TABLE lr_entries ADD COLUMN mismatches JSON"))
            for name, typ in ladds.items():
                if name not in lcols:
                    conn.execute(text(f"ALTER TABLE lr_entries ADD COLUMN {name} {typ}"))
            # rows that predate the manual form all came in by import
            if "entry_source" not in lcols:
                conn.execute(text("UPDATE lr_entries SET entry_source = 'import'"))
        for name in ldrops:
            if name not in lcols:
                continue
            try:
                with engine.begin() as conn:      # its own transaction: one
                    conn.execute(text(           # unsupported drop must not roll
                        f"ALTER TABLE lr_entries DROP COLUMN {name}"))   # back the rest
            except Exception:
                pass
            # the register's name column is the RECEIVING person, not the
            # purchaser — carry old values over to the renamed column. The
            # legacy `purchaser` column is left in place (SQLite can't always
            # drop) but is no longer mapped or read.
            if "received_by" not in lcols:
                conn.execute(text("ALTER TABLE lr_entries ADD COLUMN received_by VARCHAR"))
                if "purchaser" in lcols:
                    conn.execute(text("UPDATE lr_entries SET received_by = purchaser"))
    # purchase_lines: category and unit type chosen at GRN time
    if "purchase_lines" in insp.get_table_names():
        plcols = {c["name"] for c in insp.get_columns("purchase_lines")}
        for name, typ in (("category", "VARCHAR"), ("unit_type", "VARCHAR"),
                          # retail pricing carried from the invoice review
                          ("mrp", "FLOAT"), ("sale_price", "FLOAT"),
                          ("sale_discount_pct", "FLOAT"),
                          # the supplier's size cell, often the whole size run
                          ("size", "VARCHAR")):
            if name not in plcols:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE purchase_lines ADD COLUMN {name} {typ}"))
    # purchase_line_splits: grew from size-only to the full attribute breakdown
    if "purchase_line_splits" in insp.get_table_names():
        scols = {c["name"] for c in insp.get_columns("purchase_line_splits")}
        sadds = {"color": "VARCHAR", "material": "VARCHAR", "pattern": "VARCHAR",
                 "fit": "VARCHAR", "product_type": "VARCHAR", "design_no": "VARCHAR",
                 "sale_discount_pct": "FLOAT", "category": "VARCHAR",
                 "unit_type": "VARCHAR",
                 "brand": "VARCHAR", "style": "VARCHAR", "sleeve": "VARCHAR"}
        smissing = {k: v for k, v in sadds.items() if k not in scols}
        if smissing:
            with engine.begin() as conn:
                for name, typ in smissing.items():
                    conn.execute(text(
                        f"ALTER TABLE purchase_line_splits ADD COLUMN {name} {typ}"))
        if "margin_pct" in scols:
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        "ALTER TABLE purchase_line_splits DROP COLUMN margin_pct"))
            except Exception:
                pass
    # stock_outwards: the receiving (Stock Inward) end of a dispatch
    if "stock_outwards" in insp.get_table_names():
        ocols = {c["name"] for c in insp.get_columns("stock_outwards")}
        oadds = {"received_date": "VARCHAR", "received_at": "DATETIME"}
        omissing = {k: v for k, v in oadds.items() if k not in ocols}
        if omissing:
            with engine.begin() as conn:
                for name, typ in omissing.items():
                    conn.execute(text(
                        f"ALTER TABLE stock_outwards ADD COLUMN {name} {typ}"))
    # purchase_return_lines: which received row each returned line came from, so
    # the debit note can be re-valued from the GRN rather than from a stale rate —
    # plus `shortage_id`, which marks the lines that claim goods never delivered
    # and must therefore settle without moving stock
    if "purchase_return_lines" in insp.get_table_names():
        rcols = {c["name"] for c in insp.get_columns("purchase_return_lines")}
        radds = {"purchase_line_id": "INTEGER", "split_id": "INTEGER", "uom": "VARCHAR",
                 "shortage_id": "INTEGER"}
        rmissing = {k: v for k, v in radds.items() if k not in rcols}
        if rmissing:
            with engine.begin() as conn:
                for name, typ in rmissing.items():
                    conn.execute(text(
                        f"ALTER TABLE purchase_return_lines ADD COLUMN {name} {typ}"))


SCHEMA_ADDED = []

# Whether the schema/seed pass below has run in THIS process, and what it cost.
BOOT_RAN = False
BOOT_SECONDS = None


def _boot_database():
    """Build the schema, add new columns, and seed the starting data.

    This used to run flat at import, and on a serverless deployment that is what
    made the whole app unreachable. It is ~1000 SQL statements — `create_all`
    over 36 tables, a `get_columns()` reflection PER TABLE in
    `_add_missing_columns`, then seven seed and backfill passes. On the warehouse
    PC those are local SQLite reads and cost about five seconds. Across a network
    to Postgres they are a thousand round trips, and with NullPool (see
    database.py, and it is right to be NullPool) several of them open a fresh TLS
    connection of their own.

    The platform kills a function that has not answered within its init budget,
    so the import never finished, every route returned an opaque
    INTERNAL_FUNCTION_INVOCATION_FAILED, and — worst of it — /api/status could
    not report the failure either, because the app object it hangs off did not
    exist yet. A deployment that cannot say what is wrong with it.

    So it is a function now, and WHEN it runs is decided below. Nothing about
    what it does has changed; only that a cold start no longer pays for it.
    """
    global SCHEMA_ADDED, BOOT_RAN, BOOT_SECONDS
    import time
    started = time.time()
    BOOT_RAN = True

    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:                   # noqa: BLE001 — reported, not raised
        _record_startup_failure(exc, "creating the schema")

    try:
        # Dialect-agnostic first: this is what keeps a column added to a model from
        # working locally and 500-ing on the deployment.
        SCHEMA_ADDED = _add_missing_columns()
        _migrate()
    except Exception as exc:                   # noqa: BLE001 — reported, not raised
        _record_startup_failure(exc, "migrating the schema")

    # ---- accounts ----
    # Held apart from the convenience seeds below. Those are conveniences: an
    # install that starts without its category list is merely inconvenient. An
    # install that starts with an empty users table cannot be signed into at all,
    # and presenting that as a login screen which rejects every correct password is
    # the worst outcome available.
    #
    # So it is still not swallowed — it is reported, and /api/status names it. The
    # reason it is no longer raised is the one at the top of this file: a raise here
    # is invisible on a serverless deployment, and "cannot sign in, no reason given"
    # is exactly what it produces.
    from .database import SessionLocal as _Session
    from .services import users as _users
    try:
        _udb = _Session()
        try:
            _users.seed(_udb)
        finally:
            _udb.close()
    except Exception as exc:                   # noqa: BLE001 — reported, not raised
        _record_startup_failure(exc, "seeding the accounts")

    # load the product-category master (from the GRN Excel) on first run, and heal
    # any LR row that was linked to an invoice before the cross-fill was symmetric
    # (linked, but Inv No / Inv Date left blank)
    try:
        from .database import SessionLocal
        from .services import masters as _masters
        from .services import lr_link as _lr_link
        from .services import unit_types as _unit_types
        _db = SessionLocal()
        _n = _masters.import_categories(_db)
        _masters.seed_options(_db)
        # The business lines this company trades in, and the filing of everything
        # that predates them under the garment line. AFTER import_categories, so
        # the 686 rows it may have just loaded are tagged in the same pass rather
        # than staying untagged until the next start. See services/catalogues.
        from .services import catalogues as _catalogues
        _catalogues.ensure_seed(_db)
        # The legal entity this install has always been, written down so a second
        # one can exist beside it — and every warehouse filed under it. A no-op
        # once done; see services/businesses.
        from .services import businesses as _businesses
        _businesses.ensure_seed(_db)
        # dozens-to-pieces needs a unit master to convert against, and the warehouse
        # should not have to build one before the first receipt
        _unit_types.seed(_db)
        # a warehouse with no label template cannot print a sticker, and designing
        # one before the first receipt is a wall in front of the first garment
        from .services import label_designer as _label_designer
        _label_designer.ensure_default(_db)
        _lr_link.backfill_linked_rows(_db)
        # GRNs raised before numbering existed, and numbers that never reached
        # the consignment row beside them — see inventory.backfill_grn_numbers.
        from .services import inventory as _inv
        _inv.backfill_grn_numbers(_db)
        # Stock movements written before stock was split by warehouse belong to
        # the one warehouse that existed, and the per-warehouse balances are
        # built from them. A no-op once done — see stock_locations.backfill.
        from .services import stock_locations as _stock_loc
        _stock_loc.backfill(_db)
        _db.close()
    except Exception:
        pass

    BOOT_SECONDS = round(time.time() - started, 2)
    return BOOT_SECONDS


def _boot_wanted() -> bool:
    """Whether to run the schema/seed pass as part of starting up.

    ESSA_BOOT_MIGRATE decides it outright, either way — "1" to force it on a
    deployment for one release, "0" to hold it off on a laptop.

    Left unset, the answer is the platform: a long-lived process (the warehouse
    PC, a container, a dev laptop) starts once and can afford it, so it behaves
    exactly as it always has. A serverless instance starts constantly and cannot,
    so it skips — and the schema is applied deliberately instead, either by a
    release with the variable set to "1", or through /api/admin/boot.
    """
    flag = (os.environ.get("ESSA_BOOT_MIGRATE") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return not os.environ.get("VERCEL")


# Bumped when something a user could OBSERVE changes, so /api/status can settle
# "is the fix actually live?" without a guess. 0.2.0: a document's pages are all
# viewable (the image route takes ?page=), a merge refuses rather than destroys
# when a page's file is gone, and a failed reading says which of the two things
# went wrong instead of always claiming it fell back to OCR.
app = FastAPI(title="ESSA AI", version="0.2.0",
              description="Trainable invoice-to-data extraction for Essa Garments")

# ---- who may call what ----
# One middleware rather than a dependency on each route — see security.POLICY
# for the table it reads, and why it is a table.
#
# Order is load-bearing and reads backwards: Starlette runs the LAST-added
# middleware first, so CORS is added after this one in order to sit outside it.
# That is what makes a rejection usable — a 401 raised inside CORS still comes
# back with the headers the browser needs to hand the body to the app, and a
# preflight OPTIONS is answered by CORS before it ever reaches an auth check it
# carries no token to pass.
app.middleware("http")(auth_middleware)

# Within one GET, an expensive read asked for twice is computed once (see
# request_memo.py). Its position does not matter: the contextvar it sets is
# copied into every task and threadpool call the request makes.
from .request_memo import RequestMemoMiddleware
app.add_middleware(RequestMemoMiddleware)

# Request / SQL timing, registered only when ESSA_PERF_LOG is set (see perf.py).
# Added after auth so it sits outside it and times the whole request.
from . import perf as _perf
_perf.install(app, engine)


@app.exception_handler(OperationalError)
@app.exception_handler(ProgrammingError)
def _schema_missing(request, exc):
    """A query against a table that is not there.

    Since the schema pass stopped running on every cold start, there is one new
    way to be wrong: deployed against a database it was never applied to. The
    raw fault for that is `no such table: users` on the login query — a 500 with
    a traceback, which reads as the app being broken rather than as a step not
    yet taken.

    Both are reported the same way for a different reason each: SQLite says
    OperationalError, Postgres says ProgrammingError (UndefinedTable), and the
    person reading it should not have to know which database they got.
    """
    from .database import DB_URL
    msg = _scrub(exc)
    missing = ("no such table" in msg.lower() or "does not exist" in msg.lower()
               or "undefinedtable" in type(exc).__name__.lower())
    from fastapi.responses import JSONResponse
    if missing and not BOOT_RAN:
        return JSONResponse(
            {"detail": "The database has no schema yet. Apply it once with "
                       "POST /api/admin/boot (header x-essa-boot-key: "
                       "ESSA_AUTH_SECRET), or redeploy with ESSA_BOOT_MIGRATE=1.",
             "database": _database_status(DB_URL)}, status_code=503)
    return JSONResponse({"detail": f"Database error: {msg}"}, status_code=500)


@app.exception_handler(storage_svc.StorageError)
def _storage_error(request, exc):
    """A file that could not be stored or read back is not an unexplained
    fault, and answering with a bare 500 makes it look like one — "Extract
    failed: 500" is what the screen shows, and it names neither the step nor
    the reason. 502 with the message says which of the two halves failed, and
    the message came from the storage service itself."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": f"File storage: {exc}"}, status_code=502)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# CSP, HSTS, framing, nosniff and the rest — see security_headers.py. Added
# last so it is outermost, and so even a 401 or a CORS preflight carries them.
from .security_headers import SecurityHeadersMiddleware
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(documents.router)
app.include_router(suppliers.router)
app.include_router(purchase_orders.router)
app.include_router(pricing.router)
app.include_router(stock_audit.router)
app.include_router(physical_audit.router)
app.include_router(purchases.router)
app.include_router(inventory.router)
app.include_router(outward.router)
app.include_router(payments.router)
app.include_router(returns.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(auth.router)
app.include_router(masters.router)
app.include_router(master_data.router)
app.include_router(lr.router)
app.include_router(bundles.router)
app.include_router(dashboard.router)
app.include_router(search.router)
app.include_router(labels.router)
app.include_router(dead_stock.router)
app.include_router(notifications.router)
app.include_router(voice.router)
app.include_router(locations.router)
app.include_router(catalogues.router)
app.include_router(users.router)
app.include_router(command.router)
app.include_router(audit.router)


# ---- the retail shop (POS) at /pos ----
# A separate Flask app, mounted here so it shares this origin — see pos_mount.
# It is optional: a missing folder or an environment without Flask leaves the
# POS screen saying what to install, which is more use than a 500 behind a
# button someone just clicked.
#
# Built on the first request to /pos rather than at import, which is the second
# half of the cold-start fix above. Loading the shop is not a cheap import: it
# opens a second engine, runs CREATE SCHEMA, its own create_all, and then four
# full syncs — the whole product catalogue, the categories, the branch list and
# the transfer history — every one of them a table scan. See pos_mount.
#
# All of that was happening before the warehouse could answer /api/auth/login,
# on a deployment where nobody had asked for the till at all. Deferred, the cost
# lands on whoever opens the POS, once per instance, and the syncs still run so
# the shop is as up to date as it ever was.
_pos_error = None
_pos_app = None
_pos_loaded = False


@app.on_event("startup")
def _preload_pos():
    """On a long-lived process, build the shop BEFORE the first request.

    Loading the shop swaps `sys.modules["app"]` to the shop's package for as long
    as its syncs run (see pos_mount.load_pos_app), and every warehouse request
    that does a function-level import in that window fails with "No module named
    app.services". With a demo catalogue the window is a second; with a real one
    (hundreds of thousands of products to sync) it is a minute, and the
    inventory list, the dashboard and the rest fail for whoever is using them.

    Done here, the swap happens while the server is not yet answering anything.
    Serverless keeps the deferred build (_boot_wanted says no), because there the
    cold-start cost this deferral exists for is the thing that matters.
    """
    if _boot_wanted():
        _pos_asgi()


@app.on_event("startup")
def _start_search_sync():
    """Keep the Elasticsearch read index in step with PostgreSQL (app/search).

    Registered after _preload_pos, so it starts once the shop is loaded.
    A background thread on a long-lived server only — serverless instances
    start constantly and never read the index. Off unless ELASTICSEARCH_ENABLED
    is set; failing to start it changes nothing, reads use PostgreSQL."""
    if _boot_wanted():
        try:
            from .search import sync as search_sync
            search_sync.start_background()
        except Exception:                                  # noqa: BLE001 — optional layer
            import logging
            logging.getLogger("uvicorn.error").warning("search sync did not start", exc_info=True)
        # the till's slowest facts, read now in the background so the first
        # dashboard after a start finds them ready (services/pos_sales)
        try:
            from .services import pos_sales
            pos_sales.warm_till()
        except Exception:                                  # noqa: BLE001 — the first read computes
            pass


def _pos_asgi():
    """The mounted shop, built on demand and kept for the life of the instance."""
    global _pos_app, _pos_error, _pos_loaded
    if _pos_loaded:
        return _pos_app
    _pos_loaded = True
    try:
        from .pos_mount import load_pos_app
        flask_app = load_pos_app()
        import warnings
        with warnings.catch_warnings():                # the module warns on import
            warnings.simplefilter("ignore", DeprecationWarning)
            from starlette.middleware.wsgi import WSGIMiddleware
        _pos_app = WSGIMiddleware(flask_app)
    except Exception as exc:                           # noqa: BLE001 — reported, not raised
        _pos_app, _pos_error = None, f"{type(exc).__name__}: {exc}"
    return _pos_app


def _pos_unavailable_html(rest: str = "") -> str:
    """Why the POS button led nowhere, as a page rather than a 500."""
    return ("<body style=\"font:14px/1.6 system-ui;padding:32px;color:#33261F\">"
            "<h2 style='margin:0 0 8px'>POS is not loaded</h2>"
            f"<p>{_pos_error}</p>"
            "<p>The shop lives in the <b>Textile Retail Shop</b> folder beside "
            "this project and needs its Python packages in the backend "
            "environment:</p>"
            "<pre style='background:#F2EEEB;padding:12px;border-radius:6px'>"
            "cd backend\n.venv\\Scripts\\activate\npip install -r requirements.txt</pre>"
            "<p>Then restart the server.</p></body>")


def _with_store_access(scope):
    """Tell the shop when the Essa account behind this request is a Store user.

    An account whose role is User — or one Users & Access narrows — gets only
    the floor's Store screens (billing, delivery, stock, customers, returns,
    alteration; services/permissions.STORE_ACCESS). Hiding the other screens in our
    menu is not enough: the frame has its own navigation, and a URL typed into
    it would open anything. So the shop has to know, and it can only know
    through the request — it has its own login and no idea who is signed in here.

    The account comes off the `essa_token` cookie, which is set at path=/ and so
    rides along on every /pos request from inside the frame. Any copy of the
    header a client sent is stripped first, whatever happens next. No Essa
    session — the shop opened on its own, a phone at /pos/floor — adds nothing,
    and the shop's own login decides exactly as it did before.

    Never raises: a database hiccup here must not take the till down with it.
    It fails NARROW instead — an account that could not be looked up gets the
    Store user's floor screens, so billing carries on and nothing else opens.
    """
    from .security import token_from
    from .services import permissions as perms_svc
    from .services.users import resolve_token
    from starlette.requests import Request as _Request

    name = perms_svc.STORE_HEADER.encode()
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != name]
    if "/static/" in scope.get("path", ""):
        return dict(scope, headers=headers)            # css, icons: nothing to gate
    # A throwaway copy for reading the cookie: Starlette's Request replaces the
    # scope's header list with one of its own, so a list appended to afterwards
    # would not be the one the shop receives.
    token = token_from(_Request(dict(scope, headers=list(headers))))
    narrow = False
    if token:
        try:
            from .database import SessionLocal
            db = SessionLocal()
            try:
                user = resolve_token(db, token)
                narrow = user is not None and perms_svc.store_access(
                    perms_svc.normalise(user.permissions), user.role) == "user"
            finally:
                db.close()
        except Exception:                              # noqa: BLE001
            narrow = True
    return dict(scope, headers=headers + ([(name, b"user")] if narrow else []))


class _PosMount:
    """An ASGI app that is the shop once the shop has been loaded.

    Starlette resolves a mount at request time, so standing this in front of the
    real WSGI middleware is enough — there is no need to re-mount anything after
    startup, and a shop that fails to load still answers with the page below
    rather than a 500.
    """

    async def __call__(self, scope, receive, send):
        pos = _pos_asgi()
        if pos is not None:
            if scope.get("type") == "http":
                scope = _with_store_access(scope)
            return await pos(scope, receive, send)
        from starlette.responses import HTMLResponse
        await HTMLResponse(_pos_unavailable_html(""), status_code=503)(scope, receive, send)


app.mount("/pos", _PosMount())


@app.get("/api/status")
def status():
    """Also the deployment's own diagnostic. `database` is the part worth
    reading first: if the server came up without one, every other answer here
    is beside the point, and this is the only place that says so in words."""
    from .database import DB_URL
    return {
        "company": {"name": COMPANY_NAME, "gstin": COMPANY_GSTIN},
        "provider_preference": runtime.get("provider_preference"),
        "providers": provider_status(),
        # "not loaded yet" is not "broken": the shop is built on the first
        # request to /pos now, so before that there is nothing to report either
        # way, and saying `available: false` would read as a fault.
        "pos": {"loaded": _pos_loaded,
                "available": None if not _pos_loaded else _pos_app is not None,
                "error": _pos_error},
        # Whether the schema/seed pass ran in this instance, and what it cost.
        # `ran: false` on a serverless deployment is the intended state — see
        # _boot_wanted — and means the schema is managed deliberately rather
        # than rebuilt on every cold start.
        "boot": {"ran": BOOT_RAN, "seconds": BOOT_SECONDS,
                 "wanted": _boot_wanted()},
        "database": _database_status(DB_URL),
        # WHICH code is answering. Three fixes in a row were tested against a
        # deployment that had not finished replacing the previous one, and an
        # unchanged symptom looks identical to a fix that did not work — so the
        # running server states what it is. The commit comes from whichever
        # variable the platform sets by itself; nothing has to be remembered at
        # deploy time, and `null` for it means "not deployed from git", which is
        # the honest answer on a warehouse PC running from a folder.
        "build": {
            "version": app.version,
            "commit": (os.environ.get("RENDER_GIT_COMMIT")
                       or os.environ.get("VERCEL_GIT_COMMIT_SHA")
                       or os.environ.get("ESSA_BUILD_COMMIT") or None),
        },
        # A bare string, kept because it is what earlier notes and error
        # messages tell people to look at. `storage_detail.ok` is the one that
        # answers whether the files will still be there tomorrow.
        "storage": storage_svc.backend_name(),
        "storage_detail": {
            **_storage_status(DB_URL),
            "blob_api_version": storage_svc.BLOB_API_VERSION,
            "blob_access": storage_svc.BLOB_ACCESS,
        },
    }


# ---- phone web app (PWA) at /m — same origin, no build step, no Expo ----
MOBILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile")


@app.get("/m")
def mobile_root():
    return FileResponse(os.path.join(MOBILE_DIR, "index.html"))


@app.get("/m/manifest.webmanifest")
def mobile_manifest():
    return FileResponse(os.path.join(MOBILE_DIR, "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/m/{fname}")
def mobile_asset(fname: str):
    path = os.path.join(MOBILE_DIR, os.path.basename(fname))
    if not os.path.exists(path):
        return FileResponse(os.path.join(MOBILE_DIR, "index.html"))  # SPA-ish fallback
    return FileResponse(path)


# ---- serve the built frontend if present (single-origin deploy) ----
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "frontend", "dist")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

    @app.get("/")
    def spa_root():
        # Everything under /assets is content-hashed by the build, so a stale copy
        # is impossible — a rebuild changes the filename. index.html is the one
        # file with a fixed name, and it is what points at those filenames. Cached
        # without a directive it goes on naming the previous build's bundle, and a
        # rebuild silently does nothing until the browser decides to look again.
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"),
                            headers={"Cache-Control": "no-cache"})


# ---- applying the schema on a deployment that skips it at start ----
@app.post("/api/admin/boot")
def admin_boot(request: Request):
    """Run the schema/seed pass once, now.

    A serverless deployment no longer does this while starting (see
    _boot_wanted), so this is how a release that adds a table or a column gets
    it applied without a cold start having to carry ~1000 statements it would
    be killed for.

    Authorised by ESSA_AUTH_SECRET rather than by a signed-in super admin, and
    deliberately: the case this exists for includes a database with no accounts
    in it yet, where there is nobody to sign in AS. It is in PUBLIC in
    security.py for the same reason, and checks the header itself.

    Safe to call twice. create_all skips tables that exist, _add_missing_columns
    only ever ADDS, and every seed creates only what is missing.
    """
    from .config import AUTH_SECRET
    supplied = (request.headers.get("x-essa-boot-key") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, AUTH_SECRET):
        raise HTTPException(401, "Provide the ESSA_AUTH_SECRET as x-essa-boot-key")
    seconds = _boot_database()
    return {"ok": STARTUP_ERROR is None, "seconds": seconds,
            "columns_added": SCHEMA_ADDED, "error": STARTUP_ERROR}


# ---- and the ordinary case: a process that starts once and stays up ----
# Last in the file on purpose. By here the app object exists, every router is
# registered and /api/status can answer — so if this pass is slow, or the
# database is unreachable and it spends its connect timeouts finding out, the
# deployment still comes up and still says what went wrong. That is the whole
# difference from where this code used to sit.
if _boot_wanted():
    _boot_database()
