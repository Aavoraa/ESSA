# ESSA AI — UI redesign: route inventory, component audit and migration plan

Status: **plan only — nothing in this document has been implemented, deleted or replaced.**
Reference design: the Central Dashboard (`CentralDashboard` in `frontend/src/App.jsx`).

---

## 1. What the application actually is

ESSA is **two front ends shown as one product**:

| Part | Technology | Where | Screens |
|---|---|---|---|
| **ESSA** (Central + Warehouse workspaces) | React 18 + Vite, one file `frontend/src/App.jsx` (~19,400 lines) + `DatabaseConnection.jsx`, `keyboard.jsx`; one stylesheet `styles.css` (3,556 lines); **no router library** — screens are keyed tabs (`setTab('inventory')`) inside one shell | served by FastAPI at `/` | 33 screens + 6 overlays |
| **Store / POS** (Store workspace) | Flask + Jinja2 + Bootstrap 5.3 + Bootstrap Icons; `static/css/app.css` (1,417 lines) | mounted at `/pos`, shown **inside an iframe** in the ESSA shell | 19 menu screens, 61 templates |
| **Phone page** | single `backend/app/mobile/index.html` (2,671 lines), loaded by the Android/iOS WebView apps | `/m` | 1 app (scan, LR, GRN, stock) |

Consequence for the redesign: one design system has to be expressed **twice** — as React components + CSS for ESSA, and as Jinja partials + CSS for the shop — from **one shared token file**, or the two halves will drift again.

Dependencies today: React, React-DOM, Vite (ESSA); Bootstrap, Bootstrap Icons, Chart.js, html5-qrcode (shop, from CDN). ESSA's icons are already **Lucide outline paths inlined** in the `ICONS` map — no package needed.

---

## 2. Route / screen inventory — ESSA (React)

Workspaces are switched by the context bar (Central · Warehouse · Store / POS). Role gates come from `MODULES[].min` and `security.py` POLICY and are **not changed** by the redesign.

### Central workspace (no warehouse chosen)

| Rail group | Screen (tab key) | Min role | Screen type | Notes |
|---|---|---|---|---|
| Home | Command Center (`command`) | superadmin | Dashboard | KPI tiles, question box + voice, Business overview charts, warehouse/store tables |
| Home | Central Dashboard (`central`) | admin | Dashboard | **The reference design** |
| Home | Choose a warehouse (`pickwh`) | user | Picker | card grid |
| Stock | Item Locator (`locator`) | user | Search → result | |
| Reports | Reports (`reports`) | admin | Report catalogue + report view | ask bar, catalogue list, report tables, CSV export |
| Reports | Audit Trail (`audit`) | admin | Listing | filters, segmented filter, table |
| Setup | Locations (`locations`) | admin | Tree editor | warehouse → store → floor → till |
| Setup | Users & Access (`users`) | superadmin | Listing + inline forms | role legend, access editor |
| Setup | Database Connection (`database`) | superadmin | Settings form + diagnostics | |

### Warehouse workspace (inside one warehouse)

| Rail group | Screen (tab key) | Min role | Screen type | Notes |
|---|---|---|---|---|
| Home | Warehouse Dashboard (`dashboard`) | user | Dashboard | notifications list, Figures / Charts switch |
| Purchase | Purchase Orders (`purchase_orders`) | user | Listing + form | |
| Purchase | LR Entry (`lr`) | user | Register + entry form | big editable register |
| Purchase | Invoice Entry (`documents`) | user | Register + **invoice review editor** | sub-tabs Invoices / Products; **GRN (`purchases`) and Inventory (`inventory`) fold into this screen** |
| Purchase | Returns (`returns`) | admin | List-detail | new return wizard |
| Purchase | Payments (`payments`) | admin | List-detail | |
| Purchase | Suppliers (`suppliers`) | admin | List-detail | |
| Stock | Item Locator (`locator`) | user | Search → result | |
| Stock | Stock Audit (`stock_audit`) | user | Hub with Shelf / Quantity switch | **Physical Stock Audit (`physical_audit`) folds in here** |
| Stock | Price Changer (`pricing`) | user | 3-step wizard | |
| Stock | Dead Stock & Clearance (`deadstock`) | admin | Dashboard + register tabs | 6 internal tabs |
| Transfers | Stock Outward (`outward`) | user | List-detail + new-outward form | |
| Transfers | Stock Inward (`inward`) | user | List-detail | |
| Labels | Label Designer (`labels`) | admin | Card grid + visual editor | |
| Labels | QR / Label Printing (`labelprint`) | user | Listing + preview | |
| Reports | Reports, Audit Trail | admin | as above | |
| Setup | Masters (`masters`) | admin | Card grid → master tables | ~10 master editors |
| Setup | Catalogues (`catalogues`) | admin | Settings | |
| Setup | Locations, Users & Access, Database Connection | as above | | |

### Shell, overlays and shared surfaces

| Surface | Component | Notes |
|---|---|---|
| Login | `LoginScreen` | |
| Top header | in `App()` + `UserMenu`, `NotificationBell` | search, bell, account menu |
| Context bar | in `App()` | Back, workspace tabs, place chip / warehouse picker |
| Open-screens strip | `TabStrip` | appears with 2+ open screens |
| Side rail + fly-out | `NavRail` | per-workspace groups (`NAV_RAIL`) |
| Category page | `CategoryPage` | opened from a fly-out heading |
| Jump palette (Ctrl + K) | `JumpPalette` | |
| Notifications | `NotificationPanel` | drawer |
| Keyboard help (F1) | `keyboard.jsx` | |
| Change password | `ChangePassword` | modal |
| Vision settings | `VisionSettings` | modal |
| Toasts | `toast()` | |

---

## 3. Route / screen inventory — Store / POS (Flask, in the iframe)

| Rail group | Screen | Route | Main templates | Screen type |
|---|---|---|---|---|
| Home | Store Dashboard | `/pos/` home | `dashboard.html` | Dashboard |
| Billing | Floor Sales | `/floor/` | `floor/index, salesperson, customer` | Workflow |
| Billing | Billing Counter | `/pos/` counter | `pos/counter.html` | **POS work screen** |
| Billing | Settlement | `/pos/settle` | `pos/settle.html` | POS work screen |
| Billing | Delivery | `/delivery/` | `delivery/index, list, pending, note` | Work screen + print |
| Billing | Invoices | `/pos/invoices` | `pos/invoices, invoice` | Listing + print |
| Billing | Returns | `/returns/` | `returns/index, list, note` | Workflow + print |
| Stock | Store Stock | `/inventory/` | `inventory/list, form, categories, movements, pieces, labels` | Listing + forms |
| Stock | Stock Inward | `/stock-inward/` | `store_stock/inward` | Workflow |
| Stock | Transfer Goods | `/transfer-goods/` | `store_stock/transfer` | Form |
| Stock | Physical Stock Audit | `/audits/` | `audits/index, detail, print` | Listing + detail + print |
| Stock | Stock Check | `/stock-check/` | `checker/index` | Search |
| Stock | Alteration | `/alterations/` | `alterations/index, list, tailors, ticket` | Workflow + print |
| Customers | Customers | `/customers/` | `customers/list, detail, form, card, advances, advance_receipt, feedback, messages` | Listing + detail + forms |
| Customers | Promotions | `/promotions/` | `promotions/schemes, list, detail, form` + `coupons/index` | Editor |
| Reports | Store Reports | `/reports/` | `reports/index, view, low_stock` | Catalogue + report |
| Setup | Floors & Tills | `/stores/` | `stores/index, series` | Settings |
| Setup | Staff | `/staff/` | `staff/list, form, card, attendance, commissions, advances` | Listing + forms + print |
| — | Cash drawer | `/drawer/` | `drawer/index` | Form |
| — | Login / store access / feedback | | `auth/login`, `store_access`, `feedback/give` | |

Route count by blueprint: pos 24, floor 16, promotions 16, customers 15, inventory 13, audits 11, staff 9, store_stock 9, alterations 8, delivery 8, stores 7, returns 6, coupons 5, reports 5, drawer 3, auth 2, main/checker/feedback 1 each.

**Printed documents** (bills, delivery notes, return notes, alteration tickets, staff/customer cards, audit print, labels) are print layouts and are **out of scope** — they keep their print design.

---

## 4. Requested areas vs what exists (nothing will be invented)

| Requested in the brief | What exists today | Plan |
|---|---|---|
| Stock Ledger, Stock Movement | Inventory → product detail movements (ESSA); `inventory/movements.html` (shop) | redesign those; no new screens |
| Transfer creation / tracking / dispatch / receiving / pending / history | Stock Outward (create, dispatch, register), Stock Inward (receive), Transfer Goods (store) | redesign those three |
| Label generation / barcode management / printing / history | Label Designer, QR / Label Printing, shop label print | no "label history" screen exists — not created |
| Purchase Reports, Inventory reports, Financial summaries | Reports catalogue (ESSA) + Store Reports (shop) | redesign the two catalogues and the report view |
| Permissions, System configuration | Users & Access (roles, access editor), Database Connection, Vision settings | no separate permissions screen — not created |
| Warehouse / store configuration | Locations, Floors & Tills, Catalogues, Masters | redesign those |

---

## 5. UI component audit

### ESSA (React)

| Component | Defined | Used | Today | Target |
|---|---|---|---|---|
| `Icon` (Lucide paths) | 1 | 56 + rail/menus | inline SVG map | keep; add missing Lucide glyphs; one size scale (14/16/18/20) |
| `Section` (panel) | 1 | 51 | card with collapse `−` | `Card` with header (icon, title, actions, collapse) |
| `DashTile` | 1 | 34 | tile, CSS-mask icon | merge with `CdTile` → one `KpiCard` |
| `CdTile`, `CdStatus`, `CdMenu` | 1 each | 16 / 3 / 2 | Central Dashboard only | become the system `KpiCard`, `StatusBadge`, `DropdownMenu` |
| `table.items` (raw markup) | — | 60 tables | CSS class, per-screen columns | `DataTable` styles (+ sort/empty/loading where the screen already supports them) |
| `Pager` | 1 | 23 | | restyle as `Pagination` |
| `SearchBox`, `FilterPanel`, `Sidebar` | 1 each | 18 / 14 / 8 | | `SearchInput`, `FilterBar`, list-detail `ListPane` |
| `ChartCard`, `LineChart`, `GroupedBars`, `Legend` | 1 each | 17 / 3 / 5 / 5 | | restyle to palette |
| `AskAnything`, `AskBar`, `VoiceButton` | 1 each | 1 / 1 / 2 | | one `AskBar` |
| Buttons | class `.btn` | 257 (68 primary) | | `Button` variants: primary, secondary, ghost, danger, icon |
| Badges | class `.badge` | 42 | | `StatusBadge` with semantic colours |
| Modals | ad-hoc `.modal-back` | 5 | | `Modal` (sizes S/M/L) |
| **Native `confirm()`** | — | **31** | browser dialog | `ConfirmDialog` (same yes/no outcome) |
| Empty / warning boxes | `.empty`, `.warnbox` | 91 / 31 | | `EmptyState`, `Alert` |
| Selects / dates | raw `<select>`, `type=date` | 67 / 4 | | styled `Select`, `DateInput` |
| **Inline styles** | `style={{…}}` | **1,068** | spacing/layout per screen | replaced by layout utilities screen by screen |
| `styles.css` | — | 3,556 lines, 34 `:root`/`@media` blocks, 161 distinct colours | **many override layers added over time** | rebuilt as `tokens.css` + component CSS |

### Store / POS (Flask)

| Item | Today | Target |
|---|---|---|
| Layout | `base.html` + Bootstrap grid | keep Bootstrap grid; restyle via tokens |
| Page head | `.page-head` (most pages), ad-hoc on some | Jinja macro `page_header(title, sub, actions)` |
| KPI | `.kpi-card` (dashboard), `.stat-card` (17 uses) | one `kpi` macro, same look as ESSA `KpiCard` |
| Panels | Bootstrap `.card` | token-styled card |
| Tables | Bootstrap `.table` | token-styled, same as ESSA `DataTable` |
| Icons | **Bootstrap Icons, 106 distinct** | Lucide (decision D3) |
| Inline styles | 149 | moved to classes |
| Native `confirm()` | 19 | Bootstrap modal confirm (decision D4) |

---

## 6. Design system (Phase 2)

One token file, imported by both front ends:

- **Colour** — exactly the brief's palette: primary `#075B45`, secondary `#168568`, light green `#E8F4EE`, accent `#D9A441`, background `#F6F8F7`, card `#FFFFFF`, text `#182B25` / `#68756F`, border `#E5EBE7`, success `#249B68`, warning `#D99A35`, error `#D9534F`, info `#4385B8`. Amber only for money/attention.
- **Type** — Inter. Page title 26px/700, section 16px/600, card title 13px/500, body 13–14px, support 12px, table 13px.
- **Space** — 4px base: 4 / 8 / 12 / 16 / 20 / 24 / 32.
- **Radius** — 6 (inputs, badges), 10 (buttons, cards), 14 (dialogs).
- **Elevation** — 3 shadows: card, dropdown/flyout, modal.
- **Icons** — Lucide outline, 1.75 stroke, sizes 14/16/18/20.
- **States** — hover, focus-visible ring (2px secondary), disabled, loading, for every control.

Components built once: Button, IconButton, Input, Select, DateInput, SearchInput, Checkbox/Switch, Card, KpiCard, DataTable, Pagination, Tabs/Segmented, StatusBadge, DropdownMenu, Modal, ConfirmDialog, Drawer, Tooltip, Toast, Breadcrumb, EmptyState, LoadingState, Alert, PageHeader, FilterBar, FormSection.

---

## 7. Migration approach — how every screen moves without losing a workflow

**Rule: restyle and recompose, never rewrite logic.** Each screen keeps its state, API calls, permission checks and fields; only its markup is wrapped in the system components and its inline styles replaced.

Per screen, the same 6 steps:
1. **Baseline** — screenshot + note its workflows (what buttons do, what it posts) before touching it.
2. Swap the header for `PageHeader` (title, subtitle, actions stay the same buttons).
3. Swap tiles → `KpiCard`, panels → `Card`, tables → `DataTable` classes, badges → `StatusBadge`.
4. Replace inline layout styles with layout utilities.
5. Replace `confirm()` with `ConfirmDialog` (same outcome on Yes / No).
6. **Verify** — run the screen's workflow end to end, compare against the baseline, check each role that can open it.

### Screen type → template

| Type | Screens | Template |
|---|---|---|
| Dashboard | Command Center, Central, Warehouse, Dead Stock, Store Dashboard | PageHeader + filters → KPI row → charts → tables → alerts |
| Listing | Purchase Orders, LR register, Invoice register, QR Printing, Audit Trail, Users, Store Stock, Customers, Invoices (shop), Staff | PageHeader (+ primary action) → FilterBar (search, filters) → DataTable → Pagination |
| List-detail | Returns, Payments, Suppliers, Outward, Inward, Reports, Physical audit (shop) | ListPane (title, search, filters, list, pager) + DetailPane (record header, status, metadata, sections, actions) |
| Form / wizard | PO form, LR entry, New outward, New return, Price Changer, Transfer Goods, Customer/Staff forms, Promotions | FormSection groups, max 3 fields per row, required markers, sticky action bar |
| Editor | Invoice review, Label Designer, Locations tree, Masters editors, Floors & Tills | split layout, tokenised controls; editing behaviour untouched |
| POS work screens | Billing Counter, Settlement, Delivery, Floor Sales | dense keyboard-first layout kept; restyle only — **no change to keys, focus order or scanning** |
| Settings | Database Connection, Catalogues, Vision settings | FormSection cards |

### Phases

| Phase | Work | Output you can check |
|---|---|---|
| 1 Audit | this document | approve / correct |
| 2 Design system | `tokens.css`, component CSS, React components, Jinja macros, shop token layer | a component gallery page (dev only) |
| 3 Shell | sidebar (collapsible, tooltips), white fly-out, header, breadcrumbs, context bar | every screen opens with the new shell; workspace switching tested |
| 4 Central Dashboard | rebuilt on the system components | final standard signed off |
| 5 Modules | Central → Warehouse (Purchase, Stock, Transfers, Labels, Reports, Setup) → Store / POS | module by module, each verified before the next |
| 6 Forms & tables | listing, forms, detail, reports standardised; `confirm()` replaced | |
| 7 QA | all routes × roles × workspaces; key workflows; 1366 / 1920 / tablet | checklist with results |

Each phase is a separate commit, so any phase can be reverted on its own.

### Workflows that must pass after every phase

LR → Invoice upload/review → GRN → Inventory · Purchase Order · Purchase Return · Supplier Payment · Stock Outward → Stock Inward · Stock Audit (shelf and quantity) · Price Changer · Label design → print · Reports (ask, view, export CSV) · Users & roles · Locations · Database test · Billing Counter → Settlement → Delivery · Floor Sales · Store Return · Store Stock Inward / Transfer · Customer · Promotion · Staff · warehouse ↔ store context switching · Ctrl + K, F1, notifications · login/logout per role (user, admin, superadmin, superboss, store user).

---

## 8. Decisions needed before Phase 2

| # | Question | Recommendation |
|---|---|---|
| D1 | **Sidebar and fly-out.** The brief asks for a collapsible icon + label sidebar and a **white** fly-out, and says not to use the dark-green fly-out. Earlier in this project you asked to keep the old rail and dark fly-out. | Follow the brief (white fly-out, collapsible sidebar) — confirm |
| D2 | **Palette change** from the current `#0B3D2E` to the brief's `#075B45` everywhere, including the header and rail | Yes |
| D3 | **Shop icons** — replace 106 Bootstrap Icons with Lucide via a local SVG sprite (no new runtime dependency, no CDN) | Yes |
| D4 | **Native confirm dialogs** (31 ESSA + 19 shop) → in-app confirmation dialogs with the same Yes/No outcome | Yes |
| D5 | **Phone page `/m`** — include in this redesign or leave for later | Later, separate pass |
| D6 | **Printed documents** stay as they are | Yes |

Nothing will be deleted, renamed or re-routed. Module names, menu groups, routes, roles and fields stay exactly as listed above.

---

## Phase 2 — design system (done)

| Piece | File | Notes |
|---|---|---|
| Tokens (single source) | `frontend/src/design/tokens.css` | the brief's palette, type scale, spacing, radii, shadows, control heights — all `--ui-*` |
| Component styles | `frontend/src/design/components.css` | buttons, fields, form sections, page header, breadcrumb, filter bar, cards, KPI cards, badges, tables, pagination, tabs, segmented switch, dropdown menu, modal, confirm, drawer, tooltip, toasts, alerts, empty / loading / skeleton — all `.ui-*` |
| Icons (single source) | `frontend/src/design/icons.js` | the Lucide outline set as plain data (75 icons); ESSA's `Icon` now reads it |
| React components | `frontend/src/design/ui.jsx` | `Icon, Button, IconButton, Field, Input, Select, DateInput, Textarea, SearchInput, Switch, FormSection, Breadcrumb, PageHeader, FilterBar, Card, KpiGrid, KpiCard, StatusBadge, DataTable, Pagination, Tabs, Segmented, DropdownMenu, Modal, Drawer, ConfirmProvider/useConfirm, Alert, EmptyState, LoadingState, Skeleton, Tooltip, ToastHost` |
| Shop copy | `Textile Retail Shop/app/static/css/tokens.css`, `components.css`, `static/icons/sprite.svg` | **generated** by `vite.config.js` on every build / dev start — edit the React-side files only |
| Shop macros | `Textile Retail Shop/app/templates/_ui.html` | `icon, page_header, button, kpi, card, badge, empty, alert` |
| Shop behaviour | `Textile Retail Shop/app/static/js/ui.js` | `uiConfirm()` and `data-ui-confirm` — the in-app replacement for `confirm()` |
| Gallery | `frontend/src/design/Gallery.jsx` | dev server only: `npm run dev`, open `http://localhost:5173/#ui-gallery`; excluded from production builds |

No screen uses the new components yet, so the running application looks the same; screens move onto them from Phase 3.

## Phase 3 — application shell (done)

| Piece | Where | What changed |
|---|---|---|
| Sidebar | `NavRail` in `App.jsx`, styles in `frontend/src/design/shell.css` | fixed, icon + label rows, ESSA AI brand and workspace at the top, clear active marker, **collapses to 72 px icons with tooltips** (remembered per computer), connection status and Collapse at the bottom |
| Fly-out | same | **white** panel beside the sidebar: module name, its one-line description and the building it applies to; each screen with an outline icon and its own description; the open screen in light green; scrolls on its own; closes on outside click, Escape, or choosing a screen; overlays the page without moving it; "All of … on one page" opens the existing category page |
| Header | `App()` header | brand moves to the sidebar on desktop widths; **breadcrumbs** (place › menu group › screen); screen search with the Ctrl K hint; notifications; account menu |
| Context bar | `App()` | Back, Central / Warehouse / Store, place chip — same behaviour, new tokens |
| Palette | `styles.css`, shop `app.css` and templates | every brand green moved to the new palette (#075B45 family, 302 replacements) |
| Menu descriptions | `NAV_RAIL` | one short description per menu group (17) |

Unchanged on purpose: every module, menu group, screen, role check and workspace switch.
Below 1024 px the sidebar gives way to the existing menu button and drawer (to be restyled with the forms in Phase 6).

### Phase 3 — revised on request (2026-11-25)

- **D1 reversed:** the side rail and its dark-green fly-out are kept as they were (`NavRail`, `.navrail*` styles). The new sidebar was removed.
- **D2 reversed:** ESSA's original green stays everywhere — `--ui-primary #0B3D2E`, `--ui-secondary #2F8F68`, light green `#E7F4EC`. The design tokens now carry these values.
- Kept from Phase 3: header breadcrumbs, the Ctrl K hint, context-bar styling, and one accent colour per KPI tile on the Central Dashboard and Command Center.
