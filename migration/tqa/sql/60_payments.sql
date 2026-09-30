-- 60 · Supplier payments (legacy supplierpayment + supplierpaymentinvoice →
--      ESSA payments + payment_allocations)
--
-- A legacy payment settles one or more purchase invoices; an allocation row
-- flagged islrinvoicereturn is a debit note (supplier return) set against the
-- payment instead — ESSA's `debit_adjust`. Cancelled / deleted payments are
-- not carried (they paid nothing) and are listed in the report.

create temp table pay as
select migration.int(p.id) id, migration.txt(p.receiptno) receipt_no, migration.iso(p.paymentdate) paid_on,
       migration.ref(p.suppliercompanyid) supplier_id, coalesce(migration.num(p.paidamount), 0) paid,
       p.paymentmode mode_code, migration.txt(p.referencedetail) ref, migration.iso(p.instrumentdate) instrument_date,
       migration.txt(p.remarks) remarks, coalesce(migration.num(p.tdsamount), 0) tds,
       (select migration.txt(r.name) from legacy.referencelist r where r.type = 'BANK' and r.id = p.bankid) bank,
       migration.txt(p.paymentinfo)::jsonb info,
       coalesce(migration.bool(p.isactive), true) and not coalesce(migration.bool(p.hascancel), false) live,
       migration.txt(p.cancelreason) cancel_reason, migration.utc(p.createdon) created
from legacy.supplierpayment p;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'supplierpayment', id::text, 'excluded', 'Cancelled or deleted in the legacy system; paid nothing, not migrated.',
       jsonb_build_object('receipt_no', receipt_no, 'amount', paid, 'cancel_reason', cancel_reason)
from pay where not live;

-- The export labels none of the three mode codes. 0 (97% of payments, nearly
-- all carrying the supplier's bank account) is left at ESSA's own default,
-- NEFT; the two rare codes are kept visibly as what they are.
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'supplierpayment', null, 'review',
       'Legacy payment-mode codes are unlabelled. Code 0 was migrated as ESSA''s default mode (NEFT); codes 1 and 2 as "Legacy mode 1/2". Confirm or relabel.',
       jsonb_object_agg(coalesce(mode_code, 'blank'), n)
from (select mode_code, count(*) n from pay where live group by 1) x;

create table migration.map_payment (old_id bigint primary key, new_id int not null);

create temp table alloc as
select migration.int(a.supplierpaymentid) payment_id, migration.int(a.invoiceid) invoice_id,
       migration.txt(a.invoiceno) invoice_no, coalesce(migration.num(a.paidamount), 0) paid,
       coalesce(migration.num(a.discountamount), 0) discount,
       coalesce(migration.bool(a.islrinvoicereturn), false) is_return
from legacy.supplierpaymentinvoice a
where coalesce(migration.bool(a.isactive), true) and not coalesce(migration.bool(a.hascancel), false);

create temp table pay_base as select coalesce(max(id), 0) max_id from payments;

insert into payments (receipt_no, supplier_id, date, mode, bank, cheque_no, cheque_date, ref_no, remarks,
                      gross_amount, discount_total, tds_total, debit_adjust_total, paid_amount, created_at)
select p.receipt_no, (select new_id from migration.map_supplier where old_id = p.supplier_id), p.paid_on,
       case p.mode_code when '0' then 'NEFT' else 'Legacy mode ' || p.mode_code end,
       coalesce(p.bank, migration.txt(p.info ->> 'bankname')),
       case when p.mode_code <> '0' then p.ref end, p.instrument_date, p.ref,
       concat_ws(' · ', p.remarks,
                 case when migration.txt(p.info ->> 'bankacno') is not null
                      then 'to A/c ' || (p.info ->> 'bankacno') || ' ' || coalesce(p.info ->> 'bankifsccode', '') end),
       coalesce((select sum(a.paid + a.discount) from alloc a where a.payment_id = p.id and not a.is_return), p.paid),
       coalesce((select sum(a.discount) from alloc a where a.payment_id = p.id), 0),
       p.tds,
       -- legacy stores a debit note as a NEGATIVE paid amount; ESSA's debit_adjust is the positive deduction
       coalesce((select sum(abs(a.paid)) from alloc a where a.payment_id = p.id and a.is_return), 0),
       p.paid, coalesce(p.created, now())
from pay p where p.live order by p.id;

insert into migration.map_payment
select a.id, b.id from (select id, row_number() over (order by id) rn from pay where live) a
join (select id, row_number() over (order by id) rn from payments where id > (select max_id from pay_base)) b
  on b.rn = a.rn;

do $$ begin
  if exists (select 1 from migration.map_payment m join payments x on x.id = m.new_id
             join pay on pay.id = m.old_id where x.receipt_no is distinct from pay.receipt_no) then
    raise exception 'payment map misaligned';
  end if;
end $$;

insert into payment_allocations (payment_id, purchase_id, invoice_number, invoice_total, discount, tds,
                                 debit_adjust, settled)
select m.new_id,
       case when not a.is_return then mg.new_id end,
       coalesce(a.invoice_no, pu.invoice_number),
       coalesce(pu.grand_total, 0),
       a.discount, 0,
       case when a.is_return then abs(a.paid) else 0 end,
       case when a.is_return then abs(a.paid) else a.paid + a.discount end
from alloc a
join migration.map_payment m on m.old_id = a.payment_id
left join migration.map_grn mg on mg.old_id = a.invoice_id and not a.is_return
left join purchases pu on pu.id = mg.new_id
order by m.new_id;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'supplierpaymentinvoice', null, 'unmatched',
       'Payment allocations naming a purchase invoice that is not a migrated GRN (deleted in legacy); kept on the payment by invoice number only.',
       jsonb_build_object('allocations', count(*), 'amount', sum(a.paid))
from alloc a join migration.map_payment m on m.old_id = a.payment_id
left join migration.map_grn mg on mg.old_id = a.invoice_id
where not a.is_return and mg.new_id is null having count(*) > 0;

-- GRNs from the legacy system's own go-live import (invoices dated 2012–2022)
-- carry no payment in the legacy data, and legacy's own "settled" flag is false
-- on every invoice, so ESSA shows them as outstanding payables — exactly what
-- the legacy data says. If they were paid before go-live, that is an opening
-- settlement for the business to record; none is invented here.
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'lrinvoice', null, 'review',
       'GRNs from the Aug-2022 go-live import with no payment recorded in legacy: shown in ESSA as outstanding payables. If they were settled before go-live, record an opening settlement.',
       jsonb_build_object('grns', count(*), 'value', round(sum(pu.grand_total)::numeric, 2))
from purchases pu join migration.map_grn mg on mg.new_id = pu.id
where pu.created_at < '2022-09-01'
  and not exists (select 1 from payment_allocations a where a.purchase_id = pu.id);
