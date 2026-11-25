// The design system on one page — for development only.
// Open the dev server at  http://localhost:5173/#ui-gallery  (never in a build).
// Every value here is sample text for showing the components; no screen uses it.
import React, { useState } from 'react'
import { ICON_SVG } from './icons.js'
import {
  Icon, Button, IconButton, Field, Input, Select, DateInput, Textarea, SearchInput, Switch, FormSection,
  PageHeader, FilterBar, Card, KpiGrid, KpiCard, StatusBadge, DataTable, Pagination, Tabs, Segmented,
  DropdownMenu, Modal, Drawer, ConfirmProvider, useConfirm, Alert, EmptyState, LoadingState, Skeleton, Tooltip, ToastHost,
} from './ui.jsx'

const SWATCHES = [
  ['Primary', '--ui-primary'], ['Secondary', '--ui-secondary'], ['Light green', '--ui-green-100'], ['Accent', '--ui-accent'],
  ['Background', '--ui-bg'], ['Card', '--ui-surface'], ['Text', '--ui-text'], ['Text 2', '--ui-text-2'], ['Border', '--ui-border'],
  ['Success', '--ui-success'], ['Warning', '--ui-warning'], ['Error', '--ui-error'], ['Info', '--ui-info'],
]

function Section({ title, children }) {
  return <Card title={title} className="gal-sec">{children}</Card>
}

function Demo() {
  const confirm = useConfirm()
  const [tab, setTab] = useState('all')
  const [seg, setSeg] = useState('list')
  const [q, setQ] = useState('')
  const [sw, setSw] = useState(true)
  const [modal, setModal] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const [page, setPage] = useState(2)
  const [sort, setSort] = useState({ key: 'name', dir: 'asc' })
  const [toasts, setToasts] = useState([])
  const [answer, setAnswer] = useState('')
  const toast = (text, tone) => { const id = Date.now(); setToasts((t) => [...t, { id, text, tone }]); setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 2500) }

  const rows = [
    { id: 1, name: 'Sample row A', code: 'A-001', qty: 120, value: '₹ 12,400.00', st: ['Active', 'success'] },
    { id: 2, name: 'Sample row B', code: 'B-002', qty: 8, value: '₹ 960.00', st: ['Pending', 'warning'] },
    { id: 3, name: 'Sample row C', code: 'C-003', qty: 0, value: '₹ 0.00', st: ['Closed', 'neutral'] },
  ]

  return (
    <div className="ui" style={{ background: 'var(--ui-bg)', minHeight: '100vh' }}>
      <PageHeader title="Design system" sub="Every component, as ESSA and the shop draw it."
        breadcrumb={[{ label: 'Development' }, { label: 'UI gallery' }]}
        actions={<><Button icon="refresh" variant="secondary">Refresh</Button><Button icon="plus" variant="primary">Primary action</Button></>} />
      <div style={{ padding: '0 24px 40px', display: 'grid', gap: 16 }}>

        <Section title="Colours">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(130px,1fr))', gap: 10 }}>
            {SWATCHES.map(([n, v]) => (
              <div key={v} style={{ border: '1px solid var(--ui-border)', borderRadius: 10, overflow: 'hidden' }}>
                <div style={{ height: 44, background: `var(${v})` }} />
                <div style={{ padding: '6px 10px', fontSize: 12 }}><b>{n}</b><br /><span className="ui-hint">{v}</span></div>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Typography">
          <div style={{ fontSize: 'var(--ui-fs-page)', fontWeight: 700 }}>Page title 26</div>
          <div style={{ fontSize: 'var(--ui-fs-section)', fontWeight: 600 }}>Section title 16</div>
          <div style={{ fontSize: 'var(--ui-fs-card)', fontWeight: 500, color: 'var(--ui-text-2)' }}>Card title 13</div>
          <div>Body text 14 — the quick brown fox.</div>
          <div className="ui-hint">Supporting text 12</div>
        </Section>

        <Section title="Buttons">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <Button variant="primary" icon="plus">Primary</Button>
            <Button variant="secondary" icon="refresh">Secondary</Button>
            <Button icon="download">Default</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="danger" icon="trash">Danger</Button>
            <Button variant="primary" kbd="F10" icon="printer">With shortcut</Button>
            <Button variant="primary" busy>Busy</Button>
            <Button disabled>Disabled</Button>
            <Button size="sm">Small</Button>
            <Button size="lg" variant="primary">Large</Button>
            <IconButton icon="more" label="More actions" />
            <IconButton icon="pencil" label="Edit" variant="default" />
          </div>
        </Section>

        <Section title="Form fields">
          <FormSection title="Supplier details" sub="Grouped fields, at most three across.">
            <Field label="Name" required htmlFor="g1"><Input id="g1" placeholder="Supplier name" /></Field>
            <Field label="GSTIN" hint="15 characters"><Input placeholder="33AAAAA0000A1Z5" /></Field>
            <Field label="City"><Select defaultValue=""><option value="" disabled>Choose…</option><option>Chennai</option></Select></Field>
            <Field label="Invoice date"><DateInput /></Field>
            <Field label="Amount" error="Enter an amount above zero"><Input defaultValue="0" /></Field>
            <Field label="Active"><Switch checked={sw} onChange={setSw} label={sw ? 'Yes' : 'No'} /></Field>
            <Field label="Notes" className="ui-span-all"><Textarea placeholder="Anything else" /></Field>
          </FormSection>
          <div className="ui-actionbar"><Button variant="ghost">Cancel</Button><Button>Save draft</Button><Button variant="primary">Submit</Button></div>
        </Section>

        <Section title="KPI cards">
          <KpiGrid>
            <KpiCard icon="building" label="Warehouses" value="1" meta="Active" live onClick={() => {}} />
            <KpiCard icon="rupee" tone="money" label="Stock value" value="₹ 1,34,71,181.83" meta="at weighted-average cost" />
            <KpiCard icon="boxes" label="Inventory (qty)" value="63,999.25" meta="9,863 items" />
            <KpiCard icon="alerttri" tone="warning" label="Low stock" value="4,25,051" meta="at or below reorder level" />
            <KpiCard icon="undo" tone="error" label="Returns" value="₹ 0" meta="0 customer · 0 supplier" />
            <KpiCard icon="info" tone="info" label="Pending transfers" value="46" meta="in transit" />
          </KpiGrid>
        </Section>

        <Card title="Listing" icon="spreadsheet" sub="Search, filters, sortable table, badges, pagination"
          actions={<><DropdownMenu label="Report" items={[{ label: 'Open in Reports', icon: 'trending', run: () => {} }, { label: 'Download CSV', icon: 'download', run: () => {} }]} />
            <Segmented label="View" value={seg} onChange={setSeg} options={[{ key: 'list', label: 'List', icon: 'list' }, { key: 'grid', label: 'Grid', icon: 'grid' }]} /></>}
          flush footer={<><span>3 rows</span><span>Σ qty <b>128</b></span><span>Σ value <b>₹ 13,360.00</b></span></>}>
          <div style={{ padding: '12px 20px 0' }}>
            <Tabs value={tab} onChange={setTab} tabs={[{ key: 'all', label: 'All', count: 3 }, { key: 'active', label: 'Active', count: 1 }, { key: 'pending', label: 'Pending', count: 1 }]} />
            <div style={{ height: 12 }} />
            <FilterBar>
              <SearchInput value={q} onChange={setQ} placeholder="Search name or code…" />
              <Button icon="filter">Filters</Button>
              <span className="ui-spacer" />
              <Button icon="download">Export</Button>
            </FilterBar>
          </div>
          <DataTable rows={rows} sort={sort} onSort={(k) => setSort((s) => ({ key: k, dir: s.key === k && s.dir === 'asc' ? 'desc' : 'asc' }))}
            onRowClick={() => {}} selectedKey={1}
            columns={[
              { key: 'name', label: 'Name', sortable: true, className: 'strong' },
              { key: 'code', label: 'Code', className: 'mono muted' },
              { key: 'qty', label: 'Qty', num: true, sortable: true },
              { key: 'value', label: 'Value', num: true },
              { key: 'st', label: 'Status', render: (r) => <StatusBadge tone={r.st[1]}>{r.st[0]}</StatusBadge> },
              { key: 'a', label: '', className: 'actions', render: () => <IconButton size="sm" icon="more" label="Row actions" /> },
            ]} />
          <div style={{ padding: '12px 20px' }}><Pagination page={page} pages={9} total={412} from={51} to={100} onPage={setPage} /></div>
        </Card>

        <Section title="Badges">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <StatusBadge tone="success">Active</StatusBadge><StatusBadge tone="warning">Pending</StatusBadge>
            <StatusBadge tone="error">Cancelled</StatusBadge><StatusBadge tone="info">Posted</StatusBadge>
            <StatusBadge tone="primary">In stock</StatusBadge><StatusBadge>Draft</StatusBadge><StatusBadge plain tone="info">Plain</StatusBadge>
          </div>
        </Section>

        <Section title="Feedback and overlays">
          <div style={{ display: 'grid', gap: 10 }}>
            <Alert tone="info" title="Information">The till database is read once a minute.</Alert>
            <Alert tone="success">Saved.</Alert>
            <Alert tone="warning" title="Needs attention">3 consignments not received.</Alert>
            <Alert tone="error" title="Could not save">The server did not answer.</Alert>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
            <Button onClick={() => setModal(true)}>Open modal</Button>
            <Button onClick={() => setDrawer(true)}>Open drawer</Button>
            <Button variant="danger" onClick={async () => setAnswer((await confirm({ title: 'Delete this coupon?', message: 'It stops working at every till straight away.', confirmLabel: 'Delete', danger: true })) ? 'Yes' : 'No')}>Confirm dialog</Button>
            <Button onClick={() => toast('Saved', 'success')}>Toast</Button>
            <Button onClick={() => toast('Could not reach the server', 'error')}>Error toast</Button>
            <Tooltip label="Tooltip text"><Button>Hover for tooltip</Button></Tooltip>
            {answer && <span className="ui-hint" style={{ alignSelf: 'center' }}>Confirm answered: {answer}</span>}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginTop: 14 }}>
            <Card><EmptyState title="No invoices yet">Upload a supplier invoice to start.</EmptyState></Card>
            <Card><LoadingState /></Card>
            <Card><Skeleton width="60%" /><Skeleton style={{ marginTop: 8 }} /><Skeleton width="80%" style={{ marginTop: 8 }} /></Card>
          </div>
        </Section>

        <Section title={`Icons (${Object.keys(ICON_SVG).length})`}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(110px,1fr))', gap: 8 }}>
            {Object.keys(ICON_SVG).map((n) => (
              <div key={n} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--ui-text-2)' }}>
                <Icon name={n} size={18} stroke={1.75} /> {n}</div>
            ))}
          </div>
        </Section>
      </div>

      {modal && <Modal title="Edit supplier" onClose={() => setModal(false)}
        footer={<><Button onClick={() => setModal(false)}>Cancel</Button><Button variant="primary" onClick={() => setModal(false)}>Save</Button></>}>
        <div className="ui-form-grid" style={{ '--cols': 2 }}>
          <Field label="Name" required><Input /></Field><Field label="Phone"><Input /></Field>
        </div>
      </Modal>}
      {drawer && <Drawer title="Notifications" onClose={() => setDrawer(false)}><EmptyState title="Nothing unread" /></Drawer>}
      <ToastHost toasts={toasts} />
    </div>
  )
}

export default function Gallery() {
  return <ConfirmProvider><Demo /></ConfirmProvider>
}
