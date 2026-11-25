// ==========================================================================
//  Database Connection — super admin only
//  ------------------------------------------------------------------------
//  Tests and inspects an OUTSIDE PostgreSQL database (the client's), strictly
//  read-only. ESSA's own database is not touched by anything on this screen.
//  The password is typed here and sent to the server once; it is never sent
//  back, so the field only ever says whether one is saved.
//  Server side: backend/app/routers/admin_database.py, services/extdb.py
// ==========================================================================
import { Fragment, useEffect, useState } from 'react'
import { api } from './api.js'

const ENVS = [
  ['development', 'Development', 'Local PostgreSQL on this PC or a developer machine'],
  ['client_test', 'Client testing', "The client's test database or a cloned copy"],
  ['production', 'Production', "The client's live database — connect only with approval"],
]
const SSL = [
  ['disable', 'disable — no encryption'],
  ['allow', 'allow'],
  ['prefer', 'prefer — encrypt if the server can'],
  ['require', 'require — always encrypted'],
  ['verify-ca', 'verify-ca — encrypted, CA checked'],
  ['verify-full', 'verify-full — encrypted, CA and host checked'],
]
const BLANK = { host: '127.0.0.1', port: 5432, database: '', user: '', sslmode: 'prefer', timeout_ms: 5000, pool_max: 5 }

const when = (s) => (s ? new Date(s).toLocaleString() : '—')
const bytes = (n) => {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`
}
const envTone = (e) => (e === 'production' ? 'cancelled' : e === 'client_test' ? 'pending' : 'confirmed')

export default function DatabaseConnection({ toast }) {
  const [st, setSt] = useState(null)
  const [env, setEnv] = useState('development')
  const [form, setForm] = useState(BLANK)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState('')
  const [tables, setTables] = useState(null)
  const [schema, setSchema] = useState('')
  const [filter, setFilter] = useState('')
  const [detail, setDetail] = useState(null)
  const [phrase, setPhrase] = useState('')

  const saved = st?.environments?.[env] || null
  const isProd = env === 'production'
  const connectedHere = st?.connected && st.environment === env

  const fill = (e, status) => {
    const p = status?.environments?.[e]
    setForm(p ? { host: p.host, port: p.port, database: p.database, user: p.user, sslmode: p.sslmode,
      timeout_ms: p.timeout_ms, pool_max: p.pool_max } : BLANK)
    setPassword(''); setConfirm(''); setPhrase(''); setResult(null)
  }
  const refresh = (pick) => api.dbStatus().then((s) => {
    setSt(s)
    if (pick) { const e = s.environment || s.active || 'development'; setEnv(e); fill(e, s) }
    return s
  }).catch((e) => toast(e.detail || 'Could not read the connection status', 'err'))
  useEffect(() => { refresh(true) }, [])
  useEffect(() => { if (!st?.connected) { setTables(null); setDetail(null) } }, [st?.connected])

  const chooseEnv = (e) => { setEnv(e); fill(e, st) }
  const set = (k) => (ev) => setForm((f) => ({ ...f, [k]: ev.target.value }))
  const body = () => ({ environment: env, ...form, port: Number(form.port), timeout_ms: Number(form.timeout_ms),
    pool_max: Number(form.pool_max), password: password || null, confirm: isProd ? confirm : null })

  const run = async (what, fn) => {
    setBusy(what)
    try { return await fn() } catch (e) { toast(e.detail || 'That did not work', 'err'); return null } finally { setBusy('') }
  }
  const doTest = () => run('test', async () => {
    const r = await api.dbTest(body()); setResult(r)
    toast(r.ok ? `Connected in ${Math.round(r.total_ms)} ms` : r.issue?.title || 'Connection failed', r.ok ? 'ok' : 'err')
    refresh()
  })
  const doSave = () => run('save', async () => {
    await api.dbSaveConfig(body()); setPassword('')
    toast(`${ENVS.find((x) => x[0] === env)[1]} settings saved`, 'ok'); refresh()
  })
  const doConnect = () => run('connect', async () => {
    const r = await api.dbConnect({ environment: env, confirm: isProd ? confirm : null }); setResult(r)
    toast(r.ok ? 'Connected (read-only)' : r.issue?.title || 'Could not connect', r.ok ? 'ok' : 'err')
    await refresh(); if (r.ok) loadTables('')
  })
  const doDisconnect = () => run('disconnect', async () => {
    await api.dbDisconnect(); toast('Disconnected', 'ok'); refresh()
  })
  const doRefresh = () => run('refresh', () => refresh())
  const loadTables = (s) => run('inspect', async () => { setTables(await api.dbInspect(s)); setDetail(null) })
  const openTable = (t) => run('table', async () => setDetail(await api.dbInspectTable(t.schema, t.name)))
  const doApproval = (approve) => run('approve', async () => {
    await api.dbWriteApproval({ environment: env, approve, confirm: approve ? phrase : null })
    setPhrase(''); toast(approve ? 'Write approval recorded' : 'Write approval revoked', 'ok'); refresh()
  })

  const changedLogin = saved && ['host', 'port', 'database', 'user'].some((k) => String(saved[k]) !== String(form[k]))
  const shown = (tables?.tables || []).filter((t) => !filter || `${t.schema}.${t.name}`.toLowerCase().includes(filter.toLowerCase()))
  const info = st?.connected ? st.info : null
  const lastOk = st?.last_success?.[env]
  const lastErr = st?.last_error?.[env]

  return (
    <div className="screen scrolls">
      <div className="pagehead">
        <h2>Database Connection</h2>
        <div className="pagesub small">
          Test and inspect the client's PostgreSQL — read-only. Nothing here writes to that database,
          and ESSA's own database is not changed.
        </div>
      </div>
      <div className="screenbody">

        {/* 1. which environment */}
        <div className="section" style={{ maxWidth: 1080 }}>
          <div className="toolbar" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
            <div className="segbar" role="tablist" aria-label="Environment">
              {ENVS.map(([k, label]) => (
                <button key={k} role="tab" aria-selected={env === k} className={'seg' + (env === k ? ' on' : '')}
                  onClick={() => chooseEnv(k)}>{label}</button>
              ))}
            </div>
            <span className={'badge ' + envTone(env)}>{ENVS.find((x) => x[0] === env)[1]}</span>
            {st?.connected
              ? <span className="badge confirmed">Connected · {st.labels?.[st.environment]}</span>
              : <span className="badge draft">Disconnected</span>}
            <span className="small" style={{ color: 'var(--muted)' }}>{ENVS.find((x) => x[0] === env)[2]}</span>
          </div>
          {isProd && (
            <div className="warnbox" style={{ marginTop: 12, background: 'var(--danger-bg)', borderColor: 'var(--danger-line)' }}>
              <h4 style={{ color: 'var(--danger)' }}>Production database</h4>
              <ul>
                <li>Connect only with the client's approval. Every session is read-only.</li>
                <li>To test or connect, type the database name in the confirmation box below.</li>
              </ul>
            </div>
          )}
        </div>

        {/* 2. the settings */}
        <div className="section" style={{ maxWidth: 1080 }}>
          <h3 style={{ marginTop: 0 }}>Connection settings</h3>
          <div className="grid">
            <div className="field"><label htmlFor="db-host">Host / IP</label>
              <input id="db-host" value={form.host} onChange={set('host')} placeholder="127.0.0.1 or 192.168.1.20" autoComplete="off" /></div>
            <div className="field"><label htmlFor="db-port">Port</label>
              <input id="db-port" type="number" min="1" max="65535" value={form.port} onChange={set('port')} /></div>
            <div className="field"><label htmlFor="db-name">Database name</label>
              <input id="db-name" value={form.database} onChange={set('database')} autoComplete="off" /></div>
            <div className="field"><label htmlFor="db-user">Username</label>
              <input id="db-user" value={form.user} onChange={set('user')} autoComplete="off" /></div>
            <div className="field"><label htmlFor="db-pass">Password</label>
              <input id="db-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                placeholder={saved?.has_password && !changedLogin ? `•••••••• (${saved.password_source})` : 'not saved'} /></div>
            <div className="field"><label htmlFor="db-ssl">SSL mode</label>
              <select id="db-ssl" value={form.sslmode} onChange={set('sslmode')}>
                {SSL.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select></div>
            <div className="field"><label htmlFor="db-timeout">Connection timeout (ms)</label>
              <input id="db-timeout" type="number" min="1000" max="60000" step="500" value={form.timeout_ms} onChange={set('timeout_ms')} /></div>
            <div className="field"><label htmlFor="db-pool">Pool size</label>
              <input id="db-pool" type="number" min="1" max="20" value={form.pool_max} onChange={set('pool_max')} /></div>
            {isProd && (
              <div className="field"><label htmlFor="db-confirm">Type the database name to confirm</label>
                <input id="db-confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off"
                  placeholder={form.database || 'database name'} /></div>
            )}
          </div>
          {changedLogin && saved?.has_password && (
            <div className="small" style={{ color: 'var(--warn)', marginTop: 8 }}>
              The server or login differs from the saved one, so the saved password is not used — type it.
            </div>
          )}
          {!st?.can_store_passwords && (
            <div className="small" style={{ color: 'var(--muted)', marginTop: 8 }}>
              This server cannot store passwords; put DB_PASSWORD in the backend .env.
            </div>
          )}
          <div className="toolbar" style={{ gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
            <button className="btn primary" onClick={doTest} disabled={!!busy}>{busy === 'test' ? 'Testing…' : 'Test Connection'}</button>
            <button className="btn" onClick={doSave} disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save Configuration'}</button>
            <button className="btn" onClick={doConnect} disabled={!!busy || !saved}
              title={saved ? 'Open a read-only connection with the saved settings' : 'Save the settings first'}>
              {busy === 'connect' ? 'Connecting…' : 'Connect'}</button>
            <button className="btn danger" onClick={doDisconnect} disabled={!!busy || !st?.connected}>Disconnect</button>
            <button className="btn" onClick={doRefresh} disabled={!!busy}>{busy === 'refresh' ? 'Refreshing…' : 'Refresh Status'}</button>
          </div>
          {saved?.saved_at && (
            <div className="small" style={{ color: 'var(--muted)', marginTop: 8 }}>
              Saved {when(saved.saved_at)}{saved.saved_by ? ` by ${saved.saved_by}` : ''}
              {saved.source === '.env' ? ' · from the backend .env' : ''}
            </div>
          )}
        </div>

        {/* 3. the state of the connection */}
        <div className="section" style={{ maxWidth: 1080 }}>
          <h3 style={{ marginTop: 0 }}>Connection status</h3>
          <div className="kv">
            <span className="k">Status</span>
            <span>{connectedHere ? <span className="badge confirmed">Connected</span>
              : st?.connected ? <span className="badge pending">Connected to {st.labels?.[st.environment]}, not this one</span>
              : <span className="badge draft">Disconnected</span>}</span>
            <span className="k">Server version</span><span>{(connectedHere && info?.server_version) || result?.info?.server_version || '—'}</span>
            <span className="k">Database</span><span>{(connectedHere && info?.database) || result?.info?.database || '—'}</span>
            <span className="k">Database user</span><span>{(connectedHere && info?.user) || result?.info?.user || '—'}</span>
            <span className="k">Server time</span><span>{when((connectedHere && info?.server_time) || result?.info?.server_time)}</span>
            <span className="k">Response time</span>
            <span>{connectedHere && st.response_ms != null ? `${Math.round(st.response_ms)} ms`
              : result?.total_ms != null ? `${Math.round(result.total_ms)} ms (last test)` : '—'}</span>
            <span className="k">Last successful connection</span><span>{lastOk ? `${when(lastOk.at)} · ${Math.round(lastOk.response_ms)} ms` : '—'}</span>
            <span className="k">Session</span>
            <span>{(connectedHere ? info : result?.info)?.session_read_only ? 'read-only (enforced by PostgreSQL)' : 'read-only'}</span>
            {lastErr && <><span className="k">Last error</span>
              <span style={{ color: 'var(--danger)' }}>{lastErr.issue?.title} · {when(lastErr.at)}</span></>}
          </div>
          {st?.connected && st.error && (
            <div className="warnbox" style={{ marginTop: 12 }}><h4>{st.error.title}</h4><div className="small">{st.error.detail}</div></div>
          )}
        </div>

        {/* 4. what the last test or connect found */}
        {result && (
          <div className="section" style={{ maxWidth: 1080 }}>
            <h3 style={{ marginTop: 0 }}>Diagnostics</h3>
            <div className="tablewrap">
              <table className="items">
                <thead><tr><th style={{ width: 34 }} /><th>Step</th><th>Result</th><th style={{ textAlign: 'right' }}>Time</th></tr></thead>
                <tbody>
                  {result.steps.map((s) => (
                    <tr key={s.step}>
                      <td style={{ color: s.ok ? 'var(--ok)' : 'var(--danger)', fontWeight: 700 }}>{s.ok ? '✓' : '✗'}</td>
                      <td><b>{s.step}</b></td>
                      <td className="small">{s.detail}</td>
                      <td className="small" style={{ textAlign: 'right' }}>{Math.round(s.ms)} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {result.issue && <Issue issue={result.issue} danger />}
            {(result.warnings || []).map((w) => <Issue key={w.code} issue={w} />)}
            {result.ok && !(result.warnings || []).length && (
              <div className="warnbox clean" style={{ marginTop: 12 }}><h4>All checks passed</h4>
                <div className="small">{result.info.readable_tables} of {result.info.tables} tables readable ·
                  {' '}{result.info.standby ? 'standby (read replica)' : 'primary server'} · {result.info.version}</div></div>
            )}
          </div>
        )}

        {/* 5. read-only inspection */}
        <div className="section" style={{ maxWidth: 1080 }}>
          <h3 style={{ marginTop: 0 }}>Database inspection</h3>
          {!st?.connected ? (
            <div className="empty">Connect to a saved environment to see its schemas and tables. No records are read — only the structure.</div>
          ) : (
            <>
              <div className="toolbar" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                <select className="sel" value={schema} onChange={(e) => { setSchema(e.target.value); loadTables(e.target.value) }}
                  aria-label="Schema">
                  <option value="">All schemas</option>
                  {(tables?.schemas || []).map((s) => <option key={s.name} value={s.name} disabled={!s.usable}>
                    {s.name}{s.usable ? '' : ' (no access)'}</option>)}
                </select>
                <input type="search" placeholder="Filter tables…" value={filter} onChange={(e) => setFilter(e.target.value)} />
                <button className="btn" onClick={() => loadTables(schema)} disabled={!!busy}>
                  {busy === 'inspect' ? 'Reading…' : tables ? 'Reload' : 'Read structure'}</button>
                {tables && <span className="small" style={{ color: 'var(--muted)' }}>
                  {tables.schemas.length} schemas · {tables.tables.length} tables and views{tables.truncated ? ' (first 5000)' : ''}</span>}
              </div>
              {tables && (
                <div className="tablewrap" style={{ maxHeight: 360, overflow: 'auto' }}>
                  <table className="items">
                    <thead><tr><th>Schema</th><th>Name</th><th>Kind</th><th style={{ textAlign: 'right' }}>Rows (approx.)</th>
                      <th style={{ textAlign: 'right' }}>Size</th><th>Access</th></tr></thead>
                    <tbody>
                      {shown.map((t) => (
                        <tr key={t.schema + '.' + t.name} onClick={() => openTable(t)} style={{ cursor: 'pointer' }}
                          className={detail && detail.schema === t.schema && detail.table === t.name ? 'sel' : ''}>
                          <td className="small">{t.schema}</td><td><b>{t.name}</b></td><td className="small">{t.kind}</td>
                          <td className="small" style={{ textAlign: 'right' }}>{t.approx_rows == null ? '—' : t.approx_rows.toLocaleString()}</td>
                          <td className="small" style={{ textAlign: 'right' }}>{bytes(t.size_bytes)}</td>
                          <td className="small">{t.readable ? 'read' : <span style={{ color: 'var(--muted)' }}>none</span>}</td>
                        </tr>
                      ))}
                      {!shown.length && <tr><td colSpan={6} className="empty">No tables match.</td></tr>}
                    </tbody>
                  </table>
                </div>
              )}
              {detail && <TableDetail d={detail} />}
            </>
          )}
        </div>

        {/* 6. writes stay off until an administrator says otherwise */}
        {saved && (
          <div className="section" style={{ maxWidth: 1080 }}>
            <h3 style={{ marginTop: 0 }}>Write access</h3>
            {saved.writes_approved ? (
              <>
                <div className="small" style={{ marginBottom: 8 }}>
                  Approved by <b>{saved.writes_approved.by}</b> on {when(saved.writes_approved.at)}. This screen still never writes;
                  the approval is what any future import or sync must find before it may.
                </div>
                <button className="btn danger" onClick={() => doApproval(false)} disabled={!!busy}>Revoke write approval</button>
              </>
            ) : (
              <>
                <div className="small" style={{ marginBottom: 8, color: 'var(--text-2)' }}>
                  Writes are <b>not approved</b> for this database. Everything is read-only. To approve, type
                  {' '}<code>ALLOW WRITES TO {saved.database}</code>.
                </div>
                <div className="toolbar" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <input value={phrase} onChange={(e) => setPhrase(e.target.value)} placeholder={`ALLOW WRITES TO ${saved.database}`}
                    style={{ minWidth: 280 }} aria-label="Approval phrase" />
                  <button className="btn" onClick={() => doApproval(true)} disabled={!!busy || phrase !== `ALLOW WRITES TO ${saved.database}`}>
                    Approve writes</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Issue({ issue, danger }) {
  return (
    <div className="warnbox" style={{ marginTop: 12, ...(danger ? { background: 'var(--danger-bg)', borderColor: 'var(--danger-line)' } : {}) }}>
      <h4 style={danger ? { color: 'var(--danger)' } : undefined}>{issue.title}</h4>
      <div className="small" style={{ marginBottom: 6 }}>{issue.detail}</div>
      <ul>{issue.steps.map((s) => <li key={s} className="small">{s}</li>)}</ul>
      <div className="small" style={{ color: 'var(--muted)', marginTop: 6 }}>Code: {issue.code}</div>
    </div>
  )
}

function TableDetail({ d }) {
  return (
    <div style={{ marginTop: 16 }}>
      <h4 style={{ margin: '0 0 8px' }}>{d.schema}.{d.table}</h4>
      <div className="small" style={{ color: 'var(--muted)', marginBottom: 8 }}>
        Primary key: {d.primary_key ? d.primary_key.columns.join(', ') : 'none'}
        {d.unique.length ? ` · unique: ${d.unique.map((u) => u.columns.join('+')).join(', ')}` : ''}
      </div>
      <div className="tablewrap">
        <table className="items">
          <thead><tr><th>#</th><th>Column</th><th>Type</th><th>Null</th><th>Default</th><th>Key</th></tr></thead>
          <tbody>
            {d.columns.map((c) => (
              <tr key={c.name}>
                <td className="small">{c.position}</td><td><b>{c.name}</b></td><td className="small">{c.type}</td>
                <td className="small">{c.nullable ? 'yes' : 'no'}</td>
                <td className="small" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.default || ''}</td>
                <td className="small">{c.primary_key ? 'PK' : ''}
                  {d.foreign_keys.some((f) => f.columns.includes(c.name)) ? (c.primary_key ? ' · FK' : 'FK') : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(d.foreign_keys.length > 0 || d.referenced_by.length > 0) && (
        <div className="kv" style={{ marginTop: 12 }}>
          {d.foreign_keys.map((f) => (<Fragment key={'o' + f.name}><span className="k">References</span>
            <span>{f.columns.join(', ')} → {f.references} ({f.referenced_columns.join(', ')})</span></Fragment>))}
          {d.referenced_by.map((f) => (<Fragment key={'i' + f.from + f.name}><span className="k">Referenced by</span>
            <span>{f.from} ({f.columns.join(', ')}) → {f.referenced_columns.join(', ')}</span></Fragment>))}
        </div>
      )}
    </div>
  )
}
