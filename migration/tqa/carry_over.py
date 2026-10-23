"""Stage 5 — after a re-migration, bring across what was set up IN ESSA since go-live.

A re-migration rebuilds ESSA's database from the legacy system, so anything
that was entered in ESSA itself after the previous run is not in it. This copies
the parts that can be copied without guessing — accounts and setup — from the
previous database into the rebuilt one, matched by NAME, never by id (ids are
reassigned on every rebuild):

    ESSA accounts      users            by username: password, role, access, active
    POS accounts       shop.users       by username: password, role, pay, active
    item master        master_records   the 'item' rows imported from essa-old-data
    dropdown options   attribute_options by catalogue code + attribute + value
    label templates    label_templates  by name
    category aliases   category_aliases by key
    agents, transports                  by name
    suppliers          suppliers        by GSTIN, else by name
    invoice formats    supplier_profiles by supplier + template key

Transactions made in ESSA since go-live (GRNs, LR entries, sales, transfers,
uploaded invoices) are NOT copied: each one moved stock in a ledger that the
rebuild has just recomputed from the legacy side, and replaying them would need
decisions per document. They are listed, one row each, in
``output/post_golive_transactions.csv`` to be re-entered.

Everything runs in ONE transaction on the target: it lands whole or not at all.
The source is only read.

    OLD_DATABASE_URL=postgresql://…/essa_before_20261008 \\
    ESSA_DATABASE_URL=postgresql://…/essa \\
        python carry_over.py --since "2026-09-23 12:17"      # go-live, UTC (ESSA stores UTC)
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

HERE = Path(__file__).resolve().parent


def _arg(name: str, default: str | None = None) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _j(v):
    """A json column value, ready to write back."""
    return None if v is None else Json(v)


def carry(src: psycopg.Connection, dst: psycopg.Connection, since: str) -> dict:
    n = {}

    # ---- warehouses and catalogues are matched by code ----
    old_wh = {r["id"]: r["code"] for r in src.execute("select id, code from warehouses")}
    new_wh = {r["code"]: r["id"] for r in dst.execute("select id, code from warehouses")}
    old_cat = {r["id"]: r["code"] for r in src.execute("select id, code from catalogues")}
    new_cat = {r["code"]: r["id"] for r in dst.execute("select id, code from catalogues")}

    def remap_perms(p):
        if not isinstance(p, dict) or not p.get("warehouses"):
            return p
        p = dict(p)
        p["warehouses"] = [new_wh[old_wh[w]] for w in p["warehouses"]
                           if w in old_wh and old_wh[w] in new_wh]
        return p

    # ---- ESSA accounts ----
    n["users updated"] = n["users added"] = 0
    have = {r["username"]: r["id"] for r in dst.execute("select id, username from users")}
    for u in src.execute("select * from users order by id"):
        vals = (u["password_hash"], u["role"], u["full_name"], u["active"], u["token_seed"],
                _j(remap_perms(u["permissions"])), u["last_login_at"], u["created_by"])
        if u["username"] in have:
            dst.execute("""update users set password_hash=%s, role=%s, full_name=%s, active=%s,
                           token_seed=%s, permissions=%s, last_login_at=%s, created_by=%s
                           where id=%s""", vals + (have[u["username"]],))
            n["users updated"] += 1
        else:
            dst.execute("""insert into users (password_hash, role, full_name, active, token_seed,
                           permissions, last_login_at, created_by, username, created_at)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        vals + (u["username"], u["created_at"]))
            n["users added"] += 1

    # ---- POS accounts ----
    n["POS users updated"] = n["POS users added"] = 0
    have = {r["username"]: r["id"] for r in dst.execute("select id, username from shop.users")}
    for u in src.execute("select * from shop.users order by id"):
        vals = (u["full_name"], u["email"], u["phone"], u["password_hash"], u["role"],
                u["salary"], u["commission_pct"], u["active"])
        if u["username"] in have:
            dst.execute("""update shop.users set full_name=%s, email=%s, phone=%s, password_hash=%s,
                           role=%s, salary=%s, commission_pct=%s, active=%s where id=%s""",
                        vals + (have[u["username"]],))
            n["POS users updated"] += 1
        else:
            dst.execute("""insert into shop.users (full_name, email, phone, password_hash, role,
                           salary, commission_pct, active, username, created_at)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        vals + (u["username"], u["created_at"]))
            n["POS users added"] += 1

    # ---- the item master imported from essa-old-data ----
    have = {r["k"] for r in dst.execute(
        "select data->>'old_id' as k from master_records where master='item'")}
    rows = [r for r in src.execute("select * from master_records where master='item' order by id")
            if (r["data"] or {}).get("old_id") not in have]
    with dst.cursor() as cur:
        cur.executemany("""insert into master_records (master, code, name, data, grids, matrix, active,
                           created_at, updated_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [(r["master"], r["code"], r["name"], _j(r["data"]), _j(r["grids"]),
                          _j(r["matrix"]), r["active"], r["created_at"], r["updated_at"]) for r in rows])
    n["item master rows"] = len(rows)

    # ---- dropdown options ----
    have = {(r["catalogue_id"], r["attr"], r["value"]) for r in dst.execute(
        "select catalogue_id, attr, value from attribute_options")}
    add = []
    for r in src.execute("select * from attribute_options"):
        cid = new_cat.get(old_cat.get(r["catalogue_id"])) if r["catalogue_id"] else None
        if r["catalogue_id"] and cid is None:
            continue
        if (cid, r["attr"], r["value"]) not in have:
            have.add((cid, r["attr"], r["value"]))
            add.append((cid, r["attr"], r["value"], r["sort"], r["created_at"]))
    with dst.cursor() as cur:
        cur.executemany("insert into attribute_options (catalogue_id, attr, value, sort, created_at) "
                        "values (%s,%s,%s,%s,%s)", add)
    n["dropdown options"] = len(add)

    # ---- label templates ----
    have = {r["name"] for r in dst.execute("select name from label_templates")}
    n["label templates"] = 0
    for r in src.execute("select * from label_templates order by id"):
        if r["name"] in have:
            continue
        dst.execute("""insert into label_templates (name, description, width_mm, height_mm, padding_mm,
                       border, target, font, elements, is_default, active, created_by, created_at, updated_at)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s,%s,%s)""",
                    (r["name"], r["description"], r["width_mm"], r["height_mm"], r["padding_mm"],
                     r["border"], r["target"], r["font"], _j(r["elements"]), r["active"],
                     r["created_by"], r["created_at"], r["updated_at"]))
        n["label templates"] += 1

    # ---- category aliases ----
    have = {r["key"] for r in dst.execute("select key from category_aliases")}
    n["category aliases"] = 0
    for r in src.execute("select * from category_aliases order by id"):
        if r["key"] in have:
            continue
        cid = new_cat.get(old_cat.get(r["catalogue_id"])) if r["catalogue_id"] else None
        dst.execute("""insert into category_aliases (key, catalogue_id, sample, category, section, source,
                       hits, created_at, updated_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (r["key"], cid, r["sample"], r["category"], r["section"], r["source"], r["hits"],
                     r["created_at"], r["updated_at"]))
        n["category aliases"] += 1

    # ---- agents and transports ----
    for t in ("agents", "transports"):
        have = {r["name"] for r in dst.execute(f"select name from {t}")}
        n[t] = 0
        for r in src.execute(f"select * from {t} order by id"):
            if r["name"] not in have:
                dst.execute(f"insert into {t} (name, phone, created_at) values (%s,%s,%s)",
                            (r["name"], r["phone"], r["created_at"]))
                n[t] += 1

    # ---- suppliers, then their trained invoice formats ----
    def skey(r):
        g = (r["gstin"] or "").strip().upper()
        return ("g", g) if g else ("n", (r["name"] or "").strip().upper())
    new_sup = {skey(r): r["id"] for r in dst.execute("select id, name, gstin from suppliers")}
    sup_map, n["suppliers"] = {}, 0
    for r in src.execute("select * from suppliers order by id"):
        k = skey(r)
        if k not in new_sup:
            new_sup[k] = dst.execute(
                """insert into suppliers (name, gstin, pan, state, state_code, address, phone, email,
                   bank, aliases, created_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (r["name"], r["gstin"], r["pan"], r["state"], r["state_code"], r["address"], r["phone"],
                 r["email"], _j(r["bank"]), _j(r["aliases"]), r["created_at"])).fetchone()["id"]
            n["suppliers"] += 1
        sup_map[r["id"]] = new_sup[k]
    have = {(r["supplier_id"], r["template_key"]) for r in dst.execute(
        "select supplier_id, template_key from supplier_profiles")}
    n["invoice formats"] = 0
    for r in src.execute("select * from supplier_profiles order by id"):
        sid = sup_map.get(r["supplier_id"])
        if sid is None or (sid, r["template_key"]) in have:
            continue
        dst.execute("""insert into supplier_profiles (supplier_id, template_key, version, is_active,
                       detect_gstin, detect_keywords, tax_mode, default_tax_rates, has_tds, column_map,
                       field_hints, uom_default, reference_example, trained_from_document_id, sample_count,
                       created_at, updated_at)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,null,%s,%s,%s)""",
                    (sid, r["template_key"], r["version"], r["is_active"], r["detect_gstin"],
                     _j(r["detect_keywords"]), r["tax_mode"], _j(r["default_tax_rates"]), r["has_tds"],
                     _j(r["column_map"]), _j(r["field_hints"]), r["uom_default"],
                     _j(r["reference_example"]), r["sample_count"], r["created_at"], r["updated_at"]))
        have.add((sid, r["template_key"]))
        n["invoice formats"] += 1

    # Every id sequence past what was inserted — an explicit-id insert elsewhere
    # must never collide with these rows.
    for t in ("users", "shop.users", "master_records", "attribute_options", "label_templates",
              "category_aliases", "agents", "transports", "suppliers", "supplier_profiles"):
        dst.execute(f"select setval(pg_get_serial_sequence('{t}', 'id'), "
                    f"greatest((select coalesce(max(id), 0) from {t}), 1))")
    return n


def report(src: psycopg.Connection, since: str) -> int:
    """The ESSA-only transactions since go-live, one row each, for re-entry."""
    q = [
        ("GRN", "select grn_no as ref, invoice_number as detail, invoice_date::text as date, "
                "status, created_at from purchases where created_at >= %s"),
        ("LR entry", "select lr_entry_no as ref, coalesce(supplier_name,'') || ' · LR ' || coalesce(lr_no,'') "
                     "as detail, lr_entry_date::text as date, '' as status, created_at from lr_entries "
                     "where created_at >= %s"),
        ("Stock outward", "select code as ref, coalesce(to_destination,'') as detail, date::text as date, "
                          "status, created_at from stock_outwards where created_at >= %s"),
        ("Uploaded invoice", "select filename as ref, document_type as detail, '' as date, status, "
                             "uploaded_at as created_at from documents where uploaded_at >= %s"),
        ("Store sale", "select invoice_number as ref, total::text as detail, invoice_date::text as date, "
                       "payment_status as status, invoice_date as created_at from shop.invoices "
                       "where invoice_number like 'INV-%%'"),
    ]
    out = HERE / "output" / "post_golive_transactions.csv"
    rows = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "reference", "detail", "date", "status", "entered_at_utc"])
        for kind, sql_ in q:
            try:
                for r in src.execute(sql_, (since,) if "%s" in sql_ else ()):
                    w.writerow([kind, r["ref"], r["detail"], r["date"], r["status"], r["created_at"]])
                    rows += 1
            except psycopg.Error as e:              # a column this install lacks — note it, carry on
                src.rollback()
                w.writerow([kind, "", f"could not list: {e.__class__.__name__}", "", "", ""])
    print(f"wrote {out} ({rows} rows)")
    return rows


def main() -> int:
    old, new, since = os.environ.get("OLD_DATABASE_URL"), os.environ.get("ESSA_DATABASE_URL"), _arg("--since")
    if not old or not new or not since:
        print(__doc__)
        return 2
    with psycopg.connect(old, row_factory=dict_row) as src, \
            psycopg.connect(new, row_factory=dict_row) as dst:
        src.read_only = True
        if "--report-only" in sys.argv:          # just the re-entry list; writes nothing
            report(src, since)
            return 0
        with dst.transaction():
            counts = carry(src, dst, since)
        for k, v in counts.items():
            print(f"  {k:22s} {v:>8,d}")
        report(src, since)
    return 0


if __name__ == "__main__":
    sys.exit(main())
