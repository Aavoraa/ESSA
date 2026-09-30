"""Stage 2 — build an empty ESSA target database the way ESSA builds one.

1. Creates the target database (e.g. ``essa_staging``) if it does not exist. With
   ``--recreate`` it is dropped first — but ONLY if it carries this migration's
   marker comment, so a database this script did not create can never be dropped
   by it.
2. Lets ESSA itself create its schema and its standard seeds, by importing
   ``backend/app/main.py`` against the target with the backend's own interpreter
   (``_boot_database`` → ``create_all`` + seeds). Nothing here defines a table.
3. Stages the legacy tables the transform reads into schema ``legacy`` of the
   target, reading them as ``legacy_reader`` — a role with SELECT on ``raw``
   only and read-only sessions — so the source cannot be written to.

    ESSA_DATABASE_URL=postgresql://postgres:***@localhost:5432/essa_staging \
    LEGACY_DATABASE_URL=postgresql://postgres:***@localhost:5432/legacy_tqa \
        python prepare_target.py [--recreate] [--save-base]
        python prepare_target.py --from-base          # fast re-run: clone the pristine base
        python prepare_target.py --stage-only --tables a,b
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

MARKER = "tqa-migration-target"
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent / "backend"


def _admin(url: str) -> str:
    return psycopg.conninfo.make_conninfo(url, dbname="postgres")


def ensure_target(url: str, recreate: bool) -> None:
    name = psycopg.conninfo.conninfo_to_dict(url)["dbname"]
    with psycopg.connect(_admin(url), autocommit=True) as c:
        row = c.execute("select shobj_description(oid, 'pg_database') from pg_database where datname=%s",
                        (name,)).fetchone()
        if row and recreate:
            if row[0] != MARKER:
                sys.exit(f"refusing to drop {name}: it was not created by this migration (no marker)")
            c.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            print(f"dropped {name}")
            row = None
        if not row:
            c.execute(sql.SQL("create database {} encoding 'UTF8' template template0").format(sql.Identifier(name)))
            c.execute(sql.SQL("comment on database {} is {}").format(sql.Identifier(name), sql.Literal(MARKER)))
            print(f"created {name}")


def boot_essa(url: str) -> None:
    py = BACKEND / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = BACKEND / ".venv" / "bin" / "python"
    env = dict(os.environ, ESSA_DATABASE_URL=url, ESSA_BOOT_MIGRATE="1")
    code = ("import app.main as m; "
            "print('boot seconds', m.BOOT_SECONDS, 'startup error', m.STARTUP_ERROR)")
    subprocess.run([str(py), "-c", code], cwd=BACKEND, env=env, check=True)


#: The legacy tables the transform reads, and the columns it reads from each
#: ("*" = all). Only what the mapping uses is staged: the two largest tables are
#: mostly a JSON snapshot column (lrinvoiceitems.itemdetail, billitems.attributes
#: is kept — it carries brand/size/design per sold piece).
LEGACY_TABLES = {
    "referencelist": "*", "company": "*", "tax": "*", "products": "*", "items": "*",
    "brand": "*", "supplier": "*", "suppliercompany": "*", "customer": "*",
    "employee": "*", "users": "*", "transport": "*", "agent": "*",
    "purchaseorder": "*", "purchaseorderitems": "*", "lrentry": "*", "lrinvoice": "*",
    "lrinvoicetax": "*", "lrinvoicereturn": "*", "lrinvoicereturnitems": "*",
    "supplierpayment": "*", "supplierpaymentinvoice": "*",
    "stock": "*", "salablegoods": "*", "stocktransaction": "*", "bundles": "*",
    "bill": "*", "billsettlement": "*", "billmaster": "*",
    "billsettlementmaster": ("id,code,billmastercode,locationid,counterid,cashierid,customerid,settlementon,"
                             "billamount,receivable,paidamount,paid_cash,paid_card,paid_upi,paid_voucher,"
                             "paid_credit,paid_debt,loan_paid,paid_refund,paid_discount,mode,isactive,createdon"),
    "billitems": "*",
    "delivery": "id,billid,billno,status,receivedby,receivedon,deliveredby,deliveredon,createdby,createdon,isactive",
    "lrinvoiceitems": ("id,isactive,createdon,modifiedon,itemid,productid,lrinvoiceid,designid,"
                       "buyingqty,buyingprice,basebuyingprice,mrp,sellingprice,actualqty,sellableqty,"
                       "damagedqty,returnqty,freeqty,hsncode,purchasediscount,purchasediscountpercentage,"
                       "purchasetax,purchasetaxpercentage,salestaxpercentage,amount,stocklocationid,"
                       "sno,taxid,ptaxid,barcodes,sourcesupplierid,weight"),
}


#: `--tables a,b` restages just those (e.g. after adding one to the list above)
ONLY_TABLES = (sys.argv[sys.argv.index("--tables") + 1].split(",") if "--tables" in sys.argv else [])


def _grant_reader(legacy_url: str, password: str) -> None:
    src = psycopg.conninfo.conninfo_to_dict(legacy_url)
    with psycopg.connect(legacy_url, autocommit=True) as c:
        if not c.execute("select 1 from pg_roles where rolname='legacy_reader'").fetchone():
            c.execute("create role legacy_reader login")
        c.execute(sql.SQL("alter role legacy_reader with login password {} "
                          "nosuperuser nocreatedb nocreaterole").format(sql.Literal(password)))
        c.execute("alter role legacy_reader set default_transaction_read_only = on")
        c.execute(sql.SQL("grant connect on database {} to legacy_reader").format(sql.Identifier(src["dbname"])))
        c.execute("grant usage on schema raw to legacy_reader")
        c.execute("grant select on all tables in schema raw to legacy_reader")
        c.execute("alter default privileges in schema raw grant select on tables to legacy_reader")


def stage_legacy(target_url: str, legacy_url: str) -> None:
    """Copy the tables the transform reads into ``legacy`` in the target.

    Read through ``legacy_reader`` — a role that can only SELECT from ``raw`` and
    whose sessions are read-only — so staging cannot change the source. (A
    postgres_fdw link would avoid the copy, but its DLL is blocked by Windows
    Application Control on this machine, and that policy is not ours to bend.)
    Values stay TEXT exactly as exported; typing happens in the transform.
    """
    password = secrets.token_urlsafe(24)          # rotated every run, never written anywhere
    _grant_reader(legacy_url, password)
    reader = psycopg.conninfo.make_conninfo(legacy_url, user="legacy_reader", password=password)
    with psycopg.connect(reader) as src, psycopg.connect(target_url, autocommit=True) as dst:
        src.read_only = True
        dst.execute("create schema if not exists legacy")
        only = set(ONLY_TABLES)
        for table, cols in LEGACY_TABLES.items():
            if only and table not in only:
                continue
            names = ([r[0] for r in src.execute(
                "select column_name from information_schema.columns where table_schema='raw' "
                "and table_name=%s order by ordinal_position", (table,))]
                if cols == "*" else [c.strip() for c in cols.split(",")])
            if not names:
                sys.exit(f"legacy table raw.{table} is missing — run load_legacy.py first")
            ident = sql.Identifier("legacy", table)
            col_sql = sql.SQL(", ").join(map(sql.Identifier, names))
            with dst.transaction():
                dst.execute(sql.SQL("drop table if exists {}").format(ident))
                dst.execute(sql.SQL("create table {} ({})").format(
                    ident, sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(n)) for n in names)))
                with src.cursor().copy(sql.SQL("copy (select {} from raw.{}) to stdout").format(
                        col_sql, sql.Identifier(table))) as out, \
                        dst.cursor().copy(sql.SQL("copy {} ({}) from stdin").format(ident, col_sql)) as inp:
                    for chunk in out:
                        inp.write(chunk)
            src.rollback()
            n = dst.execute(sql.SQL("select count(*) from {}").format(ident)).fetchone()[0]
            print(f"staged legacy.{table:24s} {n:>10,d}", flush=True)


def _base_name(url: str) -> str:
    return psycopg.conninfo.conninfo_to_dict(url)["dbname"] + "_base"


def save_base(url: str) -> None:
    """Keep a pristine copy (ESSA booted + legacy staged, nothing migrated) so a
    re-run can start from it in a minute instead of re-staging gigabytes."""
    name, base = psycopg.conninfo.conninfo_to_dict(url)["dbname"], _base_name(url)
    with psycopg.connect(_admin(url), autocommit=True) as c:
        row = c.execute("select shobj_description(oid, 'pg_database') from pg_database where datname=%s",
                        (base,)).fetchone()
        if row:
            if row[0] != MARKER:
                sys.exit(f"refusing to replace {base}: not created by this migration")
            c.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(base)))
        c.execute(sql.SQL("select pg_terminate_backend(pid) from pg_stat_activity where datname = {}")
                  .format(sql.Literal(name)))
        c.execute(sql.SQL("create database {} template {}").format(sql.Identifier(base), sql.Identifier(name)))
        c.execute(sql.SQL("comment on database {} is {}").format(sql.Identifier(base), sql.Literal(MARKER)))
    print(f"saved pristine base {base}")


def clone_from_base(url: str) -> None:
    """Recreate the target as a copy of its pristine base (see save_base)."""
    name, base = psycopg.conninfo.conninfo_to_dict(url)["dbname"], _base_name(url)
    with psycopg.connect(_admin(url), autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (base,)).fetchone():
            sys.exit(f"{base} does not exist — run once with --save-base")
        row = c.execute("select shobj_description(oid, 'pg_database') from pg_database where datname=%s",
                        (name,)).fetchone()
        if row:
            if row[0] != MARKER:
                sys.exit(f"refusing to drop {name}: it was not created by this migration (no marker)")
            c.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
        c.execute(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(base)))
        c.execute(sql.SQL("comment on database {} is {}").format(sql.Identifier(name), sql.Literal(MARKER)))
    print(f"recreated {name} from {base}")


def main() -> int:
    target, legacy = os.environ.get("ESSA_DATABASE_URL"), os.environ.get("LEGACY_DATABASE_URL")
    if not target or not legacy:
        print(__doc__)
        return 2
    if "--from-base" in sys.argv:
        clone_from_base(target)
        return 0
    if "--stage-only" not in sys.argv:
        ensure_target(target, "--recreate" in sys.argv)
        boot_essa(target)
    stage_legacy(target, legacy)
    if "--save-base" in sys.argv:
        save_base(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
