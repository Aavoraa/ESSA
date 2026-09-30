-- 72 · Shop: sales bills → invoices, invoice_items, invoice_payments
--
-- Which legacy bills are SALES: everything not numbered on a return series
-- (RET…) and not flagged as a return. That is the retail bills (billtype 0)
-- and the sales that were later cancelled (billtype 9, iscancel) — the latter
-- migrate as cancelled invoices, which is how the shop records a cancellation
-- (the row stays, payment_status = 'cancelled'; app/cancellation.py).
--
-- The legacy system recorded a cancellation a second way too, as a reversal
-- document on the RET series (isreturn = false). The shop has no such document
-- — the cancelled invoice IS the record — so those reversals are not carried;
-- they are counted in the report. Returns proper become credit notes (step 74).
--
-- Amounts: a legacy line price is already net of MRP discount and coupon, so a
-- line carries that net taxable price and the legacy tax exactly; the
-- invoice total is exactly what the customer paid. `discount` holds only the
-- bill's round-off, so the shop's own identity holds on every migrated bill:
--     total = subtotal − discount + cgst + sgst + igst

create temp table bill_t as
select migration.int(b.id) id, btrim(b.billno) billno, migration.utc(b.billdate) billed_at,
       migration.ref(b.customerid) customer_id, migration.ref(b.createdby) created_by,
       migration.int(b.locationid) location_id, migration.ref(b.floorid) floor_id,
       migration.ref(b.counterid) counter_id, coalesce(migration.num(b.receivable), 0) receivable,
       coalesce(migration.bool(b.iscancel), false) cancelled, coalesce(migration.bool(b.isreturn), false) is_return,
       coalesce(migration.bool(b.isinterstatesale), false) interstate, b.billtype,
       migration.utc(b.modifiedon) modified, btrim(b.returnbillno) return_of,
       substring(btrim(b.billno) from '^([A-Z]+)[0-9]{2}[-/][0-9]+$') prefix,
       substring(btrim(b.billno) from '^[A-Z]+([0-9]{2})[-/][0-9]+$') fy,
       migration.int(substring(btrim(b.billno) from '^[A-Z]+[0-9]{2}[-/]([0-9]+)$')) seq
from legacy.bill b;
create index on bill_t(id);

create table migration.bill_class as
select id, case when billno ~ '^RET' and not is_return then 'reversal'
                when is_return and cancelled then 'cancelled_return'
                when is_return then 'return'
                else 'sale' end cls
from bill_t;
create index on migration.bill_class(id);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bill', null, 'excluded',
       case c.cls when 'reversal' then 'Cancellation-reversal documents (RET series, not returns): the shop records a cancellation on the invoice itself, which is migrated as cancelled.'
                  else 'Returns that were cancelled in the legacy system: they returned nothing.' end,
       jsonb_build_object('bills', count(*), 'amount', sum(b.receivable))
from bill_t b join migration.bill_class c on c.id = b.id
where c.cls in ('reversal', 'cancelled_return') group by c.cls;

-- lines, typed once (billitems.stockid → the piece → its ESSA product → the shop product)
create table migration.bill_line as
select migration.int(i.id) id, migration.int(i.billid) bill_id, migration.int(i.stockid) piece_id,
       coalesce(migration.num(i.qty), 0) qty, coalesce(migration.num(i.rate), 0) rate,
       coalesce(migration.num(i.tax), 0) tax, coalesce(migration.num(i.taxpercentage), 0) tax_pct,
       coalesce(migration.num(i.salerate), 0) mrp, coalesce(migration.num(i.receivable), 0) receivable,
       migration.ref(i.salesmanid) salesman_id
from legacy.billitems i;
create index on migration.bill_line(bill_id);
create index on migration.bill_line(piece_id);

alter table migration.bill_line add column shop_product_id int;
update migration.bill_line l set shop_product_id = sp.id
from migration.piece p join shop.products sp on sp.warehouse_id = p.product_id
where p.id = l.piece_id;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'billitems', null, 'unmatched', 'Bill lines whose piece has no migrated product; the line cannot be carried (the shop requires a product per line).',
       jsonb_build_object('lines', count(*), 'amount', sum(receivable))
from migration.bill_line where shop_product_id is null having count(*) > 0;

-- the places, by their ESSA ids
create temp table place as
select ml.old_id legacy_loc, msl.location_id, msl.company_id
from migration.map_location ml join migration.map_shop_location msl on msl.store_id = ml.new_id
where ml.kind = 'store';

create temp table till as
select mc.old_id legacy_counter, c.id counter_id
from migration.map_counter mc join shop.counters c on c.wh_id = mc.new_id;

create temp table storey as
select mf.old_id legacy_floor, f.id floor_id
from migration.map_floor mf join shop.floors f on f.wh_id = mf.new_id;

create temp table unknown_user as select shop_user_id id from migration.map_user where old_id = 0;

-- who served: the legacy salesman is an employee; a shop user exists for them
-- only when that employee also had a login
create temp table staff_of as
select distinct on (l.bill_id) l.bill_id, m.shop_user_id
from migration.bill_line l
join legacy.users u on migration.int(u.employeeid) = l.salesman_id
join migration.map_user m on m.old_id = migration.int(u.id)
where l.salesman_id is not null
order by l.bill_id, m.shop_user_id;
create index on staff_of(bill_id);

create temp table sale_sum as
select l.bill_id, round(sum(round(l.rate * l.qty, 2)), 2) subtotal, round(sum(l.tax), 2) tax,
       round(sum(l.mrp * l.qty), 2) mrp_value
from migration.bill_line l join migration.bill_class c on c.id = l.bill_id
where c.cls = 'sale' and l.shop_product_id is not null
group by l.bill_id;
create index on sale_sum(bill_id);

-- ---- invoices ---------------------------------------------------------------
create table migration.map_bill (old_id bigint primary key, invoice_id int not null);

insert into shop.invoices (invoice_number, customer_id, cashier_id, staff_id, invoice_date,
                           subtotal, discount, cgst, sgst, igst, total, loyalty_earned, loyalty_redeemed,
                           payment_method, company_id, location_id, counter_id, floor_id, fin_year,
                           bill_prefix, bill_seq, payment_status, is_interstate, notes,
                           cancelled_at, cancelled_by_id, cancel_reason, coupon_discount)
select b.billno, mc.new_id,
       coalesce(mu.shop_user_id, (select id from unknown_user)), st.shop_user_id,
       b.billed_at,
       coalesce(s.subtotal, 0),
       round(coalesce(s.subtotal, 0) + coalesce(s.tax, 0) - b.receivable, 2),
       case when b.interstate then 0 else round(coalesce(s.tax, 0) / 2, 2) end,
       case when b.interstate then 0 else coalesce(s.tax, 0) - round(coalesce(s.tax, 0) / 2, 2) end,
       case when b.interstate then coalesce(s.tax, 0) else 0 end,
       b.receivable, 0, 0,
       'cash',                                -- restated from the tenders below
       pl.company_id, pl.location_id, t.counter_id, sf.floor_id,
       b.fy, b.prefix, b.seq,
       case when b.cancelled then 'cancelled' else 'paid' end,
       b.interstate,
       left('Legacy bill. MRP value ' || coalesce(s.mrp_value, 0)::text || ', customer paid ' || b.receivable::text, 256),
       case when b.cancelled then coalesce(b.modified, b.billed_at) end,
       null,
       case when b.cancelled then 'Cancelled in the legacy system' end,
       0
from bill_t b
join migration.bill_class c on c.id = b.id and c.cls = 'sale'
left join sale_sum s on s.bill_id = b.id
left join migration.map_customer mc on mc.old_id = b.customer_id
left join migration.map_user mu on mu.old_id = b.created_by and mu.shop_user_id is not null
left join staff_of st on st.bill_id = b.id
left join place pl on pl.legacy_loc = b.location_id
left join till t on t.legacy_counter = b.counter_id
left join storey sf on sf.legacy_floor = b.floor_id
order by b.billed_at, b.id;

insert into migration.map_bill
select b.id, i.id from bill_t b join migration.bill_class c on c.id = b.id and c.cls = 'sale'
join shop.invoices i on i.invoice_number = b.billno;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bill', null, 'review', 'Bills whose creating login is not a legacy user; kept, with the inactive LEGACY-UNKNOWN account as cashier.',
       jsonb_build_object('bills', count(*))
from bill_t b join migration.bill_class c on c.id = b.id and c.cls = 'sale'
where not exists (select 1 from migration.map_user m where m.old_id = b.created_by and m.shop_user_id is not null)
having count(*) > 0;

-- ---- invoice lines -------------------------------------------------------------
create temp table item_base as select coalesce(max(id), 0) max_id from shop.invoice_items;

insert into shop.invoice_items (invoice_id, product_id, quantity, unit_price, gst_rate, line_total,
                                tax_amount, promo_qty, promo_value)
select mb.invoice_id, l.shop_product_id, l.qty, l.rate, l.tax_pct, round(l.rate * l.qty, 2), round(l.tax, 2), 0, 0
from migration.bill_line l join migration.map_bill mb on mb.old_id = l.bill_id
where l.shop_product_id is not null
order by mb.invoice_id, l.id;

-- which invoice line each legacy bill line became (returns point at these)
create table migration.map_bill_line (old_id bigint primary key, invoice_item_id int not null);
insert into migration.map_bill_line
select a.id, b.id from
  (select l.id, row_number() over (order by mb.invoice_id, l.id) rn
   from migration.bill_line l join migration.map_bill mb on mb.old_id = l.bill_id
   where l.shop_product_id is not null) a
join (select id, row_number() over (order by id) rn from shop.invoice_items
      where id > (select max_id from item_base)) b on b.rn = a.rn;

do $$ begin
  if exists (select 1 from migration.map_bill_line m join shop.invoice_items i on i.id = m.invoice_item_id
             join migration.bill_line l on l.id = m.old_id join migration.map_bill mb on mb.old_id = l.bill_id
             where i.invoice_id <> mb.invoice_id or i.product_id <> l.shop_product_id) then
    raise exception 'invoice line map misaligned';
  end if;
end $$;

-- ---- tenders ---------------------------------------------------------------------
-- A legacy settlement pays one visit's bills together (a customer who shopped on
-- two floors pays once). Each bill's share of the settlement's cash / card / UPI
-- is in proportion to what was settled against that bill, and the largest tender
-- absorbs the paisa so every invoice's tenders sum exactly to its total.
create temp table settle as
select migration.int(bs.billid) bill_id, migration.int(bs.billsettlementmasterid) master_id,
       coalesce(migration.num(bs.paid), 0) paid
from legacy.billsettlement bs where coalesce(migration.bool(bs.isactive), true);
create index on settle(master_id);

create temp table master_t as
select migration.int(m.id) id, coalesce(migration.num(m.paid_cash), 0) cash,
       coalesce(migration.num(m.paid_card), 0) card, coalesce(migration.num(m.paid_upi), 0) upi,
       migration.utc(m.settlementon) settled_at
from legacy.billsettlementmaster m where coalesce(migration.bool(m.isactive), true);
create index on master_t(id);

create temp table share as
select s.bill_id, m.cash, m.card, m.upi, m.settled_at,
       s.paid / nullif(sum(s.paid) over (partition by s.master_id), 0) frac
from settle s join master_t m on m.id = s.master_id;

create temp table tender as
select mb.invoice_id, x.method, round(sum(x.amount * sh.frac), 2) amount, min(sh.settled_at) at
from share sh
join migration.map_bill mb on mb.old_id = sh.bill_id
cross join lateral (values ('cash', sh.cash), ('card', sh.card), ('upi', sh.upi)) x(method, amount)
where x.amount <> 0 and sh.frac is not null
group by mb.invoice_id, x.method;

-- the paisa, onto the largest tender
with t as (
  select tn.invoice_id, tn.method,
         row_number() over (partition by tn.invoice_id order by abs(tn.amount) desc, tn.method) rk,
         i.total - sum(tn.amount) over (partition by tn.invoice_id) gap
  from tender tn join shop.invoices i on i.id = tn.invoice_id)
update tender tn set amount = tn.amount + t.gap
from t where t.invoice_id = tn.invoice_id and t.method = tn.method and t.rk = 1 and abs(t.gap) < 1;

insert into shop.invoice_payments (invoice_id, method, amount, tendered, reference, created_at)
select invoice_id, method, amount, amount, null, at from tender where amount <> 0
order by invoice_id, method;

update shop.invoices i set payment_method = x.pm
from (select invoice_id, case when count(*) > 1 then 'mixed' else min(method) end pm
      from tender where amount <> 0 group by invoice_id) x
where x.invoice_id = i.id;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'billsettlement', null, 'review',
       'Live sales with no legacy settlement row: the tender is unknown, so the invoice shows its default method (cash).',
       jsonb_build_object('bills', count(*), 'amount', sum(i.total))
from shop.invoices i join migration.map_bill mb on mb.invoice_id = i.id
where i.payment_status <> 'cancelled' and not exists (select 1 from shop.invoice_payments p where p.invoice_id = i.id)
having count(*) > 0;

-- the customer's lifetime spend, as the till keeps it (Customer.total_spent)
update shop.customers c set total_spent = x.spent
from (select customer_id, sum(total) spent from shop.invoices
      where customer_id is not null and payment_status <> 'cancelled' group by 1) x
where x.customer_id = c.id;

-- bills where the legacy lines do not add up to what the customer paid
-- (beyond ordinary round-off) and bills whose settlement differs from the bill
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bill', mb.old_id::text, 'review',
       'Legacy lines do not add up to the amount paid (difference over ₹1, carried as the invoice''s discount so its total stays what the customer paid).',
       jsonb_build_object('invoice', i.invoice_number, 'lines_plus_tax', i.subtotal + i.cgst + i.sgst + i.igst,
                          'paid', i.total)
from shop.invoices i join migration.map_bill mb on mb.invoice_id = i.id
where abs(i.discount) > 1;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bill', mb.old_id::text, 'review',
       'Legacy settlement for this bill does not equal the bill amount; tenders migrated as recorded.',
       jsonb_build_object('invoice', i.invoice_number, 'bill_total', i.total, 'tendered', p.s)
from shop.invoices i join migration.map_bill mb on mb.invoice_id = i.id
join (select invoice_id, sum(amount) s from shop.invoice_payments group by 1) p on p.invoice_id = i.id
where i.payment_status = 'paid' and abs(i.total - p.s) > 0.01;
