"""A read-only connection to an OUTSIDE PostgreSQL database - the client's.

This is NOT ESSA's own database (that is app.database / ESSA_DATABASE_URL, and
nothing here reads or changes it). It is a second, separate connection, used to
test and inspect the database a client already runs - on this PC, on another PC
in the shop, or over a VPN - before ESSA is deployed against it.

Safety, because the database on the other end may be a live production one:

* every session is read-only: libpq is asked for ``default_transaction_read_only``,
  each connection is put in a read-only session, and every use ends in ROLLBACK;
* only fixed, parameterised catalogue queries run - nothing here accepts SQL;
* no migrations, no DDL, no INSERT / UPDATE / DELETE / TRUNCATE, ever;
* the production environment needs the database name typed back before any
  connection is opened to it;
* "write approval" is recorded per environment by an administrator, but nothing in
  this module writes - ``require_write_approval`` is the gate any future feature
  that writes must pass first.

Secrets: the password is never sent back to the browser, never logged, never put
in an error message. It comes from ``DB_PASSWORD`` in the backend .env, or is
saved encrypted with Windows DPAPI (machine scope: the file is useless on any
other PC, or in Git). Where DPAPI is unavailable a password is not stored at all.
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from ..config import STATE_DIR

log = logging.getLogger("essa.extdb")

ENVIRONMENTS = {
    "development": "Development",
    "client_test": "Client testing",
    "production": "Production",
}
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
CONFIG_PATH = os.path.join(STATE_DIR, "db_connections.json")
STATEMENT_TIMEOUT_MS = 15000
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")

_lock = threading.RLock()
_pool = None                 # psycopg2 ThreadedConnectionPool for the connected environment
_state: dict = {"environment": None, "connected_at": None, "last_success": {}, "last_error": {}}


# --------------------------------------------------------------------------- password storage (DPAPI)

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def can_store_passwords() -> bool:
    return sys.platform == "win32"


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    entropy_raw = b"ESSA external database password"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = _Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    flags = 0x4 | 0x1                         # CRYPTPROTECT_LOCAL_MACHINE | UI_FORBIDDEN
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(out)):
        raise OSError("Windows could not encrypt/decrypt the saved password")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _seal(password: str) -> str:
    return base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")


def _unseal(sealed: str) -> str:
    return _dpapi(base64.b64decode(sealed), False).decode("utf-8")


# --------------------------------------------------------------------------- configuration

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_profile() -> dict | None:
    """The connection described by DB_* in the backend .env, if any."""
    host = (os.environ.get("DB_HOST") or "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("DB_PORT") or 5432),
        "database": (os.environ.get("DB_NAME") or "").strip(),
        "user": (os.environ.get("DB_USER") or "").strip(),
        "sslmode": _sslmode(os.environ.get("DB_SSL")),
        "timeout_ms": int(os.environ.get("DB_CONNECTION_TIMEOUT") or 5000),
        "pool_max": int(os.environ.get("DB_POOL_MAX") or 5),
        "source": ".env",
    }


def _sslmode(v) -> str:
    v = (v or "").strip().lower()
    if v in SSL_MODES:
        return v
    return "require" if v in ("1", "true", "yes", "on") else "disable" if v in ("0", "false", "no", "off") else "prefer"


def _env_environment() -> str:
    e = (os.environ.get("DB_ENVIRONMENT") or "development").strip().lower()
    return e if e in ENVIRONMENTS else "development"


def load() -> dict:
    with _lock:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("active", _env_environment())
        envs = cfg.setdefault("environments", {})
        env_prof = _env_profile()
        if env_prof and _env_environment() not in envs:
            envs[_env_environment()] = env_prof
        return cfg


def _save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def public_profile(env: str, prof: dict | None) -> dict | None:
    """What the browser may see of a profile: everything but the password."""
    if not prof:
        return None
    has_saved = bool(prof.get("password_sealed"))
    from_env = env == _env_environment() and bool(os.environ.get("DB_PASSWORD"))
    return {
        "environment": env, "label": ENVIRONMENTS[env],
        "host": prof.get("host", ""), "port": prof.get("port", 5432),
        "database": prof.get("database", ""), "user": prof.get("user", ""),
        "sslmode": prof.get("sslmode", "prefer"), "timeout_ms": prof.get("timeout_ms", 5000),
        "pool_max": prof.get("pool_max", 5),
        "has_password": has_saved or from_env,
        "password_source": "saved, encrypted on this PC" if has_saved else ".env (DB_PASSWORD)" if from_env else None,
        "source": prof.get("source", "saved"),
        "saved_by": prof.get("saved_by"), "saved_at": prof.get("saved_at"),
        "writes_approved": prof.get("writes_approved"),
    }


def validate(p: dict) -> dict:
    """Checked, normalised connection settings. Raises ValueError with a plain message."""
    env = str(p.get("environment") or "").strip()
    if env not in ENVIRONMENTS:
        raise ValueError("Choose an environment: development, client testing or production.")
    host = str(p.get("host") or "").strip()
    if not host:
        raise ValueError("Enter the database server's host name or IP address.")
    try:
        import ipaddress
        ipaddress.ip_address(host.strip("[]"))
        host = host.strip("[]")
    except ValueError:
        if not HOST_RE.match(host):
            raise ValueError(f"'{host}' is not a valid host name or IP address.")
    try:
        port = int(p.get("port") or 5432)
    except (TypeError, ValueError):
        raise ValueError("The port must be a number.")
    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535 (PostgreSQL normally uses 5432).")
    out = {"environment": env, "host": host, "port": port}
    for key, label in (("database", "database name"), ("user", "user name")):
        v = str(p.get(key) or "").strip()
        if not v:
            raise ValueError(f"Enter the {label}.")
        if len(v) > 63 or any(c in v for c in "\x00\r\n"):
            raise ValueError(f"The {label} is not valid (at most 63 characters, one line).")
        out[key] = v
    ssl = str(p.get("sslmode") or "prefer").strip().lower()
    if ssl not in SSL_MODES:
        raise ValueError(f"SSL mode must be one of: {', '.join(SSL_MODES)}.")
    out["sslmode"] = ssl
    try:
        out["timeout_ms"] = int(p.get("timeout_ms") or 5000)
        out["pool_max"] = int(p.get("pool_max") or 5)
    except (TypeError, ValueError):
        raise ValueError("Timeout and pool size must be numbers.")
    if not 1000 <= out["timeout_ms"] <= 60000:
        raise ValueError("The connection timeout must be between 1000 and 60000 ms.")
    if not 1 <= out["pool_max"] <= 20:
        raise ValueError("The pool size must be between 1 and 20.")
    pw = p.get("password")
    if pw is not None and (len(str(pw)) > 1024 or "\x00" in str(pw)):
        raise ValueError("The password is not valid.")
    out["password"] = str(pw) if pw else None
    return out


def same_login(a: dict, b: dict) -> bool:
    return all(str(a.get(k)) == str(b.get(k)) for k in ("host", "port", "database", "user"))


def password_for_test(p: dict) -> str | None:
    """The typed password, else the saved one - but only for the SAME server and
    login it was saved for, so changing the host can never send it elsewhere."""
    if p.get("password"):
        return p["password"]
    prof = load()["environments"].get(p["environment"]) or {}
    return _password_for(p["environment"], prof, None) if same_login(p, prof) else None


def _password_for(env: str, prof: dict, typed: str | None) -> str | None:
    if typed:
        return typed
    if prof.get("password_sealed"):
        try:
            return _unseal(prof["password_sealed"])
        except OSError:
            return None
    if env == _env_environment():
        return os.environ.get("DB_PASSWORD") or None
    return None


def save_profile(p: dict, who: str) -> dict:
    """Save one environment's settings. Never touches ESSA's own database settings."""
    with _lock:
        cfg = load()
        old = cfg["environments"].get(p["environment"], {})
        prof = {k: p[k] for k in ("host", "port", "database", "user", "sslmode", "timeout_ms", "pool_max")}
        prof.update(source="saved", saved_by=who, saved_at=_now())
        if p.get("password"):
            if not can_store_passwords():
                raise ValueError("This server cannot store passwords safely. Put DB_PASSWORD in the backend .env instead.")
            prof["password_sealed"] = _seal(p["password"])
        elif old.get("password_sealed") and all(old.get(k) == prof[k] for k in ("host", "port", "database", "user")):
            prof["password_sealed"] = old["password_sealed"]          # same login: keep the saved password
        # A different server or login is a different approval: writes go back to off.
        if old.get("writes_approved") and all(old.get(k) == prof[k] for k in ("host", "port", "database", "user")):
            prof["writes_approved"] = old["writes_approved"]
        cfg["environments"][p["environment"]] = prof
        _save(cfg)
        log.info("external db settings saved: env=%s host=%s port=%s db=%s user=%s by=%s",
                 p["environment"], prof["host"], prof["port"], prof["database"], prof["user"], who)
        return public_profile(p["environment"], prof)


def set_active(env: str, confirm: str | None) -> None:
    with _lock:
        cfg = load()
        prof = cfg["environments"].get(env)
        if not prof:
            raise ValueError(f"Save the {ENVIRONMENTS[env]} settings first.")
        check_production(env, prof.get("database", ""), confirm)
        if _state["environment"] and _state["environment"] != env:
            disconnect()
        cfg["active"] = env
        _save(cfg)


def check_production(env: str, database: str, confirm: str | None) -> None:
    if env == "production" and (confirm or "").strip() != database:
        raise PermissionError(f"This is the PRODUCTION database. Type its name ({database}) to confirm.")


def set_write_approval(env: str, approve: bool, confirm: str | None, who: str) -> dict:
    with _lock:
        cfg = load()
        prof = cfg["environments"].get(env)
        if not prof:
            raise ValueError(f"Save the {ENVIRONMENTS[env]} settings first.")
        if approve:
            phrase = f"ALLOW WRITES TO {prof['database']}"
            if (confirm or "").strip() != phrase:
                raise PermissionError(f"To approve writes, type exactly: {phrase}")
            prof["writes_approved"] = {"by": who, "at": _now()}
        else:
            prof.pop("writes_approved", None)
        _save(cfg)
        log.warning("external db write approval %s: env=%s db=%s by=%s",
                    "GRANTED" if approve else "revoked", env, prof["database"], who)
        return public_profile(env, prof)


def require_write_approval(env: str) -> None:
    """The gate for any future feature that would write to the outside database."""
    prof = load()["environments"].get(env) or {}
    if not prof.get("writes_approved"):
        raise PermissionError("Writes to this database have not been approved by an administrator.")


# --------------------------------------------------------------------------- diagnostics

def _issue(code: str, title: str, detail: str, steps: list[str]) -> dict:
    return {"code": code, "title": title, "detail": detail, "steps": steps}


def _classify(msg: str, p: dict) -> dict:
    """A libpq / server error -> what it means and what to do about it."""
    m = msg.lower()
    host, port, db, user = p["host"], p["port"], p["database"], p["user"]
    if "password authentication failed" in m or "authentication failed" in m:
        return _issue("AUTH_FAILED", "Wrong user name or password",
                      f"PostgreSQL rejected the password for user '{user}'.",
                      ["Check the user name and password (they are case-sensitive).",
                       "Ask the client's DBA to confirm the account exists and its password.",
                       "If the password was changed recently, save the new one here."])
    if "no password supplied" in m:
        return _issue("PASSWORD_REQUIRED", "No password was given",
                      "The server asks for a password, and none was sent. A saved password is only used "
                      "for the exact server, port, database and user it was saved with.",
                      ["Type the password and test again.",
                       "Or save these settings with the password, then test without typing it."])
    if re.search(r'role ".*" does not exist', m):
        return _issue("AUTH_FAILED", "That user does not exist",
                      f"There is no PostgreSQL user (role) called '{user}' on this server.",
                      ["Check the spelling of the user name.",
                       f"Ask the DBA to create it: CREATE ROLE {user} LOGIN PASSWORD '…';"])
    if "no pg_hba.conf entry" in m or "pg_hba.conf rejects" in m:
        ssl_hint = "no encryption" in m
        return _issue("HBA_REJECTED", "The server does not allow connections from this PC",
                      "PostgreSQL's host-based authentication (pg_hba.conf) has no rule letting "
                      f"user '{user}' reach database '{db}' from this computer's address"
                      + (" without SSL." if ssl_hint else "."),
                      ["On the database server, open pg_hba.conf (in the PostgreSQL data folder).",
                       f"Add a line for this PC's network, e.g.:  host  {db}  {user}  192.168.1.0/24  scram-sha-256",
                       "Reload PostgreSQL (pg_ctl reload, or restart the service).",
                       "If the rule says 'hostssl', set SSL mode to 'require' here."
                       if not ssl_hint else "The rule needs SSL: set SSL mode to 'require' here."])
    if re.search(r'database ".*" does not exist', m):
        return _issue("DB_NOT_FOUND", "That database does not exist",
                      f"The server is reachable and the login works, but there is no database called '{db}'.",
                      ["Check the database name (it is case-sensitive when it was created in quotes).",
                       "Ask the DBA for the exact name, or list them with: psql -l"])
    if "server does not support ssl" in m or ("ssl" in m and "required" in m and "not" in m):
        return _issue("SSL_MISMATCH", "SSL setting does not match the server",
                      "This connection asks for SSL, but the server has SSL turned off.",
                      ["Set SSL mode to 'prefer' or 'disable' for a server on the shop's own network.",
                       "Or turn SSL on at the server (ssl = on in postgresql.conf, with a certificate)."])
    if "ssl" in m and ("certificate" in m or "root cert" in m or "verify" in m):
        return _issue("SSL_MISMATCH", "The server's SSL certificate could not be verified",
                      "verify-ca / verify-full need the server's CA certificate on this PC.",
                      ["Use SSL mode 'require' (encrypted, without checking the certificate), or",
                       "install the server's root certificate at %APPDATA%\\postgresql\\root.crt."])
    if "permission denied" in m or "not have connect privilege" in m:
        return _issue("PERMISSION_DENIED", "The user may not use this database",
                      f"User '{user}' logged in, but has no permission on database '{db}'.",
                      [f"Ask the DBA to run: GRANT CONNECT ON DATABASE {db} TO {user};",
                       f"…and for reading: GRANT USAGE ON SCHEMA public TO {user}; "
                       f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {user};"])
    if "timeout expired" in m or "timed out" in m:
        return _issue("NETWORK_TIMEOUT", "The server did not answer in time",
                      f"Nothing answered from {host}:{port} before the timeout.",
                      ["Check the address is right and the server PC is switched on.",
                       "A firewall (Windows Firewall on the server, or the router) may be dropping port "
                       f"{port}: allow it for the shop's network.",
                       "Over a VPN: check the VPN is connected and routes to that network.",
                       "On a slow link, raise the connection timeout."])
    if "too many connections" in m or "remaining connection slots" in m:
        return _issue("TOO_MANY_CONNECTIONS", "The server has no free connections",
                      "PostgreSQL has reached max_connections.",
                      ["Lower the pool size here.", "Ask the DBA to check max_connections and idle sessions."])
    if "the database system is starting up" in m or "shutting down" in m:
        return _issue("SERVICE_UNAVAILABLE", "PostgreSQL is starting or stopping",
                      "The server answered, but is not accepting logins right now.",
                      ["Wait a minute and test again."])
    return _issue("UNKNOWN", "The connection failed", msg.strip()[:400],
                  ["Read the detail above.", "Check the PostgreSQL server log for the same time."])


def _clean(msg: str, password: str | None) -> str:
    """An error message with anything secret removed."""
    msg = str(msg or "")
    if password:
        msg = msg.replace(password, "********")
    return re.sub(r"password=\S+", "password=********", msg)


def _connect_kwargs(p: dict, password: str | None) -> dict:
    return {
        "host": p["host"], "port": p["port"], "dbname": p["database"], "user": p["user"],
        "password": password or "", "sslmode": p["sslmode"],
        "connect_timeout": max(1, round(p["timeout_ms"] / 1000)),
        "application_name": "ESSA connection test (read-only)",
        # read-only from the first statement, and nothing may run away
        "options": f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
    }


def _readonly(conn) -> None:
    conn.set_session(readonly=True, autocommit=False)


PROBE_SQL = "SELECT current_database(), current_user, version(), NOW()"
DETAIL_SQL = """
SELECT current_setting('server_version'),
       pg_is_in_recovery(),
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
       has_database_privilege(current_user, current_database(), 'CREATE'),
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg_toast%%'),
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg_toast%%'
           AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')),
       current_setting('default_transaction_read_only'),
       inet_server_addr()::text
"""


def _probe(conn) -> dict:
    _readonly(conn)
    try:
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(PROBE_SQL)
            db, user, version, now = cur.fetchone()
            ms = (time.perf_counter() - t0) * 1000
            cur.execute(DETAIL_SQL)
            (short_ver, standby, superuser, can_create, tables, readable, ro, addr) = cur.fetchone()
    finally:
        conn.rollback()
    return {"database": db, "user": user, "version": version, "server_version": short_ver,
            "server_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
            "query_ms": round(ms, 1), "standby": bool(standby), "superuser": bool(superuser),
            "can_create": bool(can_create), "tables": int(tables), "readable_tables": int(readable),
            "session_read_only": ro == "on", "server_address": addr}


def _warnings(info: dict, p: dict) -> list[dict]:
    w = []
    if info["tables"] and not info["readable_tables"]:
        w.append(_issue("NO_READ_ACCESS", "Connected, but this user cannot read any table",
                        f"{info['tables']} tables exist; user '{info['user']}' has SELECT on none of them.",
                        [f"Ask the DBA: GRANT USAGE ON SCHEMA public TO {info['user']}; "
                         f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {info['user']};"]))
    elif info["readable_tables"] < info["tables"]:
        w.append(_issue("PARTIAL_READ_ACCESS", "This user can read only some tables",
                        f"Readable: {info['readable_tables']} of {info['tables']} tables.",
                        ["That may be intended. If ESSA needs the others, ask the DBA to grant SELECT on them."]))
    if info["superuser"]:
        w.append(_issue("SUPERUSER", "This login is a PostgreSQL superuser",
                        "This screen stays read-only, but a superuser login is more power than ESSA needs.",
                        ["Ask the DBA for a dedicated login with only the rights ESSA needs (e.g. erp_user)."]))
    if p["sslmode"] in ("disable", "allow", "prefer") and p["environment"] == "production" \
            and not _is_private(p["host"]):
        w.append(_issue("NO_SSL", "Production over an outside network without required SSL",
                        "The password and data may cross the network unencrypted.",
                        ["Use SSL mode 'require' (or a VPN) for anything outside the shop's own network."]))
    return w


def _is_private(host: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(host)
        return a.is_private or a.is_loopback
    except ValueError:
        return host.lower() == "localhost" or "." not in host


def test(p: dict, password: str | None) -> dict:
    """Step by step: name -> network -> PostgreSQL login -> read-only query."""
    import psycopg2

    steps, started = [], time.perf_counter()
    timeout = p["timeout_ms"] / 1000

    def step(name, ok, detail, ms):
        steps.append({"step": name, "ok": ok, "detail": detail, "ms": round(ms, 1)})

    def fail(issue):
        out = {"ok": False, "environment": p["environment"], "steps": steps, "issue": issue,
               "total_ms": round((time.perf_counter() - started) * 1000, 1), "at": _now()}
        _state["last_error"][p["environment"]] = {"at": out["at"], "issue": issue}
        log.info("external db test FAILED: env=%s host=%s port=%s db=%s user=%s code=%s",
                 p["environment"], p["host"], p["port"], p["database"], p["user"], issue["code"])
        return out

    # 1. the name
    t = time.perf_counter()
    try:
        addrs = socket.getaddrinfo(p["host"], p["port"], type=socket.SOCK_STREAM)
        ip = addrs[0][4][0]
        step("Find the server", True, f"{p['host']} is {ip}", (time.perf_counter() - t) * 1000)
    except socket.gaierror:
        step("Find the server", False, f"'{p['host']}' could not be found", (time.perf_counter() - t) * 1000)
        return fail(_issue("INVALID_HOST", "The server name or IP address was not found",
                           f"'{p['host']}' does not resolve to any address from this PC.",
                           ["Check the spelling, or use the server's IP address instead of its name.",
                            "For a PC name on the shop network, try its IP (run 'ipconfig' on that PC).",
                            "Over a VPN, check the VPN is connected."]))

    # 2. the network
    t = time.perf_counter()
    try:
        with socket.create_connection((ip, p["port"]), timeout=timeout):
            pass
        step("Reach port " + str(p["port"]), True, f"{ip}:{p['port']} is open", (time.perf_counter() - t) * 1000)
    except ConnectionRefusedError:
        step("Reach port " + str(p["port"]), False, "connection refused", (time.perf_counter() - t) * 1000)
        return fail(_issue("PORT_CLOSED", "Nothing is accepting connections on that port",
                           f"{ip} answered, but refused port {p['port']}: PostgreSQL is not running there, "
                           "is on another port, or is listening only on its own localhost.",
                           [f"Check the port (PostgreSQL's default is 5432; you entered {p['port']}).",
                            "On the server: Services (services.msc) > postgresql-x64-NN > Running?",
                            "In postgresql.conf set  listen_addresses = '*'  (or the server's LAN IP) "
                            "and restart the service.",
                            f"Allow inbound TCP {p['port']} in Windows Firewall on the server."]))
    except (socket.timeout, TimeoutError):
        step("Reach port " + str(p["port"]), False, f"no answer in {p['timeout_ms']} ms",
             (time.perf_counter() - t) * 1000)
        return fail(_classify("timeout expired", p))
    except OSError as e:
        step("Reach port " + str(p["port"]), False, _clean(e.strerror or str(e), password),
             (time.perf_counter() - t) * 1000)
        return fail(_issue("NETWORK_UNREACHABLE", "This PC has no route to that address",
                           _clean(str(e), password),
                           ["Check this PC is on the same network as the server, or the VPN is connected.",
                            "Check the IP address."]))

    # 3. PostgreSQL login, and 4. the read-only test query
    t = time.perf_counter()
    try:
        conn = psycopg2.connect(**_connect_kwargs(p, password))
    except psycopg2.Error as e:
        msg = _clean(str(e), password)
        step("Log in to PostgreSQL", False, msg.splitlines()[0][:200] if msg else "failed",
             (time.perf_counter() - t) * 1000)
        return fail(_classify(msg, p))
    step("Log in to PostgreSQL", True, f"as {p['user']} on {p['database']} ({p['sslmode']})",
         (time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    try:
        info = _probe(conn)
        step("Read-only test query", True, "SELECT current_database(), current_user, version(), NOW()",
             (time.perf_counter() - t) * 1000)
    except psycopg2.Error as e:
        msg = _clean(str(e), password)
        step("Read-only test query", False, msg.splitlines()[0][:200], (time.perf_counter() - t) * 1000)
        return fail(_classify(msg, p))
    finally:
        conn.close()

    total = round((time.perf_counter() - started) * 1000, 1)
    out = {"ok": True, "environment": p["environment"], "steps": steps, "info": info,
           "warnings": _warnings(info, p), "total_ms": total, "at": _now()}
    _state["last_success"][p["environment"]] = {"at": out["at"], "response_ms": total}
    log.info("external db test ok: env=%s host=%s port=%s db=%s user=%s %.0fms",
             p["environment"], p["host"], p["port"], p["database"], p["user"], total)
    return out


# --------------------------------------------------------------------------- the pooled connection

def connect(env: str, confirm: str | None) -> dict:
    """Open the pool for a saved environment (read-only sessions)."""
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    global _pool

    cfg = load()
    prof = cfg["environments"].get(env)
    if not prof:
        raise ValueError(f"Save the {ENVIRONMENTS[env]} settings first.")
    p = dict(prof, environment=env)
    check_production(env, p["database"], confirm)
    password = _password_for(env, prof, None)
    result = test(p, password)                     # step-by-step diagnosis, and no pool if it fails
    if not result["ok"]:
        return result
    with _lock:
        disconnect()
        try:
            _pool = ThreadedConnectionPool(1, p["pool_max"], **_connect_kwargs(p, password))
        except psycopg2.Error as e:
            msg = _clean(str(e), password)
            return {"ok": False, "environment": env, "steps": result["steps"], "issue": _classify(msg, p)}
        _state.update(environment=env, connected_at=_now())
        if cfg.get("active") != env:
            cfg["active"] = env
            _save(cfg)
    return result


def disconnect() -> bool:
    global _pool
    with _lock:
        was = _pool is not None
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:                    # noqa: BLE001 - closing is best effort
                pass
        _pool = None
        _state.update(environment=None, connected_at=None)
        return was


def _with_conn(fn):
    """Run fn(cursor) on a pooled, read-only connection; always rolled back."""
    import psycopg2
    with _lock:
        pool = _pool
    if pool is None:
        raise LookupError("Not connected. Connect to a saved environment first.")
    conn = pool.getconn()
    broken = False
    try:
        _readonly(conn)
        with conn.cursor() as cur:
            return fn(cur)
    except psycopg2.OperationalError:
        broken = True
        raise
    finally:
        try:
            conn.rollback()
        except Exception:                        # noqa: BLE001
            broken = True
        pool.putconn(conn, close=broken)


def status() -> dict:
    """Connection state; with a live pool, the probe is run again (read-only)."""
    import psycopg2
    cfg = load()
    env = _state["environment"]
    out = {
        "active": cfg.get("active"),
        "environments": {e: public_profile(e, cfg["environments"].get(e)) for e in ENVIRONMENTS},
        "labels": ENVIRONMENTS,
        "can_store_passwords": can_store_passwords(),
        "connected": False, "environment": env, "connected_at": _state["connected_at"],
        "last_success": _state["last_success"], "last_error": _state["last_error"],
        "essa_own_database_untouched": True,
    }
    if env:
        t = time.perf_counter()
        try:
            conn_info = _probe_pooled()
            out.update(connected=True, info=conn_info,
                       response_ms=round((time.perf_counter() - t) * 1000, 1),
                       pool={"max": cfg["environments"][env].get("pool_max", 5)})
            _state["last_success"][env] = {"at": _now(), "response_ms": out["response_ms"]}
        except (psycopg2.Error, LookupError) as e:
            prof = dict(cfg["environments"].get(env, {}), environment=env)
            issue = _classify(_clean(str(e), None), prof) if prof.get("host") else None
            out.update(connected=False, error=issue)
            if issue:
                _state["last_error"][env] = {"at": _now(), "issue": issue}
    return out


def _probe_pooled() -> dict:
    import psycopg2  # noqa: F401
    with _lock:
        pool = _pool
    if pool is None:
        raise LookupError("Not connected.")
    conn = pool.getconn()
    broken = False
    try:
        return _probe(conn)
    except Exception:
        broken = True
        raise
    finally:
        pool.putconn(conn, close=broken)


# --------------------------------------------------------------------------- read-only inspection

SCHEMAS_SQL = """
SELECT n.nspname, pg_get_userbyid(n.nspowner), has_schema_privilege(n.oid, 'USAGE')
  FROM pg_namespace n
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
   AND n.nspname NOT LIKE 'pg_temp_%%'
 ORDER BY n.nspname
"""
TABLES_SQL = """
SELECT n.nspname, c.relname,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized view' WHEN 'f' THEN 'foreign table' END,
       GREATEST(c.reltuples, -1)::bigint,
       CASE WHEN has_table_privilege(c.oid, 'SELECT') OR pg_has_role(c.relowner, 'USAGE')
            THEN pg_total_relation_size(c.oid) END,
       has_table_privilege(c.oid, 'SELECT'),
       obj_description(c.oid, 'pg_class')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
   AND (%(schema)s::text IS NULL OR n.nspname = %(schema)s)
 ORDER BY n.nspname, c.relname
 LIMIT 5000
"""
TABLE_OID_SQL = """
SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %(schema)s AND c.relname = %(table)s
"""
COLUMNS_SQL = """
SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum)
  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""
CONSTRAINTS_SQL = """
SELECT con.conname, con.contype,
       ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.num ORDER BY k.ord),
       fn.nspname, fc.relname,
       ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute att ON att.attrelid = con.confrelid AND att.attnum = k.num ORDER BY k.ord),
       cn.nspname, cc.relname
  FROM pg_constraint con
  JOIN pg_class cc ON cc.oid = con.conrelid JOIN pg_namespace cn ON cn.oid = cc.relnamespace
  LEFT JOIN pg_class fc ON fc.oid = con.confrelid LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
 WHERE con.contype IN ('p', 'f', 'u') AND (con.conrelid = %(oid)s OR con.confrelid = %(oid)s)
 ORDER BY con.contype, con.conname
"""


def inspect_tables(schema: str | None) -> dict:
    def run(cur):
        cur.execute(SCHEMAS_SQL)
        schemas = [{"name": n, "owner": o, "usable": bool(u)} for n, o, u in cur.fetchall()]
        cur.execute(TABLES_SQL, {"schema": schema})
        tables = [{"schema": s, "name": t, "kind": k, "approx_rows": r if r is not None and r >= 0 else None,
                   "size_bytes": sz, "readable": bool(rd), "comment": cm}
                  for s, t, k, r, sz, rd, cm in cur.fetchall()]
        return {"schemas": schemas, "tables": tables, "truncated": len(tables) >= 5000}
    return _with_conn(run)


def inspect_table(schema: str, table: str) -> dict:
    def run(cur):
        cur.execute(TABLE_OID_SQL, {"schema": schema, "table": table})
        row = cur.fetchone()
        if not row:
            raise LookupError(f"No table {schema}.{table}.")
        oid = row[0]
        cur.execute(COLUMNS_SQL, {"oid": oid})
        cols = [{"position": n, "name": c, "type": t, "nullable": bool(nl), "default": d, "comment": cm}
                for n, c, t, nl, d, cm in cur.fetchall()]
        cur.execute(CONSTRAINTS_SQL, {"oid": oid})
        pk, uniques, fks_out, fks_in = None, [], [], []
        for name, kind, cols_, fs, ft, fcols, cs, ct in cur.fetchall():
            if kind == "p":
                pk = {"name": name, "columns": list(cols_)}
            elif kind == "u":
                uniques.append({"name": name, "columns": list(cols_)})
            elif cs == schema and ct == table:
                fks_out.append({"name": name, "columns": list(cols_), "references": f"{fs}.{ft}",
                                "referenced_columns": list(fcols)})
            else:
                fks_in.append({"name": name, "from": f"{cs}.{ct}", "columns": list(cols_),
                               "referenced_columns": list(fcols)})
        pk_cols = set(pk["columns"]) if pk else set()
        for c in cols:
            c["primary_key"] = c["name"] in pk_cols
        return {"schema": schema, "table": table, "columns": cols, "primary_key": pk,
                "unique": uniques, "foreign_keys": fks_out, "referenced_by": fks_in}
    return _with_conn(run)
