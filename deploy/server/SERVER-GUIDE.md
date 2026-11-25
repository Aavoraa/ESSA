# ESSA on a shop network: no exe, just a link

One PC is the **server**: ESSA and PostgreSQL run there. Every other PC opens
**`http://SERVER-IP:8000`** in Chrome or Edge. Nothing is installed on them.

```
  PC 1 (browser) ─┐
  PC 2 (browser) ─┼── http://192.168.x.x:8000 ──► SERVER PC:  ESSA (port 8000) ──► PostgreSQL (this PC only)
  PC 3 (browser) ─┘
```

Only the server talks to PostgreSQL. The other PCs never see the database or its password.

---

## Part A: the server PC (once)

### A1. ESSA itself
Copy the ESSA folder onto the server (e.g. `C:\ESSA`) and run **`setup.bat`** once.

### A2. Connect ESSA to PostgreSQL
Stop ESSA first (close any `run.bat` window). Then, in `C:\ESSA`:

| Your situation | Run | What it does |
|---|---|---|
| PostgreSQL already holds the ESSA data (e.g. the TAQUA migration filled database `essa`) | `deploy\server\postgres_setup.bat adopt --db essa` | Hands only ESSA's schemas (`public`, `shop`) to a dedicated login `essa_app`. Copies nothing and touches no rows. Saves the old owners to an undo file first. |
| Start from this PC's SQLite data | `deploy\server\postgres_setup.bat` | Creates login `essa_app` and a new database `essa`, then copies every row from both SQLite files and compares the counts |
| Another PostgreSQL port | add `--port 5433` | |

It asks for the **PostgreSQL admin (`postgres`) password**, typed hidden, used once and not saved.
ESSA's own login and its long random password are written to `C:\ESSA\.env`, and nowhere else.

Keep PostgreSQL private to the server. In `postgresql.conf` set `listen_addresses = 'localhost'`,
and do **not** open port 5432 in the firewall: the other PCs only need ESSA's port 8000.

### A3. Make it the server
Right-click **`deploy\server\server_setup.bat`** › **Run as administrator**. It:
- makes ESSA start with Windows, before anyone logs in, and restart if it stops,
- opens port 8000 in Windows Firewall for the private (shop) network only,
- creates ESSA's own login secret in `.env` (everyone signs in again once),
- stops the PC from sleeping while plugged in,
- waits until ESSA answers, then **prints the address for the other PCs**.

### A4. Two things in Windows settings
- **Fixed IP:** reserve the server's IP in the router (DHCP reservation), or set a static IP.
  If the IP changes, every PC's link breaks.
- **Network = Private:** Settings › Network & internet › your network › **Private**.
  On a Public network Windows blocks the other PCs.

### A5. Check
```powershell
deploy\server\server_setup.bat status
```
It shows whether the start-up task is installed, who holds port 8000, the database in use
(postgresql), and the address for the other PCs, with whether it answers.

---

## Part B: every other PC (one minute each)

1. Open **Chrome** or **Edge** and go to the address from A3, e.g. **`http://192.168.1.10:8000`**.
2. Sign in.
3. For a desktop icon and a window of its own, use
   **Edge: ⋯ › Apps › Install this site as an app**, or
   **Chrome: ⋯ › Cast, save and share › Install page as app**.

USB barcode scanners, printing, billing and every screen work as they are.

**Voice input and the phone-camera scanner** need a secure page, which `http://192.168.x.x`
isn't. To allow it on a PC, open `edge://flags/#unsafely-treat-insecure-origin-as-secure`
(or `chrome://flags/#unsafely-treat-insecure-origin-as-secure`), enter `http://192.168.1.10:8000`,
choose Enabled, and restart the browser.

---

## Day to day

| What | How |
|---|---|
| Is the server OK? | `deploy\server\server_setup.bat status` |
| Update ESSA | Update the folder on the server, then restart the server PC. Every PC gets the new version on its next page load. |
| Backups | Run on the server: `deploy\backup\daily_backup.bat` (Run as administrator, once) for 10:30 PM every night; `instant_backup.bat` for a backup right now. PostgreSQL backups hold ESSA's `public` and `shop` schemas. |
| Restart ESSA (after changing `.env` or a database setting) | `deploy\server\server_setup.bat restart` (as administrator) |
| Server log | `C:\ProgramData\ESSA\server.log` |
| Stop being the server | `deploy\server\server_setup.bat remove` (as administrator). Data and `.env` are kept. |
| Back to SQLite | Delete the `ESSA_DATABASE_URL` line in `.env` and restart ESSA |
| Undo "adopt" | Run the `deploy\server\owners-before-adopt-*.sql` file as `postgres` in the `essa` database |

## If a screen hangs on "Loading…" with PostgreSQL 19.0
ESSA's login has PostgreSQL's parallel workers switched off (`postgres_setup` does this), because on
PostgreSQL 19.0 for Windows a parallel join can hang for minutes. To check it, or set it by hand:
```sql
SELECT rolconfig FROM pg_roles WHERE rolname = 'essa_app';   -- {max_parallel_workers_per_gather=0}
ALTER ROLE essa_app SET max_parallel_workers_per_gather = 0;  -- then: server_setup.bat restart
```

## If a PC cannot open ESSA
1. Is the server PC on, and is ESSA running there? Check with `server_setup.bat status`.
2. On that PC, run `Test-NetConnection 192.168.1.10 -Port 8000`. If `False`, check the server's network
   is **Private**, and that both PCs are on the same network or Wi-Fi.
3. Did the server's IP change? Run `ipconfig` on the server, and reserve the IP in the router.
