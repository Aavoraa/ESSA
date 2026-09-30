-- 50 · Stock: piece codes, supplier returns, dispatches to stores, and the
--      warehouse ledger that ties them together.
--
-- ESSA's rule (services/integrity.py): stock exists only because a posted GRN
-- put it there, and changes only through ledger rows written by
-- stock_locations.apply. So the warehouse ledger is rebuilt from documents:
--
--   + GRN inwards        kind=inward     ref_type=purchase         (legacy pieces, per GRN)
--   − supplier returns   kind=return     ref_type=purchase_return  (legacy lrinvoicereturn)
--   − dispatches         kind=outward    ref_type=outward          (legacy transfer notes, `bundles`)
--   ± reconciliation     kind=adjustment ref_type=adjustment       (one per product, see below)
--
-- The legacy system also moved goods in ways that left no document ESSA has a
-- type for (store → warehouse returns, transfers keyed without a transfer
-- note, the Aug-2022 go-live balances). Those are not invented as documents.
-- Instead each product gets ONE adjustment that brings ESSA's warehouse
-- balance to the legacy closing warehouse balance, and every one is listed in
-- the migration report. After this step ESSA's warehouse stock equals legacy's.
--
-- Legacy closing warehouse stock per piece = qty − transferred − returned, for
-- active pieces the legacy system still flags as held (stock.hasstock).

create temp table wh as
select new_id id from migration.map_location where kind = 'warehouse' order by old_id limit 1;

create temp table cutoff as
select coalesce(max(migration.utc(transactedon)), now()::timestamp) at from legacy.stocktransaction;

-- ---------------------------------------------------------------------------
--  Piece → posted GRN
-- ---------------------------------------------------------------------------
alter table migration.piece add column purchase_id int;
update migration.piece p set purchase_id = mg.new_id
from migration.grn_line g join migration.map_grn mg on mg.old_id = g.invoice_id
where g.id = p.grn_line_id;

-- pieces that hold (or held) goods: active, positive quantity, a posted GRN
create temp table stock_piece as
select * from migration.piece where active and qty > 0 and purchase_id is not null and product_id is not null;
create index on stock_piece(id);
-- temp tables are never auto-analysed, and rows written earlier in this same
-- transaction are not in the planner's statistics either: without these the
-- planner takes million-row tables for empty ones and picks nested loops
analyze stock_piece;
analyze migration.piece;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'stock', null, 'excluded', why, jsonb_build_object('pieces', count(*), 'qty', sum(qty))
from (select case when not active then 'Pieces deactivated in the legacy system — not stock.'
                  when qty <= 0 then 'Barcodes with zero quantity (nothing was received under them) — no piece code created.'
                  else 'Pieces whose GRN was deleted in the legacy system — no posted GRN to receive them against.' end why,
             qty
      from migration.piece p
      where not (active and qty > 0 and purchase_id is not null and product_id is not null)) x
group by why;

-- ---------------------------------------------------------------------------
--  Store-side current stock per piece (used for unit status here, and for the
--  shop's stock in step 70). Legacy `salablegoods` is one row per piece per
--  place; `hasstock` is the legacy system's own "held here" flag and matches
--  its stock ledger for 99.8% of pieces the ledger covers.
-- ---------------------------------------------------------------------------
create table migration.store_piece as
select migration.int(g.id) id, migration.int(g.stockid) piece_id, migration.int(g.locationid) location_id,
       coalesce(migration.bool(g.hasstock), false) has_stock,
       coalesce(migration.num(g.iqty), 0) - coalesce(migration.num(g.oqty), 0) balance,
       migration.utc(g.createdon) created
from legacy.salablegoods g;
create index on migration.store_piece(piece_id);

alter table migration.store_piece add column held numeric;
update migration.store_piece set held = case when has_stock and balance > 0 then balance else 0 end;
analyze migration.store_piece;

-- ---------------------------------------------------------------------------
--  Piece codes (ESSA product_units). The code IS the legacy barcode printed on
--  the garment, so every tag already in the shop scans (warehouse_items.
--  resolve_scan → fetch_item(unit_code)). seq numbers pieces within their SKU.
-- ---------------------------------------------------------------------------
create temp table sold_piece as
select distinct migration.int(stockid) piece_id from legacy.billitems
where migration.int(stockid) is not null;
create index on sold_piece(piece_id);

create temp table store_held as
select piece_id, sum(held) held from migration.store_piece group by 1;
create index on store_held(piece_id);

insert into product_units (product_id, code, seq, purchase_id, bundle_id, status, print_count,
                           last_printed_at, last_printed_by, created_at)
select p.product_id, p.barcode,
       row_number() over (partition by p.product_id order by p.created nulls first, p.id),
       p.purchase_id, null,
       case when p.returned >= p.qty then 'returned'
            when p.has_stock and p.qty - p.transferred - p.returned > 0 then 'in_stock'
            when coalesce(sh.held, 0) > 0 then 'dispatched'
            when sp.piece_id is not null then 'sold'
            else 'dispatched' end,
       -- the tag is already on the garment: it was printed by the legacy system
       1, p.created, 'legacy', coalesce(p.created, now())
from stock_piece p
left join store_held sh on sh.piece_id = p.id
left join sold_piece sp on sp.piece_id = p.id
where p.barcode is not null and p.barcode <> ''
order by p.product_id, p.created nulls first, p.id;

-- Pack tags: one legacy barcode over several pieces (41K tags, ~1.2M pieces).
-- ESSA gives every received piece its own code — units.create_for_receipt mints
-- {SKU}-001, -002 … at GRN, one per piece, for receipts of up to MAX_PER_RECEIPT
-- (500) pieces. The legacy tag stays the code of the pack's first piece (so it
-- still scans); the pack's other pieces get ESSA's own codes, exactly as a GRN
-- posted in ESSA would have made them. Receipts over 500 pieces are not
-- serialised by ESSA and are left with their legacy tags only.
analyze product_units;

create temp table receipt_ok as
select product_id, purchase_id from stock_piece
group by 1, 2 having sum(qty) <= 500 and sum(qty) = round(sum(qty));

create temp table pack as
select p.id piece_id, p.product_id, p.purchase_id, p.qty::int q, u.status, p.created
from stock_piece p
join product_units u on u.code = p.barcode
join receipt_ok r on r.product_id = p.product_id and r.purchase_id = p.purchase_id
where p.qty > 1 and p.qty = round(p.qty);

analyze receipt_ok;
analyze pack;

create temp table seq_base as
select product_id, max(seq) max_seq from product_units group by 1;
analyze seq_base;

insert into product_units (product_id, code, seq, purchase_id, bundle_id, status, print_count,
                           last_printed_at, last_printed_by, created_at)
select x.product_id, pr.sku || '-' || migration.pad(x.seq, 3), x.seq, x.purchase_id, null, x.status,
       0, null, null, coalesce(x.created, now())
from (select pk.product_id, pk.purchase_id, pk.status, pk.created,
             sb.max_seq + row_number() over (partition by pk.product_id
                                             order by pk.created nulls first, pk.piece_id, g) seq
      from pack pk cross join generate_series(2, pk.q) g
      join seq_base sb on sb.product_id = pk.product_id) x
join products pr on pr.id = x.product_id
order by x.product_id, x.seq;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'stock', null, 'review',
       'Pack tags (one legacy barcode over several pieces): the pack''s other pieces were given ESSA piece codes as a GRN would; receipts over ESSA''s 500-piece limit, or in part-units, keep the legacy tag only.',
       jsonb_build_object('pack_tags', count(*), 'pieces', sum(qty),
                          'codes_minted', (select coalesce(sum(q - 1), 0) from pack),
                          'tags_not_serialised', count(*) filter (where not exists (
                              select 1 from receipt_ok r where r.product_id = s.product_id and r.purchase_id = s.purchase_id)))
from stock_piece s where qty > 1;

-- ---------------------------------------------------------------------------
--  Ledger rows, gathered first so they can be written in time order
--  (ESSA replays the ledger by id — stock_locations.replay).
-- ---------------------------------------------------------------------------
create temp table mv (product_id int, qty_delta numeric, kind text, ref_type text, ref_id int,
                      rate numeric, note text, at timestamp, ord int);

-- (1) GRN inwards: what each posted GRN put on the floor, per stock item
insert into mv
select p.product_id, sum(p.qty), 'inward', 'purchase', p.purchase_id,
       round(sum(p.qty * coalesce(p.cost, 0)) / nullif(sum(p.qty), 0), 4), null,
       pu.posted_at, 1
from stock_piece p join purchases pu on pu.id = p.purchase_id
group by p.product_id, p.purchase_id, pu.posted_at;

-- ---------------------------------------------------------------------------
--  Supplier returns (debit notes)
-- ---------------------------------------------------------------------------
create table migration.map_purchase_return (old_id bigint primary key, new_id int not null);

create temp table ret as
select migration.int(r.id) id, migration.txt(r.returnno) return_no, migration.iso(r.returndate) return_date,
       migration.ref(r.suppliercompanyid) supplier_id, migration.txt(r.remarks) reason,
       coalesce(migration.num(r.amount), 0) amount, coalesce(migration.num(r.tax), 0) tax,
       coalesce(migration.num(r.total), 0) total, coalesce(migration.bool(r.isactive), true) active,
       migration.utc(r.createdon) created, migration.utc(r.returndate) returned_on
from legacy.lrinvoicereturn r;

create temp table ret_line as
select migration.int(i.id) id, migration.int(i.lrinvoicereturnid) return_id,
       migration.ref(i.stockid) piece_id, migration.ref(i.lrinvoiceitemsid) grn_line_id,
       coalesce(migration.num(i.qty), 0) qty, migration.num(i.rate) rate,
       coalesce(migration.num(i.amount), 0) amount, migration.txt(i.barcode) barcode,
       coalesce(migration.bool(i.isactive), true) active
from legacy.lrinvoicereturnitems i;
analyze ret_line;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoicereturn', id::text, 'excluded', 'Deleted in the legacy system (isactive = false); not migrated.',
       jsonb_build_object('return_no', return_no, 'total', total)
from ret where not active;

insert into purchase_returns (code, supplier_id, purchase_id, invoice_number, date, reason,
                              taxable_total, tax_total, total, status, posted_at, created_at)
select 'PR-' || migration.pad(migration.int(r.return_no), 5),
       (select new_id from migration.map_supplier where old_id = r.supplier_id),
       case when count(distinct mg.new_id) = 1 then min(mg.new_id) end,
       string_agg(distinct pu.invoice_number, ', '),
       r.return_date, r.reason, r.amount, r.tax, r.total, 'posted',
       coalesce(r.returned_on, r.created, now()), coalesce(r.created, now())
from ret r
left join ret_line rl on rl.return_id = r.id and rl.active
left join migration.grn_line g on g.id = rl.grn_line_id
left join migration.map_grn mg on mg.old_id = g.invoice_id
left join purchases pu on pu.id = mg.new_id
where r.active
group by r.id, r.return_no, r.supplier_id, r.return_date, r.reason, r.amount, r.tax, r.total,
         r.returned_on, r.created
order by r.id;

insert into migration.map_purchase_return
select r.id, x.id from ret r
join purchase_returns x on x.code = 'PR-' || migration.pad(migration.int(r.return_no), 5)
where r.active;

-- each returned line: the piece's stock item, else the GRN line's own product.
-- The breakdown row it returns from, looked up through a keyed copy: the
-- foreign-key indexes are only built in step 85.
create temp table split_of as
select line_id, product_id, min(id) id from purchase_line_splits group by 1, 2;
create index on split_of(line_id, product_id);

insert into purchase_return_lines (return_id, product_id, purchase_line_id, split_id, shortage_id,
                                   barcode, description, hsn, uom, qty, rate, amount)
select mr.new_id, coalesce(p.product_id, pl.product_id), ml.new_id,
       so.id,
       null, coalesce(rl.barcode, p.barcode), coalesce(pr.description, pl.description),
       coalesce(pr.hsn, pl.hsn), 'PCS', rl.qty, rl.rate, rl.amount
from ret_line rl
join migration.map_purchase_return mr on mr.old_id = rl.return_id
left join migration.piece p on p.id = rl.piece_id
left join migration.map_grn_line ml on ml.old_id = rl.grn_line_id
left join purchase_lines pl on pl.id = ml.new_id
left join products pr on pr.id = coalesce(p.product_id, pl.product_id)
left join split_of so on so.line_id = ml.new_id and so.product_id = coalesce(p.product_id, pl.product_id)
where rl.active
order by mr.new_id, rl.id;

-- A debit note in ESSA names ONE reference invoice (PurchaseReturn.purchase_id),
-- and the returns screen finds a note through it. A legacy return drawn from
-- several invoices references the one it took the most value from; each of
-- its lines still points at the exact GRN line it returns, and the header
-- lists every invoice number.
update purchase_returns r set purchase_id = x.purchase_id
from (select distinct on (l.return_id) l.return_id, pl.purchase_id
      from purchase_return_lines l join purchase_lines pl on pl.id = l.purchase_line_id
      group by l.return_id, pl.purchase_id
      order by l.return_id, sum(l.amount) desc, pl.purchase_id) x
where x.return_id = r.id and r.purchase_id is null
  and r.id in (select new_id from migration.map_purchase_return);

-- (2) the stock each posted return took back out of the warehouse
insert into mv
select l.product_id, -sum(l.qty)::numeric, 'return', 'purchase_return', l.return_id,
       round((sum(l.qty * coalesce(l.rate, 0)) / nullif(sum(l.qty), 0))::numeric, 4), null, r.posted_at, 2
from purchase_return_lines l join purchase_returns r on r.id = l.return_id
where l.product_id is not null and l.return_id in (select new_id from migration.map_purchase_return)
group by l.product_id, l.return_id, r.posted_at;

-- ---------------------------------------------------------------------------
--  Dispatches (legacy transfer notes from the warehouse → ESSA stock_outwards
--  to a store). Stock leaves the warehouse ledger at post; the store's side is
--  the shop's (step 70), exactly as ESSA splits it.
-- ---------------------------------------------------------------------------
create table migration.map_outward (old_id bigint primary key, new_id int not null);

create temp table tn as
select migration.int(b.id) id, btrim(b.code) code, migration.int(b.fromlocationid) from_loc,
       migration.int(b.tolocationid) to_loc, b.status, coalesce(migration.bool(b.isactive), true) active,
       migration.utc(b.packedon) packed_on, migration.utc(b.receivedon) received_on,
       migration.ref(b.packedby) packed_by, migration.ref(b.receivedby) received_by,
       migration.utc(b.createdon) created, b.items
from legacy.bundles b;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bundles', id::text, 'excluded', 'Transfer note deleted in the legacy system (isactive = false); not migrated.',
       jsonb_build_object('code', code) from tn where not active and from_loc = (select old_id from migration.map_location where kind = 'warehouse' limit 1);

create temp table tn_wh as
select tn.*, ml.new_id store_id, ml.name store_name from tn
join migration.map_location ml on ml.old_id = tn.to_loc and ml.kind = 'store'
where tn.active and tn.from_loc = (select old_id from migration.map_location where kind = 'warehouse' limit 1);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bundles', id::text, 'review',
       'Legacy transfer-note status 9 has no label in the export; migrated by whether the destination recorded receiving it.',
       jsonb_build_object('code', code, 'received_on', received_on)
from tn_wh where status = '9';

create temp table emp_by_user as
select m.old_id, coalesce(e.name, m.username) name
from migration.map_user m
left join legacy.users u on migration.int(u.id) = m.old_id
left join migration.map_employee e on e.old_id = migration.int(u.employeeid);

insert into stock_outwards (code, date, from_company, from_location, from_warehouse_id, to_destination,
                            to_warehouse_id, to_store_id, packed_by, received_by, received_date,
                            received_at, status, posted_at, created_at)
select t.code, to_char(coalesce(t.packed_on, t.created), 'YYYY-MM-DD'),
       (select legal_name from businesses where is_default limit 1), 'WAREHOUSE', (select id from wh),
       t.store_name, null, t.store_id,
       (select name from emp_by_user where old_id = t.packed_by),
       (select name from emp_by_user where old_id = t.received_by),
       to_char(t.received_on, 'YYYY-MM-DD'), t.received_on,
       case when t.received_on is not null then 'received' else 'posted' end,
       coalesce(t.packed_on, t.created, now()), coalesce(t.created, now())
from tn_wh t order by t.id;

insert into migration.map_outward
select a.id, b.id from (select id, row_number() over (order by id) rn from tn_wh) a
join (select id, row_number() over (order by id) rn from stock_outwards
      where id > coalesce((select max(new_id) from migration.map_outward), 0)) b on b.rn = a.rn;

do $$ begin
  if exists (select 1 from migration.map_outward m join stock_outwards o on o.id = m.new_id
             join tn_wh t on t.id = m.old_id where o.code is distinct from t.code) then
    raise exception 'outward map misaligned';
  end if;
end $$;

-- the note's pieces, from its JSON item list
create temp table tn_item as
select t.id note_id, (x->>'stockid')::bigint piece_id, coalesce((x->>'qty')::numeric, 0) qty,
       coalesce((x->>'iqty')::numeric, 0) iqty, x->>'barcode' barcode
from tn_wh t cross join lateral jsonb_array_elements(coalesce(t.items::jsonb, '[]'::jsonb)) x;
analyze tn_item;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bundles', null, 'unmatched', 'Transfer-note items whose piece is not a migrated stock piece; not carried on the dispatch.',
       jsonb_build_object('items', count(*), 'qty', sum(i.qty))
from tn_item i left join stock_piece p on p.id = i.piece_id where p.id is null having count(*) > 0;

insert into stock_outward_lines (outward_id, product_id, barcode, description, qty, accepted_qty, rate)
select m.new_id, p.product_id,
       case when count(*) = 1 then min(p.barcode) end, pr.description,
       sum(i.qty),
       case when bool_or(t.received_on is not null) then sum(i.iqty) end,
       round(sum(i.qty * coalesce(p.cost, 0)) / nullif(sum(i.qty), 0), 4)
from tn_item i
join stock_piece p on p.id = i.piece_id
join tn_wh t on t.id = i.note_id
join migration.map_outward m on m.old_id = i.note_id
join products pr on pr.id = p.product_id
group by m.new_id, p.product_id, pr.description
having sum(i.qty) > 0
order by m.new_id, p.product_id;

-- (3) what each dispatch took out of the warehouse
insert into mv
select l.product_id, -l.qty::numeric, 'outward', 'outward', l.outward_id, l.rate::numeric, null, o.posted_at, 3
from stock_outward_lines l join stock_outwards o on o.id = l.outward_id
where l.outward_id in (select new_id from migration.map_outward);

-- ---------------------------------------------------------------------------
--  (4) Goods sent BACK to the warehouse from a store on a legacy transfer note.
--  ESSA has no store → warehouse document (a store's stock is the till's), so
--  each note becomes an adjustment that names it — traceable, not invented.
-- ---------------------------------------------------------------------------
create temp table tn_back as
select tn.*, ml.name store_name from tn
join migration.map_location ml on ml.old_id = tn.from_loc and ml.kind = 'store'
where tn.active and tn.received_on is not null
  and tn.to_loc = (select old_id from migration.map_location where kind = 'warehouse' limit 1);

insert into mv
select p.product_id, sum(coalesce(nullif((x->>'iqty')::numeric, 0), (x->>'qty')::numeric, 0)),
       'adjustment', 'adjustment', null,
       round(sum(coalesce((x->>'qty')::numeric, 0) * coalesce(p.cost, 0))
             / nullif(sum(coalesce((x->>'qty')::numeric, 0)), 0), 4),
       'Returned to the warehouse from ' || t.store_name || ' on legacy transfer note ' || t.code,
       t.received_on, 4
from tn_back t cross join lateral jsonb_array_elements(coalesce(t.items::jsonb, '[]'::jsonb)) x
join stock_piece p on p.id = (x->>'stockid')::bigint
group by p.product_id, t.id, t.code, t.store_name, t.received_on
having sum(coalesce(nullif((x->>'iqty')::numeric, 0), (x->>'qty')::numeric, 0)) > 0;

-- ---------------------------------------------------------------------------
--  (5) Reconciliation to the legacy closing warehouse balance
-- ---------------------------------------------------------------------------
create temp table wh_target as
select product_id, sum(greatest(qty - transferred - returned, 0)) qty
from stock_piece where has_stock group by 1;

create temp table wh_running as
select product_id, sum(qty_delta) qty from mv group by 1;

analyze mv;

create table migration.reconciliation as
select coalesce(t.product_id, r.product_id) product_id,
       coalesce(r.qty, 0) documented, coalesce(t.qty, 0) legacy_closing,
       coalesce(t.qty, 0) - coalesce(r.qty, 0) adjustment
from wh_target t full join wh_running r on r.product_id = t.product_id;

insert into mv
select rc.product_id, rc.adjustment, 'adjustment', 'adjustment', null,
       coalesce(p.last_rate, 0),
       'Legacy migration: balance brought forward so the warehouse ends on the legacy '
       || 'closing balance (' || rc.legacy_closing || '). Covers movements the legacy '
       || 'system made before or outside its documents — the Aug-2022 go-live balances '
       || 'and transfers keyed without a transfer note.',
       -- dated at the START of the documented history, not at the cutoff: it is
       -- what was there before (or beside) the documents, and dating it at the
       -- cutoff would show it on the movement chart as one month's receipts
       (select min(at) - interval '1 second' from mv where ord < 5), 5
from migration.reconciliation rc join products p on p.id = rc.product_id
where abs(rc.adjustment) >= 0.001;

-- ---------------------------------------------------------------------------
--  Write the ledger in time order, with the running warehouse balance
-- ---------------------------------------------------------------------------
insert into stock_movements (product_id, warehouse_id, qty_delta, kind, ref_type, ref_id, rate,
                             balance_after, note, created_at)
select product_id, (select id from wh), round(qty_delta, 3), kind, ref_type, ref_id, rate,
       round(sum(qty_delta) over (partition by product_id order by at, ord, ref_id
                                  rows between unbounded preceding and current row), 3),
       note, at
from mv
order by at, ord, product_id, ref_id;

-- ---------------------------------------------------------------------------
--  Balances: replay the ledger exactly as stock_locations.replay does —
--  quantity is the running sum; the weighted-average cost moves only on an
--  inward (_VALUING_KINDS) and is kept, not zeroed, when stock runs out.
-- ---------------------------------------------------------------------------
create temp table replayed (product_id int primary key, qty numeric, avg_cost numeric);

do $$
declare
  r record; cur int := null; q numeric := 0; a numeric := 0; nq numeric;
begin
  for r in select product_id, qty_delta::numeric qty_delta, kind, coalesce(rate, 0)::numeric rate
           from stock_movements where warehouse_id = (select id from wh)
           order by product_id, id loop
    if cur is distinct from r.product_id then
      if cur is not null then insert into replayed values (cur, q, a); end if;
      cur := r.product_id; q := 0; a := 0;
    end if;
    if r.qty_delta > 0 and r.kind = 'inward' then
      nq := round(q + r.qty_delta, 3);
      a := case when nq > 0.001 then round((q * a + r.qty_delta * r.rate) / nq, 4) else r.rate end;
      q := nq;
    else
      q := round(q + r.qty_delta, 3);
    end if;
  end loop;
  if cur is not null then insert into replayed values (cur, q, a); end if;
end $$;

-- The QUANTITY above is the ledger's. The COST is taken from what the pieces
-- actually cost (legacy buying price): the legacy ledger records some outflows
-- before the receipts that fed them, and a weighted average replayed through a
-- running balance that dips below zero divides by a near-zero quantity (one
-- product came out at ₹12.5 crore a piece). Goods on hand are valued at their
-- own cost; a product with none on hand at the average of all it received.
create temp table piece_cost as
select product_id,
       coalesce(sum(greatest(qty - transferred - returned, 0) * cost)
                  filter (where has_stock and qty - transferred - returned > 0 and cost > 0)
                / nullif(sum(greatest(qty - transferred - returned, 0))
                  filter (where has_stock and qty - transferred - returned > 0 and cost > 0), 0),
                sum(qty * cost) filter (where cost > 0) / nullif(sum(qty) filter (where cost > 0), 0)) cost
from stock_piece group by 1;

update replayed r set avg_cost = round(pc.cost, 4)
from piece_cost pc where pc.product_id = r.product_id and pc.cost is not null;

insert into stock_balances (product_id, warehouse_id, qty, avg_cost, updated_at)
select product_id, (select id from wh), qty, avg_cost, (select at from cutoff) from replayed;

-- Product.stock_qty / avg_cost are the company roll-up (stock_locations.roll_up)
update products p set stock_qty = r.qty,
                      avg_cost = case when r.avg_cost > 0 then r.avg_cost else p.avg_cost end
from replayed r where r.product_id = p.id;

-- ---------------------------------------------------------------------------
--  Products ESSA will not count as stock: nothing that a posted GRN created
--  points at them (services/integrity: "orphan"). These are SKUs whose only
--  pieces came in on a GRN the legacy system itself deleted. Listed one by one,
--  because ESSA's Inventory Repair offers exactly these for removal.
-- ---------------------------------------------------------------------------
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'items', m.item_id || '|' || m.design, 'review',
       'No posted GRN behind this SKU in ESSA (its legacy GRN was deleted); ESSA''s Inventory Repair lists it as removable. Keep or remove by decision.',
       jsonb_build_object('sku', p.sku, 'description', p.description)
from products p join migration.map_product m on m.new_id = p.id
where p.id not in (select product_id from purchase_lines where product_id is not null
                   union select product_id from purchase_line_splits where product_id is not null
                   union select product_id from stock_movements where product_id is not null);
