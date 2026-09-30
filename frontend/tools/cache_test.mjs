// The read cache in src/api.js, against a fake server: `node tools/cache_test.mjs`.
// Run by `npm run check`. Every rule the cache promises is asserted here.
const calls = []
let serverValue = 1
globalThis.localStorage = (() => {
  const m = new Map()
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }
})()
globalThis.window = {
  fetch: async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase()
    const wh = opts.headers.get('X-Essa-Warehouse') || ''
    calls.push(`${method} ${url} wh=${wh}`)
    await new Promise((r) => setTimeout(r, 5))
    if (method !== 'GET') { serverValue += 1; return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }) }
    if (url.includes('/fail')) return new Response('{"detail":"no"}', { status: 403, headers: { 'Content-Type': 'application/json' } })
    return new Response(JSON.stringify({ url, wh, v: serverValue }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  },
}
const { api, session, warehouse, readCache, swr } = await import('../src/api.js')

let failures = 0
const check = (label, ok) => { console.log(`  ${ok ? 'ok  ' : 'FAIL'}  ${label}`); if (!ok) failures++ }
// GET requests to exactly this path (a write to /api/purchases/7/warehouse is not a read of /7)
const count = (path) => calls.filter((c) => c.startsWith(path.startsWith('GET ') ? path : `GET ${path} `)).length

session.set('tok-A')
warehouse.set({ id: 1, name: 'W1' })

console.log('read cache')
const a1 = await api.getPurchase(7); const a2 = await api.getPurchase(7)
check('a second read of the same screen is answered from memory', count('/api/purchases/7') === 1 && a1.v === a2.v)
check('the remembered answer is a fresh copy each time', a1 !== a2)

await api.setGrnWarehouse(7, 2)                       // a write (PATCH/POST)
const a3 = await api.getPurchase(7)
check('any write empties it: the next read goes to the server and sees the change',
  count('/api/purchases/7') === 2 && a3.v === a1.v + 1)

warehouse.set({ id: 2, name: 'W2' })
const b1 = await api.getPurchase(7)
check('another warehouse is never answered from the first one\'s memory', b1.wh === '2' && count('/api/purchases/7') === 3)

session.set('tok-B')
await api.getPurchase(7)
check('another account starts with nothing remembered', count('/api/purchases/7') === 4)

await api.notificationCount(); await api.notificationCount()
check('the bell\'s count is never cached', count('/api/notifications/count') === 2)
await api.lrList(); await api.lrList()
check('the LR register\'s poll is never cached', count('/api/lr') === 2)
await api.lrPage({ page: 2, size: 100 }); await api.lrPage({ page: 2, size: 100 })
check('…nor a page of it (the phone writes to it)', calls.filter((c) => c.startsWith('GET /api/lr/page?')).length === 2)

const [p1, p2] = await Promise.all([api.getPurchase(9), api.getPurchase(9)])
check('two screens asking at once share one request', count('/api/purchases/9') === 1 && p1.v === p2.v)

await readCache.refresh(() => api.getPurchase(9))
check('↻ Refresh goes to the server', count('/api/purchases/9') === 2)

try { await api.getPurchase('fail') } catch { /* refused */ }
try { await api.getPurchase('fail') } catch { /* refused */ }
check('a refused / failed answer is never remembered', count('/api/purchases/fail') === 2)

// a read that was in flight while a write landed must not be remembered
const slow = api.getPurchase(11)
await api.setGrnWarehouse(11, 3)
await slow
await api.getPurchase(11)
check('a read overlapping a write is not kept', count('/api/purchases/11') === 2)

// swr: remembered figures first, then the server's
await api.getPurchase(12)
serverValue += 10
const seen = []
await swr(() => api.getPurchase(12), (r) => seen.push(r.v))
await new Promise((r) => setTimeout(r, 30))
check('a dashboard shows the remembered figures, then the server\'s', seen.length === 2 && seen[1] === seen[0] + 10)
const seen2 = []
readCache.clear()
await swr(() => api.getPurchase(13), (r) => seen2.push(r.v))
await new Promise((r) => setTimeout(r, 30))
check('with nothing remembered it asks once, as before', seen2.length === 1 && count('/api/purchases/13') === 1)

console.log('dashboards (kept for the session)')
session.set('tok-C'); warehouse.set({ id: 1, name: 'W1' })
await api.dashboardSummary()
await api.setGrnWarehouse(20, 1)                         // a save
const shown = []
await swr(() => api.dashboardSummary(), (r) => shown.push(r.v))
await new Promise((r) => setTimeout(r, 30))
check('a save does not discard a dashboard: shown at once, then the server\'s',
  shown.length === 2 && count('/api/dashboard/summary') === 2)
await api.getPurchase(21)                                // an ordinary screen, read now
const realNow = Date.now
Date.now = () => realNow() + 10 * 60 * 1000              // ten minutes later
const later = []
await swr(() => api.dashboardSummary(), (r) => later.push(r.v))
await new Promise((r) => setTimeout(r, 30))
check('…and is still instant ten minutes later (then refreshed)', later.length === 2)
await api.getPurchase(21)
check('an ordinary screen is NOT kept past its minute', count('/api/purchases/21') === 2)
Date.now = realNow
warehouse.set({ id: 2, name: 'W2' })
const w2 = []
await swr(() => api.dashboardSummary(), (r) => w2.push(r.wh))
check('another warehouse gets its own dashboard, never the first one\'s', w2[0] === '2')
warehouse.set({ id: 1, name: 'W1' })
const back = []
await swr(() => api.dashboardSummary(), (r) => back.push(r.wh))
check('coming back to the first warehouse finds its dashboard at once', back[0] === '1')
await new Promise((r) => setTimeout(r, 30))
session.set('tok-D')
const n = count('/api/dashboard/summary')
await api.dashboardSummary()
check('another account starts with no dashboards remembered', count('/api/dashboard/summary') === n + 1)

console.log(failures ? `\n${failures} FAILED` : '\nall passing')
process.exit(failures ? 1 : 0)
