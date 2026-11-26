# ESSA go-live at the client: from migration to handover

**Client server:** Windows Server 2016 (64-bit) with PostgreSQL 12. Quanta (ASP.NET on IIS) runs on the same server.
**Goal for the day:** Quanta's data (TAQUALIVE, 237 tables) is converted into ESSA. ESSA runs as the server for every shop PC, backups are on, and the client signs off.

Tick each box as you go. Never write a password into this paper, a script or a chat.

---

## 0. Before leaving our office (today)

| ✓ | Item |
|---|---|
| ☐ | `ESSA-Client-Package-WS2016-PG12.zip` on **two** pen drives |
| ☐ | Python **3.12 64-bit** installer (`python-3.12.x-amd64.exe`) on the pen drive, in case the server has no internet |
| ☐ | **Chrome** offline installer (`ChromeStandaloneSetup64.exe`) on the pen drive. Server 2016 only has Internet Explorer, which can't run ESSA |
| ☐ | Laptop with this checklist |
| ☐ | Decide the **temporary first sign-in password** for all migrated users. Tell it to the client in person; don't write it here |

**Ask the client to have ready:**

| ✓ | Item |
|---|---|
| ☐ | A Windows **administrator** login on the server |
| ☐ | The PostgreSQL **`postgres` password** (their IT or Quanta vendor has it) |
| ☐ | **pgAdmin** working on the server |
| ☐ | Router access (to fix the server's IP), or the IT person |
| ☐ | Agreement on the **cut-over time**: when Quanta billing stops for good (see step 3) |
| ☐ | List of staff and the role each should have in ESSA |

---

## 1. Check the server (15 min)

| ✓ | Check | How / what we need |
|---|---|---|
| ☐ | Windows version | `winver`: Server 2016, 64-bit |
| ☐ | PostgreSQL version | pgAdmin › server › Properties, or `"C:\Program Files\PostgreSQL\12\bin\psql" --version`: 12.x |
| ☐ | Free disk space | At least **40 GB free** on the PostgreSQL drive (legacy copy + ESSA + backups) |
| ☐ | Size of TAQUALIVE | pgAdmin › TAQUALIVE › Properties › Statistics. Note it here: ______ |
| ☐ | Server IP | `ipconfig`: ____________ |
| ☐ | Port 8000 is free | `netstat -ano \| findstr :8000` shows nothing (IIS / Quanta use 80/443) |
| ☐ | Note how Quanta reaches PostgreSQL | **Don't change** `postgresql.conf` or the 5432 firewall rule; Quanta may need them |

---

## 2. Install ESSA (20 min)

1. ☐ Install **Python 3.12 64-bit** and tick **"Add python.exe to PATH"**. Check it in a new PowerShell with `python --version`.
2. ☐ Install **Chrome** (or Edge).
3. ☐ Unzip the package and copy the `ESSA` folder to **`C:\ESSA`**.
4. ☐ Double-click **`C:\ESSA\setup.bat`**. It must end with **"Setup complete"**. Don't run `run.bat`.

---

## 3. Cut-over: stop Quanta billing and take the final backup

From this point, **nothing new may be entered in Quanta**. Any bill made in Quanta after the backup won't be in ESSA.
The best time is after shop closing, or before opening.

1. ☐ Confirm with the client that billing in Quanta has stopped on every counter. Time: ______
2. ☐ Create the folder `D:\migration` (or `C:\migration` if there's no D:).
3. ☐ pgAdmin › right-click **TAQUALIVE** › **Backup…**
   - Format: **Custom**
   - Filename: `D:\migration\TAQUALIVE.backup`
   - Click Backup and wait for "completed successfully".
4. ☐ Copy `TAQUALIVE.backup` to the pen drive too; it's the client's last Quanta state.

TAQUALIVE itself is **never changed** by anything below. Quanta stays installed as the fallback.

---

## 4. Convert the data (allow 3–5 hours; start it and wait)

1. ☐ pgAdmin › Databases › right-click › **Create › Database…**, name **`legacy_tqa`**, then Save.
2. ☐ Open **PowerShell** and go to the ESSA folder:
   ```powershell
   cd C:\ESSA
   ```
3. ☐ Set the connection details for this window only. Replace `<pw>` with the postgres password as you type, and don't save it anywhere:
   ```powershell
   $env:LEGACY_DATABASE_URL = "postgresql://postgres:<pw>@localhost:5432/legacy_tqa"
   $env:ESSA_DATABASE_URL   = "postgresql://postgres:<pw>@localhost:5432/essa"
   $env:TQA_TEMP_PASSWORD   = "<temporary first sign-in password>"
   ```
4. ☐ **Load the backup** into `legacy_tqa`. It finds PostgreSQL 12's tools by itself.
   ```powershell
   backend\.venv\Scripts\python.exe migration\tqa\load_backup.py "D:\migration\TAQUALIVE.backup"
   ```
   It must end with **`DONE`**, with no "mismatches". Time taken: ______
5. ☐ **Prepare the ESSA database.** It creates `essa`, builds ESSA's tables, stages the old data and keeps a clean copy for re-runs.
   ```powershell
   backend\.venv\Scripts\python.exe migration\tqa\prepare_target.py --recreate --save-base
   ```
6. ☐ **Migrate:**
   ```powershell
   backend\.venv\Scripts\python.exe migration\tqa\migrate.py
   ```
   It stops at the first problem and leaves the database as it was before that step. Time taken: ______
7. ☐ **Validate:**
   ```powershell
   backend\.venv\Scripts\python.exe migration\tqa\validate.py
   ```
   It **must end with "no HARD check failed"**. The reports are in `C:\ESSA\migration\tqa\output\`. Copy that folder to the pen drive.
8. ☐ **Close this PowerShell window.** It holds the postgres password.

**If a step fails:** note the message and the step, then fix it. Re-run from the clean copy without reloading the backup:
```powershell
backend\.venv\Scripts\python.exe migration\tqa\prepare_target.py --from-base
backend\.venv\Scripts\python.exe migration\tqa\migrate.py
```
(Set the three `$env:` lines again first in a new window.)

---

## 5. Connect ESSA to the converted database (5 min)

In a **new** PowerShell window in `C:\ESSA`:
```powershell
deploy\server\postgres_setup.bat adopt --db essa
```
☐ It asks for the postgres password (hidden, not saved), creates ESSA's own login `essa_app` and writes the connection into `C:\ESSA\.env`.

If the migration is ever run again with `--recreate`, run this adopt step again after it.

---

## 6. Make ESSA the server (10 min)

1. ☐ Right-click **`C:\ESSA\deploy\server\server_setup.bat`** › **Run as administrator**. It:
   - starts ESSA with Windows and restarts it if it stops,
   - opens port 8000 for the shop network only,
   - stops the server from sleeping,
   - prints **the address for the other PCs**. Write it here: `http://____________:8000`
   - puts **"Restart ESSA Server"** and **"ESSA"** on the desktop.
2. ☐ Check it:
   ```powershell
   deploy\server\server_setup.bat status
   ```
   Database must say **postgresql**, and the address must answer.
3. ☐ **Fixed IP:** reserve the server's IP in the router, or set a static IP. If it changes, every PC's link breaks.
4. ☐ **Network = Private:** Server Manager › Local Server, or the network settings.
5. ☐ **Windows Update active hours** set to cover shop hours, so the server never restarts during billing.

---

## 7. Check the data with the client (30–45 min)

Sign in on the server with the **ESSA** icon. Compare against Quanta with the client watching:

| ✓ | Check | Quanta | ESSA |
|---|---|---|---|
| ☐ | Number of items / products | | |
| ☐ | Stock of 5 items the client picks | | |
| ☐ | Last bill number and date | | |
| ☐ | Yesterday's sales total | | |
| ☐ | One supplier's balance | | |
| ☐ | One customer's balance / points | | |
| ☐ | Users can sign in with the temporary password | | |

☐ Make **one test bill** at the billing counter and print it. Then cancel or return it, as the client prefers.

---

## 8. Set up every shop PC (2 min each)

On each PC:
1. ☐ Open **Chrome** or **Edge** and go to `http://<server IP>:8000`.
2. ☐ Sign in.
3. ☐ For a desktop icon: **Edge › ⋯ › Apps › Install this site as an app**, or **Chrome › ⋯ › Cast, save and share › Install page as app**.
4. ☐ Test the **barcode scanner** and the **bill printer** on that PC.

| PC / counter | Opens ESSA | Scanner | Printer |
|---|---|---|---|
| | ☐ | ☐ | ☐ |
| | ☐ | ☐ | ☐ |
| | ☐ | ☐ | ☐ |
| | ☐ | ☐ | ☐ |

If a PC can't open it, run `Test-NetConnection <server IP> -Port 8000` on that PC. If the result is False, check that the server's network is **Private** and that both PCs are on the same network.

---

## 9. Backups (10 min)

1. ☐ Right-click **`C:\ESSA\deploy\backup\daily_backup.bat`** › **Run as administrator**. This backs up every night at **10:30 PM** and takes a first backup now. It must report OK.
2. ☐ Run **`deploy\backup\instant_backup.bat`** once. Show the client this is the "backup now" button.
3. ☐ Backups go to `D:\ESSA-BACKUP`. Tell the client to copy that folder to a pen drive weekly.

---

## 10. Handover

**Train (30 min):** billing, returns, stock check, the reports the owner uses daily, and adding an item.

**Show the client:**

| ✓ | Item |
|---|---|
| ☐ | The **"Restart ESSA Server"** icon: if ESSA stops opening on any PC, double-click it on the server |
| ☐ | Every user changes the temporary password after signing in (ESSA doesn't force it, so do it with each person today) |
| ☐ | The owner / admin account and Users & Access: who can do what |
| ☐ | Backups: nightly at 10:30 PM, plus the "backup now" button |
| ☐ | Server must stay **on**; don't shut it down during shop hours |
| ☐ | **Quanta stays installed but isn't used for billing.** Keep it read-only for old lookups |

**Leave with the client:** the server address, our support contact, and `C:\ESSA\READ-ME-FIRST.txt`.

**Take back with us:** `migration\tqa\output\` reports, `TAQUALIVE.backup`, and this filled-in checklist.

**Sign-off**

| | Name | Signature | Date / time |
|---|---|---|---|
| Client | | | |
| Us | | | |

---

## If something goes wrong

| Problem | Do this |
|---|---|
| Migration fails and can't be fixed today | Don't connect ESSA. Restart billing **in Quanta**; nothing in it was changed. Bring the `output` reports back to the office. |
| ESSA doesn't open on the server | Double-click **Restart ESSA Server**, then check `C:\ProgramData\ESSA\server.log` |
| ESSA opens on the server but not on other PCs | Network must be Private; check `Test-NetConnection <IP> -Port 8000` from that PC |
| `setup.bat` fails installing packages | Check the internet, or use a package that includes the `wheels` folder |
| A screen shows wrong figures | Note the screen and the item, then compare with the validate reports in `migration\tqa\output\` |
