"""
Accounts, passwords and sign-in tokens.

Four ranked roles. Everything the app gates on is a comparison against this
rank, never a string equality test, so adding a tier later is one line here
rather than a hunt through the routers:

    user        the floor — receive, count, scan, print, dispatch
    admin       the floor plus the setup it works against, and the money screens
    superadmin  all of it, plus this table and the server's own settings
    superboss   all of it, plus the one thing a super admin cannot do: manage a
                Super Boss. The owner's seat — see `may_manage` below.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt, using only the
standard library — the deployment is a laptop or a small server and adding a
bcrypt build step to it buys nothing here. Tokens are HMACs rather than rows in
a session table: the phone is offline half the day and a stateless token
survives a server restart, which a session row would not. Revocation still
works because the user's `token_seed` is inside the signature — change the seed
and every token already issued stops verifying.
"""
import datetime as dt
import hashlib
import hmac
import os
import secrets

from sqlalchemy.orm import Session

from ..config import AUTH_SECRET, SEED_ACCOUNTS, SUPERBOSS_SEED
from ..models import User
from . import permissions

ROLES = ("user", "admin", "superadmin", "superboss")
ROLE_RANK = {"user": 1, "admin": 2, "superadmin": 3, "superboss": 4}
ROLE_LABEL = {"user": "User", "admin": "Admin", "superadmin": "Super Admin",
              "superboss": "Super Boss"}


def rank(role) -> int:
    return ROLE_RANK.get(role or "", 0)


def may_manage(actor_role, target_role) -> bool:
    """Whether one account may change another at all — its role, its access, its
    password, whether it can sign in.

    Only an equal or higher rank may. Without this, the ladder stops at the rung
    that can open Users & Access: a super admin could reset the Super Boss's
    password and sign in as them, and the top of the hierarchy would be a label.
    """
    return rank(actor_role) >= rank(target_role)


def has_active(db: Session, role: str) -> bool:
    return db.query(User).filter(User.role == role,
                                 User.active == True).first() is not None  # noqa: E712


def grantable_roles(db: Session, actor_role) -> list:
    """The roles this account may give someone: its own rank and below.

    With one exception, which is how the first Super Boss comes to exist without
    a default password shipped in a file: while NO active Super Boss exists, a
    super admin may appoint one. The moment somebody holds the seat, only they
    (or another Super Boss) can hand it out again.
    """
    r = rank(actor_role)
    out = [x for x in ROLES if ROLE_RANK[x] <= r]
    if ("superboss" not in out and r >= ROLE_RANK["superadmin"]
            and not has_active(db, "superboss")):
        out.append("superboss")
    return out

_PBKDF2_ROUNDS = 120_000


# --------------------------------------------------------------------------
#  passwords
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                 _PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                   bytes.fromhex(salt), int(rounds)).hex()
    except (ValueError, AttributeError):
        return False
    # constant time: a timing difference here leaks how much of the digest matched
    return hmac.compare_digest(calc, digest)


def password_problem(password: str) -> str:
    """Why this password is not acceptable, or "" if it is. Deliberately mild —
    this is a warehouse floor, and a rule strict enough to force a sticky note
    on the monitor is worse than a short password typed from memory."""
    if len(password or "") < 6:
        return "Password must be at least 6 characters"
    return ""


# --------------------------------------------------------------------------
#  tokens
# --------------------------------------------------------------------------

def _sign(payload: str, seed: str) -> str:
    key = f"{AUTH_SECRET}:{seed or ''}".encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


#: How long a token is honoured after it was issued, checked here on the server
#: rather than left to the cookie's own expiry — a token copied out of a browser
#: does not carry the browser's expiry with it.
TOKEN_MAX_AGE = 60 * 60 * 24 * 30

#: Marks the current token shape. The first shape began with the username, so a
#: stolen token announced exactly whose session it was ("superadmin.…"); those
#: no longer verify, and everybody signs in once more after the upgrade.
_TOKEN_VERSION = "v2"


def mint_token(user: User) -> str:
    """`v2.<user id>.<issued-at>.<signature>`. Says nothing about whose account
    or what rank — the row is looked up by id and the role read from it — and
    the signature is an HMAC keyed by the server's ESSA_AUTH_SECRET together
    with this user's random token_seed, so no part can be edited or forged."""
    issued = str(int(dt.datetime.utcnow().timestamp()))
    payload = f"{_TOKEN_VERSION}.{user.id}.{issued}"
    return f"{payload}.{_sign(payload, user.token_seed)}"


def resolve_token(db: Session, token: str):
    """The User this token names, or None. Deactivating an account takes effect
    here on the next request, because the row is re-read every time rather than
    trusted from the token's contents."""
    if not token or token.count(".") != 3:
        return None
    version, uid, issued, sig = token.split(".")
    if version != _TOKEN_VERSION or not uid.isdigit() or not issued.isdigit():
        return None
    age = dt.datetime.utcnow().timestamp() - int(issued)
    if age < -300 or age > TOKEN_MAX_AGE:
        return None
    user = db.get(User, int(uid))
    if not user or not user.active:
        return None
    if not hmac.compare_digest(_sign(f"{version}.{uid}.{issued}", user.token_seed), sig):
        return None
    return user


# --------------------------------------------------------------------------
#  the table itself
# --------------------------------------------------------------------------

def new_seed() -> str:
    return secrets.token_hex(16)


def create_user(db: Session, username: str, password: str, role: str,
                full_name: str = "", created_by: str = "") -> User:
    user = User(username=username.strip(), password_hash=hash_password(password),
                role=role, full_name=(full_name or "").strip() or None,
                active=True, token_seed=new_seed(), created_by=created_by or None)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def set_password(db: Session, user: User, password: str) -> None:
    """Changing a password also rotates the seed, which signs out every device
    that was holding a token for this account — including, on a reset, whoever
    the super admin is resetting it away from."""
    user.password_hash = hash_password(password)
    user.token_seed = new_seed()
    db.commit()


def out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "role_label": ROLE_LABEL.get(user.role, user.role),
        "full_name": user.full_name or "",
        "active": bool(user.active),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "created_by": user.created_by or "",
        # What this account may do screen by screen. Empty means the role decides
        # on its own — see services/permissions.has_map, which is the distinction
        # the whole feature turns on.
        "permissions": permissions.normalise(user.permissions),
    }


def seed(db: Session) -> None:
    """Make sure there is a way in on a fresh database, and on an existing one
    that predates this table.

    The accounts and passwords are the ones this install was already configured
    with (ESSA_ADMIN_* / ESSA_USER_*, defaults unchanged), plus a super admin —
    so upgrading does not lock the warehouse out of its own app on Monday
    morning. Only missing rows are created: a password changed in the app is
    never reverted to the environment's value on the next restart.
    """
    existing = {row[0] for row in db.query(User.username).all()}
    for username, spec in SEED_ACCOUNTS.items():
        if not username or username in existing:
            continue
        # A blank password would hash and store like any other, and then let
        # anyone in as that account with nothing typed. config._env already
        # turns an empty variable back into the default; this is the second
        # lock, because seeding is the one path that does not go through
        # password_problem and an account nobody can lock is worth two.
        if not (spec.get("password") or "").strip():
            raise RuntimeError(
                f"Refusing to seed '{username}' with an empty password — "
                f"unset its ESSA_*_PASSWORD variable to use the default.")
        create_user(db, username, spec["password"], spec["role"],
                    full_name=spec.get("full_name", ""), created_by="system")

    # The Super Boss is seeded ONLY when the environment names a password for
    # it. The three accounts above ship defaults because an install needs a way
    # in; the top seat does not, and a well-known password on the one account that
    # can manage everyone else would be a door left open on every deployment that
    # upgraded. Without the variables, a super admin appoints the first Super Boss
    # in Users & Access (see grantable_roles).
    boss_user, boss_password = SUPERBOSS_SEED
    if boss_user and boss_password and boss_user not in existing \
            and not has_active(db, "superboss"):
        create_user(db, boss_user, boss_password, "superboss",
                    full_name="Super Boss", created_by="system")

    # A database with no super admin has no way to reach user management, which
    # would make the tier unusable on any install that upgraded into it. If the
    # seeded super admin name is taken by an older account, promote it rather
    # than creating a second one.
    if not db.query(User).filter(User.role == "superadmin", User.active == True).first():  # noqa: E712
        name = os.environ.get("ESSA_SUPERADMIN_USER", "superadmin")
        row = db.query(User).filter(User.username == name).first()
        if row:
            row.role = "superadmin"
            row.active = True
            db.commit()
