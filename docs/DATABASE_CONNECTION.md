# Connecting ESSA to the client's PostgreSQL (read-only testing)

ESSA can test and inspect a PostgreSQL database the client already runs, on the
same PC, on another PC in the shop (LAN) or through an authorised VPN, before
ESSA is deployed against it.

> **Two databases, kept apart.** ESSA's *own* database is `ESSA_DATABASE_URL`, or the
> SQLite file when that is blank. This feature never reads, changes or replaces it. The
> client's database is a separate, read-only connection made only from the
> **Setup › Database Connection** screen.

## 1. What it does

| Part | Where |
|---|---|
| Screen (super admin only) | **Setup › Database Connection** (or Ctrl K, type "Database") |
| API | `POST /api/admin/database/test`, `GET /api/admin/database/status`, and `config`, `active`, `connect`, `disconnect`, `write-approval`, `inspect`, `inspect/table` under the same prefix |
| Logic | `backend/app/services/extdb.py` |
| Settings file | `backend/data/db_connections.json`, in Git's ignore list. Passwords are encrypted. |

**Environments:** *Development* (local PostgreSQL), *Client testing* (the client's test
database or a cloned copy) and *Production* (the client's approved live database). Each has
its own saved settings. Only one is connected at a time, and the screen always shows which.
**Production** needs the database name typed back before any connection is opened, and is
shown in red.

**Safety, enforced by code and not only by convention:**
- **Every session is read-only.** It is opened with `default_transaction_read_only=on`, each connection is set read-only, and every use ends in `ROLLBACK`. A write attempt is refused by PostgreSQL itself (`cannot execute INSERT in a read-only transaction`, which is tested).
- **The test query is fixed:** `SELECT current_database(), current_user, version(), NOW()`, plus read-only catalogue lookups. Nothing accepts SQL from the screen.
- **Nothing is changed.** No migrations, no DDL, no INSERT, UPDATE, DELETE or TRUNCATE.
- **Inspection reads structure only:** schemas, tables, columns, keys and size estimates from `pg_catalog`. **Never rows.** Table names are bound as parameters.
- **15-second cap.** `statement_timeout` limits every statement.
- **Writes stay off until approved.** Per environment, an administrator must type `ALLOW WRITES TO <database>`. Nothing in this screen writes even then. The approval is the gate (`extdb.require_write_approval`) that any future import or sync must pass. Changing the server or login clears the approval.

**Secrets:**
- **Where a password lives:** it is typed on the screen and stored encrypted with Windows DPAPI (machine scope, so the file is useless on any other PC), or it is put in `DB_PASSWORD` in the backend `.env`. React never receives it: no response contains it, and no `VITE_` variable exists for it.
- **Logs** record host, port, database, user and the outcome. Never the password or a connection string.
- **No redirecting a saved password.** It is used only for the exact host, port, database and user it was saved with. Change the host and you must type the password again, so a saved password can't be sent to another server.
- **Access** is super admin only. The check is made in `security.py` before the route runs: no sign-in gives 401, and any other role gives 403.

## 2. Installation

Nothing new to install: ESSA already ships the `psycopg2` PostgreSQL driver
(`backend/requirements.txt`). On a fresh PC, run `setup.bat` as usual.

The frontend is part of the normal build. After pulling the code:

```powershell
cd frontend; npm install; npm run build
```

## 3. Configuration (.env)

Optional. The screen can do everything. If you prefer `.env`, add the following to
`ESSA\.env` (see `.env.example`):

```
DB_ENVIRONMENT=client_test      # which environment these values fill
DB_HOST=192.168.1.20
DB_PORT=5432
DB_NAME=client_database
DB_USER=erp_user
DB_PASSWORD=                    # or leave blank and type it on the screen
DB_SSL=false                    # false | prefer | require | verify-ca | verify-full
DB_CONNECTION_TIMEOUT=5000
DB_POOL_MAX=5
```

Settings saved on the screen take precedence over `.env` for that environment.

## 4. Starting the backend

Start ESSA as usual (`run.bat`, `ESSA.exe`, or the server task). The screen and API are part of the
normal server. There is no separate service.

## 5. PostgreSQL server setup on Windows (on the client's server)

Ask the client's DBA to do this. **Do not change a production server without their approval.**

1. **A dedicated, least-privilege login** for ESSA. Never use `postgres`:
   ```sql
   CREATE ROLE erp_user LOGIN PASSWORD 'a-long-random-password';
   GRANT CONNECT ON DATABASE client_database TO erp_user;
   \c client_database
   GRANT USAGE ON SCHEMA public TO erp_user;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO erp_user;       -- read-only testing
   ALTER ROLE erp_user SET default_transaction_read_only = on;    -- belt and braces
   ```
2. **Listen on the network.** In `postgresql.conf` (in the data folder, e.g.
   `C:\Program Files\PostgreSQL\16\data\`):
   ```
   listen_addresses = '*'          # or the server's LAN IP, e.g. '192.168.1.20'
   port = 5432
   ```
3. **Allow the ESSA PC in `pg_hba.conf`.** Make the rule as narrow as you can:
   ```
   # TYPE  DATABASE         USER      ADDRESS            METHOD
   host    client_database  erp_user  192.168.1.50/32    scram-sha-256   # one PC
   host    client_database  erp_user  192.168.1.0/24     scram-sha-256   # or the shop LAN
   hostssl client_database  erp_user  10.8.0.0/24        scram-sha-256   # VPN range, SSL only
   ```
4. **Restart the service:** `services.msc` › *postgresql-x64-NN* › Restart. A reload is
   enough for `pg_hba.conf` changes: `SELECT pg_reload_conf();`.
5. **IIS / ASP.NET on the same server:** no conflict. They use ports 80 and 443; PostgreSQL uses
   5432. Leave their connection strings alone. ESSA's settings are separate.

## 6. Windows Firewall and LAN / VPN access

On the **PostgreSQL server**, as administrator:

```powershell
New-NetFirewallRule -DisplayName "PostgreSQL 5432 (ESSA)" -Direction Inbound -Protocol TCP -LocalPort 5432 -RemoteAddress 192.168.1.0/24 -Profile Private,Domain -Action Allow
```

- `-RemoteAddress` limits it to the shop network. Use the VPN's range for VPN users.
- **Never open 5432 to the internet.** For remote access use the client's VPN, and set SSL mode
  to `require`.
- The server's network must be **Private** or **Domain**, not Public (Settings › Network).

From the **ESSA PC**, check the network path before testing in ESSA:

```powershell
Test-NetConnection 192.168.1.20 -Port 5432
```

`TcpTestSucceeded : True` means the network and firewall are fine.

## 7. Testing the connection

1. Sign in as a **super admin**, then open **Setup › Database Connection**.
2. Choose the environment. Start with *Development* or *Client testing*, not *Production*.
3. Enter the host or IP, port, database, user and password, and choose the SSL mode and timeout.
4. **Test Connection.** It runs four steps and shows each one with its time:
   *Find the server* (DNS), *Reach port*, *Log in to PostgreSQL*, and *Read-only test query*.
5. On success it shows the server version, database, user, server time, response time,
   readable tables and warnings (partial access, superuser login, no SSL off-site).
6. **Save Configuration** to keep the settings. Then **Connect** opens the read-only pool,
   **Refresh Status** re-checks it, and **Disconnect** closes it.
7. Once connected, **Database inspection** lists schemas and tables with estimated rows and
   size. Click a table for its columns, types, primary key and foreign keys. No rows are shown.

## 8. Troubleshooting

Each failure comes back with a code, a plain explanation and the steps to fix it.

| Code | Meaning | Usually fixed by |
|---|---|---|
| `INVALID_HOST` | The name or IP does not resolve | Check spelling; use the IP; connect the VPN |
| `PORT_CLOSED` | The server refused the port | Service stopped (services.msc), wrong port, `listen_addresses` still `localhost` |
| `NETWORK_TIMEOUT` | No answer before the timeout | Firewall dropping 5432, wrong IP, VPN down; raise the timeout on a slow link |
| `NETWORK_UNREACHABLE` | No route to that address | Wrong network or VPN not connected |
| `AUTH_FAILED` | Wrong password, or the user does not exist | Check credentials with the DBA |
| `PASSWORD_REQUIRED` | No password was sent | Type it; a saved one is only reused for the same server and login |
| `HBA_REJECTED` | `pg_hba.conf` has no rule for this PC, user or database | Add a `host` line (section 5.3) and reload |
| `DB_NOT_FOUND` | Login works, the database name does not | Check the exact name (`psql -l`) |
| `SSL_MISMATCH` | SSL asked for but off on the server, or certificate not trusted | Use `prefer` on the LAN; `require` needs `ssl = on` on the server |
| `PERMISSION_DENIED` | No CONNECT right on that database | `GRANT CONNECT ON DATABASE … TO erp_user` |
| `NO_READ_ACCESS` / `PARTIAL_READ_ACCESS` | Connected, but some or all tables are unreadable | `GRANT USAGE ON SCHEMA …` and `GRANT SELECT …` as needed |
| `TOO_MANY_CONNECTIONS` | The server is at `max_connections` | Lower the pool size; ask the DBA |

The server log (`C:\Program Files\PostgreSQL\NN\data\log\`) shows the server's side of any
refusal at the same time.

## 9. What was tested, and what was not

These were tested on this development PC against a throwaway PostgreSQL 19 cluster with
password (scram) login, through the real API (59 of 59 checks passed):

| Scenario | Result |
|---|---|
| localhost connection | connected, read-only session, version, time, response time |
| LAN address (this PC's 192.168.x address) | connected |
| wrong password / unknown user | `AUTH_FAILED` |
| PostgreSQL service stopped | `PORT_CLOSED` |
| wrong port | `PORT_CLOSED` |
| user without CONNECT / without SELECT / missing one table | `PERMISSION_DENIED` / `NO_READ_ACCESS` / `PARTIAL_READ_ACCESS` |
| unreachable address, 2 s timeout | `NETWORK_TIMEOUT` after 2.0 s |
| unknown host name / database | `INVALID_HOST` / `DB_NOT_FOUND` |
| `pg_hba.conf` refusing the LAN address | `HBA_REJECTED` |
| SSL required, server without SSL | `SSL_MISMATCH` |
| production without the typed name | refused (409) |
| an INSERT forced through the pool | refused by PostgreSQL (read-only transaction) |
| data and structure before and after all tests | identical (row checksums, catalogue, write counters) |
| every password, in all API responses and all logs | never present |
| not signed in / admin instead of super admin | 401 / 403 |

**Not tested: needs the client's environment.**
- A real second machine on the client's LAN, and their VPN. The LAN test used this PC's own LAN address.
- The client's PostgreSQL version and settings, any SSL certificates (`verify-ca`, `verify-full`), and IIS on their server.
- Connection poolers (PgBouncer) in front of their database. These may reject the read-only `options`; if so, rely on the role-level `default_transaction_read_only` from section 5.1.
- DPAPI-saved passwords on a server where ESSA runs as SYSTEM: the machine-scope key is used, so this should work, but it was tested only under a normal user.

## 10. Files changed

| File | Change |
|---|---|
| `backend/app/services/extdb.py` | **new**: connection, diagnostics, pool, read-only inspection, secret handling |
| `backend/app/routers/admin_database.py` | **new**: the `/api/admin/database/*` endpoints with input validation |
| `backend/app/main.py` | registers the router (2 lines) |
| `backend/app/security.py` | `/api/admin/database` restricted to super admin (1 rule) |
| `frontend/src/DatabaseConnection.jsx` | **new**: the screen |
| `frontend/src/App.jsx` | menu entry, Setup group, icon, route (no other change) |
| `frontend/src/api.js` | API helpers; `/api/admin/` never cached |
| `.env.example` | `DB_*` example settings, no real values |
| `.gitignore` | `backend/data/db_connections.json` |
| `docs/DATABASE_CONNECTION.md` | this guide |
