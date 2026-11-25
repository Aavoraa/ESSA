"""Stage 1, from JSON — load Quanta / TAQUALIVE data exported as JSON into ``legacy_tqa.raw``.

The third way in, beside the CSV export (``load_legacy.py``) and the database
backup (``load_backup.py``). It produces exactly what they produce — one
``raw.<table>`` per legacy table, every column TEXT, in PostgreSQL's output form
— so stages 2 and 3 (prepare_target.py, migrate.py, validate.py) run unchanged
and cannot tell which source they were fed.

What it accepts (the table name comes from the file name, lower-cased):

    bill.json            [ {"billid": 1, "billno": "TG/1", ...}, ... ]
    bill.json            {"bill": [ ... ]}   or   {"data"|"rows"|"records"|"items": [ ... ]}
    bill.jsonl / .ndjson one row object per line
    export.json          {"bill": [ ... ], "billitems": [ ... ], ...}   (several tables)
    a folder or a .zip   of any of the above

How values become TEXT (what the CSV export and pg_dump wrote):
    true/false -> t/f        null -> NULL         numbers -> exact digits, no 1e+05
    "2024-05-01T10:00:00"    -> "2024-05-01 10:00:00"
    "2024-05-01T04:30:00Z"   -> "2024-05-01 10:00:00"  (an explicit offset is turned
                                into IST local time, which legacy timestamps are)
    nested objects / lists   -> their JSON text

Columns: a JSON export usually leaves out keys whose value is null, so a table's
columns are the union of every row's keys. If ``raw.<table>`` already exists
from an earlier load, its columns are kept too (as NULL when the JSON lacks
them) so the migration SQL still finds every column it reads; each such gap is
reported, since a column the JSON never has may mean the export is incomplete.

Re-runnable: every table is dropped and reloaded in its own transaction, the
row count is checked against the rows read, and the result goes in
``raw._load_log``. Big files are streamed, not read into memory whole.

    LEGACY_DATABASE_URL=postgresql://postgres:***@localhost:5432/legacy_tqa \\
        python load_json.py "C:/path/quanta-export" [table ...] [--dry-run]

``--dry-run`` reads and converts everything and prints what would be loaded,
without connecting to a database. Needs psycopg (3), like the other loaders.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile
from decimal import Decimal
from typing import Iterator

IST = dt.timezone(dt.timedelta(minutes=330))
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?$")
OFFSET_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")
WRAPPERS = ("data", "rows", "records", "items", "result", "results")
SUFFIXES = (".json", ".jsonl", ".ndjson")
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ------------------------------------------------------------- value form
def as_text(v, fixes: dict) -> str | None:
    """One JSON value in the form PostgreSQL's COPY would have written it."""
    if v is None:
        return None
    if v is True:
        return "t"
    if v is False:
        return "f"
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    s = str(v)
    if len(s) >= 16 and TS_RE.match(s):
        if OFFSET_RE.search(s[10:]):
            # A plain ::timestamp cast would DROP the offset, so "04:45Z" would
            # read as 04:45 IST. Turn it into the IST wall-clock time instead.
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            if re.search(r"[+-]\d{2}$", iso):                 # "+05" -> "+05:00"
                iso += ":00"
            when = dt.datetime.fromisoformat(iso.replace(" ", "T", 1))
            s = when.astimezone(IST).replace(tzinfo=None).isoformat(sep=" ")
            fixes["offset"] = fixes.get("offset", 0) + 1
        else:
            s = s.replace("T", " ", 1)
    return s


# ---------------------------------------------------------------- reading
def iter_array(fh: io.TextIOBase, chunk: int = 1 << 20) -> Iterator[dict]:
    """Stream the objects of a top-level JSON array without loading the file."""
    dec = json.JSONDecoder(parse_float=Decimal)
    buf, pos, started = "", 0, False
    while True:
        more = fh.read(chunk)
        buf = buf[pos:] + more
        pos = 0
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if not started:
                if pos < len(buf):
                    if buf[pos] != "[":
                        raise ValueError("not a JSON array")
                    started, pos = True, pos + 1
                    continue
                break
            if pos < len(buf) and buf[pos] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if not more:
                    raise
                break                                   # need more text
            yield obj
            pos = end
        if not more:
            if started:
                raise ValueError("JSON array is not closed")
            return


def first_char(open_fn) -> str:
    """The first character that is not white space (or a byte-order mark)."""
    with open_fn() as fh:
        while True:
            c = fh.read(1)
            if not c or not (c.isspace() or c == "\ufeff"):
                return c


def tables_in(name: str, open_fn) -> list[tuple[str, callable]]:
    """[(table, rows_fn)] found in one file. rows_fn() yields row dicts and can
    be called twice (once for columns, once to load), re-reading the file."""
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    table = stem.lower()
    if ext in (".jsonl", ".ndjson"):
        def rows():
            with open_fn() as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line, parse_float=Decimal)
        return [(table, rows)]
    if first_char(open_fn) == "[":
        def rows():
            with open_fn() as fh:
                yield from iter_array(fh)
        return [(table, rows)]
    with open_fn() as fh:                               # an object: small enough to load
        doc = json.load(fh, parse_float=Decimal)
    if not isinstance(doc, dict):
        raise ValueError(f"{base}: expected a list of rows or an object of tables")
    if "rows" in doc and isinstance(doc.get("table"), str):
        return [(doc["table"].lower(), lambda d=doc: iter(d["rows"]))]
    lists = {k: v for k, v in doc.items() if isinstance(v, list)}
    for w in WRAPPERS:
        if len(lists) == 1 and w in lists:
            return [(table, lambda v=lists[w]: iter(v))]
    if table in lists and len(lists) == 1:
        return [(table, lambda v=lists[table]: iter(v))]
    if not lists:
        raise ValueError(f"{base}: no list of rows found")
    return [(k.lower(), lambda v=v: iter(v)) for k, v in lists.items()]


def discover(path: str) -> list[tuple[str, str, callable]]:
    """[(table, where, rows_fn)] for a file, folder or zip."""
    found = []
    if os.path.isdir(path):
        for base, _, files in os.walk(path):
            for f in sorted(files):
                if f.lower().endswith(SUFFIXES) and not f.startswith(("_", ".")):
                    full = os.path.join(base, f)
                    opener = lambda p=full: open(p, encoding="utf-8-sig")  # noqa: E731
                    found += [(t, f, r) for t, r in tables_in(f, opener)]
    elif zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        for m in sorted(zf.namelist()):
            leaf = m.rsplit("/", 1)[-1]
            if leaf.lower().endswith(SUFFIXES) and not leaf.startswith(("_", ".")):
                opener = lambda m=m: io.TextIOWrapper(zf.open(m), "utf-8-sig")  # noqa: E731
                found += [(t, m, r) for t, r in tables_in(leaf, opener)]
    elif os.path.isfile(path):
        opener = lambda: open(path, encoding="utf-8-sig")       # noqa: E731
        found += [(t, os.path.basename(path), r) for t, r in tables_in(path, opener)]
    else:
        raise FileNotFoundError(path)
    names = [t for t, _, _ in found]
    dup = sorted({t for t in names if names.count(t) > 1})
    if dup:
        raise ValueError(f"table(s) found in more than one file: {', '.join(dup)}")
    bad = [t for t in names if not NAME_RE.match(t)]
    if bad:
        raise ValueError(f"not usable as table names: {', '.join(bad)}")
    return found


def scan(rows_fn) -> tuple[list[str], int]:
    """Column order (first seen) and row count, reading the rows once."""
    cols, seen, n = [], set(), 0
    for row in rows_fn():
        if not isinstance(row, dict):
            raise ValueError(f"row {n + 1} is not an object")
        for k in row:
            lk = str(k).lower()
            if lk not in seen:
                seen.add(lk)
                cols.append(lk)
        n += 1
    return cols, n


# ------------------------------------------------------------------ loading
def load_table(conn, table, rows_fn, cols, expected, keep_cols, fixes):
    from psycopg import sql
    missing = [c for c in keep_cols if c not in cols]
    allcols = cols + missing
    with conn.transaction():
        conn.execute(sql.SQL("drop table if exists raw.{}").format(sql.Identifier(table)))
        conn.execute(sql.SQL("create table raw.{} ({})").format(      # LOGGED, see load_backup
            sql.Identifier(table),
            sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in allcols)))
        copy_sql = sql.SQL("copy raw.{} ({}) from stdin").format(
            sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in cols))
        with conn.cursor().copy(copy_sql) as cp:
            for row in rows_fn():
                low = {str(k).lower(): v for k, v in row.items()}
                cp.write_row([as_text(low.get(c), fixes) for c in cols])
        n = conn.execute(sql.SQL("select count(*) from raw.{}").format(sql.Identifier(table))).fetchone()[0]
        status = "ok" if n == expected else f"MISMATCH expected {expected}"
        if missing:
            status += f" (no JSON values for {len(missing)} earlier column(s): {', '.join(missing[:8])}"
            status += "…)" if len(missing) > 8 else ")"
        conn.execute("insert into raw._load_log(tbl, rows_loaded, rows_expected, status) values (%s,%s,%s,%s)",
                     (table, n, expected, status))
    return n, status


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    url = os.environ.get("LEGACY_DATABASE_URL")
    if not args or (not url and not dry):
        print(__doc__)
        return 2
    source, only = args[0], {a.lower() for a in args[1:]}
    try:
        found = [f for f in discover(source) if not only or f[0] in only]
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"cannot read {source}: {e}")
        return 2
    if not found:
        print("no JSON tables found" + (f" named {', '.join(sorted(only))}" if only else ""))
        return 2

    conn = None
    if not dry:
        import psycopg
        from load_legacy import _ensure_database       # same "create if missing" as the CSV loader
        _ensure_database(url)
        conn = psycopg.connect(url, autocommit=True)
        conn.execute("create schema if not exists raw")
        conn.execute("""create table if not exists raw._load_log(
            tbl text, rows_loaded bigint, rows_expected bigint, status text, loaded_at timestamptz default now())""")

    bad = 0
    try:
        for table, where, rows_fn in found:
            t0 = time.time()
            fixes: dict = {}
            try:
                cols, expected = scan(rows_fn)
                if not cols:
                    print(f"{table:32s} {'':>10s}  skipped: no rows ({where})")
                    continue
                if dry:
                    for row in rows_fn():
                        low = {str(k).lower(): v for k, v in row.items()}
                        for c in cols:
                            as_text(low.get(c), fixes)
                    n, status = expected, f"would load {len(cols)} columns"
                else:
                    keep = [r[0] for r in conn.execute(
                        "select column_name from information_schema.columns where table_schema='raw' "
                        "and table_name=%s order by ordinal_position", (table,))]
                    n, status = load_table(conn, table, rows_fn, cols, expected, keep, fixes)
            except (ValueError, json.JSONDecodeError) as e:
                n, status = 0, f"FAILED: {e}"
            if fixes.get("offset"):
                status += f"; {fixes['offset']} timestamp(s) with a time zone turned into IST"
            bad += status.startswith(("MISMATCH", "FAILED"))
            print(f"{table:32s} {n:>10,d}  {status}  {time.time() - t0:6.1f}s", flush=True)
    finally:
        if conn is not None:
            conn.close()
    print("DONE" if not bad else f"DONE with {bad} problem table(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
