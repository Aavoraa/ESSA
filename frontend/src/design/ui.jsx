// ==========================================================================
//  ESSA AI — UI components
//  ------------------------------------------------------------------------
//  The design system as React components. Styles live in components.css and
//  tokens.css (shared with the shop); this file only decides the markup.
//
//  These components carry no business logic. A screen moving onto them keeps
//  its own state, API calls and permission checks; only the markup changes.
// ==========================================================================
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { ICON_SVG } from './icons.js'

const cx = (...a) => a.filter(Boolean).join(' ')

// ---------------------------------------------------------------- icons
export function Icon({ name, size = 16, className, stroke = 2, title }) {
  const svg = ICON_SVG[name] || ICON_SVG.dashboard
  return (
    <svg className={cx('ico-svg ui-icon', className)} width={size} height={size} viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round"
      aria-hidden={title ? undefined : 'true'} role={title ? 'img' : undefined} aria-label={title}
      focusable="false" dangerouslySetInnerHTML={{ __html: svg }} />
  )
}

// ---------------------------------------------------------------- buttons
export function Button({ variant = 'default', size, icon, iconRight, busy, block, kbd, className, children, ...rest }) {
  return (
    <button type="button" {...rest}
      className={cx('ui-btn', variant !== 'default' && `ui-btn--${variant}`, size && `ui-btn--${size}`,
        block && 'ui-btn--block', busy && 'ui-btn--busy', className)}
      aria-busy={busy || undefined}>
      {icon && <Icon name={icon} size={size === 'sm' ? 14 : 16} stroke={1.75} />}
      {children}
      {iconRight && <Icon name={iconRight} size={size === 'sm' ? 14 : 16} stroke={1.75} />}
      {kbd && <kbd className="ui-kbd">{kbd}</kbd>}
    </button>
  )
}

export function IconButton({ icon, label, size, variant = 'ghost', className, tipSide, ...rest }) {
  return (
    <button type="button" aria-label={label} title={tipSide ? undefined : label} {...rest}
      data-ui-tip={tipSide ? label : undefined} data-ui-tip-pos={tipSide}
      className={cx('ui-btn ui-btn--icon', `ui-btn--${variant}`, size && `ui-btn--${size}`, className)}>
      <Icon name={icon} size={size === 'sm' ? 15 : 17} stroke={1.75} />
    </button>
  )
}

// ---------------------------------------------------------------- form fields
export function Field({ label, required, hint, error, htmlFor, className, children }) {
  return (
    <div className={cx('ui-field', error && 'ui-field--invalid', className)}>
      {label && <label className="ui-label" htmlFor={htmlFor}>{label}{required && <span className="ui-req" aria-hidden="true">*</span>}</label>}
      {children}
      {error ? <span className="ui-error-text" role="alert">{error}</span> : hint ? <span className="ui-hint">{hint}</span> : null}
    </div>
  )
}

export const Input = React.forwardRef(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={cx('ui-input', className)} {...rest} />
})

export function Select({ className, children, ...rest }) {
  return <select className={cx('ui-select', className)} {...rest}>{children}</select>
}

export function DateInput({ className, ...rest }) {
  return <input type="date" className={cx('ui-input', className)} {...rest} />
}

export function Textarea({ className, ...rest }) {
  return <textarea className={cx('ui-textarea', className)} {...rest} />
}

export function SearchInput({ value, onChange, placeholder = 'Search…', kbd, className, inputRef, ...rest }) {
  return (
    <span className={cx('ui-input-icon', className)}>
      <Icon name="search" size={16} stroke={1.75} />
      <input ref={inputRef} type="search" className="ui-input" value={value} placeholder={placeholder}
        onChange={(e) => onChange && onChange(e.target.value)} {...rest} />
      {kbd && <kbd className="ui-kbd">{kbd}</kbd>}
    </span>
  )
}

export function Switch({ checked, onChange, label, disabled }) {
  return (
    <span className="ui-check">
      <button type="button" role="switch" aria-checked={!!checked} className="ui-switch" disabled={disabled}
        onClick={() => onChange && onChange(!checked)} aria-label={label} />
      {label && <span>{label}</span>}
    </span>
  )
}

export function FormSection({ title, sub, cols = 3, children, className }) {
  return (
    <section className={cx('ui-form-section', className)}>
      {title && <h3 className="ui-form-section__title">{title}</h3>}
      {sub && <p className="ui-form-section__sub">{sub}</p>}
      <div className="ui-form-grid" style={{ '--cols': cols }}>{children}</div>
    </section>
  )
}

// ---------------------------------------------------------------- page structure
export function Breadcrumb({ items }) {
  if (!items?.length) return null
  return (
    <nav className="ui-breadcrumb" aria-label="Breadcrumb">
      {items.map((it, i) => {
        const last = i === items.length - 1
        return (
          <React.Fragment key={i}>
            {i > 0 && <Icon name="chevron" size={12} />}
            {last ? <span aria-current="page">{it.label}</span>
              : it.onClick ? <button type="button" onClick={it.onClick}>{it.label}</button>
              : <span>{it.label}</span>}
          </React.Fragment>
        )
      })}
    </nav>
  )
}

export function PageHeader({ title, sub, breadcrumb, actions, badge, className }) {
  return (
    <header className={cx('ui-pagehead', className)}>
      <div className="ui-pagehead__text">
        <Breadcrumb items={breadcrumb} />
        <h1 className="ui-pagehead__title">{title}{badge}</h1>
        {sub && <p className="ui-pagehead__sub">{sub}</p>}
      </div>
      {actions && <div className="ui-pagehead__actions">{actions}</div>}
    </header>
  )
}

export function FilterBar({ children, className }) {
  return <div className={cx('ui-filterbar', className)}>{children}</div>
}

export function Card({ title, sub, icon, actions, footer, flush, collapsible, defaultOpen = true, className, bodyClassName, children }) {
  const [open, setOpen] = useState(defaultOpen)
  const head = title || actions
  return (
    <section className={cx('ui-card', !open && 'ui-card--collapsed', className)}>
      {head && (
        <div className="ui-card__head">
          {icon && <Icon name={icon} size={18} stroke={1.75} />}
          <div style={{ minWidth: 0 }}>
            {title && <h2 className="ui-card__title">{title}</h2>}
            {sub && <div className="ui-card__sub">{sub}</div>}
          </div>
          {(actions || collapsible) && (
            <div className="ui-card__actions">
              {open && actions}
              {collapsible && <IconButton size="sm" icon={open ? 'chevup' : 'chevdown'}
                label={open ? 'Collapse' : 'Expand'} onClick={() => setOpen((o) => !o)} />}
            </div>
          )}
        </div>
      )}
      {open && <div className={cx('ui-card__body', flush && 'ui-card__body--flush', bodyClassName)}>{children}</div>}
      {open && footer && <div className="ui-card__foot">{footer}</div>}
    </section>
  )
}

// ---------------------------------------------------------------- KPI
const longKpi = (v) => String(v ?? '').length >= 12

export function KpiGrid({ children, className, style }) {
  return <div className={cx('ui-kpis', className)} style={style}>{children}</div>
}

export function KpiCard({ icon, label, value, meta, live, tone, hint, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag type={onClick ? 'button' : undefined} className={cx('ui-kpi', tone && `ui-kpi--${tone}`)}
      onClick={onClick} title={hint}>
      {icon && <span className="ui-kpi__icon"><Icon name={icon} size={18} stroke={1.75} /></span>}
      <span className="ui-kpi__body">
        <span className="ui-kpi__label">{label}</span>
        <span className={cx('ui-kpi__value', longKpi(value) && 'ui-kpi__value--long')}>{value}</span>
        {meta != null && meta !== '' && <span className="ui-kpi__meta">{live && <i className="ui-dot" />}{meta}</span>}
      </span>
    </Tag>
  )
}

// ---------------------------------------------------------------- status
// tone: success | warning | error | info | primary | neutral
export function StatusBadge({ tone = 'neutral', plain, children, title }) {
  return <span className={cx('ui-badge', tone !== 'neutral' && `ui-badge--${tone}`, plain && 'ui-badge--plain')} title={title}>{children}</span>
}

// ---------------------------------------------------------------- tables
// Columns: [{ key, label, num, sortable, render(row), className, width }]
export function DataTable({ columns, rows, rowKey = 'id', onRowClick, selectedKey, sort, onSort, empty, loading, compact, className }) {
  if (loading) return <LoadingState />
  if (!rows?.length) return empty || <EmptyState title="Nothing to show" />
  return (
    <div className={cx('ui-table-wrap', className)}>
      <table className={cx('ui-table', compact && 'ui-table--compact')}>
        <thead><tr>
          {columns.map((c) => (
            <th key={c.key} className={cx(c.num && 'num', c.className)} style={c.width ? { width: c.width } : undefined}
              aria-sort={sort?.key === c.key ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}>
              {c.sortable && onSort
                ? <button type="button" className="ui-th-sort" onClick={() => onSort(c.key)}>
                    {c.label}<Icon name={sort?.key === c.key ? (sort.dir === 'asc' ? 'chevup' : 'chevdown') : 'sortboth'} size={12} />
                  </button>
                : c.label}
            </th>
          ))}
        </tr></thead>
        <tbody>
          {rows.map((r, i) => {
            const k = typeof rowKey === 'function' ? rowKey(r, i) : r[rowKey] ?? i
            return (
              <tr key={k} className={cx(onRowClick && 'is-clickable', selectedKey != null && k === selectedKey && 'is-selected')}
                onClick={onRowClick ? () => onRowClick(r) : undefined}>
                {columns.map((c) => (
                  <td key={c.key} className={cx(c.num && 'num', c.className)}>{c.render ? c.render(r) : r[c.key]}</td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function Pagination({ page, pages, total, from, to, onPage, unit = 'rows' }) {
  if (!pages || pages < 1) return null
  const nums = []
  for (let p = Math.max(1, page - 2); p <= Math.min(pages, page + 2); p++) nums.push(p)
  return (
    <div className="ui-pagination">
      {total != null && <span className="ui-pagination__info"><b>{from}–{to}</b> of <b>{Number(total).toLocaleString('en-IN')}</b> {unit}</span>}
      <span className="ui-pagination__nav">
        <button type="button" className="ui-pagination__page" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page">
          <Icon name="chevleft" size={14} /></button>
        {nums.map((p) => (
          <button key={p} type="button" className="ui-pagination__page" aria-current={p === page ? 'page' : undefined}
            onClick={() => onPage(p)}>{p}</button>
        ))}
        <button type="button" className="ui-pagination__page" disabled={page >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">
          <Icon name="chevron" size={14} /></button>
      </span>
    </div>
  )
}

// ---------------------------------------------------------------- tabs
export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="ui-tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.key} type="button" role="tab" className="ui-tab" aria-selected={value === t.key}
          onClick={() => onChange(t.key)}>
          {t.icon && <Icon name={t.icon} size={15} stroke={1.75} />}{t.label}
          {t.count != null && <span className="ui-tab__count">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

export function Segmented({ options, value, onChange, label }) {
  return (
    <div className="ui-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" className="ui-seg__btn" aria-pressed={value === o.key} onClick={() => onChange(o.key)}>
          {o.icon && <Icon name={o.icon} size={14} stroke={1.75} />}{o.label}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- dropdown menu
function useDismiss(open, setOpen, ref) {
  useEffect(() => {
    if (!open) return undefined
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const esc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', esc) }
  }, [open, setOpen, ref])
}

// items: [{ label, hint, icon, danger, run } | 'sep']
export function DropdownMenu({ label, icon, variant, size, align = 'start', items, title }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useDismiss(open, setOpen, ref)
  return (
    <span className="ui-menu-anchor" ref={ref}>
      <Button variant={variant} size={size} icon={icon} iconRight="chevdown" title={title}
        aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{label}</Button>
      {open && (
        <div className={cx('ui-menu', align === 'end' && 'ui-menu--end')} role="menu">
          {items.map((it, i) => it === 'sep'
            ? <div key={i} className="ui-menu__sep" role="separator" />
            : (
              <button key={i} type="button" role="menuitem" className={cx('ui-menu__item', it.danger && 'ui-menu__item--danger')}
                onClick={() => { setOpen(false); it.run && it.run() }}>
                {it.icon && <Icon name={it.icon} size={16} stroke={1.75} />}
                <span>{it.label}{it.hint && <small>{it.hint}</small>}</span>
              </button>
            ))}
        </div>
      )}
    </span>
  )
}

// ---------------------------------------------------------------- modal / drawer
export function Modal({ title, size = 'md', onClose, footer, children, closeOnBackdrop = true }) {
  const box = useRef(null)
  useEffect(() => {
    const esc = (e) => { if (e.key === 'Escape') onClose && onClose() }
    document.addEventListener('keydown', esc)
    const first = box.current?.querySelector('input, select, textarea, button:not(.ui-modal__close)')
    first?.focus()
    return () => document.removeEventListener('keydown', esc)
  }, [onClose])
  return (
    <div className="ui-backdrop" onMouseDown={(e) => { if (closeOnBackdrop && e.target === e.currentTarget) onClose && onClose() }}>
      <div ref={box} className={cx('ui-modal', `ui-modal--${size}`)} role="dialog" aria-modal="true" aria-label={title}>
        <div className="ui-modal__head">
          <h2 className="ui-modal__title">{title}</h2>
          {onClose && <IconButton className="ui-modal__close" size="sm" icon="x" label="Close" onClick={onClose} />}
        </div>
        <div className="ui-modal__body">{children}</div>
        {footer && <div className="ui-modal__foot">{footer}</div>}
      </div>
    </div>
  )
}

export function Drawer({ title, onClose, footer, children }) {
  useEffect(() => {
    const esc = (e) => { if (e.key === 'Escape') onClose && onClose() }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [onClose])
  return (
    <div className="ui-backdrop ui-drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose && onClose() }}>
      <aside className="ui-drawer" role="dialog" aria-modal="true" aria-label={title}>
        <div className="ui-modal__head">
          <h2 className="ui-modal__title">{title}</h2>
          <IconButton className="ui-modal__close" size="sm" icon="x" label="Close" onClick={onClose} />
        </div>
        <div className="ui-modal__body" style={{ flex: 1 }}>{children}</div>
        {footer && <div className="ui-modal__foot">{footer}</div>}
      </aside>
    </div>
  )
}

// ---------------------------------------------------------------- confirm
// Replaces window.confirm with the same yes / no outcome:
//   const confirm = useConfirm()
//   if (!(await confirm({ title, message, confirmLabel, danger }))) return
const ConfirmCtx = createContext(null)

export function ConfirmProvider({ children }) {
  const [req, setReq] = useState(null)
  const confirm = useCallback((opts) => new Promise((resolve) => {
    setReq({ ...(typeof opts === 'string' ? { message: opts } : opts), resolve })
  }), [])
  const close = (answer) => { req?.resolve(answer); setReq(null) }
  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {req && (
        <Modal size="sm" title={req.title || 'Please confirm'} onClose={() => close(false)}
          footer={<>
            <Button onClick={() => close(false)}>{req.cancelLabel || 'Cancel'}</Button>
            <Button variant={req.danger ? 'danger' : 'primary'} onClick={() => close(true)} autoFocus>
              {req.confirmLabel || 'Confirm'}</Button>
          </>}>
          <div className={cx('ui-confirm', req.danger && 'ui-confirm--danger')}>
            <span className="ui-confirm__icon"><Icon name={req.danger ? 'alerttri' : 'info'} size={20} stroke={1.75} /></span>
            <p className="ui-confirm__text">{req.message}</p>
          </div>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  )
}

// Falls back to the browser's own dialog where no provider is mounted, so a
// screen moved onto useConfirm never loses its question.
export function useConfirm() {
  const ctx = useContext(ConfirmCtx)
  return ctx || ((opts) => Promise.resolve(window.confirm(typeof opts === 'string' ? opts : opts.message)))
}

// ---------------------------------------------------------------- feedback
export function Alert({ tone = 'info', title, children, icon }) {
  const ic = icon || { info: 'info', success: 'checkcircle', warning: 'alerttri', error: 'alertcircle' }[tone]
  return (
    <div className={cx('ui-alert', `ui-alert--${tone}`)} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon name={ic} size={18} stroke={1.75} />
      <div>{title && <p className="ui-alert__title">{title}</p>}<div>{children}</div></div>
    </div>
  )
}

export function EmptyState({ icon = 'inbox', title, children, action }) {
  return (
    <div className="ui-empty">
      <span className="ui-empty__icon"><Icon name={icon} size={20} stroke={1.75} /></span>
      {title && <p className="ui-empty__title">{title}</p>}
      {children && <p className="ui-empty__text">{children}</p>}
      {action}
    </div>
  )
}

export function LoadingState({ label = 'Loading…' }) {
  return <div className="ui-loading" role="status"><span className="ui-spinner" />{label}</div>
}

export function Skeleton({ width = '100%', height = 14, style }) {
  return <span className="ui-skeleton" style={{ display: 'block', width, height, ...style }} />
}

export function Tooltip({ label, side = 'right', children }) {
  return <span data-ui-tip={label} data-ui-tip-pos={side === 'top' ? 'top' : undefined} style={{ display: 'inline-flex' }}>{children}</span>
}

// toasts: <ToastHost toasts={[{id, text, tone}]} />
export function ToastHost({ toasts }) {
  return (
    <div className="ui-toasts" aria-live="polite">
      {(toasts || []).map((t) => (
        <div key={t.id} className={cx('ui-toast', t.tone && `ui-toast--${t.tone}`)}>{t.text}</div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- filter dropdown
// A styled replacement for a native <select> in filter bars: an icon, the chosen
// value and a chevron; the list opens as a menu with the selection ticked.
// options: [{ value, label }]. Keyboard: ↑ ↓ Home End, Enter / Space, Escape.
export function FilterSelect({ icon, value, options, onChange, label, title, align = 'start', minWidth, block }) {
  const [open, setOpen] = useState(false)
  const [hi, setHi] = useState(0)
  const [q, setQ] = useState('')
  const [pos, setPos] = useState(null)
  const ref = useRef(null)
  const btn = useRef(null)
  const list = useRef(null)
  const search = useRef(null)
  useDismiss(open, setOpen, ref)
  const searchable = options.length > 8
  const s = q.trim().toLowerCase()
  const shown = s ? options.filter((o) => String(o.label).toLowerCase().includes(s)) : options
  const idx = Math.max(0, options.findIndex((o) => String(o.value) === String(value)))
  const cur = options[idx]

  // The list is placed against the window, not its parent, so a narrow or
  // scrolling container (a Filters panel, a sidebar) can never clip it. It
  // opens upward when there is more room above.
  const place = () => {
    const r = btn.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom - 12
    const above = r.top - 12
    const up = below < 220 && above > below
    const maxH = Math.min(340, up ? above : below)
    setPos({
      left: align === 'end' ? undefined : Math.max(8, r.left),
      right: align === 'end' ? Math.max(8, window.innerWidth - r.right) : undefined,
      top: up ? undefined : r.bottom + 6,
      bottom: up ? window.innerHeight - r.top + 6 : undefined,
      minWidth: Math.max(r.width, 200), maxWidth: Math.max(r.width, 360), maxHeight: maxH,
    })
  }
  useEffect(() => {
    if (!open) { setQ(''); return undefined }
    place()
    setHi(searchable ? 0 : idx)
    setTimeout(() => (searchable ? search.current : list.current)?.focus(), 0)
    const re = () => place()
    window.addEventListener('resize', re)
    window.addEventListener('scroll', re, true)
    return () => { window.removeEventListener('resize', re); window.removeEventListener('scroll', re, true) }
  }, [open]) // eslint-disable-line
  useEffect(() => { if (open && searchable) setHi(0) }, [q]) // eslint-disable-line
  useEffect(() => {
    if (!open) return
    list.current?.querySelector(`[data-i="${hi}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [hi, open])
  const pick = (o) => { setOpen(false); if (String(o.value) !== String(value)) onChange(o.value); btn.current?.focus() }
  const key = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => Math.min(shown.length - 1, h + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => Math.max(0, h - 1)) }
    else if (e.key === 'Home' && !searchable) { e.preventDefault(); setHi(0) }
    else if (e.key === 'End' && !searchable) { e.preventDefault(); setHi(shown.length - 1) }
    else if (e.key === 'Enter' || (e.key === ' ' && !searchable)) { e.preventDefault(); shown[hi] && pick(shown[hi]) }
    else if (e.key === 'Tab') setOpen(false)
  }
  return (
    <span className={cx('ui-menu-anchor ui-fsel-wrap', block && 'is-block')} ref={ref}>
      <button ref={btn} type="button" className={cx('ui-fsel', open && 'is-open')} title={title} style={minWidth ? { minWidth } : undefined}
        aria-haspopup="listbox" aria-expanded={open} aria-label={label ? `${label}: ${cur?.label}` : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); setOpen(true) } }}>
        {icon && <Icon name={icon} size={16} stroke={1.75} className="ui-fsel__icon" />}
        <span className="ui-fsel__text">
          {label && <span className="ui-fsel__label">{label}</span>}
          <span className="ui-fsel__value">{cur?.label}</span>
        </span>
        <Icon name="chevdown" size={15} stroke={1.75} className="ui-fsel__chev" />
      </button>
      {open && pos && (
        <div className="ui-menu ui-fsel__menu" style={{ position: 'fixed', ...pos }} onKeyDown={key}>
          {searchable && (
            <div className="ui-fsel__search">
              <Icon name="search" size={14} stroke={1.75} />
              <input ref={search} value={q} onChange={(e) => setQ(e.target.value)}
                placeholder={`Search ${options.length - 1} options…`} aria-label="Search the list" />
            </div>
          )}
          <div ref={list} className="ui-fsel__list" role="listbox" tabIndex={-1} aria-activedescendant={`fsel-${hi}`}>
            {!shown.length && <div className="ui-fsel__none">No match for “{q}”</div>}
            {shown.map((o, i) => {
              const sel = String(o.value) === String(value)
              return (
                <div key={String(o.value)} id={`fsel-${i}`} data-i={i} role="option" aria-selected={sel}
                  className={cx('ui-fsel__opt', sel && 'is-selected', i === hi && 'is-hi')}
                  onMouseEnter={() => setHi(i)} onMouseDown={(e) => e.preventDefault()} onClick={() => pick(o)}>
                  <span>{o.label}</span>
                  {sel && <Icon name="check" size={15} stroke={2} />}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </span>
  )
}
