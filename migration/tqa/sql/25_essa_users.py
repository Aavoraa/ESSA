"""25 · Legacy logins → ESSA (warehouse) accounts.

Legacy passwords are stored encrypted by the old system and cannot be carried
over, so every migrated account is given the one temporary password in
TQA_TEMP_PASSWORD, hashed by ESSA's own services/users.hash_password. People
change it from the app (Change Password) on first sign-in.

Who gets a WAREHOUSE account: anyone the legacy system let into warehouse work —
an LR / purchase / supplier-payment permission, or a back-office role (1 admin,
4 purchase, 7 warehouse, 11 accounts). Billing-counter staff (role 2) sell at
the shop and get a shop login in step 70 instead.

Role, from the legacy role: 1 → admin (setup + money screens, as in legacy).
Everyone else → user. Nobody is made superadmin: that seat stays with ESSA's own
seeded account. Inactive legacy logins are created inactive.
"""
import json
import os
import sys

from sqlalchemy import text

from app import models
from app.database import SessionLocal
from app.services.users import hash_password, new_seed

WAREHOUSE_PERMS = {"LRENTRY", "LRINVOICE", "DIRECTPURCHASE", "PURCHASEORDER", "SUPPLIERPAYMENT"}
BACK_OFFICE_ROLES = {"1", "4", "7", "11"}


def main():
    password = os.environ.get("TQA_TEMP_PASSWORD", "")
    if len(password) < 6:
        sys.exit("TQA_TEMP_PASSWORD must be set (6+ characters) — see migration/tqa/README.md")
    db = SessionLocal()
    db.execute(text("create table if not exists migration.map_user ("
                    "old_id bigint primary key, username text not null, "
                    "essa_user_id int, shop_user_id int)"))
    rows = db.execute(text(
        "select migration.int(id) id, btrim(username) username, role, "
        "coalesce(migration.bool(isactive), true) active, permissions, "
        "migration.utc(createdon) created, migration.int(employeeid) employee "
        "from legacy.users order by migration.int(id)")).mappings().all()
    names = {r[0] for r in db.execute(text("select username from users"))}
    hashed = hash_password(password)       # one hash: every account shares the temp password
    made = 0
    for r in rows:
        username = r["username"]
        if username in names:               # a second legacy login with the same name
            new_name = f"{username}-{r['id']}"
            db.execute(text("insert into migration.exceptions(entity, old_id, kind, reason, detail) "
                            "values ('users', :id, 'review', :why, cast(:d as jsonb))"),
                       {"id": str(r["id"]), "why": "Username already taken by another legacy login; "
                        "renamed so both can exist.", "d": json.dumps({"legacy": username, "essa": new_name})})
            username = new_name
        names.add(username)
        try:
            perms = {p.get("code") for p in json.loads(r["permissions"] or "[]") if p.get("HasPermission")}
        except ValueError:
            perms = set()
        emp = db.execute(text("select name from migration.map_employee where old_id = :e"),
                         {"e": r["employee"]}).scalar()
        essa_id = None
        if r["role"] in BACK_OFFICE_ROLES or perms & WAREHOUSE_PERMS:
            u = models.User(username=username, password_hash=hashed,
                            role="admin" if r["role"] == "1" else "user",
                            full_name=emp or username, active=bool(r["active"]),
                            token_seed=new_seed(), created_by="legacy-migration",
                            created_at=r["created"])
            db.add(u)
            db.flush()
            essa_id = u.id
            made += 1
        db.execute(text("insert into migration.map_user(old_id, username, essa_user_id) "
                        "values (:o, :u, :e)"), {"o": r["id"], "u": username, "e": essa_id})
    db.commit()
    print(f"    ESSA accounts created: {made} of {len(rows)} legacy logins")


if __name__ == "__main__":
    main()
