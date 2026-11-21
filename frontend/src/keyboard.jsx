// ==========================================================================
//  Keyboard first
//  ------------------------------------------------------------------------
//  One layer, mounted once by App, that makes every screen workable without a
//  mouse — Tally style, because that is what the people keying in bills here
//  already have in their fingers:
//
//    Enter          next field (Shift+Enter: previous). Off the last field it
//                   lands on the screen's main button; Enter again presses it.
//    Ctrl+S         the screen's Save button
//    Ctrl+Enter     the screen's main action (Post / Submit / Confirm / Save)
//    Alt+N          the screen's New / Add button
//    Esc            close the open dialog; otherwise leave the field you are in
//    /              jump to the screen's search box
//    ↑ ↓ (or J K)   move through the rows of a list; Enter opens the row
//    ↑ ↓ in a grid  same column, row above / below (entry tables)
//    Alt+1 … Alt+9  the first nine screens in the side menu
//    Ctrl+K         jump to any screen (App's palette)
//    F1 or ?        this list
//
//  WHY ONE LAYER RATHER THAN 600 EDITS. The screens hold some six hundred
//  inputs and buttons in one file. Wiring each by hand would miss some, and
//  every screen added later would have to remember to join in. A layer that
//  reads the DOM covers them all, today's and tomorrow's.
//
//  IT NEVER OVERRIDES A FIELD THAT KNOWS BETTER. React attaches its listeners
//  at the root, so they have all run by the time this document-level listener
//  sees the key. A field with its own key handler (the search boxes that run
//  on Enter, the palette, the label designer) keeps its own behaviour: this
//  layer checks for that handler and stands aside, and also stands aside for
//  any key a handler already claimed with preventDefault.
//
//  IT NEVER PRESSES A BUTTON ON A GUESS. Ctrl+S and friends press a button only
//  when exactly one candidate is on screen; otherwise they say so. Enter off the
//  last field FOCUSES the main button rather than pressing it, so posting a GRN
//  is always a deliberate second Enter.
// ==========================================================================
import { useEffect, useRef, useState } from 'react'

const FIELD = 'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset]):not([type=file]):not([type=image]), select, textarea'
const OVERLAYS = '.modal-back, .jump-back, .navdrawer-back, .scan-overlay, .kb-help-back'

const isTyping = (el) => !!el && (el.matches?.('input, select, textarea') || el.isContentEditable)
const visible = (el) => !!el && el.getClientRects().length > 0
  && getComputedStyle(el).visibility !== 'hidden'

// React keeps a node's props on the node under a per-build key. Reading them is
// how this layer tells "a field with its own Enter" from "a plain field" without
// anyone having to mark six hundred inputs.
function reactProps (el) {
  for (const k in el) if (k.startsWith('__reactProps$')) return el[k]
  return null
}
function ownsKeys (el, stop) {
  for (let n = el; n && n !== stop && n !== document.body; n = n.parentElement) {
    const p = reactProps(n)
    if (p && (p.onKeyDown || p.onKeyUp || p.onKeyPress)) return true
  }
  return false
}

// Where a key acts: the dialog on top if one is open, else the screen.
function scope () {
  const tops = [...document.querySelectorAll(OVERLAYS)].filter(visible)
  if (tops.length) return tops[tops.length - 1]
  return document.querySelector('.shellmain') || document.body
}

function fields (root) {
  return [...root.querySelectorAll(FIELD)].filter((el) => !el.disabled && !el.readOnly
    && el.tabIndex !== -1 && visible(el))
}

// The words on a button, without the icon or emoji in front ("📄 New entry").
const label = (b) => (b.textContent || b.getAttribute('aria-label') || b.title || '')
  .replace(/\s+/g, ' ').replace(/^[^\p{L}\p{N}+]+/u, '').trim()
function buttons (root) {
  return [...root.querySelectorAll('button, [role=button]')].filter((b) => !b.disabled && visible(b)
    && !b.closest('.topbar, .navrail, .navdrawer, .tabstrip'))
}
// The one button matching `re`, preferring the screen's primary if several do.
function pick (root, re) {
  const all = buttons(root).filter((b) => re.test(label(b)))
  if (all.length === 1) return all[0]
  const prim = all.filter((b) => b.classList.contains('primary'))
  return prim.length === 1 ? prim[0] : null
}
const MAIN_ACTION = /^(post|submit|confirm|save|approve|finish|accept|create|done|apply|send|receive)\b/i

let toastTimer = null
function say (text) {
  let el = document.getElementById('kb-toast')
  if (!el) {
    el = document.createElement('div')
    el.id = 'kb-toast'; el.className = 'kb-toast'; el.setAttribute('role', 'status')
    document.body.appendChild(el)
  }
  el.textContent = text; el.classList.add('on')
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => el.classList.remove('on'), 2200)
}

// ---- list rows ------------------------------------------------------------
// Body rows: React builds a <table> without the <tbody> an HTML parser would add,
// so rows are found as "has a data cell, not in the header" rather than by tbody.
const bodyRows = (t) => [...t.rows].filter((r) => !r.closest('thead, tfoot')
  && r.closest('table') === t && r.querySelector(':scope > td') && visible(r))
function listTable (root) {
  const cur = root.querySelector('tr.kb-row')
  if (cur && visible(cur)) return cur.closest('table')
  return [...root.querySelectorAll('table')].find((t) => visible(t) && bodyRows(t).length) || null
}
function moveRow (root, d) {
  const t = listTable(root)
  if (!t) return false
  const rows = bodyRows(t)
  const cur = rows.findIndex((r) => r.classList.contains('kb-row'))
  const next = rows[cur < 0 ? (d > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, cur + d))]
  document.querySelectorAll('tr.kb-row').forEach((r) => r.classList.remove('kb-row'))
  next.classList.add('kb-row')
  next.scrollIntoView({ block: 'nearest' })
  return true
}
function openRow (root) {
  const r = root.querySelector('tr.kb-row')
  if (!r || !visible(r)) return false
  if (reactProps(r)?.onClick) { r.click(); return true }
  const first = r.querySelector('a[href], button:not([disabled]), ' + FIELD)
  if (first) { first.focus(); return true }
  return false
}

// ---- grids: ↑/↓ inside a table cell keeps the column ------------------------
function gridMove (el, d) {
  const td = el.closest('td'); const tr = td?.parentElement; const tbody = tr?.parentElement
  if (!td || !tbody) return false
  const col = [...tr.children].indexOf(td)
  const rows = [...tbody.children].filter((r) => r.tagName === 'TR' && visible(r))
  for (let i = rows.indexOf(tr) + d; i >= 0 && i < rows.length; i += d) {
    const f = rows[i].children[col]?.querySelector(FIELD)
    if (f && !f.disabled && visible(f)) { f.focus(); f.select?.(); return true }
  }
  return false
}

function nextField (el, back) {
  const root = el.closest(OVERLAYS) || scope()
  const list = fields(root)
  const i = list.indexOf(el)
  if (i < 0) return false
  const to = list[i + (back ? -1 : 1)]
  if (to) {
    to.focus()
    if (to.select && /^(text|search|number|tel|email|url|)$/.test(to.type || '')) to.select()
    return true
  }
  if (back) return false
  // off the end: land on the main button, do not press it
  const main = pick(root, MAIN_ACTION) || buttons(root).find((b) => b.classList.contains('primary'))
  if (main) { main.focus(); return true }
  return false
}

export const SHORTCUTS = [
  { group: 'Anywhere', keys: [
    ['Ctrl K', 'Jump to any screen'],
    ['Alt 1 … 9', 'Open the first nine screens in the menu'],
    ['Alt ←', 'Back to the previous screen'],
    ['/', 'Go to the search box on this screen'],
    ['Esc', 'Close the dialog, or leave the field you are in'],
    ['F1  ?', 'Show this list'],
  ] },
  { group: 'Entering data (Tally style)', keys: [
    ['Enter', 'Next field — off the last field, onto the main button'],
    ['Shift Enter', 'Previous field'],
    ['↑  ↓', 'Same column, row above / below, in an entry table'],
    ['Ctrl S', 'Save'],
    ['Ctrl Enter', 'Main action — Post, Submit, Confirm'],
    ['Alt N', 'New / Add'],
  ] },
  { group: 'Lists and reports', keys: [
    ['↑  ↓  J  K', 'Move through the rows'],
    ['Enter', 'Open the highlighted row'],
    ['Home  End', 'First / last row'],
  ] },
  { group: 'Store billing counter', keys: [
    ['F2', 'Scan box'], ['F3', 'Customer (same box as scan)'], ['F4', 'New customer'],
    ['F6', 'Discount card'], ['F7', 'Salesman scheme'], ['F8', 'Previous bill'],
    ['F9', 'Change till'], ['F10  Ctrl Enter', 'Bill & print delivery copy'],
    ['Ctrl Delete', 'Clear the bill'],
  ] },
  { group: 'Store settlement', keys: [
    ['F2', 'Cash received'], ['F8', 'Exact cash'], ['Alt 1 … 4', 'Add ₹100 / 200 / 500 / 2000 cash'],
    ['F3', 'Card — rest of the bill'], ['F4', 'UPI — rest of the bill'],
    ['F6', 'Credit note'], ['F7', 'Advance — rest of the bill'],
    ['Enter', 'On to Settle & print, once the bill is covered'],
    ['F10  Ctrl Enter', 'Settle & print'], ['Esc', 'Clear the amounts entered'],
    ['PgUp  PgDn', 'Previous / next bill waiting'], ['F9', 'Find a bill by its QR or number'],
  ] },
]

export function ShortcutHelp ({ onClose }) {
  const ref = useRef(null)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <div className="kb-help-back" onMouseDown={onClose}>
      <div className="kb-help" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts"
        tabIndex={-1} ref={ref} onMouseDown={(e) => e.stopPropagation()}>
        <div className="kb-help-head">
          <b>Keyboard shortcuts</b>
          <button className="modal-x" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="kb-help-body">
          {SHORTCUTS.map((g) => (
            <section key={g.group}>
              <h4>{g.group}</h4>
              <dl>
                {g.keys.map(([k, what]) => (
                  <div key={k}>
                    <dt>{k.split('  ').map((part, i) => (
                      <span key={i}>{i > 0 && <i> or </i>}{part.split(' ').map((x, j) => <kbd key={j}>{x}</kbd>)}</span>))}</dt>
                    <dd>{what}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}

/**
 * The layer itself. `menu` is the side menu's screens in order (for Alt+1…9);
 * `onGo(key)` opens one.
 */
export function KeyboardLayer ({ menu, onGo }) {
  const [help, setHelp] = useState(false)
  const live = useRef({ menu, onGo })
  live.current = { menu, onGo }

  useEffect(() => {
    const open = () => setHelp(true)
    window.addEventListener('essa:kbhelp', open)
    return () => window.removeEventListener('essa:kbhelp', open)
  }, [])

  useEffect(() => {
    const onKey = (e) => {
      if (e.defaultPrevented || e.isComposing) return
      // The target, not document.activeElement: a field whose own Enter handler
      // commits by blurring has already let go of focus by the time we run.
      const el = e.target instanceof Element && e.target !== document.body ? e.target : document.activeElement
      const typing = isTyping(el)
      const k = e.key
      const ctrl = e.ctrlKey || e.metaKey

      // ---- anywhere ----
      if (k === 'F1' || (k === '?' && !typing)) { e.preventDefault(); setHelp((h) => !h); return }
      if (e.altKey && !ctrl && /^Digit[1-9]$/.test(e.code)) {
        const items = (live.current.menu || []).filter(Boolean)
        const it = items[Number(e.code.slice(5)) - 1]
        if (it) { e.preventDefault(); live.current.onGo(it.key) }
        return
      }
      const root = scope()
      if (ctrl && !e.altKey && k.toLowerCase() === 's') {
        e.preventDefault()                       // never the browser's "save page"
        const b = pick(root, /^(save|update)\b/i)
        if (b) b.click(); else say('No single Save button on this screen')
        return
      }
      if (ctrl && k === 'Enter') {
        const b = pick(root, MAIN_ACTION)
        e.preventDefault()
        if (b) b.click(); else say('No single main action here — Tab to the button and press Enter')
        return
      }
      if (e.altKey && !ctrl && e.code === 'KeyN') {
        const b = pick(root, /^(\+\s*)?(new|add)\b/i)
        e.preventDefault()
        if (b) b.click(); else say('Nothing to add on this screen')
        return
      }
      if (k === 'Escape') {
        const top = [...document.querySelectorAll(OVERLAYS)].filter(visible).pop()
        if (top) {
          e.preventDefault()
          if (top.classList.contains('kb-help-back')) { setHelp(false); return }
          const x = top.querySelector('.modal-x, .navdrawer-head button, [aria-label="Close"]')
          if (x) x.click(); else top.click()      // a backdrop click is every dialog's close
          return
        }
        if (typing) { el.blur(); return }
        document.querySelectorAll('tr.kb-row').forEach((r) => r.classList.remove('kb-row'))
        return
      }

      // ---- in a field ----
      if (typing) {
        if (ctrl || e.altKey) return
        if (el.tagName === 'TEXTAREA' || el.isContentEditable) return
        if (ownsKeys(el, root)) {
          // Its own Enter only committed the value by blurring ("Enter = done
          // with this box"). Done with a box, Tally style, means the next one.
          if (k === 'Enter' && !e.shiftKey && document.activeElement === document.body) {
            setTimeout(() => { if (document.activeElement === document.body && el.isConnected) nextField(el) }, 0)
          }
          return
        }
        if (k === 'Enter') {                      // on a tick box too: Space ticks, Enter moves on
          if (nextField(el, e.shiftKey)) e.preventDefault()
          return
        }
        if ((k === 'ArrowDown' || k === 'ArrowUp') && el.tagName === 'INPUT' && el.closest('td')
            && !el.getAttribute('list')) {
          if (gridMove(el, k === 'ArrowDown' ? 1 : -1)) e.preventDefault()
        }
        return
      }

      // ---- not in a field ----
      if (ctrl || e.altKey) return
      if (k === '/') {
        const box = fields(root).find((f) => f.type === 'search'
          || /search|find|filter|look ?up/i.test(`${f.placeholder} ${f.getAttribute('aria-label') || ''}`))
        e.preventDefault()
        if (box) { box.focus(); box.select?.() } else say('No search box here — Ctrl K jumps to a screen')
        return
      }
      const onButton = el && el.matches?.('button, a[href], [role=button]')
      if (onButton) return                        // Enter / arrows belong to the button
      if (k === 'ArrowDown' || k === 'j') { if (moveRow(root, 1)) e.preventDefault(); return }
      if (k === 'ArrowUp' || k === 'k') { if (moveRow(root, -1)) e.preventDefault(); return }
      if (k === 'Home' || k === 'End') {
        if (root.querySelector('tr.kb-row') && moveRow(root, k === 'Home' ? -1e6 : 1e6)) e.preventDefault()
        return
      }
      if (k === 'Enter') { if (openRow(root)) e.preventDefault() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return help ? <ShortcutHelp onClose={() => setHelp(false)} /> : null
}
