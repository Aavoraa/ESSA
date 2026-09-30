-- 76 · Shop: what each store holds, and the records that explain it
--
-- In ESSA a store's stock belongs to the till (shop.location_stock, and the
-- shop-wide shop.products.stock_qty); it arrives by the shop taking in the
-- warehouse's dispatches (app/transfers.sync_transfers → TransferReceipt).
--
--  1. transfer_receipts — one per migrated dispatch line to a store, exactly the
--     row sync_transfers would write. Its unique wh_line_id is also what stops
--     the shop taking the same dispatch in again on its next start.
--  2. location_stock — each store's closing stock per product = the legacy
--     closing store stock (salablegoods held here), and products.stock_qty is
--     their sum, as the shop keeps it.
--  3. the shop's stock ledger (shop.stock_movements): transfer-ins, sales and
--     returns as the till writes them, plus one adjustment per product that
--     accounts for what the legacy system moved between stores (and before its
--     own ledger began) — so the ledger ends on the legacy closing figure.
--  4. bill_sequences — every floor's series continues after its last legacy bill.

create temp table cutoff as
select coalesce(max(migration.utc(transactedon)), now()::timestamp) at from legacy.stocktransaction;

-- ---- 1. transfer receipts --------------------------------------------------------
insert into shop.transfer_receipts (wh_line_id, wh_outward_id, code, location_id, product_id, qty,
                                    received_on, applied_at)
select l.id, o.id, left(o.code, 32), msl.location_id, sp.id,
       coalesce(l.accepted_qty, l.qty), left(coalesce(o.received_date, o.date), 16),
       coalesce(o.received_at, o.posted_at, o.created_at)
from stock_outward_lines l
join stock_outwards o on o.id = l.outward_id
join migration.map_outward mo on mo.new_id = o.id
join migration.map_shop_location msl on msl.store_id = o.to_store_id
join shop.products sp on sp.warehouse_id = l.product_id
where coalesce(l.accepted_qty, l.qty) > 0
order by o.id, l.id;

-- ---- 2. closing stock per store ---------------------------------------------------
create table migration.store_closing as
select msl.location_id, sp.id product_id, sum(s.held) qty
from migration.store_piece s
join migration.piece p on p.id = s.piece_id
join migration.map_location ml on ml.old_id = s.location_id and ml.kind = 'store'
join migration.map_shop_location msl on msl.store_id = ml.new_id
join shop.products sp on sp.warehouse_id = p.product_id
where s.held > 0 and p.product_id is not null
group by 1, 2;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'salablegoods', null, 'excluded', 'Stock held at a legacy place that is not a store (or whose piece has no product); not counted as store stock.',
       jsonb_build_object('pieces', count(*), 'qty', sum(s.held), 'locations', array_agg(distinct s.location_id))
from migration.store_piece s
left join migration.map_location ml on ml.old_id = s.location_id and ml.kind = 'store'
left join migration.piece p on p.id = s.piece_id
where s.held > 0 and (ml.new_id is null or p.product_id is null)
having count(*) > 0;

insert into shop.location_stock (location_id, product_id, qty)
select location_id, product_id, round(qty, 3) from migration.store_closing where qty <> 0;

-- Every migrated shop product holds exactly its stores' legacy stock — zero where
-- no store holds any. This deliberately overrides the figure the shop's own
-- sync opened the product with in step 65 (warehouse_items._apply copies the
-- WAREHOUSE's quantity in as the shop's "opening" stock): legacy warehouse
-- stock was never on a store's shelf, and leaving it there would let a till
-- sell goods that are standing in the warehouse.
update shop.products p set stock_qty = coalesce(x.qty, 0)
from shop.products p2
left join (select product_id, round(sum(qty), 3) qty from migration.store_closing group by 1) x
       on x.product_id = p2.id
where p2.id = p.id and p.warehouse_id is not null;

-- ---- 3. the shop's stock ledger ---------------------------------------------------
create temp table smv (product_id int, change numeric, reason text, reference text, at timestamp);

insert into smv
select r.product_id, r.qty, 'transfer-in', left(coalesce(r.code, 'transfer') || ' → ' || l.name, 64), r.applied_at
from shop.transfer_receipts r join shop.locations l on l.id = r.location_id;

insert into smv
select it.product_id, -it.quantity, 'sale',
       left(i.invoice_number || coalesce(' @ ' || l.name, ''), 64), i.invoice_date
from shop.invoice_items it join shop.invoices i on i.id = it.invoice_id
left join shop.locations l on l.id = i.location_id
where i.payment_status <> 'cancelled' and i.id in (select invoice_id from migration.map_bill);

insert into smv
select ci.product_id, ci.quantity, 'return', left(cn.number, 64), cn.created_at
from shop.credit_note_items ci join shop.credit_notes cn on cn.id = ci.credit_note_id
where cn.id in (select credit_note_id from migration.map_credit_note);

create table migration.store_reconciliation as
select coalesce(c.product_id, m.product_id) product_id, coalesce(m.qty, 0) documented,
       coalesce(c.qty, 0) legacy_closing, coalesce(c.qty, 0) - coalesce(m.qty, 0) adjustment
from (select product_id, sum(qty) qty from migration.store_closing group by 1) c
full join (select product_id, sum(qty) qty from (
             select product_id, change qty from smv
             -- the "opening" rows the shop's own sync wrote in step 65
             union all select product_id, change from shop.stock_movements) all_mv
           group by 1) m on m.product_id = c.product_id;

insert into smv
select product_id, adjustment, 'adjustment', 'Legacy migration: closing store stock', (select at from cutoff)
from migration.store_reconciliation where abs(adjustment) >= 0.001;

insert into shop.stock_movements (product_id, change, reason, reference, created_at)
select product_id, round(change, 3), reason, reference, at from smv order by at, product_id;

-- ---- 4. bill number series ---------------------------------------------------------
insert into shop.bill_sequences (prefix, fin_year, last_number, updated_at)
select bill_prefix, fin_year, max(bill_seq), now()
from shop.invoices
where bill_prefix is not null and fin_year is not null and bill_seq is not null
  and id in (select invoice_id from migration.map_bill)
group by 1, 2
on conflict (prefix, fin_year) do update set last_number = greatest(shop.bill_sequences.last_number, excluded.last_number);
