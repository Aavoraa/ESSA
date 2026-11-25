"""Move ESSA from its SQLite files to PostgreSQL on this PC.

    postgres_setup.bat                      create the login + database, copy the data
    postgres_setup.bat --port 5433          another PostgreSQL on this PC
    postgres_setup.bat --no-copy            just create and connect, start empty
    postgres_setup.bat adopt --db essa      use an EXISTING ESSA database (e.g. the
                                            migrated one) - hands only its public and
                                            shop schemas to the ESSA login, copies nothing
    postgres_setup.bat verify               compare SQLite and PostgreSQL row counts

What it does:
  1. Asks for the PostgreSQL ADMIN password (typed hidden, used once, never saved).
  2. Creates a dedicated login (essa_app, with a long random password) and a
     database (essa) owned by it. The admin login is not used by ESSA.
  3. Writes ESSA_DATABASE_URL into ESSA\\.env - the only place that password is
     kept. The old .env is copied to .env.bak-<time> first.
  4. Starts ESSA once against the empty database so it builds its own tables
     exactly as the code expects (ESSA in "public", the shop/POS in "shop").
  5. Copies every row from backend\\data\\essa.db and the shop's textile_shop.db,
     sets every ID counter past the copied rows, and compares the counts table
     by table.

Safe by design:
  * the SQLite files are only READ - they stay as they are, as the way back;
  * the copy only ever writes into a database THIS run created; an existing
    database is never emptied or overwritten (use --replace-existing, and type
    the database name, if you really mean it);
  * stop ESSA before running this (run.bat window, ESSA.exe, or the server task).

Way back to SQLite: delete the ESSA_DATABASE_URL line from .env and restart ESSA.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(HOME, "backend")
PY = os.path.join(BACKEND, ".venv", "Scripts", "python.exe")
ENV_PATH = os.path.join(HOME, ".env")
ESSA_DB = os.path.join(BACKEND, "data", "essa.db")
SHOP_DB = os.path.join(HOME, "Textile Retail Shop", "textile_shop.db")
SOURCES = [(ESSA_DB, "public", "ESSA"), (SHOP_DB, "shop", "shop / POS")]


def say(msg=""):
    print(msg, flush=True)


# --------------------------------------------------------------------------- .env

def read_env() -> list[str]:
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def env_get(key: str) -> str:
    for ln in read_env():
        k, sep, v = ln.partition("=")
        if sep and k.strip() == key and not ln.lstrip().startswith("#"):
            return v.strip()
    return ""


def env_set(key: str, value: str) -> None:
    lines = read_env()
    if os.path.exists(ENV_PATH):
        shutil.copy2(ENV_PATH, ENV_PATH + time.strftime(".bak-%Y%m%d-%H%M%S"))
    done = False
    for i, ln in enumerate(lines):
        if ln.partition("=")[0].strip() == key and not ln.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        lines.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- PostgreSQL

def admin_connect(a, password, dbname="postgres"):
    import psycopg2
    conn = psycopg2.connect(host=a.host, port=a.port, user=a.admin_user, password=password,
                            dbname=dbname, connect_timeout=8)
    conn.autocommit = True
    return conn


def tune_login(cur, app_user: str) -> None:
    """ESSA's queries are short; PostgreSQL's parallel workers do not speed them up,
    and on PostgreSQL 19.0 for Windows a parallel join can hang for minutes (seen on
    the dashboard). Off for ESSA's own login only - nothing else on the server changes."""
    from psycopg2 import sql
    cur.execute(sql.SQL("ALTER ROLE {} SET max_parallel_workers_per_gather = 0").format(sql.Identifier(app_user)))


def create_login_and_db(a, admin_pw: str) -> tuple[str, bool]:
    """Returns (url, created_new_database)."""
    from psycopg2 import sql
    conn = admin_connect(a, admin_pw)
    cur = conn.cursor()
    cur.execute("SELECT current_setting('server_version')")
    say(f"Connected to PostgreSQL {cur.fetchone()[0]} on {a.host}:{a.port} as {a.admin_user}.")

    app_pw = secrets.token_urlsafe(32)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (a.app_user,))
    if cur.fetchone():
        # Reuse the login; give it a fresh password, which goes into .env below.
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(a.app_user)), (app_pw,))
        say(f"Login {a.app_user} exists - its password was renewed.")
    else:
        cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(a.app_user)), (app_pw,))
        say(f"Created login {a.app_user}.")
    tune_login(cur, a.app_user)

    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (a.db,))
    exists = bool(cur.fetchone())
    if exists:
        if a.replace_existing:
            typed = input(f"Database '{a.db}' already exists. ALL its data will be replaced by the SQLite data.\n"
                          f"Type the database name to confirm: ").strip()
            if typed != a.db:
                raise SystemExit("Not confirmed - nothing was changed in the database.")
            created = True
            say(f"Database {a.db} will be replaced (confirmed).")
        else:
            conn.close()
            raise SystemExit(f"Database '{a.db}' already exists on this server - nothing was changed in it.\n"
                             f"Use 'adopt' to run ESSA on it, choose a new name (--db essa_new), "
                             f"or --replace-existing to overwrite it.")
    else:
        cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0")
                    .format(sql.Identifier(a.db), sql.Identifier(a.app_user)))
        created = True
        say(f"Created database {a.db}, owned by {a.app_user}.")
    if created:
        # Only a database this run made (or the user confirmed replacing) is
        # handed to the ESSA login. An existing one keeps its owners untouched.
        cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(a.db), sql.Identifier(a.app_user)))
        conn.close()
        # PG 15+: the owner needs CREATE on public; give it the schema too.
        conn = admin_connect(a, admin_pw, a.db)
        conn.cursor().execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(a.app_user)))
    conn.close()
    url = f"postgresql://{quote(a.app_user)}:{quote(app_pw, safe='')}@{a.host}:{a.port}/{quote(a.db)}"
    return url, created


def build_schema(url: str) -> None:
    """Start ESSA once against the database so it creates every table itself."""
    port = free_port()
    env = dict(os.environ, ESSA_DATABASE_URL=url, ESSA_BOOT_MIGRATE="1", PYTHONIOENCODING="utf-8")
    log_path = os.path.join(HERE, "schema-build.log")
    say(f"Building the tables (ESSA starting once on port {port})...")
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 600
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit(f"ESSA stopped while building the tables - see {log_path}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
                    import json
                    st = json.loads(r.read())
                if not (st.get("database") or {}).get("ok"):
                    raise SystemExit(f"Database problem: {st.get('database')}")
                if (st.get("pos") or {}).get("available") is not None:
                    if not st["pos"]["available"]:
                        raise SystemExit(f"The shop did not build: {st['pos'].get('error')}")
                    say("Tables built (ESSA + shop).")
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise SystemExit(f"Timed out building the tables - see {log_path}")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- the copy

def _convert(value, coltype):
    from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, Float
    if value is None:
        return None
    if isinstance(coltype, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "t", "true", "y", "yes")
        return bool(value)
    if isinstance(value, str):
        if "\x00" in value:
            value = value.replace("\x00", "")
        if isinstance(coltype, (Integer, Numeric, Float)) and value.strip() == "":
            return None
        if isinstance(coltype, (DateTime, Date)) and value.strip() == "":
            return None
    return value


def copy_data(url: str) -> bool:
    from sqlalchemy import MetaData, create_engine, func, select, text
    dst = create_engine(url)
    ok = True
    for path, schema, label in SOURCES:
        if not os.path.exists(path):
            say(f"  {label}: {path} not found - skipped")
            continue
        src = create_engine(f"sqlite:///{path}")
        smd, dmd = MetaData(), MetaData(schema=schema)
        smd.reflect(src)
        dmd.reflect(dst)
        tables = [t for t in dmd.sorted_tables if t.schema == schema]
        say(f"\n{label}: {len(smd.tables)} SQLite tables -> {len(tables)} PostgreSQL tables in '{schema}'")
        with dst.begin() as conn:
            # Only ever a database this run created: empty out what the start-up
            # seeded, so the copy is exactly the SQLite data and nothing else.
            names = ", ".join(f'"{schema}"."{t.name}"' for t in tables)
            if names:
                conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
            with src.connect() as sconn:
                for t in tables:
                    st = smd.tables.get(t.name)
                    if st is None:
                        continue
                    cols = [c for c in t.columns if c.name in st.columns]
                    dropped = [c for c in st.columns.keys() if c not in t.columns]
                    rows = sconn.execute(select(*[st.c[c.name] for c in cols])).fetchall()
                    batch = [{c.name: _convert(v, c.type) for c, v in zip(cols, r)} for r in rows]
                    for i in range(0, len(batch), 2000):
                        conn.execute(t.insert(), batch[i:i + 2000])
                    if dropped:
                        say(f"  note: {t.name}: SQLite-only columns not copied: {', '.join(dropped)}")
            # every ID counter past the copied rows
            seqs = conn.execute(text("""
                SELECT c.table_name, c.column_name, pg_get_serial_sequence(format('%I.%I', c.table_schema, c.table_name), c.column_name)
                  FROM information_schema.columns c
                 WHERE c.table_schema = :s
                   AND pg_get_serial_sequence(format('%I.%I', c.table_schema, c.table_name), c.column_name) IS NOT NULL
            """), {"s": schema}).fetchall()
            for tname, col, seq in seqs:
                conn.execute(text(f'SELECT setval(:seq, COALESCE((SELECT max("{col}") FROM "{schema}"."{tname}"), 0) + 1, false)'),
                             {"seq": seq})
        ok &= compare(src, dst, smd, tables, label)
    return ok


def compare(src, dst, smd, tables, label) -> bool:
    from sqlalchemy import func, select
    bad, total = [], 0
    with src.connect() as s, dst.connect() as d:
        for t in tables:
            st = smd.tables.get(t.name)
            if st is None:
                continue
            a = s.execute(select(func.count()).select_from(st)).scalar()
            b = d.execute(select(func.count()).select_from(t)).scalar()
            total += a
            if a != b:
                bad.append(f"{t.name}: SQLite {a} vs PostgreSQL {b}")
    if bad:
        say(f"  {label}: COUNTS DIFFER:\n    " + "\n    ".join(bad))
        return False
    say(f"  {label}: all {len([t for t in tables if t.name in smd.tables])} tables match - {total:,} rows")
    return True


def verify() -> int:
    from sqlalchemy import MetaData, create_engine
    url = env_get("ESSA_DATABASE_URL")
    if not url:
        say("ESSA_DATABASE_URL is not set in .env")
        return 1
    dst, ok = create_engine(url), True
    for path, schema, label in SOURCES:
        if os.path.exists(path):
            src = create_engine(f"sqlite:///{path}")
            smd, dmd = MetaData(), MetaData(schema=schema)
            smd.reflect(src)
            dmd.reflect(dst)
            ok &= compare(src, dst, smd, [t for t in dmd.sorted_tables if t.schema == schema], label)
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

ESSA_SCHEMAS = ("public", "shop")


def adopt(a, admin_pw: str) -> str:
    """Point ESSA at an EXISTING ESSA database (e.g. one the TAQUA migration filled).

    Only ESSA's own schemas (public = warehouse, shop = Store) and what is in
    them are handed to the ESSA login, so ESSA can run - and add a new column on
    an upgrade - without ever logging in as the admin. No row is read or changed.
    Other schemas (legacy, migration, ...) keep their owner. Every owner changed
    is written to a file first, so it can be put back exactly."""
    from psycopg2 import sql
    conn = admin_connect(a, admin_pw)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (a.db,))
    if not cur.fetchone():
        raise SystemExit(f"There is no database '{a.db}' on {a.host}:{a.port}.")
    app_pw = secrets.token_urlsafe(32)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (a.app_user,))
    verb = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(a.app_user)), (app_pw,))
    tune_login(cur, a.app_user)
    say(f"{'Renewed' if verb == 'ALTER' else 'Created'} login {a.app_user}.")
    cur.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (a.db,))
    db_owner = cur.fetchone()[0]
    conn.close()

    conn = admin_connect(a, admin_pw, a.db)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.users') IS NOT NULL")
    if not cur.fetchone()[0]:
        raise SystemExit(f"'{a.db}' does not look like an ESSA database (no public.users table) - nothing changed.")
    cur.execute("""
        SELECT 'schema', n.nspname, NULL, pg_get_userbyid(n.nspowner)
          FROM pg_namespace n WHERE n.nspname = ANY(%(s)s)
        UNION ALL
        SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence'
                              WHEN 'f' THEN 'foreign table' ELSE 'table' END,
               n.nspname, c.relname, pg_get_userbyid(c.relowner)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY(%(s)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
           -- a sequence that belongs to a column moves with its table
           AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid
                                                AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))
        UNION ALL
        SELECT 'type', n.nspname, t.typname, pg_get_userbyid(t.typowner)
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = ANY(%(s)s) AND t.typtype IN ('e', 'd')
    """, {"s": list(ESSA_SCHEMAS)})
    objects = [o for o in cur.fetchall() if o[3] != a.app_user]
    undo = os.path.join(HERE, time.strftime(f"owners-before-adopt-{a.db}-%Y%m%d-%H%M%S.sql"))
    with open(undo, "w", encoding="utf-8") as f:
        f.write(f"-- Owners before postgres_setup adopt. Run as {a.admin_user} in database {a.db} to put them back.\n")
        f.write(f"ALTER DATABASE {sql.Identifier(a.db).as_string(conn)} OWNER TO {sql.Identifier(db_owner).as_string(conn)};\n")
        for kind, schema, name, owner in objects:
            target = sql.Identifier(schema) if kind == "schema" else sql.Identifier(schema, name)
            f.write(f"ALTER {kind.upper()} {target.as_string(conn)} OWNER TO {sql.Identifier(owner).as_string(conn)};\n")
    say(f"Saved the current owners to {undo} (to undo, if ever needed).")
    for kind, schema, name, _owner in objects:
        target = sql.Identifier(schema) if kind == "schema" else sql.Identifier(schema, name)
        cur.execute(sql.SQL("ALTER " + kind.upper() + " {} OWNER TO {}").format(target, sql.Identifier(a.app_user)))
    cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(a.db), sql.Identifier(a.app_user)))
    conn.commit()
    conn.close()
    kinds = {}
    for k, *_ in objects:
        kinds[k] = kinds.get(k, 0) + 1
    say(f"Handed to {a.app_user}: " + ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
        + f" in {', '.join(ESSA_SCHEMAS)}. No rows were touched; other schemas keep their owners.")
    return f"postgresql://{quote(a.app_user)}:{quote(app_pw, safe='')}@{a.host}:{a.port}/{quote(a.db)}"


def server_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Move ESSA to PostgreSQL on this PC.")
    ap.add_argument("command", nargs="?", default="setup", choices=["setup", "adopt", "verify"])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--admin-user", default="postgres")
    ap.add_argument("--db", default="essa")
    ap.add_argument("--app-user", default="essa_app")
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--replace-existing", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, BACKEND)

    if a.command == "verify":
        return verify()
    if server_running():
        say("ESSA is running (port 8000). Stop it first - close run.bat / ESSA.exe, or run\n"
            "  deploy\\server\\server_setup.bat remove   (as administrator) if the server task runs it.")
        return 1
    if env_get("ESSA_DATABASE_URL"):
        say("Note: .env already has ESSA_DATABASE_URL - it will be replaced (the old .env is kept as a .bak).")

    say(f"PostgreSQL admin login '{a.admin_user}' on {a.host}:{a.port}.")
    admin_pw = getpass.getpass(f"Password for {a.admin_user} (hidden, used once, not saved): ")
    if a.command == "adopt":
        try:
            url = adopt(a, admin_pw)
        except Exception as e:                  # noqa: BLE001
            msg = str(e).replace(admin_pw, "********") if admin_pw else str(e)
            say(f"\nCould not adopt the database: {msg.strip()}")
            return 1
        finally:
            admin_pw = None
        env_set("ESSA_DATABASE_URL", url)
        say(f"Saved ESSA_DATABASE_URL in {ENV_PATH}. ESSA now uses '{a.db}' as {a.app_user}.")
        return 0
    try:
        url, created = create_login_and_db(a, admin_pw)
    except Exception as e:                      # noqa: BLE001
        msg = str(e).replace(admin_pw, "********") if admin_pw else str(e)
        say(f"\nCould not set up the database: {msg.strip()}")
        return 1
    finally:
        admin_pw = None

    env_set("ESSA_DATABASE_URL", url)
    say(f"Saved ESSA_DATABASE_URL in {ENV_PATH} (the password is only there).")

    if a.no_copy or not created:
        say("No data copied." + ("" if created else " The existing database was left exactly as it was."))
        return 0
    build_schema(url)
    say("\nCopying the data (the SQLite files are only read)...")
    ok = copy_data(url)
    say("\nDONE - ESSA now uses PostgreSQL. Start it again (run.bat, ESSA.exe or server_setup.bat)."
        if ok else "\nThe copy finished WITH DIFFERENCES - read them above before using ESSA.")
    say("Way back to SQLite: delete the ESSA_DATABASE_URL line in .env and restart ESSA.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
