-- 74 · Shop: customer returns → credit notes against the bill they came off
--
-- A legacy return bill (isreturn, not cancelled) names its source bill
-- (returnbillno). Each returned line is matched to the SAME PIECE on that source
-- bill — the barcode that was sold is the barcode that came back — which is the
-- exact invoice line the shop's CreditNoteItem must point at. A return whose
-- source bill or piece cannot be found is listed, not forced onto a guess.
--
-- refund_method, from the legacy settlement:
--   settled together with a sale in one settlement → 'exchange' (the value went
--     against the new bill; it is not kept as spendable store credit)
--   otherwise the tender its settlement paid back (cash / card / upi), and
--   where none is recorded, the shop's own default — cash.

create temp table ret_bill as
select migration.int(b.id) id, btrim(b.billno) billno, btrim(b.returnbillno) source_no,
       migration.utc(b.billdate) at, migration.ref(b.createdby) created_by,
       abs(coalesce(migration.num(b.receivable), 0)) total,
       coalesce(migration.bool(b.isinterstatesale), false) interstate,
       migration.ref(b.counterid) counter_id
from legacy.bill b join migration.bill_class c on c.id = migration.int(b.id) and c.cls = 'return';
create index on ret_bill(id);

create temp table src_invoice as
select r.id return_id, i.id invoice_id, mb.old_id source_bill_id
from ret_bill r
join shop.invoices i on i.invoice_number = r.source_no
join migration.map_bill mb on mb.invoice_id = i.id;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'bill', r.id::text, 'unmatched',
       'Return whose source bill is not a migrated sale; a credit note must name its invoice, so it is not carried.',
       jsonb_build_object('return_no', r.billno, 'source_no', r.source_no, 'amount', r.total)
from ret_bill r where not exists (select 1 from src_invoice s where s.return_id = r.id);

-- each returned line → the source bill's line for the same piece
create temp table ret_line as
select rl.id, rl.bill_id return_id, s.invoice_id, rl.piece_id, abs(rl.qty) qty, rl.rate, abs(rl.tax) tax,
       rl.tax_pct, rl.shop_product_id,
       (select mbl.invoice_item_id from migration.bill_line sl
          join migration.map_bill_line mbl on mbl.old_id = sl.id
         where sl.bill_id = s.source_bill_id and sl.piece_id = rl.piece_id
         order by sl.id limit 1) invoice_item_id
from migration.bill_line rl join src_invoice s on s.return_id = rl.bill_id;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'billitems', id::text, 'unmatched',
       'Returned piece is not on the source bill; the credit note line cannot name the invoice line it reverses, so it is not carried.',
       jsonb_build_object('return_bill', return_id, 'piece', piece_id)
from ret_line where invoice_item_id is null;

-- ---- refund method ------------------------------------------------------------
create temp table bs_t as
select migration.int(billid) bill_id, migration.int(billsettlementmasterid) master_id
from legacy.billsettlement;
create index on bs_t(bill_id);
create index on bs_t(master_id);

create temp table master_with_sale as
select distinct bs.master_id from bs_t bs
join migration.bill_class c on c.id = bs.bill_id and c.cls = 'sale';
create index on master_with_sale(master_id);

create temp table ret_settle as
select r.id return_id,
       bool_or(ms.master_id is not null) with_sale,
       sum(coalesce(migration.num(m.paid_cash), 0)) cash, sum(coalesce(migration.num(m.paid_card), 0)) card,
       sum(coalesce(migration.num(m.paid_upi), 0)) upi
from ret_bill r
join bs_t bs on bs.bill_id = r.id
join legacy.billsettlementmaster m on migration.int(m.id) = bs.master_id
left join master_with_sale ms on ms.master_id = bs.master_id
group by r.id;

-- ---- credit notes -----------------------------------------------------------------
create table migration.map_credit_note (old_id bigint primary key, credit_note_id int not null);

create temp table ret_sum as
select return_id, round(sum(round(rate * qty, 2)), 2) subtotal, round(sum(tax), 2) tax
from ret_line where invoice_item_id is not null group by 1;

insert into shop.credit_notes (number, invoice_id, staff_id, cashier_id, created_at, subtotal, discount,
                               cgst, sgst, igst, total, loyalty_reversed, promo_clawback, refund_method,
                               reason, counter_id)
select r.billno, s.invoice_id, null,
       coalesce(mu.shop_user_id, (select shop_user_id from migration.map_user where old_id = 0)),
       r.at, rs.subtotal,
       round(rs.subtotal + rs.tax - r.total, 2),
       case when r.interstate then 0 else round(rs.tax / 2, 2) end,
       case when r.interstate then 0 else rs.tax - round(rs.tax / 2, 2) end,
       case when r.interstate then rs.tax else 0 end,
       r.total, 0, 0,
       case when st.with_sale then 'exchange'
            when st.upi < 0 and st.upi <= least(st.cash, st.card) then 'upi'
            when st.card < 0 and st.card <= st.cash then 'card'
            else 'cash' end,
       'Legacy return ' || r.billno || ' against ' || r.source_no,
       (select c.id from migration.map_counter mc join shop.counters c on c.wh_id = mc.new_id
         where mc.old_id = r.counter_id)
from ret_bill r
join src_invoice s on s.return_id = r.id
join ret_sum rs on rs.return_id = r.id
left join ret_settle st on st.return_id = r.id
left join migration.map_user mu on mu.old_id = r.created_by and mu.shop_user_id is not null
order by r.at, r.id;

insert into migration.map_credit_note
select r.id, cn.id from ret_bill r join shop.credit_notes cn on cn.number = r.billno;

insert into shop.credit_note_items (credit_note_id, invoice_item_id, product_id, quantity, unit_price,
                                    gst_rate, line_total, tax_amount, condition)
select mc.credit_note_id, rl.invoice_item_id, rl.shop_product_id, rl.qty, rl.rate, rl.tax_pct,
       round(rl.rate * rl.qty, 2), round(rl.tax, 2), 'resellable'
from ret_line rl join migration.map_credit_note mc on mc.old_id = rl.return_id
where rl.invoice_item_id is not null
order by mc.credit_note_id, rl.id;
