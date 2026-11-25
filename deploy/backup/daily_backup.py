"""ESSA DAILY BACKUP: runs automatically every night at 10:30 PM.

Started by daily_backup.bat:
    Right-click daily_backup.bat > Run as administrator   (ONCE, on the server)
        -> sets up the automatic backup every night at 10:30 PM and takes the
           first one straight away to prove it works.
    Double-click daily_backup.bat
        -> shows whether the nightly backup is set up and when it last ran.
    The 10:30 PM task itself runs:  daily_backup.bat run

This file is also the backup engine and holds the SETTINGS for both backups:
instant_backup.py (the "backup now" button) uses it, so keep the four files
(daily_backup.py/.bat, instant_backup.py/.bat) together in one folder.

From a command prompt (for the technician):
    daily_backup.py run              take the daily backup (what the task runs)
    daily_backup.py setup            schedule it (administrator) + first backup
    daily_backup.py info             message box: set up? last result?
    daily_backup.py status           the same, printed
    daily_backup.py check  <zip>     prove a backup can be restored (changes nothing)
    daily_backup.py restore <zip>    put a backup back (STOP ESSA FIRST)

What a backup contains (one zip):
    - SQLite install: backend\\data\\essa.db + the Store's textile_shop.db, copied
      LIVE through SQLite's backup API (bills being made are not torn, and data
      still in the -wal file is included, which a plain file copy can miss).
    - PostgreSQL install (ESSA_DATABASE_URL in ESSA\\.env): one pg_dump holding
      the warehouse AND the Store. The password is read from .env and handed to
      pg_dump privately, never written in a script or on the command line.
    - uploads\\ (invoice images), settings.json, .env (as env.txt), manifest.json
Every copy is checked (integrity_check / pg_restore --list / zip test / SHA-256).

Exit code: 0 OK, 1 FAILED, 2 made but the pen drive / extra copy was missed.
Python standard library only: runs with ESSA's own backend\\.venv.
"""

# ============================ SETTINGS: change here ============================
SETTINGS = {
    # Main backup folder. A different disk from ESSA's if there is one.
    "backup_folder": r"D:\ESSA-BACKUP",

    # Pen drive, found by its NAME so the drive letter does not matter.
    # Name it: File Explorer > This PC > right-click the pen drive > Rename.
    # "" = no pen drive copy.
    "usb_label": "ESSA-BACKUP",
    "usb_folder": "ESSA-BACKUP",

    # A third copy away from the shop: NAS share or Google Drive/OneDrive folder.
    # Example: r"\\NAS\backup\essa"   "" = none.
    "extra_folder": "",

    # Nightly backups kept: newest of each of the last N days + of the last N months.
    "keep_daily": 30,
    "keep_monthly": 12,
    # Instant backups kept (the daily clean-up never removes them).
    "keep_manual": 20,

    # Time of the automatic nightly backup (24-hour clock).
    "nightly_time": "22:30",

    # PostgreSQL only: folder with pg_dump.exe. "" = newest under Program Files.
    "pg_bin": "",

    # PostgreSQL only: the schemas that are ESSA's data - the warehouse (public)
    # and the Store (shop). Others in the same database (e.g. a migration's
    # staging tables) are not ESSA's and are left out. "" = the whole database.
    "pg_schemas": "public,shop",
}
# ===============================================================================

import argparse
import ctypes
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
# The ESSA folder: given by the .bat, else the folder two levels up.
ROOT = os.path.normpath(os.environ.get("ESSA_HOME") or os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(ROOT, "backend")
SHOP_DB = os.path.join(ROOT, "Textile Retail Shop", "textile_shop.db")
TASK_NAME = r"ESSA\Daily backup"

DB_URL_VARS = ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL",
               "DATABASE_URL", "POSTGRES_URL_NON_POOLING")     # app's own order
NIGHTLY_RE = re.compile(r"^ESSA-DAILY-(\d{4})-(\d{2})-(\d{2})_(\d{4})\.zip$")
# Instant backups get their own name: the nightly clean-up keeps one backup per
# day, so a 10 AM backup named like the nightly one would be gone by midnight.
MANUAL_RE = re.compile(r"^ESSA-INSTANT-\d{4}-\d{2}-\d{2}_\d{6}\.zip$")
LIBPQ_PARAMS = {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout",
                "application_name", "options", "target_session_attrs"}


class BackupError(Exception):
    pass


# ------------------------------------------------------------------- helpers
class Log:
    def __init__(self, path):
        self.path = path

    def __call__(self, msg, level="INFO"):
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {level:5} {msg}"
        print(line, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def popup(title, text, error=False):
    """Message box for the person who clicked. Nothing when there is no desktop."""
    if os.name != "nt" or os.environ.get("ESSA_BACKUP_QUIET"):
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, (0x10 if error else 0x40) | 0x40000)
    except Exception:                                    # noqa: BLE001
        pass


def read_env():
    """ESSA\\.env the way run.bat reads it; real environment variables win."""
    env, path = {}, os.path.join(ROOT, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def database_url(env):
    for name in DB_URL_VARS:
        url = (env.get(name) or "").strip()
        if url and "<" not in url and ">" not in url:
            return url
    return ""


def drive_by_label(label):
    """Root ('F:\\') of the mounted volume named `label`, or None."""
    if os.name != "nt" or not label:
        return None
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            buf = ctypes.create_unicode_buffer(261)
            old = k32.SetErrorMode(1)                    # no "insert a disk" pop-up
            try:
                ok = k32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
            finally:
                k32.SetErrorMode(old)
            if ok and buf.value.strip().lower() == label.strip().lower():
                return root
    return None


def find_pg_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    if SETTINGS["pg_bin"]:
        path = os.path.join(SETTINGS["pg_bin"], exe)
        if os.path.isfile(path):
            return path
        raise BackupError(f"{exe} is not in pg_bin = {SETTINGS['pg_bin']}")
    # Newest install BEFORE PATH: a newer pg_dump can dump an older server, never
    # the reverse, and PATH often still names an older left-over version.
    hits = glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                                  "PostgreSQL", "*", "bin", exe))
    hits.sort(key=lambda p: [int(x) if x.isdigit() else 0
                             for x in p.split(os.sep)[-3].split(".")], reverse=True)
    if hits:
        return hits[0]
    found = shutil.which(name)
    if found:
        return found
    raise BackupError(f"{exe} not found: install PostgreSQL tools or set pg_bin in SETTINGS")


def pg_target(url):
    """(address without password, env with PGPASSWORD) for the pg tools."""
    u = urlsplit(url.replace("postgres://", "postgresql://", 1))
    if u.scheme != "postgresql":
        raise BackupError("the database address is not PostgreSQL")
    netloc = (quote(unquote(u.username), safe="") + "@" if u.username else "") + (u.hostname or "localhost")
    if u.port:
        netloc += f":{u.port}"
    query = urlencode([(k, v) for k, v in parse_qsl(u.query) if k in LIBPQ_PARAMS])
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    return f"postgresql://{netloc}{u.path}" + (f"?{query}" if query else ""), env


def schema_args():
    return [f"--schema={x.strip()}" for x in SETTINGS.get("pg_schemas", "").split(",") if x.strip()]


def run(cmd, env=None):
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3 * 3600)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-5:]
        raise BackupError(f"{os.path.basename(cmd[0])} failed: " + " | ".join(tail))
    return p.stdout


def sqlite_check(path):
    con = sqlite3.connect(path)
    try:
        verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        con.close()
    if verdict != "ok":
        raise BackupError(f"{os.path.basename(path)} failed integrity_check: {verdict}")
    return tables


def pg_tables(dump):
    out = run([find_pg_tool("pg_restore"), "--list", dump])
    n = sum(1 for line in out.splitlines() if " TABLE DATA " in line)
    if n == 0:
        raise BackupError("the PostgreSQL dump contains no table data")
    return n


# ------------------------------------------------------------- the backup
def copy_sqlite(src, dest, log):
    if not os.path.isfile(src):
        raise BackupError(f"database not found: {src}")
    s, d = sqlite3.connect(src, timeout=60), sqlite3.connect(dest)
    try:
        s.backup(d)                       # consistent, live, includes the -wal data
    finally:
        d.close()
        s.close()
    tables = sqlite_check(dest)
    log(f"  {os.path.basename(src)}: {human(os.path.getsize(dest))}, {tables} tables, integrity ok")
    return {"file": os.path.basename(dest), "tables": tables}


def collect(stage, env, log):
    state_dir = env.get("ESSA_STATE_DIR") or os.path.join(BACKEND, "data")
    upload_dir = env.get("ESSA_UPLOAD_DIR") or os.path.join(state_dir, "uploads")
    url = database_url(env)
    man = {"made": dt.datetime.now().isoformat(timespec="seconds"),
           "computer": os.environ.get("COMPUTERNAME", ""), "databases": [], "files": []}
    if url.startswith(("postgres://", "postgresql://")):
        man["mode"] = "postgresql"
        log("Database: PostgreSQL (warehouse + Store in one dump"
            + (f"; schemas {SETTINGS['pg_schemas']})" if SETTINGS.get("pg_schemas") else ")"))
        address, penv = pg_target(url)
        dump = os.path.join(stage, "essa.dump")
        run([find_pg_tool("pg_dump"), "--format=custom", "--no-password", *schema_args(),
             "--file", dump, "--dbname", address], env=penv)
        n = pg_tables(dump)
        log(f"  essa.dump: {human(os.path.getsize(dump))}, {n} tables with data, readable")
        man["databases"].append({"file": "essa.dump", "tables": n})
    else:
        man["mode"] = "sqlite"
        wh = url[len("sqlite:///"):] if url.startswith("sqlite:///") else os.path.join(state_dir, "essa.db")
        log("Database: SQLite files")
        man["databases"].append(copy_sqlite(wh, os.path.join(stage, "essa.db"), log))
        if os.path.isfile(SHOP_DB):
            man["databases"].append(copy_sqlite(SHOP_DB, os.path.join(stage, "textile_shop.db"), log))
        else:
            log(f"  Store database not found at {SHOP_DB}, skipped", "WARN")
    for path, name in ((os.path.join(state_dir, "settings.json"), "settings.json"),
                       (os.path.join(ROOT, ".env"), "env.txt")):
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(stage, name))
            man["files"].append(name)
    if os.path.isdir(upload_dir):
        shutil.copytree(upload_dir, os.path.join(stage, "uploads"))
        n = sum(len(f) for _, _, f in os.walk(os.path.join(stage, "uploads")))
        man["files"].append(f"uploads/ ({n} files)")
        log(f"  uploads: {n} files")
    return man


def make_zip(stage, dest, man):
    with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    tmp = dest + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, _, files in os.walk(stage):
            for f in files:
                zf.write(os.path.join(base, f), os.path.relpath(os.path.join(base, f), stage))
    with zipfile.ZipFile(tmp) as zf:
        bad = zf.testzip()
    if bad:
        raise BackupError(f"zip check failed on {bad}")
    os.replace(tmp, dest)


def copy_verified(src, folder, digest, log, what):
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(src))
    shutil.copyfile(src, dest + ".part")
    if sha256(dest + ".part") != digest:
        os.remove(dest + ".part")
        raise BackupError(f"copy to {what} does not match the original")
    os.replace(dest + ".part", dest)
    log(f"Copied to {what}: {dest}")


def prune_nightly(folder, log, what):
    """Newest of each of the last keep_daily days + of the last keep_monthly
    months. Only ever deletes ESSA-DAILY-*.zip files."""
    if not os.path.isdir(folder):
        return
    found = sorted(((f"{m[1]}{m[2]}{m[3]}{m[4]}", f, f"{m[1]}-{m[2]}-{m[3]}", f"{m[1]}-{m[2]}")
                    for f in os.listdir(folder) for m in [NIGHTLY_RE.match(f)] if m), reverse=True)
    keep, days, months = set(), [], []
    for _, f, day, month in found:
        if day not in days:
            days.append(day)
            if len(days) <= SETTINGS["keep_daily"]:
                keep.add(f)
        if month not in months:
            months.append(month)
            if len(months) <= SETTINGS["keep_monthly"]:
                keep.add(f)
    _delete(folder, [f for _, f, _, _ in found if f not in keep], log, what)


def prune_manual(folder, log, what):
    """Newest keep_manual instant backups. Only deletes ESSA-INSTANT-*.zip."""
    if os.path.isdir(folder):
        found = sorted((f for f in os.listdir(folder) if MANUAL_RE.match(f)), reverse=True)
        _delete(folder, found[SETTINGS["keep_manual"]:], log, what)


def _delete(folder, names, log, what):
    for f in names:
        try:
            os.remove(os.path.join(folder, f))
        except OSError as e:
            log(f"Could not delete old backup {f} on {what}: {e}", "WARN")
    if names:
        log(f"Removed {len(names)} old backup(s) from {what}")
    for f in os.listdir(folder):                         # interrupted copies
        if f.endswith(".zip.part"):
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass


def backup(manual):
    """One backup. Returns (exit code, status dict)."""
    folder = SETTINGS["backup_folder"]
    status = {"started": dt.datetime.now().isoformat(timespec="seconds"), "ok": False,
              "kind": "instant" if manual else "daily", "copies": {}, "warnings": []}
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        status["error"] = f"cannot use the backup folder {folder}: {e} (change backup_folder in SETTINGS)"
        print(status["error"])
        return 1, status
    log = Log(os.path.join(folder, "backup.log"))
    lock = os.path.join(folder, ".backup.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 6 * 3600:
        status["error"] = "another backup is running right now; try again in a minute"
        log(status["error"], "ERROR")
        return 1, status
    with open(lock, "w") as fh:
        fh.write(str(os.getpid()))

    prune = prune_manual if manual else prune_nightly
    stage = tempfile.mkdtemp(prefix="essa-backup-")
    code = 1
    try:
        log("=" * 60)
        log(f"ESSA {'INSTANT' if manual else 'DAILY'} backup started ({ROOT})")
        now = dt.datetime.now()
        name = f"ESSA-INSTANT-{now:%Y-%m-%d_%H%M%S}.zip" if manual else f"ESSA-DAILY-{now:%Y-%m-%d_%H%M}.zip"
        zip_path = os.path.join(folder, name)
        man = collect(stage, read_env(), log)
        need = sum(os.path.getsize(os.path.join(b, f)) for b, _, fs in os.walk(stage) for f in fs)
        if shutil.disk_usage(folder).free < need + (200 << 20):
            raise BackupError(f"not enough free space in {folder} (need about {human(need)})")
        make_zip(stage, zip_path, man)
        digest = sha256(zip_path)
        status.update(ok=True, file=zip_path, bytes=os.path.getsize(zip_path), sha256=digest,
                      mode=man["mode"])
        log(f"Backup saved: {zip_path} ({human(status['bytes'])})")
        prune(folder, log, "backup folder")
        code = 0

        # A missing pen drive must not cost the main backup, nor pass silently.
        if SETTINGS["usb_label"]:
            root = drive_by_label(SETTINGS["usb_label"])
            if not root:
                status["warnings"].append(f"pen drive '{SETTINGS['usb_label']}' is not plugged in; no copy there")
                log(status["warnings"][-1], "WARN")
                code = 2
            else:
                target = os.path.join(root, SETTINGS["usb_folder"])
                try:
                    copy_verified(zip_path, target, digest, log, f"pen drive {root}")
                    prune(target, log, "pen drive")
                    status["copies"]["usb"] = target
                except (OSError, BackupError) as e:
                    status["warnings"].append(f"pen drive copy failed: {e}")
                    log(status["warnings"][-1], "WARN")
                    code = 2
        if SETTINGS["extra_folder"]:
            try:
                copy_verified(zip_path, SETTINGS["extra_folder"], digest, log, "extra folder")
                prune(SETTINGS["extra_folder"], log, "extra folder")
                status["copies"]["extra"] = SETTINGS["extra_folder"]
            except (OSError, BackupError) as e:
                status["warnings"].append(f"extra folder copy failed: {e}")
                log(status["warnings"][-1], "WARN")
                code = 2
    except Exception as e:                                # noqa: BLE001
        status["error"] = str(e)
        log(f"BACKUP FAILED: {e}", "ERROR")
        code = 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            os.remove(lock)
        except OSError:
            pass
        status.update(finished=dt.datetime.now().isoformat(timespec="seconds"), exit_code=code)
        try:
            with open(os.path.join(folder, "last_backup.json"), "w", encoding="utf-8") as fh:
                json.dump(status, fh, indent=2)
        except OSError:
            pass
        log({0: "Finished OK", 2: "Finished with warnings"}.get(code, "Finished with ERRORS"),
            {0: "INFO", 2: "WARN"}.get(code, "ERROR"))
    return code, status


def report(code, status, extra="", title="ESSA backup"):
    """The message box shown to whoever started a backup by hand."""
    if code == 1:
        popup(f"{title} FAILED", "The backup did NOT work.\n\n" + status.get("error", "") +
              f"\n\nDetails: {os.path.join(SETTINGS['backup_folder'], 'backup.log')}"
              + (f"\n\n{extra}" if extra else ""), error=True)
        return
    lines = [f"Backup done at {dt.datetime.now():%d-%m-%Y %I:%M %p}.",
             "All data up to this moment is saved.", "",
             f"File: {status['file']}", f"Size: {human(status['bytes'])}"]
    if status["copies"].get("usb"):
        lines.append(f"Also copied to the pen drive ({status['copies']['usb']}).")
    lines += ["\nNote: " + w for w in status["warnings"]]
    if extra:
        lines += ["", extra]
    popup(f"{title} done", "\n".join(lines))


def summary():
    """Is the nightly backup set up, and how did the last backups go?"""
    lines = [f"Automatic daily backup at {SETTINGS['nightly_time']}: "
             + ("SET UP" if task_installed() else
                "NOT set up. Right-click daily_backup.bat > Run as administrator.")]
    folder = SETTINGS["backup_folder"]
    for label, pattern in (("Last daily backup", NIGHTLY_RE), ("Last instant backup", MANUAL_RE)):
        files = sorted(f for f in os.listdir(folder) if pattern.match(f)) if os.path.isdir(folder) else []
        lines.append(f"{label}: {files[-1] if files else 'none yet'}")
    path = os.path.join(folder, "last_backup.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        result = {0: "OK", 2: "OK, with a warning"}.get(st.get("exit_code"), "FAILED")
        lines.append(f"Most recent run: {st.get('kind')} at {st.get('finished', '?')}: {result}")
        lines += ["  " + w for w in st.get("warnings", [])]
        if st.get("error"):
            lines.append("  " + st["error"])
    lines.append(f"Backup folder: {folder}")
    return "\n".join(lines)


# --------------------------------------------------------- nightly schedule
def task_installed():
    if os.name != "nt":
        return True
    return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                          capture_output=True).returncode == 0


def install_task():
    """Every night at nightly_time as SYSTEM (works with nobody signed in). If
    the server was off at that time, it runs as soon as it is back on."""
    bat = os.environ.get("ESSA_DAILY_BAT") or os.path.join(HERE, "daily_backup.bat")
    if not os.path.isfile(bat):
        raise BackupError(f"cannot find the .bat to schedule: {bat}")
    p = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "DAILY",
                        "/ST", SETTINGS["nightly_time"], "/RU", "SYSTEM", "/RL", "HIGHEST",
                        "/TR", f'"{bat}" run'], capture_output=True, text=True)
    if p.returncode != 0:
        raise BackupError("could not create the nightly task (run as administrator): "
                          + (p.stderr or p.stdout).strip())
    ps = ("$t = Get-ScheduledTask -TaskPath '\\ESSA\\' -TaskName 'Daily backup';"
          "$t.Settings.StartWhenAvailable = $true;"
          "$t.Settings.DisallowStartIfOnBatteries = $false;"
          "$t.Settings.StopIfGoingOnBatteries = $false;"
          "$t.Settings.ExecutionTimeLimit = 'PT3H';"
          "Set-ScheduledTask -InputObject $t | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    print(f"Nightly backup scheduled every day at {SETTINGS['nightly_time']} ({TASK_NAME}).")


# ------------------------------------------------------------ check/restore
def open_backup(zip_path, stage):
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise BackupError(f"zip is damaged at {bad}")
        zf.extractall(stage)
    man_path = os.path.join(stage, "manifest.json")
    if not os.path.isfile(man_path):
        raise BackupError("not an ESSA backup (no manifest.json)")
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    print(f"Backup made {man.get('made')} on {man.get('computer') or '?'} ({man.get('mode')})")
    for db in man["databases"]:
        path = os.path.join(stage, db["file"])
        n = sqlite_check(path) if db["file"].endswith(".db") else pg_tables(path)
        print(f"  {db['file']}: OK, {n} tables")
    print(f"  other files: {', '.join(man.get('files', [])) or 'none'}")
    return man


def restore(stage, man):
    env = read_env()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    state_dir = env.get("ESSA_STATE_DIR") or os.path.join(BACKEND, "data")
    if man["mode"] == "sqlite":
        targets = {"essa.db": os.path.join(state_dir, "essa.db"), "textile_shop.db": SHOP_DB}
        for db in man["databases"]:
            dest = targets[db["file"]]
            for side in ("", "-wal", "-shm"):             # keep the current data aside
                if os.path.exists(dest + side):
                    os.replace(dest + side, f"{dest}.bak-{stamp}{side}")
            shutil.copy2(os.path.join(stage, db["file"]), dest)
            print(f"  restored {dest} (old one kept as {os.path.basename(dest)}.bak-{stamp})")
    else:
        address, penv = pg_target(database_url(env))
        safety = os.path.join(state_dir, f"before-restore-{stamp}.dump")
        run([find_pg_tool("pg_dump"), "--format=custom", "--no-password", *schema_args(), "--file", safety,
             "--dbname", address], env=penv)
        print(f"  safety copy of the current database: {safety}")
        run([find_pg_tool("pg_restore"), "--clean", "--if-exists", "--no-owner", "--no-password",
             "--single-transaction", "--dbname", address, os.path.join(stage, "essa.dump")], env=penv)
        print("  database restored")
    if os.path.isfile(os.path.join(stage, "settings.json")):
        shutil.copy2(os.path.join(stage, "settings.json"), os.path.join(state_dir, "settings.json"))
    if os.path.isdir(os.path.join(stage, "uploads")):
        dest = env.get("ESSA_UPLOAD_DIR") or os.path.join(state_dir, "uploads")
        shutil.copytree(os.path.join(stage, "uploads"), dest, dirs_exist_ok=True)
        print(f"  uploads put back in {dest}")
    print("  (.env from the backup is inside the zip as env.txt; copy by hand only if needed)")


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="ESSA daily backup")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "setup", "info", "status", "check", "restore"])
    ap.add_argument("zip", nargs="?", help="backup file for check / restore")
    ap.add_argument("--folder", help="use this backup folder instead of the one in SETTINGS")
    ap.add_argument("--no-copies", action="store_true", help="skip pen drive and extra folder")
    ap.add_argument("--yes", action="store_true", help="restore without asking")
    args = ap.parse_args()
    if args.folder:
        SETTINGS["backup_folder"] = args.folder
    if args.no_copies:
        SETTINGS["usb_label"] = SETTINGS["extra_folder"] = ""

    if args.command == "run":                      # the 10:30 PM task: no message box
        return backup(manual=False)[0]

    if args.command == "setup":
        try:
            install_task()
        except BackupError as e:
            print(e)
            popup("ESSA daily backup NOT set up", f"{e}\n\nRight-click daily_backup.bat > "
                  "Run as administrator.", error=True)
            return 1
        note = (f"The automatic daily backup is SET UP: every night at "
                f"{SETTINGS['nightly_time']}, even with nobody signed in. If the server is "
                "off at that time, it runs as soon as it is switched on.\n\n"
                "The backup above was the first one, to prove it works.")
        code, status = backup(manual=False)
        report(code, status, note, title="ESSA daily backup")
        print(note)
        return code

    if args.command in ("info", "status"):
        text = summary()
        print(text)
        if args.command == "info":
            popup("ESSA daily backup", text)
        return 0

    # check / restore
    if not args.zip:
        ap.error(f"{args.command} needs the backup file, e.g. {args.command} D:\\ESSA-BACKUP\\ESSA-....zip")
    stage = tempfile.mkdtemp(prefix="essa-restore-")
    try:
        man = open_backup(args.zip, stage)
        if args.command == "check":
            print("CHECK PASSED: this backup can be restored.")
            return 0
        if not args.yes:
            print("\nThis REPLACES the current ESSA data with this backup. ESSA must be stopped.")
            if input("Type RESTORE to continue: ").strip() != "RESTORE":
                print("Cancelled. Nothing changed.")
                return 1
        restore(stage, man)
        print("RESTORE DONE. Start ESSA again.")
        return 0
    except (BackupError, OSError, zipfile.BadZipFile) as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
