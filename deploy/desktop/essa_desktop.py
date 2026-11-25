"""ESSA desktop app - the source of ESSA.exe.

One ESSA server runs next to PostgreSQL; every PC's desktop app only opens a
window onto it. The first time ESSA.exe runs on a PC it asks which kind of PC
this is, and remembers the answer:

  This PC is the server
      ESSA.exe must sit in the ESSA folder (next to run.bat). Setup (it asks for
      administrator rights once) makes ESSA start with Windows - before anyone
      logs in - lets the counters reach it through the firewall (port 8000,
      private network only), and gives it its own login secret. PostgreSQL is
      not opened to the network: only ESSA, on this PC, talks to it.
      The window opens http://localhost:8000. Closing it does NOT stop ESSA,
      because the counters depend on it.

  Connect to a server
      ESSA.exe alone is enough - no ESSA folder, Python or database on that PC.
      Type the server's address, or press "Find" to search the network. The
      window opens http://SERVER:8000. If the server cannot be reached it says
      so, with "Try again" and "Change server".

Change the choice later: hold Shift while opening ESSA, or run  ESSA.exe --setup
Redo the server setup:            ESSA.exe --server-setup   (asks for admin)

Settings file: %PROGRAMDATA%\\ESSA\\desktop.json
Logs:          %PROGRAMDATA%\\ESSA\\server.log (the server), %LOCALAPPDATA%\\ESSA\\desktop.log
Build:         deploy\\desktop\\build_exe.bat
"""
from __future__ import annotations

import concurrent.futures as cf
import ctypes
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

APP = "ESSA AI"
PORT = 8000                                          # run.bat serves here, on every network card
NO_WINDOW = 0x08000000                               # CREATE_NO_WINDOW
SHARED = os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData", "ESSA")
USER = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ESSA")
CONFIG = os.path.join(SHARED, "desktop.json")
TASK_PATH, TASK_NAME = "\\ESSA\\", "Server"
FIREWALL_RULE = "ESSA server (port 8000)"
GREEN = "#0b3d2e"


# --------------------------------------------------------------------------- small helpers

def log(msg: str) -> None:
    try:
        os.makedirs(USER, exist_ok=True)
        with open(os.path.join(USER, "desktop.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def popup(text: str, error: bool = True) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP, 0x10 if error else 0x40)


def me() -> list[str]:
    """The command that runs this program again (the exe, or python + this file)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def essa_home() -> str | None:
    """The ESSA folder: ESSA_HOME, else the folder ESSA.exe is in or any above it."""
    here = os.path.dirname(os.path.abspath(me()[-1]))
    candidates = [os.environ.get("ESSA_HOME") or ""]
    while True:
        candidates.append(here)
        up = os.path.dirname(here)
        if up == here:
            break
        here = up
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "run.bat")) and os.path.isdir(os.path.join(c, "backend")):
            return c
    return None


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if cfg.get("mode") in ("server", "client") else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(SHARED, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def server_url(address: str) -> str:
    """'192.168.1.10', 'SHOP-PC', 'host:8000' or a full URL -> http://host:port/"""
    a = address.strip().rstrip("/")
    if "://" not in a:
        a = "http://" + a
    u = urllib.parse.urlsplit(a)
    if not u.hostname:
        raise ValueError("no server address")
    return f"{u.scheme}://{u.hostname}:{u.port or PORT}/"


def essa_at(url: str, timeout: float = 4) -> dict | None:
    """ESSA's own status answer from that address, or None if no ESSA answers there."""
    try:
        with urllib.request.urlopen(url + "api/status", timeout=timeout) as r:
            data = json.loads(r.read(200_000).decode("utf-8", "replace"))
        return data if isinstance(data, dict) and "company" in data else None
    except Exception:
        return None


def company(status: dict | None) -> str:
    try:
        return (status or {})["company"]["name"] or "ESSA"
    except (KeyError, TypeError):
        return "ESSA"


# --------------------------------------------------------------------------- finding servers on the network

def local_networks() -> list[ipaddress.IPv4Network]:
    ips = set()
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    try:                                              # every network card, as ipconfig lists them
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, errors="replace",
                             creationflags=NO_WINDOW).stdout
        ips.update(ln.split(":")[-1].strip().split("(")[0] for ln in out.splitlines() if "IPv4" in ln)
    except OSError:
        pass
    try:                                              # the address the default route uses
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    nets = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and not a.is_link_local and \
                a not in ipaddress.ip_network("100.64.0.0/10"):          # VPN / carrier ranges
            n = ipaddress.ip_network(f"{ip}/24", strict=False)
            if n not in nets:
                nets.append(n)
    return nets


def find_servers() -> list[tuple[str, str]]:
    """[(address, company)] for every PC on the local /24 networks answering as ESSA."""
    hosts = [str(h) for n in local_networks() for h in n.hosts()]

    def probe(host: str):
        try:
            with socket.create_connection((host, PORT), timeout=0.4):
                pass
        except OSError:
            return None
        st = essa_at(f"http://{host}:{PORT}/", timeout=2)
        return (host, company(st)) if st else None

    with cf.ThreadPoolExecutor(128) as pool:
        return [r for r in pool.map(probe, hosts) if r]


# --------------------------------------------------------------------------- the window

def find_browser() -> str | None:
    roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                return p
    return None


def open_window(url: str) -> None:
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    args = [browser, f"--app={url}", f"--user-data-dir={os.path.join(USER, 'window')}",
            "--no-first-run", "--no-default-browser-check", "--start-maximized"]
    origin = url.rstrip("/")
    if urllib.parse.urlsplit(url).hostname not in ("localhost", "127.0.0.1"):
        # Voice input and the camera scanner need a "secure" page; the server is
        # plain http on the shop's own network, so vouch for that one address.
        args.append(f"--unsafely-treat-insecure-origin-as-secure={origin}")
    subprocess.Popen(args)


# --------------------------------------------------------------------------- server mode

def tail(path: str, n: int = 12) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).strip()
    except OSError:
        return ""


def start_server_here(home: str) -> subprocess.Popen:
    """ESSA in this user's session, for when the start-with-Windows task is not there."""
    path = os.path.join(USER, "server.log")
    os.makedirs(USER, exist_ok=True)
    out = open(path, "a", encoding="utf-8", errors="replace")
    out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting ESSA from {home}\n")
    out.flush()
    return subprocess.Popen(["cmd", "/c", os.path.join(home, "run.bat")], cwd=home,
                            env=dict(os.environ, PYTHONUNBUFFERED="1"), stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)


def task_installed() -> bool:
    return subprocess.run(["schtasks", "/Query", "/TN", TASK_PATH + TASK_NAME], capture_output=True,
                          creationflags=NO_WINDOW).returncode == 0


def ensure_server(home: str) -> bool:
    local = f"http://localhost:{PORT}/"
    if essa_at(local, timeout=2):
        return True
    # Just after Windows starts, the start-with-Windows task may still be coming up.
    if task_installed():
        for _ in range(20):
            time.sleep(1)
            if essa_at(local, timeout=2):
                return True
    log(f"server not running - starting it from {home}")
    proc = start_server_here(home)
    deadline = time.time() + 180                     # a first run seeds the database
    while time.time() < deadline:
        if essa_at(local, timeout=2):
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    popup("ESSA could not start.\n\n" + tail(os.path.join(USER, "server.log")) +
          f"\n\nFull log: {os.path.join(USER, 'server.log')}")
    return False


def read_env(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def env_value(lines: list[str], key: str) -> str:
    for ln in lines:
        k, sep, v = ln.partition("=")
        if sep and k.strip() == key:
            return v.strip()
    return ""


def ps(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, env=dict(os.environ, **(env or {})),
                          creationflags=NO_WINDOW)


def server_setup(home: str) -> str:
    """Make this PC the server. Needs administrator rights. Returns a report."""
    report = []
    os.makedirs(SHARED, exist_ok=True)

    # 1. A login secret of its own (the default one is public, in the source code).
    env_path = os.path.join(home, ".env")
    lines = read_env(env_path)
    if len(env_value(lines, "ESSA_AUTH_SECRET")) < 32 or env_value(lines, "ESSA_AUTH_SECRET") == "change-me":
        new = f"ESSA_AUTH_SECRET={secrets.token_urlsafe(48)}"
        lines = [new if ln.partition("=")[0].strip() == "ESSA_AUTH_SECRET" else ln for ln in lines]
        if new not in lines:
            lines.append(new)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        report.append("OK  login secret created (everyone signs in again once)")
    else:
        report.append("OK  login secret already set")

    # 2. The database must stay private to this PC.
    db = env_value(lines, "ESSA_DATABASE_URL")
    if db:
        host = urllib.parse.urlsplit(db).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            report.append("OK  PostgreSQL is on this PC (localhost)")
        else:
            report.append(f"!!  ESSA_DATABASE_URL points at '{host}', not this PC - "
                          "install ESSA on the PostgreSQL PC instead")
    else:
        report.append("OK  database: the SQLite file in the ESSA folder")

    # 3. Start with Windows, before anyone logs in, and come back if it stops.
    runner = os.path.join(SHARED, "essa_server.bat")
    log_path = os.path.join(SHARED, "server.log")
    with open(runner, "w", encoding="utf-8", newline="") as f:
        f.write("@echo off\r\nrem Written by ESSA.exe --server-setup. Keeps ESSA running.\r\n"
                f':loop\r\necho ===== %date% %time% starting ESSA>> "{log_path}"\r\n'
                f'call "{os.path.join(home, "run.bat")}" >> "{log_path}" 2>&1\r\n'
                "timeout /t 10 /nobreak >nul\r\ngoto loop\r\n")
    r = ps("$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c \"' + $env:ESSA_RUNNER + '\"');"
           "$t = New-ScheduledTaskTrigger -AtStartup;"
           "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
           "-DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew;"
           "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest;"
           f"Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
           "-Settings $s -Principal $p -Force | Out-Null;"
           f"Start-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}'",
           {"ESSA_RUNNER": runner})
    report.append("OK  ESSA starts with Windows" if r.returncode == 0
                  else "!!  could not set ESSA to start with Windows: " + (r.stderr.strip()[:300] or "unknown error"))

    # 4. Let the counters in: port 8000, private (shop) networks only.
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                   capture_output=True, creationflags=NO_WINDOW)
    r = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
                        "dir=in", "action=allow", "protocol=TCP", f"localport={PORT}", "profile=private,domain"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    report.append("OK  firewall open for the counters (port 8000, private network)" if r.returncode == 0
                  else "!!  firewall rule failed: " + (r.stdout.strip() or r.stderr.strip())[:300])
    r = ps("(Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public').Name")
    if r.stdout.strip():
        report.append(f"!!  the network '{r.stdout.strip()}' is set to Public - set it to Private "
                      "(Settings > Network > Properties) or the counters cannot connect")

    # 5. A sleeping server is a stopped server for every counter.
    r = subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"], capture_output=True,
                       creationflags=NO_WINDOW)
    report.append("OK  this PC no longer sleeps when plugged in" if r.returncode == 0
                  else "!!  could not stop this PC from sleeping - turn sleep off in Settings > Power")

    nets = [str(n.network_address)[:-1] for n in local_networks()]
    addrs = [ip for ip in {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
             if any(ip.startswith(p) for p in nets)]
    report.append("")
    report.append("On every other PC, open this in Chrome or Edge:")
    report.append("    " + ("   or   ".join(f"http://{a}:{PORT}" for a in sorted(addrs)) if addrs
                           else f"http://<this PC's IP>:{PORT}") + f"   (or http://{socket.gethostname()}:{PORT})")
    report.append("(or with ESSA.exe: \"Connect to a server\" and the same address)")
    text = "\n".join(report)
    with open(os.path.join(SHARED, "server-setup.txt"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
    return text


def run_server_setup_as_admin() -> bool:
    """Run --server-setup elevated (one UAC prompt) and wait. False if refused."""
    exe, *rest = me() + ["--server-setup"]
    r = ps("try { $p = Start-Process -FilePath $env:ESSA_EXE -ArgumentList $env:ESSA_ARGS "
           "-Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch { exit 99 }",
           {"ESSA_EXE": exe, "ESSA_ARGS": subprocess.list2cmdline(rest)})
    return r.returncode != 99


# --------------------------------------------------------------------------- the setup window

def choose(cfg: dict, home: str | None, message: str = "") -> dict | None:
    """Ask which kind of PC this is. Returns the new settings, or None if closed."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("ESSA - set up this PC")
    root.resizable(False, False)
    root.configure(bg="white")
    try:
        root.iconbitmap(default=os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(__file__)), "essa.ico"))
    except tk.TclError:
        pass
    style = ttk.Style(root)
    style.configure("TRadiobutton", background="white", font=("Segoe UI", 11))
    style.configure("TLabel", background="white", font=("Segoe UI", 10))
    style.configure("Head.TLabel", background=GREEN, foreground="white", font=("Segoe UI Semibold", 14))
    style.configure("Note.TLabel", background="white", foreground="#5b6770", font=("Segoe UI", 9))
    style.configure("TFrame", background="white")
    result: dict = {}

    head = tk.Frame(root, bg=GREEN)
    head.pack(fill="x")
    ttk.Label(head, text="  ESSA AI  -  how does this PC use ESSA?", style="Head.TLabel").pack(anchor="w", pady=12)
    body = ttk.Frame(root, padding=(22, 14, 22, 8))
    body.pack(fill="both")

    if message:
        ttk.Label(body, text=message, foreground="#b42318", wraplength=460).pack(anchor="w", pady=(0, 10))

    mode = tk.StringVar(value=cfg.get("mode") or ("server" if home and not cfg else "client"))
    server_btn = ttk.Radiobutton(body, text="This PC is the server", variable=mode, value="server")
    server_btn.pack(anchor="w")
    if not home:
        server_btn.state(["disabled"])
    ttk.Label(body, style="Note.TLabel", wraplength=440, justify="left",
              text=("ESSA and the database run on this PC. It starts with Windows and stays on for the counters."
                    if home else "Not available: ESSA.exe must be in the ESSA folder (next to run.bat) for this.")
              ).pack(anchor="w", padx=(24, 0), pady=(0, 12))
    ttk.Radiobutton(body, text="Connect to a server", variable=mode, value="client").pack(anchor="w")
    ttk.Label(body, style="Note.TLabel", text="Opens ESSA from the server PC. Nothing else is needed on this PC."
              ).pack(anchor="w", padx=(24, 0))

    row = ttk.Frame(body)
    row.pack(anchor="w", padx=(24, 0), pady=(8, 0), fill="x")
    ttk.Label(row, text="Server address").pack(side="left")
    addr = tk.StringVar(value=cfg.get("server", ""))
    entry = ttk.Entry(row, textvariable=addr, width=24, font=("Segoe UI", 11))
    entry.pack(side="left", padx=8)
    find_btn = ttk.Button(row, text="Find")
    find_btn.pack(side="left")
    test_btn = ttk.Button(row, text="Test")
    test_btn.pack(side="left", padx=(6, 0))
    found = tk.Listbox(body, height=0, font=("Segoe UI", 10), activestyle="none", borderwidth=1)
    status = ttk.Label(body, style="Note.TLabel", text="e.g. 192.168.1.10 or the server PC's name", wraplength=440)
    status.pack(anchor="w", padx=(24, 0), pady=(4, 0))

    def set_status(text: str, colour: str = "#5b6770") -> None:
        status.configure(text=text, foreground=colour)

    def sync(*_):
        client = mode.get() == "client"
        for w in (entry, find_btn, test_btn):
            w.state(["!disabled"] if client else ["disabled"])
        if mode.get() == "server" and not home:
            mode.set("client")
    mode.trace_add("write", sync)

    def in_background(work, done):
        def run():
            out = work()
            root.after(0, lambda: done(out))
        threading.Thread(target=run, daemon=True).start()

    def do_find():
        set_status("Searching the network for ESSA servers...")
        find_btn.state(["disabled"])

        def done(servers):
            find_btn.state(["!disabled"])
            found.delete(0, "end")
            if not servers:
                found.pack_forget()
                set_status("No ESSA server found. Check the server PC is on, then type its address.", "#b42318")
                return
            for host, name in servers:
                found.insert("end", f"{host}    {name}")
            found.configure(height=min(len(servers), 4))
            found.pack(anchor="w", padx=(24, 0), pady=(6, 0), fill="x", before=status)
            if len(servers) == 1:
                addr.set(servers[0][0])
            set_status(f"Found {len(servers)}. Click one to use it.", "#067647")
        in_background(find_servers, done)

    def pick(_):
        sel = found.curselection()
        if sel:
            addr.set(found.get(sel[0]).split()[0])

    def check_address():
        try:
            url = server_url(addr.get())
        except ValueError:
            set_status("Type the server's address first.", "#b42318")
            return None
        return url

    def do_test():
        url = check_address()
        if not url:
            return
        set_status(f"Connecting to {url} ...")
        in_background(lambda: essa_at(url), lambda st: set_status(
            f"Connected: {company(st)}" if st else f"No ESSA answered at {url}",
            "#067647" if st else "#b42318"))

    def save():
        if mode.get() == "server":
            result.update(mode="server")
            root.destroy()
            return
        url = check_address()
        if not url:
            return
        set_status(f"Connecting to {url} ...")

        def done(st):
            if st or ctypes.windll.user32.MessageBoxW(
                    None, f"No ESSA server answered at {url}.\n\nSave this address anyway?", APP, 0x34) == 6:
                result.update(mode="client", server=addr.get().strip())
                root.destroy()
            else:
                set_status(f"No ESSA answered at {url}", "#b42318")
        in_background(lambda: essa_at(url), done)

    find_btn.configure(command=do_find)
    test_btn.configure(command=do_test)
    found.bind("<<ListboxSelect>>", pick)
    foot = ttk.Frame(root, padding=(22, 6, 22, 16))
    foot.pack(fill="x")
    ttk.Button(foot, text="Save and open ESSA", command=save).pack(side="right")
    ttk.Button(foot, text="Cancel", command=root.destroy).pack(side="right", padx=8)
    root.bind("<Return>", lambda _: save())
    root.bind("<Escape>", lambda _: root.destroy())
    sync()
    root.update_idletasks()
    root.geometry(f"+{(root.winfo_screenwidth() - root.winfo_width()) // 2}+"
                  f"{(root.winfo_screenheight() - root.winfo_height()) // 3}")
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    if os.environ.get("ESSA_DESKTOP_SNAPSHOT"):                  # used by the build's own check
        root.after(800, lambda: (_snapshot(root, os.environ["ESSA_DESKTOP_SNAPSHOT"]), root.destroy()))
    root.mainloop()
    return result or None


def _snapshot(root, path: str) -> None:
    from PIL import ImageGrab
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(path)


def unreachable(url: str) -> str:
    """Server did not answer: 'retry', 'change' or 'close'."""
    r = ctypes.windll.user32.MessageBoxW(
        None, f"Cannot reach the ESSA server at\n{url}\n\nCheck the server PC is switched on and on the "
              "same network.\n\nYes = try again      No = change server      Cancel = close", APP, 0x33)
    return {6: "retry", 7: "change"}.get(r, "close")


# --------------------------------------------------------------------------- main

def main() -> int:
    args = sys.argv[1:]
    home = essa_home()

    if "--server-setup" in args:                     # the elevated half of choosing "server"
        if not home:
            popup("Server setup needs ESSA.exe in the ESSA folder, next to run.bat.")
            return 1
        report = server_setup(home)
        log("server setup:\n" + report)
        popup("ESSA server setup\n\n" + report, error="!!" in report)
        return 0

    cfg = load_config()
    shift = ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000
    message = ""
    while True:
        if not cfg or "--setup" in args or shift:
            new = choose(cfg, home, message)
            if not new:
                return 0
            try:
                save_config(new)
            except OSError as e:
                popup(f"Could not save the settings to {CONFIG}:\n{e}")
                return 1
            if new["mode"] == "server" and (cfg.get("mode") != "server" or "--setup" in args or shift):
                if not run_server_setup_as_admin():
                    popup("Server setup was skipped (administrator rights were not given).\n\n"
                          "ESSA will run on this PC only while someone is logged in. To finish the setup "
                          "later, right-click ESSA.exe > Run as administrator, and add  --server-setup.",
                          error=False)
            cfg, args, shift, message = new, [], 0, ""

        if cfg["mode"] == "server":
            if not home:
                cfg, message = {}, "This PC was set as the server, but ESSA.exe is not in the ESSA folder."
                continue
            if not ensure_server(home):
                return 1
            open_window(f"http://localhost:{PORT}/")
            log("opened (server)")
            return 0

        url = server_url(cfg.get("server", ""))
        if essa_at(url):
            open_window(url)
            log(f"opened {url}")
            return 0
        log(f"cannot reach {url}")
        what = unreachable(url)
        if what == "close":
            return 1
        if what == "change":
            message = f"Could not reach the server at {url}."
            args = ["--setup"]


if __name__ == "__main__":
    sys.exit(main())
