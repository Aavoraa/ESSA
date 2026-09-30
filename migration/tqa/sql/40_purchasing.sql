-- 40 · Purchase orders, LR register, GRNs (purchases + lines + breakdowns)
--
-- Every legacy GRN (lrinvoice) was received at the WAREHOUSE — the legacy
-- stock ledger records purchase receipts (PU) only at location 2 — so every
-- ESSA purchase is filed under the migrated warehouse, posted.
--
-- Stock itself is written in step 50, from the pieces; this step writes the
-- documents.

create temp table wh as
select new_id id from migration.map_location where kind = 'warehouse' order by old_id limit 1;

create temp table emp_name as
select old_id, name from migration.map_employee;

-- ---------------------------------------------------------------------------
--  Purchase orders
-- ---------------------------------------------------------------------------
create table migration.map_po (old_id bigint primary key, new_id int not null);

create temp table po as
select migration.int(p.id) id, migration.int(p.pono) pono, migration.iso(p.podate) po_date,
       migration.ref(p.supplierid) supplier_id, migration.ref(p.agentid) agent_id,
       migration.ref(p.transportid) transport_id, p.status, migration.ref(p.actionby) action_by,
       migration.ts(p.actionon) action_on, migration.txt(p.remarks) remarks,
       migration.iso(p.expectedate) expected, migration.num(p.tolerance) tolerance,
       migration.utc(p.createdon) created, migration.utc(p.modifiedon) modified,
       migration.ref(p.createdby) created_by
from legacy.purchaseorder p;

insert into purchase_orders (po_no, po_date, warehouse_id, supplier_id, supplier_name, company, agent,
                             transport, purchaser, subtotal, discount_amount, total, status,
                             entry_source, notes, created_at, updated_at)
select 'PO-' || migration.pad(po.pono, 5), po.po_date, (select id from wh),
       ms.new_id, s.name, (select legal_name from businesses where is_default limit 1),
       (select name from agents where id = (select new_id from migration.map_agent where old_id = po.agent_id)),
       (select name from transports where id = (select new_id from migration.map_transport where old_id = po.transport_id)),
       (select username from migration.map_user where old_id = po.created_by),
       coalesce(l.amount, 0), 0, coalesce(l.amount, 0),
       'pending', 'import',
       concat_ws(E'\n', po.remarks,
                 case when po.expected is not null then 'Expected by ' || po.expected end,
                 case when po.status <> '0' then 'Legacy PO status ' || po.status || ' set by ' ||
                      coalesce((select username from migration.map_user where old_id = po.action_by), 'user ' || po.action_by)
                      || ' on ' || to_char(po.action_on, 'YYYY-MM-DD') end),
       coalesce(po.created, now()), coalesce(po.modified, po.created, now())
from po
left join migration.map_supplier ms on ms.old_id = po.supplier_id
left join suppliers s on s.id = ms.new_id
left join lateral (select sum(coalesce(migration.num(i.oqty), 0) * coalesce(migration.num(i.price), 0)) amount
                   from legacy.purchaseorderitems i where migration.int(i.purchaseorderid) = po.id
                     and coalesce(migration.bool(i.isactive), true)) l on true
order by po.id;

insert into migration.map_po
select po.id, x.id from po join purchase_orders x on x.po_no = 'PO-' || migration.pad(po.pono, 5);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'purchaseorder', po.id::text, 'review',
       'Legacy status code ' || po.status || ' was actioned in the legacy system but its meaning (approved / rejected) is not recorded; imported as pending with the legacy status in Notes.',
       jsonb_build_object('po_no', po.pono, 'action_by', po.action_by, 'action_on', po.action_on)
from po where po.status <> '0';

insert into purchase_order_lines (purchase_order_id, particulars, size, qty, uom, rate, amount,
                                  brand, design_no, hsn, notes)
select m.new_id,
       coalesce(migration.txt(i.description), it.selling_name), migration.txt(it.sizename),
       migration.num(i.oqty), 'PCS', migration.num(i.price),
       round(coalesce(migration.num(i.oqty), 0) * coalesce(migration.num(i.price), 0), 2),
       migration.txt(it.brandname), null, migration.txt(i.hsncode), migration.txt(i.groupname)
from legacy.purchaseorderitems i
join migration.map_po m on m.old_id = migration.int(i.purchaseorderid)
left join (select id, coalesce(migration.txt(sellingname), migration.txt(productname)) selling_name,
                  sizename, brandname from legacy.items) it on it.id = i.itemid
where coalesce(migration.bool(i.isactive), true)
order by m.new_id, migration.int(i.id);

-- ---------------------------------------------------------------------------
--  GRN headers (legacy lrinvoice → ESSA purchases)
-- ---------------------------------------------------------------------------
create table migration.map_grn (old_id bigint primary key, new_id int not null, grn_no text not null);

create temp table inv as
select migration.int(i.id) id, migration.int(i.lrid) lr_id,
       migration.ref(i.suppliercompanyid) supplier_id,
       migration.txt(i.invoiceno) invoice_no, migration.iso(i.invoicedate) invoice_date,
       coalesce(migration.num(i.totalamount), 0) total, coalesce(migration.num(i.taxamount), 0) tax,
       coalesce(migration.bool(i.isactive), true) active,
       coalesce(migration.utc(i.approvedon), migration.utc(i.createdon)) posted,
       migration.utc(i.createdon) created
from legacy.lrinvoice i;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoice', id::text, 'excluded', 'Deleted in the legacy system (isactive = false); not migrated.',
       jsonb_build_object('invoice_no', invoice_no, 'total', total)
from inv where not active;

-- The GRN number people quote is the legacy LR entry number (GRN10254) —
-- it is what every legacy piece's reference (GRN10254.3, .3 = line) begins
-- with, and since Oct-2022 it is unique per invoice. Where one number carries
-- several invoices (mostly the Aug-2022 go-live import, whose entry numbers
-- restart at 1) the legacy invoice id is appended after '#', which no legacy
-- entry number contains, so the result cannot collide.
create temp table grn_no as
select i.id,
       coalesce(migration.txt(e.lrentryno), 'LR' || i.lr_id)
       || case when count(*) over (partition by coalesce(migration.txt(e.lrentryno), 'LR' || i.lr_id)) > 1
               then '#' || i.id else '' end grn_no
from inv i left join legacy.lrentry e on migration.int(e.id) = i.lr_id
where i.active;

do $$ begin
  if exists (select 1 from legacy.lrentry where lrentryno like '%#%') then
    raise exception 'a legacy LR entry number contains #; choose another GRN suffix separator';
  end if;
  if (select count(*) from grn_no) <> (select count(distinct grn_no) from grn_no) then
    raise exception 'GRN numbers are not unique';
  end if;
end $$;

create temp table line_amt as
select invoice_id, sum(amount) amount from migration.grn_line where active group by 1;

insert into purchases (document_id, supplier_id, warehouse_id, grn_no, invoice_number, invoice_date,
                       taxable_total, tax_total, grand_total, status, posted_at, created_at)
select null, ms.new_id, (select id from wh), g.grn_no, i.invoice_no, i.invoice_date,
       coalesce(nullif(la.amount, 0), i.total - i.tax), i.tax, i.total,
       'posted', coalesce(i.posted, now()), coalesce(i.created, now())
from inv i join grn_no g on g.id = i.id
left join migration.map_supplier ms on ms.old_id = i.supplier_id
left join line_amt la on la.invoice_id = i.id
order by i.id;

insert into migration.map_grn
select g.id, p.id, g.grn_no from grn_no g join purchases p on p.grn_no = g.grn_no;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoice', i.id::text, 'review', 'Supplier not found for this GRN; posted without a supplier.',
       jsonb_build_object('supplier_id', i.supplier_id)
from inv i join migration.map_grn m on m.old_id = i.id
where not exists (select 1 from migration.map_supplier s where s.old_id = i.supplier_id);

-- ---------------------------------------------------------------------------
--  GRN lines
-- ---------------------------------------------------------------------------
-- A line that has neither a quantity nor a single barcoded piece is an empty
-- row (nearly all from the legacy system's own Aug-2022 go-live import) and is
-- not carried; everything else is, exactly as billed.
-- Every piece the line lists, including the Aug-2022 go-live pieces the legacy
-- system carried at zero warehouse quantity (their stock went straight to the
-- stores). Those still belong to this GRN line, and linking them is what gives
-- their product its provenance in ESSA (integrity: "created by a posted GRN");
-- the quantity counts only what the warehouse actually received.
create temp table line_pieces as
select grn_line_id, product_id,
       coalesce(sum(qty) filter (where active and qty > 0), 0) qty, count(*) pieces,
       max(price) sale_price, max(mrp) mrp
from migration.piece where grn_line_id is not null and product_id is not null
group by 1, 2;
create index on line_pieces(grn_line_id);

create temp table line_keep as
select g.* from migration.grn_line g
join migration.map_grn m on m.old_id = g.invoice_id
where g.active and (g.qty <> 0 or exists (select 1 from migration.piece p where p.grn_line_id = g.id));

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoiceitems', null, 'excluded',
       'Empty GRN lines (quantity 0 and no barcoded piece) — not carried.',
       jsonb_build_object('lines', count(*),
                          'of_which_go_live_import_2022_08', count(*) filter (where g.created < '2022-09-01'))
from migration.grn_line g join migration.map_grn m on m.old_id = g.invoice_id
where g.active and g.qty = 0 and not exists (select 1 from migration.piece p where p.grn_line_id = g.id);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoiceitems', null, 'excluded', 'Lines deleted in the legacy system (isactive = false) — not carried.',
       jsonb_build_object('lines', count(*))
from migration.grn_line g where not g.active;

create table migration.map_grn_line (old_id bigint primary key, new_id int not null);

-- the line's own product, when that variant still exists as a piece-bearing SKU
create temp table line_src as
select l.*, m.new_id purchase_id, mp.new_id own_product_id,
       it.brandname brand, it.sizename size,
       coalesce(migration.txt(it.sellingname), migration.txt(it.productname)) selling_name,
       mc.name category
from line_keep l
join migration.map_grn m on m.old_id = l.invoice_id
left join migration.map_product mp on mp.item_id = l.item_id and mp.design = l.design
left join legacy.items it on migration.int(it.id) = l.item_id
left join migration.map_category mc on mc.old_id = migration.int(it.productid);

insert into purchase_lines (purchase_id, product_id, barcode, description, hsn, qty, uom, rate, amount,
                            size, brand, design_no, mrp, sale_price, sale_discount_pct,
                            is_new_product, category, unit_type)
select ls.purchase_id, ls.own_product_id, null,
       btrim(concat_ws(' ', coalesce(ls.selling_name, 'ITEM ' || ls.item_id), nullif(ls.design, ''))),
       ls.hsn, ls.qty, 'PCS', ls.rate, coalesce(ls.amount, round(ls.qty * coalesce(ls.rate, 0), 2)),
       migration.txt(ls.size), migration.txt(ls.brand), nullif(ls.design, ''),
       ls.mrp, ls.sale_price,
       case when ls.mrp > 0 and ls.sale_price < ls.mrp then round((1 - ls.sale_price / ls.mrp) * 100, 2) end,
       false, ls.category, 'PCS'
from line_src ls
order by ls.purchase_id, ls.sno nulls last, ls.id;

-- purchase_lines ids follow insert order; map by the same ordering
insert into migration.map_grn_line
select a.id, b.id from
  (select id, row_number() over (order by purchase_id, sno nulls last, id) rn from line_src) a
join
  (select id, row_number() over (order by id) rn from purchase_lines
   where purchase_id in (select new_id from migration.map_grn)) b on b.rn = a.rn;

do $$ begin
  if (select count(*) from migration.map_grn_line) <> (select count(*) from line_src) then
    raise exception 'GRN line map incomplete';
  end if;
  if exists (select 1 from migration.map_grn_line m join purchase_lines pl on pl.id = m.new_id
             join migration.grn_line g on g.id = m.old_id join migration.map_grn mg on mg.old_id = g.invoice_id
             where pl.purchase_id <> mg.new_id) then
    raise exception 'GRN line map misaligned';
  end if;
end $$;

-- ---------------------------------------------------------------------------
--  Breakdown rows: where a line's pieces are now a different variant than the
--  line was billed as (or several), the line gets ESSA's attribute breakdown —
--  one PurchaseLineSplit per stock item the pieces became. A line whose pieces
--  are all still its own variant stays a plain line, as ESSA would post it.
-- ---------------------------------------------------------------------------
insert into purchase_line_splits (line_id, product_id, size, color, material, pattern, fit, product_type,
                                  design_no, brand, style, sleeve, category, unit_type, qty, rate, mrp,
                                  sale_price, sale_discount_pct, code, is_new_product, created_at)
select ml.new_id, lp.product_id, p.size, p.color, p.material, p.pattern, p.fit, p.product_type,
       p.design_no, p.brand, p.style, p.sleeve, p.category, 'PCS', lp.qty, g.rate, lp.mrp, lp.sale_price,
       case when lp.mrp > 0 and lp.sale_price < lp.mrp then round((1 - lp.sale_price / lp.mrp) * 100, 2) end,
       null, false, coalesce(g.created, now())
from line_pieces lp
join migration.grn_line g on g.id = lp.grn_line_id
join migration.map_grn_line ml on ml.old_id = lp.grn_line_id
join products p on p.id = lp.product_id
join line_src ls on ls.id = lp.grn_line_id
where ls.own_product_id is null
   or exists (select 1 from line_pieces o where o.grn_line_id = lp.grn_line_id
              and o.product_id <> ls.own_product_id)
order by ml.new_id, lp.product_id;

-- ---------------------------------------------------------------------------
--  LR register (legacy lrentry → ESSA lr_entries)
-- ---------------------------------------------------------------------------
create table migration.map_lr (old_id bigint primary key, new_id int not null);

create temp table lr as
select migration.int(e.id) id, migration.txt(e.lrentryno) entry_no, migration.txt(e.lrno) lr_no,
       migration.iso(e.lrdate) lr_date, migration.iso(e.receiveddate) recv_date,
       migration.iso(e.createdon) entry_date,
       migration.ref(e.transportid) transport_id, migration.ref(e.supplierid) supplier_id,
       migration.ref(e.agentid) agent_id, migration.num(e.agentcommission) agent_commission,
       migration.num(e.noofbundels) bundles, migration.num(e.noofboxes) boxes,
       migration.txt(e.invoiceno) inv_no, migration.iso(e.invoicedate) inv_date,
       migration.num(e.noofpieces) pieces, migration.num(e.totalamount) amount,
       migration.num(e.freightcharge) freight, migration.bool(e.hasfreightcharge) has_freight,
       nullif(migration.num(e.stockholdingperiod), 0) holding, nullif(migration.num(e.premargin), 0) margin,
       migration.ref(e.purchasemanagerid) manager_id, migration.txt(e.remarks) remarks,
       e.modeofdelivery, coalesce(migration.bool(e.isactive), true) active,
       migration.utc(e.createdon) created
from legacy.lrentry e;

create temp table lr_base as select coalesce(max(id), 0) max_id from lr_entries;

insert into lr_entries (document_id, warehouse_id, entry_source, lr_entry_no, lr_entry_date, lr_mode,
                        recv_date, transport, bundle, boxes, lr_no, lr_date, supplier_name, agent,
                        agent_commission, inv_no, inv_date, qty, amount, auto_transfer_location,
                        purchase_manager, stock_holding_days, additional_margin, paid_topay,
                        freight_applicable, freight_amount, freight_total, freight_charges, item,
                        received_by, source_language, original_values, purchase_order_id,
                        invoice_document_id, grn_no, matched, mismatches, created_at)
select null, (select id from wh), 'import', lr.entry_no, lr.entry_date,
       -- the legacy delivery-mode code is not labelled anywhere in the export; a
       -- named transporter is the one thing that says it came by Transport
       case when lr.transport_id is not null then 'Transport' end,
       lr.recv_date, t.name, lr.bundles, lr.boxes, lr.lr_no, lr.lr_date, s.name, a.name,
       nullif(lr.agent_commission, 0), lr.inv_no, lr.inv_date, lr.pieces, lr.amount, null,
       (select name from emp_name where old_id = lr.manager_id), coalesce(lr.holding, 90), lr.margin,
       null, coalesce(lr.has_freight, false), nullif(lr.freight, 0), nullif(lr.freight, 0), '{}'::json,
       null,
       -- ESSA marks a consignment received by who signed for it; legacy recorded
       -- the received DATE and no name, so a received one is marked as such
       case when lr.recv_date is not null then 'Legacy system' end,
       null, '{}'::json,
       null, null,
       (select string_agg(m.grn_no, ', ' order by m.grn_no) from inv i join migration.map_grn m on m.old_id = i.id
        where i.lr_id = lr.id),
       exists (select 1 from inv i join migration.map_grn m on m.old_id = i.id where i.lr_id = lr.id),
       '[]'::json, coalesce(lr.created, now())
from lr
left join transports t on t.id = (select new_id from migration.map_transport where old_id = lr.transport_id)
left join suppliers s on s.id = (select new_id from migration.map_supplier where old_id = lr.supplier_id)
left join agents a on a.id = (select new_id from migration.map_agent where old_id = lr.agent_id)
where lr.active
order by lr.id;

insert into migration.map_lr
select a.id, b.id from (select id, row_number() over (order by id) rn from lr where active) a
join (select id, row_number() over (order by id) rn from lr_entries
      where id > (select max_id from lr_base)) b on b.rn = a.rn;

do $$ begin
  if exists (select 1 from migration.map_lr m join lr_entries e on e.id = m.new_id
             join lr on lr.id = m.old_id where e.lr_no is distinct from lr.lr_no) then
    raise exception 'LR map misaligned';
  end if;
end $$;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrentry', null, 'review',
       'Legacy delivery-mode codes carry no label in the export; ESSA''s LR mode was set to Transport where a transporter is named, blank otherwise.',
       jsonb_object_agg(coalesce(modeofdelivery, 'blank'), n)
from (select modeofdelivery, count(*) n from lr where active group by 1) x;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrentry', id::text, 'unmatched', 'LR remark not carried: ESSA''s LR register has no remarks field (it was removed as unused).',
       jsonb_build_object('lr_no', lr_no, 'remarks', remarks)
from lr where active and remarks is not null;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrentry', id::text, 'excluded', 'Deleted in the legacy system (isactive = false); not migrated.',
       jsonb_build_object('lr_no', lr_no) from lr where not active;
