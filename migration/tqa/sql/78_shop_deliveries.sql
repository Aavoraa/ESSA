-- 78 · Shop: what the customer carried out
--
-- The shop knows goods were collected only through its delivery records
-- (Invoice.delivered_qty = Σ delivery lines); with none, every bill reads
-- "not collected" and the delivery desk offers to hand over goods sold years ago.
--
-- The legacy system's own record says otherwise. It ran a delivery desk from
-- Dec-2023 (`delivery`: 119,579 handed over, 5 still pending); before that a
-- bill's goods left with the customer at the counter — there was no pending
-- state to be in. So:
--   * a bill with a legacy delivery handed over → a delivery by who / when legacy says
--   * a bill before the delivery desk, or never sent to it → collected at the bill
--   * a bill whose legacy delivery is still pending (status 0) → left not collected
-- Cancelled bills owe nothing and get no delivery; lines already returned are
-- delivered in full (the shop subtracts returns itself).

create temp table legacy_delivery as
select distinct on (migration.int(d.billid)) migration.int(d.billid) bill_id, d.status,
       migration.ref(coalesce(nullif(d.deliveredby, '0'), d.receivedby)) by_user,
       coalesce(migration.utc(d.receivedon), migration.utc(d.createdon)) at
from legacy.delivery d
where coalesce(migration.bool(d.isactive), true)
order by migration.int(d.billid), migration.int(d.id) desc;
create index on legacy_delivery(bill_id);
analyze legacy_delivery;

create temp table to_deliver as
select mb.old_id bill_id, i.id invoice_id, i.invoice_number, i.customer_id, i.cashier_id,
       i.company_id, i.location_id, i.counter_id, i.invoice_date,
       ld.status legacy_status, ld.by_user, ld.at legacy_at
from migration.map_bill mb
join shop.invoices i on i.id = mb.invoice_id
left join legacy_delivery ld on ld.bill_id = mb.old_id
where i.payment_status <> 'cancelled'
  and coalesce(ld.status, '6') <> '0';
analyze to_deliver;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'delivery', ld.bill_id::text, 'review',
       'Legacy delivery still pending for this bill; left "not collected" at the shop''s delivery desk.',
       jsonb_build_object('invoice', i.invoice_number)
from legacy_delivery ld join migration.map_bill mb on mb.old_id = ld.bill_id
join shop.invoices i on i.id = mb.invoice_id
where ld.status = '0';

create temp table del_base as select coalesce(max(id), 0) max_id from shop.deliveries;

insert into shop.deliveries (number, customer_id, staff_id, cashier_id, company_id, location_id,
                             counter_id, created_at, notes)
select 'LEG-' || left(t.invoice_number, 28), t.customer_id, null,
       coalesce((select shop_user_id from migration.map_user where old_id = t.by_user and shop_user_id is not null),
                t.cashier_id),
       t.company_id, t.location_id, t.counter_id,
       coalesce(t.legacy_at, t.invoice_date),
       case when t.legacy_status is not null then 'Legacy delivery desk: handed over'
            else 'Legacy bill: goods collected at the counter' end
from to_deliver t
order by coalesce(t.legacy_at, t.invoice_date), t.invoice_id;

create table migration.map_delivery (invoice_id int primary key, delivery_id int not null);
insert into migration.map_delivery
select t.invoice_id, d.id from to_deliver t
join shop.deliveries d on d.number = 'LEG-' || left(t.invoice_number, 28) and d.id > (select max_id from del_base);

insert into shop.delivery_bills (delivery_id, invoice_id)
select delivery_id, invoice_id from migration.map_delivery order by delivery_id;

insert into shop.delivery_lines (delivery_id, invoice_item_id, product_id, quantity, scanned)
select md.delivery_id, it.id, it.product_id, it.quantity, 0
from shop.invoice_items it
join migration.map_delivery md on md.invoice_id = it.invoice_id
where it.quantity > 0
order by md.delivery_id, it.id;
