"""
The whole server, as one Vercel function.

Vercel's Python runtime serves whatever ASGI or WSGI application this module
calls `app`, so this file's only job is to make `backend/` and the shop
importable and then hand over the application that already exists. There is no
second copy of the wiring here: the routers, the access-control middleware, the
phone app at /m and the shop mounted at /pos are all assembled in
backend/app/main.py exactly as they are when the same server runs on the
warehouse PC. One code path, two ways of being started.

This module is the `app` service in vercel.json. Everything under /api, /m and
/pos is routed to it by the top-level rewrites there; every other path goes to
the `frontend` service, the desktop bundle built from frontend/ and served as
static files. The two never call each other server-side — the browser talks to
both on the one domain — so no service bindings are needed.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `backend` holds the package `app`; the shop is found relative to ROOT by
# pos_mount, which does its own sys.modules juggling because both codebases
# call their package `app`.
sys.path.insert(0, str(ROOT / "backend"))

# The one thing that differs from a laptop run, and it differs because the
# platform differs: outside /tmp the filesystem is read-only. STATE_DIR is only
# reached for scratch work now — the database is Postgres and uploaded invoices
# go to Blob storage — but config.py creates it at import, and creating it under
# the read-only code directory would fail before a single route was registered.
os.environ.setdefault("ESSA_STATE_DIR", "/tmp/essa")

# The shop finds its own database through a bare DATABASE_URL (its config.py),
# and left unset it looks for a SQLite file in a directory that is read-only
# here — so /pos would fail to start while the warehouse ran fine, which is a
# confusing half-working deployment to be handed.
#
# It is the same database as the warehouse's, so rather than ask for the
# connection string to be pasted into a second variable, resolve it the same
# way and set it here if the platform has not. config._database_url already
# knows every name a provider might have used.
sys.path.insert(0, str(ROOT / "backend"))
from app.config import DATABASE_URL as _WAREHOUSE_DB  # noqa: E402

_pos_db = (os.environ.get("DATABASE_URL") or "").strip()
# A template is not a connection string. The shop reads this variable through
# its own config and has no idea the angle brackets are placeholders, so it
# tries to resolve a host literally called "aws-0-<region>.pooler.supabase.com"
# and reports a DNS failure — which reads as a network fault rather than as a
# value nobody filled in.
if "<" in _pos_db or ">" in _pos_db:
    _pos_db = ""

if not _pos_db:
    # Same database as the warehouse when there is a real one; otherwise the
    # shop gets its own scratch file rather than failing to start. /pos then
    # works and forgets, exactly as the warehouse does, and /api/status is the
    # one place that says the whole deployment is running on scratch storage.
    _pos_db = (_WAREHOUSE_DB if not _WAREHOUSE_DB.startswith("sqlite")
               else "sqlite:////tmp/essa/textile_shop.db")
os.environ["DATABASE_URL"] = _pos_db

from app.main import app  # noqa: E402  (path must be set first)

__all__ = ["app"]
