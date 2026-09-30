"""68 · Legacy logins → shop (POS) accounts.

Every legacy login gets a shop account, because every legacy bill names the
login that rang it and the shop requires that (Invoice.cashier_id NOT NULL).
Password: TQA_TEMP_PASSWORD, hashed the way the shop's own User.set_password
does it (werkzeug.generate_password_hash).

Role, from the legacy role and the screens it landed on:
  1 (admins)            → admin
  3 (settlement desk), 11 (accounts / audit) → manager
  everyone else (billing, floor, warehouse)  → cashier

One inactive "LEGACY-UNKNOWN" account stands in as cashier on the few bills
whose creator is not a legacy login, so those bills are kept, not dropped.
"""
import json
import os
import sys

from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

ROLE = {"1": "admin", "3": "manager", "11": "manager"}


def main():
    password = os.environ.get("TQA_TEMP_PASSWORD", "")
    if len(password) < 6:
        sys.exit("TQA_TEMP_PASSWORD must be set (6+ characters)")
    url = os.environ["ESSA_DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    eng = create_engine(url)
    hashed = generate_password_hash(password)
    with eng.begin() as c:
        rows = c.execute(text(
            "select m.old_id, m.username, u.role, coalesce(migration.bool(u.isactive), true) active, "
            "coalesce(e.name, m.username) full_name, migration.utc(u.createdon) created, "
            "migration.txt(le.contactno) phone, migration.txt(le.emailid) email "
            "from migration.map_user m join legacy.users u on migration.int(u.id) = m.old_id "
            "left join migration.map_employee e on e.old_id = migration.int(u.employeeid) "
            "left join legacy.employee le on migration.int(le.id) = migration.int(u.employeeid) "
            "order by m.old_id")).mappings().all()
        taken = {r[0] for r in c.execute(text("select username from shop.users"))}
        for r in rows:
            name = r["username"][:64]
            if name in taken:
                name = f"{name[:58]}-{r['old_id']}"
            taken.add(name)
            new_id = c.execute(text(
                "insert into shop.users (username, full_name, email, phone, password_hash, role, "
                "salary, commission_pct, active, created_at) values "
                "(:u, :f, :e, :p, :h, :r, 0, 0, :a, coalesce(:c, now())) returning id"),
                {"u": name, "f": (r["full_name"] or name)[:128], "e": (r["email"] or None),
                 "p": (r["phone"] or None) and r["phone"][:32], "h": hashed,
                 "r": ROLE.get(r["role"], "cashier"), "a": bool(r["active"]),
                 "c": r["created"]}).scalar()
            c.execute(text("update migration.map_user set shop_user_id = :s where old_id = :o"),
                      {"s": new_id, "o": r["old_id"]})
        unknown = c.execute(text(
            "insert into shop.users (username, full_name, password_hash, role, salary, commission_pct, "
            "active, created_at) values ('LEGACY-UNKNOWN', 'Legacy bill — creator not recorded', "
            ":h, 'cashier', 0, 0, false, now()) returning id"), {"h": generate_password_hash(os.urandom(16).hex())}).scalar()
        c.execute(text("insert into migration.map_user(old_id, username, shop_user_id) values (0, 'LEGACY-UNKNOWN', :s)"),
                  {"s": unknown})
    print(f"    shop accounts created: {len(rows)} (+ inactive LEGACY-UNKNOWN placeholder)")


if __name__ == "__main__":
    main()
