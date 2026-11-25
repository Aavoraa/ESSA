"""Make this PC the ESSA server - no exe anywhere. Counters just open the URL.

    server_setup.bat            set up (as administrator): start with Windows,
                                firewall for the shop network, login secret,
                                no sleep - then checks the URL answers
    server_setup.bat status     is it running, on which address, which database
    server_setup.bat remove     undo: remove the start-up task and firewall rule,
                                stop the server (data and .env are kept)

Every counter PC then opens  http://<this PC's IP>:8000  in Chrome or Edge.
The setup itself is the same code ESSA.exe uses (deploy/desktop/essa_desktop.py),
so the two can never drift apart.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(HOME, "deploy", "desktop"))
import essa_desktop as core                     # noqa: E402  (shared with ESSA.exe)

PORT = core.PORT


def is_admin() -> bool:
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def lan_addresses() -> list[str]:
    prefixes = [str(n.network_address).rsplit(".", 1)[0] + "." for n in core.local_networks()]
    found = set()
    out = subprocess.run(["ipconfig"], capture_output=True, text=True, errors="replace").stdout
    for ln in out.splitlines():
        if "IPv4" in ln:
            ip = ln.split(":")[-1].strip().split("(")[0]
            if any(ip.startswith(p) for p in prefixes):
                found.add(ip)
    return sorted(found)


def status_at(url: str, timeout: float = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url + "api/status", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def listener() -> str | None:
    """Who holds port 8000 now: 'SYSTEM task', 'a user session (pid N)' or None."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$c = Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | "
                        "Select-Object -First 1; if ($c) { $p = Get-CimInstance Win32_Process -Filter "
                        "\"ProcessId=$($c.OwningProcess)\"; $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
                        "\"$($c.OwningProcess)|$($o.User)\" }"], capture_output=True, text=True)
    out = r.stdout.strip()
    if not out:
        return None
    pid, _, user = out.partition("|")
    if user.upper() == "SYSTEM" or not user:      # a normal user cannot read SYSTEM's processes
        return f"the start-up task (SYSTEM, pid {pid})"
    return f"a program in {user}'s session (pid {pid})"


def task_state() -> str:
    """Windows hides a SYSTEM task from normal users, so ask both ways."""
    if core.task_installed():
        return "installed"
    if os.path.exists(os.path.join(core.SHARED, "essa_server.bat")):
        return "installed by server setup (run as administrator to see it in Task Scheduler)"
    return "NOT installed"


def describe(st: dict | None) -> str:
    if not st:
        return "not answering"
    db = st.get("database") or {}
    pos = st.get("pos") or {}
    return (f"answering - {st.get('company', {}).get('name', 'ESSA')}; database: {db.get('dialect')} "
            f"({db.get('host')}){'' if db.get('ok') else ' NOT OK: ' + str(db.get('error'))}; "
            f"shop: {'ready' if pos.get('available') else 'loading' if pos.get('available') is None else 'ERROR ' + str(pos.get('error'))}")


def cmd_status() -> int:
    print(f"Start-up task : {task_state()}")
    print(f"Port {PORT}     : {listener() or 'nobody is listening'}")
    st = status_at(f"http://localhost:{PORT}/")
    print(f"ESSA          : {describe(st)}")
    for ip in lan_addresses():
        print(f"Counters open : http://{ip}:{PORT}   ({'answers' if status_at(f'http://{ip}:{PORT}/') else 'NOT answering'})")
    print(f"Server log    : {os.path.join(core.SHARED, 'server.log')}")
    return 0 if st else 1


def cmd_setup() -> int:
    if not is_admin():
        print("Run this as administrator: right-click server_setup.bat > Run as administrator.")
        return 1
    holder = listener()
    if holder and "SYSTEM" not in holder:
        print(f"Note: port {PORT} is in use by {holder}. Close that ESSA (run.bat window / ESSA.exe) - "
              "the start-up task takes over as soon as the port is free.\n")
    report = core.server_setup(HOME)
    print(report)
    print("\nWaiting for ESSA to answer on the network address...")
    urls = [f"http://{ip}:{PORT}/" for ip in lan_addresses()] or [f"http://localhost:{PORT}/"]
    deadline, st = time.time() + 240, None
    while time.time() < deadline:
        st = status_at(urls[0])
        if st and (st.get("pos") or {}).get("available") is not None:
            break
        time.sleep(3)
    print(f"ESSA {describe(st)}")
    if st:
        print("\nOpen ESSA on every PC in the shop (Chrome or Edge):")
        for u in urls:
            print(f"    {u}")
        print("Tip: Edge > ... > Apps > Install this site as an app, for a desktop icon.")
    else:
        print(f"ESSA did not answer yet - see {os.path.join(core.SHARED, 'server.log')}")
    return 0 if st and "!!" not in report else 1


def cmd_remove() -> int:
    if not is_admin():
        print("Run this as administrator.")
        return 1
    r = core.ps(f"Stop-ScheduledTask -TaskPath '{core.TASK_PATH}' -TaskName '{core.TASK_NAME}' -ErrorAction SilentlyContinue;"
                f"Unregister-ScheduledTask -TaskPath '{core.TASK_PATH}' -TaskName '{core.TASK_NAME}' -Confirm:$false "
                "-ErrorAction SilentlyContinue")
    # the task's cmd loop and the server it started
    core.ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*essa_server.bat*' -or "
            "($_.CommandLine -like '*uvicorn*app.main:app*' -and $_.CommandLine -like '*8000*') } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={core.FIREWALL_RULE}"],
                   capture_output=True)
    print("Removed: start-up task, firewall rule; server stopped. Data and .env are unchanged.")
    return 0 if r.returncode == 0 else 1


def stop_server_processes() -> None:
    """The ESSA server the task started (not the task's loop, which brings it back)."""
    core.ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' -and "
            "$_.CommandLine -like '*8000*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }")


def cmd_restart() -> int:
    """Restart ESSA (e.g. after changing .env or a database setting)."""
    if not is_admin():
        print("Run this as administrator.")
        return 1
    stop_server_processes()
    print("ESSA stopped; the start-up task starts it again in about 10 seconds...")
    time.sleep(5)
    deadline, st = time.time() + 240, None
    while time.time() < deadline:
        st = status_at(f"http://localhost:{PORT}/")
        if st and (st.get("pos") or {}).get("available") is not None:
            break
        time.sleep(3)
    print(f"ESSA {describe(st)}")
    return 0 if st else 1


if __name__ == "__main__":
    what = (sys.argv[1] if len(sys.argv) > 1 else "setup").lower()
    sys.exit({"setup": cmd_setup, "status": cmd_status, "remove": cmd_remove,
              "restart": cmd_restart}.get(what, cmd_status)())
