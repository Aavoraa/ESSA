"""Stage 1, from a pg_dump — load a TAQUALIVE database backup into ``legacy_tqa.raw``.

The first migration was fed a CSV export (``load_legacy.py``). Later snapshots
of the legacy system arrive as a PostgreSQL custom-format dump instead
(``TAQUALIVE.backup``). This produces exactly what ``load_legacy.py`` produces
— one ``raw.<table>`` per legacy table, every column TEXT — so stages 2 and 3
(prepare_target.py, migrate.py) run unchanged on either source.

TEXT is PostgreSQL's own output form, which is what the CSV export wrote too:
booleans t/f (NOT the 'true'/'false' a plain ::text cast gives — see
as_exported), ISO timestamps, NULL as NULL. The migration's parsers
(sql/00_setup.sql) already accept that form; nothing downstream can tell the two
sources apart. The column lists were checked identical (233 tables, 5,768
columns) before this was first used.

How:
1. ``pg_restore`` the dump's TABLE and TABLE DATA entries — nothing else: no
   functions, extensions, indexes or constraints, which the migration does not
   read — into a scratch schema of ``legacy_tqa``.
2. Rebuild each ``raw.<table>`` as TEXT from it, in its own transaction, and
   check the row count against the restored table.
3. Drop the scratch copy.

The raw tables are LOGGED. ``load_legacy.py`` made them UNLOGGED for speed, and
PostgreSQL empties unlogged tables after any unclean shutdown — which is how the
first load's copy was found empty.

    LEGACY_DATABASE_URL=postgresql://postgres:***@localhost:5432/legacy_tqa \\
        python load_backup.py "C:/path/TAQUALIVE.backup" [--pg-bin "C:/Program Files/PostgreSQL/18/bin"]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg
from psycopg import sql

SCRATCH = "public"          # where pg_restore puts the dump's tables (they are public.*)
DEFAULT_PG_BIN = r"C:\Program Files\PostgreSQL\18\bin"


def _arg(name: str, default: str | None = None) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _restore(url: str, dump: str, pg_bin: Path) -> None:
    """Restore only the tables and their data from the dump into SCRATCH."""
    toc = subprocess.run([str(pg_bin / "pg_restore.exe"), "-l", dump],
                         check=True, capture_output=True, text=True).stdout
    keep = [ln for ln in toc.splitlines()
            if not ln.startswith(";") and (" TABLE public " in ln or " TABLE DATA public " in ln)]
    if not keep:
        sys.exit("the dump has no public tables")
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False, encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
        listing = f.name
    info = psycopg.conninfo.conninfo_to_dict(url)
    env = dict(os.environ, PGPASSWORD=info.get("password") or "")
    try:
        print(f"restoring {sum(' TABLE public ' in k for k in keep)} tables from {dump} …", flush=True)
        t0 = time.time()
        subprocess.run([str(pg_bin / "pg_restore.exe"), "--no-owner", "--no-privileges",
                        "--exit-on-error", "-j", "4", "-L", listing,
                        "-h", info.get("host", "localhost"), "-p", str(info.get("port", 5432)),
                        "-U", info.get("user", "postgres"), "-d", info["dbname"], dump],
                       check=True, env=env)
        print(f"restored in {time.time() - t0:.0f}s", flush=True)
    finally:
        os.unlink(listing)


def _rebuild_raw(conn: psycopg.Connection) -> int:
    tables = [r[0] for r in conn.execute(
        "select table_name from information_schema.tables where table_schema=%s "
        "and table_type='BASE TABLE' order by table_name", (SCRATCH,))]
    conn.execute("create schema if not exists raw")
    conn.execute("""create table if not exists raw._load_log(
        tbl text, rows_loaded bigint, rows_expected bigint, status text, loaded_at timestamptz default now())""")
    bad = 0
    for t in tables:
        t0 = time.time()
        cols = conn.execute(
            "select column_name, data_type from information_schema.columns where table_schema=%s "
            "and table_name=%s order by ordinal_position", (SCRATCH, t)).fetchall()
        src, dst = sql.Identifier(SCRATCH, t), sql.Identifier("raw", t)

        # Every column as the export wrote it, which is PostgreSQL's OUTPUT form.
        # `::text` gives that for every type but one: a boolean casts to
        # 'true'/'false', while the export (and COPY) wrote 't'/'f' — and the
        # validator, at least, compares against 't' literally.
        def as_exported(name, dtype):
            c = sql.Identifier(name)
            if dtype == "boolean":
                return sql.SQL("case when {c} then 't' when not {c} then 'f' end").format(c=c)
            return sql.SQL("{}::text").format(c)
        with conn.transaction():
            expected = conn.execute(sql.SQL("select count(*) from {}").format(src)).fetchone()[0]
            conn.execute(sql.SQL("drop table if exists {}").format(dst))
            conn.execute(sql.SQL("create table {} ({})").format(
                dst, sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c, _ in cols)))
            conn.execute(sql.SQL("insert into {} select {} from {}").format(
                dst, sql.SQL(", ").join(as_exported(c, d) for c, d in cols), src))
            n = conn.execute(sql.SQL("select count(*) from {}").format(dst)).fetchone()[0]
            status = "ok" if n == expected else f"MISMATCH expected {expected}"
            bad += status != "ok"
            conn.execute("insert into raw._load_log(tbl, rows_loaded, rows_expected, status) values (%s,%s,%s,%s)",
                         (t, n, expected, status))
        print(f"{t:32s} {n:>10,d}  {status}  {time.time() - t0:6.1f}s", flush=True)
    return bad


def main() -> int:
    url = os.environ.get("LEGACY_DATABASE_URL")
    if not url or len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        print(__doc__)
        return 2
    dump = sys.argv[1]
    pg_bin = Path(_arg("--pg-bin", DEFAULT_PG_BIN))

    with psycopg.connect(url, autocommit=True) as conn:
        left = conn.execute("select count(*) from information_schema.tables where table_schema=%s",
                            (SCRATCH,)).fetchone()[0]
        if left:
            sys.exit(f"{SCRATCH} in this database already holds {left} tables — refusing to restore over them")

    _restore(url, dump, pg_bin)
    with psycopg.connect(url, autocommit=True) as conn:
        bad = _rebuild_raw(conn)
        # The scratch copy has done its job; raw.* is the source from here on.
        conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(SCRATCH)))
        conn.execute(sql.SQL("create schema {}").format(sql.Identifier(SCRATCH)))
    print("DONE" if not bad else f"DONE with {bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
