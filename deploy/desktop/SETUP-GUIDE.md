# ESSA on a shop network: one server, many counters

One PC is the **server**: ESSA and PostgreSQL run there, and only there.
Every other PC is a **counter**: it runs `ESSA.exe`, which opens a window onto the
server. The database password never leaves the server, and each click crosses
the network as one request instead of dozens of database queries.

```
  Counter PC 1 ─┐
  Counter PC 2 ─┼──  http://SERVER:8000  ──►  Server PC:  ESSA  ──►  PostgreSQL (localhost only)
  Counter PC 3 ─┘
```

## A. The server PC (once)

Pick the PC that will stay on all day, the one that already has PostgreSQL.

1. **Copy the ESSA folder** onto it, e.g. `C:\ESSA`, and run `setup.bat` once.
2. **Point ESSA at PostgreSQL on this same PC.** In `C:\ESSA\.env`:
   ```
   ESSA_DATABASE_URL=postgresql://essa:PASSWORD@localhost:5432/essa
   ```
   Keep `localhost` here. The password lives only in this file, on this PC.
3. **Keep PostgreSQL private.** In `postgresql.conf`, leave `listen_addresses = 'localhost'`
   (the default). Do not open port 5432 in the firewall.
4. **Give the server a fixed address.** Reserve its IP in the router (DHCP reservation),
   or set a static IP. If the IP changes, every counter loses the server.
5. **Set the network to Private.** Settings > Network & internet > (your network) > Private.
   On a Public network Windows blocks the counters.
6. **Run `C:\ESSA\ESSA.exe`** and choose **This PC is the server**, then **Save and open ESSA**.
   Allow the administrator prompt. Setup then:
   - creates a login secret of its own in `.env` (everyone signs in again once),
   - checks that the database is on this PC,
   - makes ESSA start with Windows, before anyone logs in, and restart if it stops,
   - opens port 8000 in the firewall, for private networks only,
   - stops the PC sleeping while it is plugged in,
   - **shows the address the counters need**. Note it down.

   A copy of that report is in `C:\ProgramData\ESSA\server-setup.txt`.
7. **Restart the server PC once.** Before logging in, open `http://SERVER-IP:8000` from
   another PC: ESSA should answer. That proves it starts on its own.
8. **Backups run here.** Set them up on this PC: `deploy\backup\daily_backup.bat` > Run as administrator.

## B. Each counter PC

1. **Copy just `ESSA.exe`** onto the PC (e.g. into `C:\ESSA`). Nothing else is needed:
   no Python, no database, no ESSA folder.
2. **Make a desktop icon:** right-click `ESSA.exe` > Send to > Desktop (create shortcut).
3. **Open it** and choose **Connect to a server**. Then either:
   - press **Find**: it searches the network and lists every ESSA server it finds; click yours, or
   - type the server address from step A6, e.g. `192.168.1.10` (or the server PC's name).

   Press **Test**, then **Save and open ESSA**.
4. **From then on, double-clicking ESSA opens it directly.** If the server cannot be reached, it
   says so and offers **Try again** or **Change server**.

The first time, Windows may say "Windows protected your PC" because the exe is not
signed: **More info > Run anyway**.

## C. Day to day

| Task | How |
|---|---|
| Change a PC between server and counter, or change the server address | Hold **Shift** while opening ESSA, or run `ESSA.exe --setup` |
| Redo the server setup (e.g. after moving the ESSA folder) | Right-click `ESSA.exe` > Run as administrator, with `--server-setup` |
| Update ESSA | Update the ESSA folder **on the server only**, then restart the server PC. Every counter gets the new version on its next click. |
| ESSA does not open on a counter | Is the server PC on? Same network? Try `http://SERVER-IP:8000` in a browser on the counter |
| Server-side errors | `C:\ProgramData\ESSA\server.log` on the server |
| Counter-side log | `%LOCALAPPDATA%\ESSA\desktop.log` on that PC |

## D. A single-PC shop

Choose **This PC is the server** and nothing else changes: the same PC is the server and its own counter.

## Rebuilding ESSA.exe

`deploy\desktop\build_exe.bat` builds it from `essa_desktop.py` and puts it in the ESSA folder.
It needs Python and the internet the first time.
