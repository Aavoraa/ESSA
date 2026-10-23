"""Central configuration. Everything overridable by environment variable so the
same code runs on a laptop (SQLite, Tesseract) or in production (Postgres,
vision model) with no edits."""
import os


def _env(name: str, default: str) -> str:
    """An environment variable, treating blank as absent.

    `os.environ.get` does not: a variable set to "" is present and returns "",
    which is how a hosting dashboard that prompts for a value and is given none
    ends up seeding an account with an EMPTY password. That fails open — anyone
    signs in with no password at all, while the documented default is refused,
    so it reads as a broken deployment rather than an unlocked one. Blank here
    means "not configured", which is what whoever left the box empty meant.
    """
    return (os.environ.get(name) or "").strip() or default


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/

# Two directories, because `backend/data` holds two different kinds of thing and
# a deployment has to treat them oppositely.
#
# DATA_DIR is SHIPPED data, read-only at runtime and versioned in git: the
# category master, the LR sample, the ground-truth fixtures and sample images.
# It travels with the code and belongs wherever the code is unpacked.
#
# STATE_DIR is what the warehouse PRODUCES and must not lose across a restart:
# the database, the uploaded invoice images, and the vision key saved from the
# settings screen. On a host that rebuilds the code directory on every deploy,
# this is the path a persistent disk gets mounted at.
#
# They default to the same folder, so a laptop sees exactly the behaviour it saw
# before this split existed. Only a deployment sets them apart — and if the two
# were left as one, pointing it at a mounted disk would take the 686-row
# category master away with it and seed an empty warehouse.
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_DIR = os.environ.get("ESSA_STATE_DIR", DATA_DIR)

UPLOAD_DIR = os.environ.get("ESSA_UPLOAD_DIR", os.path.join(STATE_DIR, "uploads"))
SAMPLE_DIR = os.path.join(DATA_DIR, "sample_images")
GROUND_TRUTH_DIR = os.path.join(DATA_DIR, "ground_truth")


def _database_url() -> str:
    """Where the warehouse database is.

    ESSA_DATABASE_URL first, because that is this app's own variable and an
    explicit answer should win. After it come the names a hosting provider sets
    BY ITSELF when a Postgres is attached to the project — Vercel and Supabase
    both write POSTGRES_URL, and the pooled spelling is the one serverless
    wants. Reading them means a database attached in the dashboard simply works,
    with nothing copied by hand.

    That matters more than it looks: the alternative is pasting a connection
    string containing a password into two variables, and the failure when it is
    pasted wrong is a 500 with no clue in it. The provider already knows the
    right value; ask it rather than the person.
    """
    for name in DB_URL_CANDIDATES:
        url = (os.environ.get(name) or "").strip()
        # A half-filled template is worse than nothing: it parses, connects to
        # nowhere, and reports a fault that looks like the database being down.
        # The angle brackets are what the providers' own examples use.
        if url and "<" not in url and ">" not in url:
            return url
    return f"sqlite:///{os.path.join(STATE_DIR, 'essa.db')}"


DB_URL_CANDIDATES = ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL",
                     "DATABASE_URL", "POSTGRES_URL_NON_POOLING")


def database_url_report() -> list:
    """Which of the candidate variables exist, and why each was or wasn't used.

    Reported by /api/status because the alternative is guessing across a
    dashboard nobody else can see. "No usable connection string" has three quite
    different causes — the variable is absent, or it is set to a template, or it
    is set on the wrong environment and is therefore absent HERE — and they lead
    to three different fixes.

    Names and verdicts only. The values are passwords.
    """
    out = []
    for name in DB_URL_CANDIDATES:
        raw = os.environ.get(name)
        if raw is None:
            verdict = "not set"
        elif not raw.strip():
            verdict = "set but empty"
        elif "<" in raw or ">" in raw:
            verdict = "IGNORED — still a template, contains < or >"
        elif not raw.strip().startswith(("postgres://", "postgresql://")):
            verdict = f"IGNORED — not a postgres URL (starts '{raw.strip()[:12]}…')"
        else:
            verdict = "usable"
        out.append({"name": name, "status": verdict})
    return out


DATABASE_URL = _database_url()

# Which extraction provider to prefer. "auto" = vision model if a key is present,
# else tesseract. The seeded provider is always consulted first for known samples.
EXTRACTION_PROVIDER = os.environ.get("ESSA_EXTRACTION_PROVIDER", "auto")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VISION_MODEL = os.environ.get("ESSA_VISION_MODEL", "claude-3-5-sonnet-20241022")

# The company this deployment belongs to (the "buyer" on every purchase invoice).
# Through _env, so a variable set to an empty string falls back to the name
# below rather than printing a blank company on every document — the same trap
# the account passwords hit, and worse here because nothing errors: the invoice
# is simply issued by nobody.
COMPANY_GSTIN = _env("ESSA_COMPANY_GSTIN", "33AADCE6591N1Z7")
COMPANY_NAME = _env("ESSA_COMPANY_NAME", "Essa Garments Private Limited")

# --- Buying against orders ---
# Whether a consignment KEYED IN BY HAND must name a confirmed purchase order.
# On by default: goods arrive against an order, and the form is where somebody
# is sitting with the paperwork and can say which one.
#
# It is a setting rather than a constant because it is a policy, not a fact.
# A business that buys on the phone and raises no orders would otherwise be
# unable to record a consignment at all — and the failure would look like a bug
# in the transport register rather than a rule it never agreed to.
#
# The IMPORT route is never held to this, whatever this says: a transporter's
# register page names no purchase order anywhere on it. See routers/lr.
REQUIRE_PO_FOR_LR = _env("ESSA_REQUIRE_PO_FOR_LR", "1").lower() not in (
    "0", "false", "no", "off")

# --- Login ---
# Accounts live in the database (see models.User and services/users), not here.
# Four ranked roles: user (the floor), admin (setup + money), superadmin (also
# accounts and server settings), superboss (all of it, and the only rank that
# can manage a Super Boss).
#
# What is left here is the signing key and the accounts used to SEED an empty
# database — a fresh install, or an existing one upgrading from the two
# hard-coded accounts these variables used to be the whole of. Seeding only ever
# creates a missing row, so a password changed in the app is not reverted to the
# value below on the next restart.
AUTH_SECRET = os.environ.get("ESSA_AUTH_SECRET", "essa-local-secret-change-me")
# The token HMAC is keyed by this AND each user's random seed, so the default
# does not by itself make tokens forgeable — but it is also the /api/admin/boot
# key, and it is public. Say so loudly rather than run quietly on it.
if AUTH_SECRET == "essa-local-secret-change-me" or len(AUTH_SECRET) < 32:
    import warnings
    warnings.warn("ESSA_AUTH_SECRET is unset, the default, or shorter than 32 "
                  "characters — set a long random value in production "
                  "(python -c \"import secrets; print(secrets.token_urlsafe(48))\").")


SEED_ACCOUNTS = {
    _env("ESSA_SUPERADMIN_USER", "superadmin"): {
        "password": _env("ESSA_SUPERADMIN_PASSWORD", "super@123"),
        "role": "superadmin", "full_name": "Super Admin"},
    _env("ESSA_ADMIN_USER", "admin"): {
        "password": _env("ESSA_ADMIN_PASSWORD", _env("ESSA_PASSWORD", "essa@123")),
        "role": "admin", "full_name": "Administrator"},
    _env("ESSA_USER_USER", "user"): {
        "password": _env("ESSA_USER_PASSWORD", "user@123"),
        "role": "user", "full_name": "Warehouse User"},
}

# The Super Boss has NO default password, deliberately — see services/users.seed.
# Set both to create one on start; leave them unset and a super admin appoints the
# first Super Boss from Users & Access instead.
SUPERBOSS_SEED = ((os.environ.get("ESSA_SUPERBOSS_USER") or "").strip() or "superboss",
                  (os.environ.get("ESSA_SUPERBOSS_PASSWORD") or "").strip())

# What "today" means on the Command Center: the business's own day, not UTC's.
# Every timestamp in both databases is stored in UTC, and a shop in Tiruppur that
# opens at 9 would otherwise have its first three and a half hours of takings
# filed under the previous day. Minutes east of UTC; India is +330 and has no DST.
BUSINESS_UTC_OFFSET_MINUTES = int(_env("ESSA_UTC_OFFSET_MINUTES", "330") or 330)

os.makedirs(UPLOAD_DIR, exist_ok=True)
# On a fresh disk this is the directory the database is about to be created in,
# so it has to exist before SQLAlchemy opens the file rather than after.
os.makedirs(STATE_DIR, exist_ok=True)
