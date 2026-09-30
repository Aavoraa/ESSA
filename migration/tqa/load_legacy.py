"""Stage 1 — load the legacy TQA CSV export, unmodified, into its own database.

The export (TQA - Data.zip, split into part_00..part_47) is one zip of CSVs, one
per legacy table. Every CSV is COPYed verbatim into ``raw.<table>`` of the
source database (default ``legacy_tqa``). Columns are kept as TEXT here on
purpose: this layer is an exact, auditable copy of the export. Typing happens in
the transform stage, which reads ``raw`` and writes ESSA's real typed tables.

Re-runnable: each table is dropped and reloaded inside its own transaction, so a
failed table never leaves a half-loaded copy behind. Row counts are checked
against the export's own manifest (_MANIFEST_row_counts.csv) and recorded in
``raw._load_log``.

    LEGACY_DATABASE_URL=postgresql://postgres:***@localhost:5432/legacy_tqa \
        python load_legacy.py "path/to/tqa.zip" [table ...]
"""
from __future__ import annotations

import csv
import io
import os
import sys
import time
import zipfile

import psycopg
from psycopg import sql

CHUNK = 8 * 1024 * 1024


def _ensure_database(url: str) -> None:
    info = psycopg.conninfo.conninfo_to_dict(url)
    name = info["dbname"]
    admin = psycopg.conninfo.make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (name,)).fetchone():
            c.execute(sql.SQL("create database {} encoding 'UTF8' template template0").format(sql.Identifier(name)))
            print(f"created database {name}")


def _manifest(zf: zipfile.ZipFile) -> dict[str, int]:
    with zf.open("_MANIFEST_row_counts.csv") as f:
        return {r["table"]: int(r["rows"]) for r in csv.DictReader(io.TextIOWrapper(f, "utf-8"))}


def load_table(conn: psycopg.Connection, zf: zipfile.ZipFile, member: str, expected: int | None) -> tuple[int, str]:
    table = member[:-4]
    with zf.open(member) as f:
        header = f.readline().decode("utf-8-sig").rstrip("\r\n")
        cols = next(csv.reader([header]))
        with conn.transaction():
            conn.execute(sql.SQL("drop table if exists raw.{}").format(sql.Identifier(table)))
            conn.execute(sql.SQL("create unlogged table raw.{} ({})").format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in cols)))
            copy_sql = sql.SQL("copy raw.{} from stdin with (format csv)").format(sql.Identifier(table))
            with conn.cursor().copy(copy_sql) as cp:
                while chunk := f.read(CHUNK):
                    cp.write(chunk)
            n = conn.execute(sql.SQL("select count(*) from raw.{}").format(sql.Identifier(table))).fetchone()[0]
            status = "ok" if expected is None or n == expected else f"MISMATCH expected {expected}"
            conn.execute("insert into raw._load_log(tbl, rows_loaded, rows_expected, status) values (%s,%s,%s,%s)",
                         (table, n, expected, status))
    return n, status


def main() -> int:
    url = os.environ.get("LEGACY_DATABASE_URL")
    if not url or len(sys.argv) < 2:
        print(__doc__)
        return 2
    only = set(sys.argv[2:])
    _ensure_database(url)
    zf = zipfile.ZipFile(sys.argv[1])
    manifest = _manifest(zf)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("create schema if not exists raw")
        conn.execute("""create table if not exists raw._load_log(
            tbl text, rows_loaded bigint, rows_expected bigint, status text, loaded_at timestamptz default now())""")
        members = sorted((m for m in zf.namelist() if m.endswith(".csv") and not m.startswith("_")),
                         key=lambda m: zf.getinfo(m).file_size)
        bad = 0
        for m in members:
            if only and m[:-4] not in only:
                continue
            t0 = time.time()
            n, status = load_table(conn, zf, m, manifest.get(m[:-4]))
            bad += status != "ok"
            print(f"{m[:-4]:32s} {n:>10,d}  {status}  {time.time() - t0:6.1f}s", flush=True)
    print("DONE" if not bad else f"DONE with {bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
