"""
The engine and session factory.

SQLite on a laptop, Postgres when deployed. The difference matters more than a
URL swap, because a serverless deployment runs many short-lived instances rather
than one long-lived process, and the pooling that is right for one is wrong for
the other.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

from .config import DATABASE_URL


def _normalise(url: str) -> str:
    """`postgres://` is what most dashboards hand out and what SQLAlchemy has
    refused since 1.4 — it wants `postgresql://`. Rewriting it here means the
    connection string can be pasted from the provider without editing, which is
    where this otherwise goes wrong once per deployment."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


DB_URL = _normalise(DATABASE_URL)
IS_SQLITE = DB_URL.startswith("sqlite")

if IS_SQLITE:
    # One process, many threads: the connection is shared across them and
    # SQLite's own check would refuse that.
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
else:
    # NullPool, deliberately. A pool assumes the process outlives the request;
    # a serverless instance does not, so pooled connections are abandoned rather
    # than reused and the database runs out of slots while most of them sit idle
    # in instances that will never be called again. Connect per request, hand it
    # back at the end, and let the provider's own pooler do the pooling — which
    # is what its pooled connection string is for.
    #
    # pre_ping because the other end may have closed a connection that this side
    # still believes in — the first query then fails on a fault nobody caused.
    #
    # A LONG-LIVED process is the opposite case, and there NullPool is pure cost:
    # every request opened one to three brand-new connections (the auth check,
    # the route, the shop), each paying TCP setup and password authentication —
    # measured at ~50-150 ms apiece on the warehouse PC, on every call, however
    # trivial. So a server that stays up (the same signal main._boot_wanted uses:
    # not a serverless platform) keeps a small pool. ESSA_DB_POOL=null|queue
    # overrides the choice either way.
    _pool_choice = (os.environ.get("ESSA_DB_POOL") or "").strip().lower()
    _serverless = bool(os.environ.get("VERCEL"))
    if _pool_choice == "null" or (_serverless and _pool_choice != "queue"):
        engine = create_engine(DB_URL, poolclass=NullPool, pool_pre_ping=True,
                               connect_args={"connect_timeout": 10})
    else:
        # Sized to the workload rather than maximised: uvicorn runs one process
        # whose threadpool serves the requests, a dashboard opens ~15 at once,
        # and each holds a connection only for the duration of its session.
        # Recycled every half hour so a connection the server dropped is never
        # handed out stale (pre_ping catches the rest).
        engine = create_engine(DB_URL, pool_size=10, max_overflow=20,
                               pool_pre_ping=True, pool_recycle=1800,
                               connect_args={"connect_timeout": 10})

if IS_SQLITE:
    # SQLite ships with foreign keys OFF and must be told, per connection.
    #
    # Left off, a delete that orphans rows succeeds here and fails on Postgres —
    # which is the worst way for the two to differ, because the database that
    # would have objected is the one nobody develops against. Deleting a
    # document referenced by an LR row did exactly that: fine on the warehouse
    # PC for as long as it has existed, a 500 on the deployment the first time
    # anybody tried it.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
