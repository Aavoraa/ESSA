-- 20 · Masters: categories, product groups, attribute vocabularies, tax, brand,
--      suppliers, agents, transporters, employees.
--
-- ESSA keeps Supplier / Agent / Transport in their own tables and every other
-- master as a MasterRecord (services/master_defs.py) whose `data` holds the
-- master's fields by key, `code`/`name` lifted into columns exactly as
-- routers/master_data.create_record does it. The field keys below are the
-- ones master_defs declares; nothing else is written into `data`.

-- reference-list lookups (city, state, bank, designation, …) by legacy id
create temp table ref as
select migration.int(id) id, type, migration.txt(name) name, migration.txt(code) code
from legacy.referencelist;
create index on ref(id);

-- ---------------------------------------------------------------------------
--  Categories  (legacy `products` = product GROUP: MENS-SHIRT, LADIES-SAREE…)
-- ---------------------------------------------------------------------------
-- ESSA ships the same business's category master (686 rows). A legacy group is
-- matched to it by name; the specific section (MENS/LADIES/KIDS) wins over the
-- OVERALL sheet when a name is on both. Unmatched groups are ADDED to the
-- master under the default catalogue — they are real categories stock was
-- bought and sold under.
create table migration.map_category (
  old_id bigint primary key, name text not null, section text, hsn text,
  sales_tax_id bigint, purchase_tax_id bigint, created boolean not null);

insert into migration.map_category
select migration.int(p.id), c.name, c.section, migration.txt(p.hsncode),
       migration.ref(p.salestaxid), migration.ref(p.purchasetaxid), false
from legacy.products p
join lateral (select name, section from categories
              where upper(name) = upper(btrim(p.name))
              order by (section = 'OVERALL'), id limit 1) c on true;

insert into categories (catalogue_id, section, name, created_at)
select (select id from catalogues where is_default limit 1),
       case when upper(p.name) like 'MENS-%' then 'MENS'
            when upper(p.name) like 'LADIES-%' then 'LADIES'
            when upper(p.name) like 'KIDS-%' or upper(p.name) like 'BOYS-%'
                 or upper(p.name) like 'GIRLS-%' then 'KIDS'
            else 'OVERALL' end,
       upper(btrim(p.name)), coalesce(migration.utc(p.createdon), now())
from legacy.products p
where migration.int(p.id) not in (select old_id from migration.map_category)
  and migration.txt(p.name) is not null;

insert into migration.map_category
select migration.int(p.id), c.name, c.section, migration.txt(p.hsncode),
       migration.ref(p.salestaxid), migration.ref(p.purchasetaxid), true
from legacy.products p join categories c on c.name = upper(btrim(p.name))
where migration.int(p.id) not in (select old_id from migration.map_category);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'products', p.id, 'review', 'Legacy product group not in ESSA''s category master; added to it.',
       jsonb_build_object('name', p.name, 'section', m.section)
from legacy.products p join migration.map_category m on m.old_id = migration.int(p.id) where m.created;

-- ---------------------------------------------------------------------------
--  Tax master
-- ---------------------------------------------------------------------------
create table migration.map_tax (old_id bigint primary key, new_id int not null, name text, rate numeric);

insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'tax', btrim(t.code), btrim(t.name),
       jsonb_build_object(
         'code', btrim(t.code), 'name', btrim(t.name),
         'tax_charges', case when migration.num(t.taxpercentage) in (0, 5, 12, 18, 28)
                             then 'GST ' || migration.num(t.taxpercentage)::int || '%' end,
         'rate', migration.num(t.taxpercentage),
         'sales_tax', coalesce(migration.bool(t.issalestax), true),
         'purchase_tax', coalesce(migration.bool(t.ispurchasetax), true),
         'disable', coalesce(migration.bool(t.disable), false),
         'active', coalesce(migration.bool(t.isactive), true))::json,
       '{}'::json, '{}'::json, coalesce(migration.bool(t.isactive), true),
       coalesce(migration.utc(t.createdon), now()), coalesce(migration.utc(t.modifiedon), now())
from legacy.tax t
-- two legacy rows share the code "GST 0%": the later one is kept apart by id
where not exists (select 1 from legacy.tax t2 where btrim(t2.code) = btrim(t.code)
                  and migration.int(t2.id) < migration.int(t.id))
order by migration.int(t.id);

insert into migration.map_tax
select migration.int(t.id), r.id, r.name, migration.num(t.taxpercentage)
from legacy.tax t join master_records r on r.master = 'tax' and r.code = btrim(t.code);

insert into migration.duplicates(entity, match_key, old_ids, new_ids, resolution)
select 'tax', 'code=' || btrim(code), array_agg(id order by migration.int(id)),
       array_agg(distinct (select new_id from migration.map_tax where old_id = migration.int(t.id))), 'merged'
from legacy.tax t group by btrim(code) having count(*) > 1;

-- ---------------------------------------------------------------------------
--  Product (group) master — the legacy `products` rows themselves
-- ---------------------------------------------------------------------------
insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'product', btrim(p.code), btrim(p.name),
       jsonb_strip_nulls(jsonb_build_object(
         'code', btrim(p.code), 'name', btrim(p.name), 'type', 'Textile',
         'hsn', migration.txt(p.hsncode), 'section', m.section,
         'sales_tax', (select name from migration.map_tax where old_id = migration.ref(p.salestaxid)),
         'purchase_tax', (select name from migration.map_tax where old_id = migration.ref(p.purchasetaxid)),
         'margin_min', migration.num(p.minmargin), 'margin_max', migration.num(p.maxmargin),
         'discount_value', coalesce(migration.num(p.discountvalue), 0),
         'stock_holding_days', nullif(migration.num(p.stockholdingperiod), 0),
         'uom', 'PCS', 'serialise', true, 'match_supplier_code', true,
         'active', coalesce(migration.bool(p.isactive), true)))::json,
       '{}'::json, '{}'::json, coalesce(migration.bool(p.isactive), true),
       coalesce(migration.utc(p.createdon), now()), coalesce(migration.utc(p.modifiedon), now())
from legacy.products p join migration.map_category m on m.old_id = migration.int(p.id)
where not exists (select 1 from legacy.products p2 where btrim(p2.code) = btrim(p.code)
                  and migration.int(p2.id) < migration.int(p.id))
order by migration.int(p.id);

insert into migration.duplicates(entity, match_key, old_ids, resolution)
select 'products', 'code=' || btrim(code), array_agg(id order by migration.int(id)),
       'kept_separate (both map to their own category; master record keeps the first code)'
from legacy.products group by btrim(code) having count(*) > 1;

-- ---------------------------------------------------------------------------
--  Brand master
-- ---------------------------------------------------------------------------
create table migration.map_brand (old_id bigint primary key, new_id int, name text not null);

insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'brand', btrim(b.code), btrim(b.name),
       jsonb_strip_nulls(jsonb_build_object(
         'code', btrim(b.code), 'name', btrim(b.name),
         'printing_name', coalesce(migration.txt(b.printingname), btrim(b.name)),
         'margin_min', migration.num(b.minmargin), 'margin_max', migration.num(b.maxmargin),
         'discount_value', coalesce(migration.num(b.discountvalue), 0),
         'active', coalesce(migration.bool(b.isactive), true)))::json,
       '{}'::json, '{}'::json, coalesce(migration.bool(b.isactive), true),
       coalesce(migration.utc(b.createdon), now()), coalesce(migration.utc(b.modifiedon), now())
from legacy.brand b
where migration.txt(b.name) is not null
  and not exists (select 1 from legacy.brand b2 where btrim(b2.code) = btrim(b.code)
                  and migration.int(b2.id) < migration.int(b.id))
order by migration.int(b.id);

insert into migration.map_brand
select migration.int(b.id), r.id, btrim(b.name) from legacy.brand b
left join master_records r on r.master = 'brand' and r.code = btrim(b.code)
                           and upper(r.name) = upper(btrim(b.name))
where migration.txt(b.name) is not null;

insert into migration.duplicates(entity, match_key, old_ids, resolution)
select 'brand', 'code=' || btrim(code), array_agg(id || ':' || name order by migration.int(id)),
       'kept_separate (products keep their own brand NAME; the master holds the first code)'
from legacy.brand group by btrim(code) having count(*) > 1;

insert into migration.duplicates(entity, match_key, old_ids, resolution)
select 'brand', 'name=' || upper(btrim(name)), array_agg(id || ':' || code order by migration.int(id)),
       'kept_separate'
from legacy.brand group by upper(btrim(name)) having count(*) > 1;

-- ---------------------------------------------------------------------------
--  Attribute vocabularies — every value the migrated stock actually carries
-- ---------------------------------------------------------------------------
create temp table item_attr as
select distinct a.attr, a.value from legacy.items i
cross join lateral (values
  ('brand', migration.txt(i.brandname)), ('size', migration.txt(i.sizename)),
  ('color', migration.txt(i.colourname)), ('pattern', migration.txt(i.patternname)),
  ('style', migration.txt(i.stylename)), ('material', migration.txt(i.materialname)),
  ('product_type', migration.txt(i.typename)),
  ('fit', (select name from ref where id = migration.ref(i.fitid) and type = 'FIT')),
  ('sleeve', (select name from ref where id = migration.ref(i.sleeveid) and type = 'SLEEVE'))
) a(attr, value)
where a.value is not null;

insert into attribute_options (catalogue_id, attr, value, sort, created_at)
select (select id from catalogues where is_default limit 1), ia.attr, ia.value, 1000, now()
from item_attr ia
where not exists (select 1 from attribute_options o
                  where o.catalogue_id = (select id from catalogues where is_default limit 1)
                    and o.attr = ia.attr and upper(o.value) = upper(ia.value))
on conflict do nothing;

-- ---------------------------------------------------------------------------
--  Suppliers  (legacy supplier + its suppliercompany row, 1:1 by id)
-- ---------------------------------------------------------------------------
-- One ESSA supplier per legacy supplier. Legacy suppliers are NOT merged even
-- when they share a GSTIN or a name: transactions reference each id, and two
-- names under one GSTIN is as often an agent's number keyed on both as it is
-- the same firm. Every such group is written to migration.duplicates for a
-- person to decide.
create table migration.map_supplier (old_id bigint primary key, new_id int not null);

create temp table sup as
select migration.int(s.id) id, btrim(s.name) name, migration.txt(s.code) code,
       upper(regexp_replace(coalesce(sc.tinanddate, ''), '\s', '', 'g')) gst_raw,
       migration.txt(sc.pan) pan, migration.txt(sc.address) address,
       (select name from ref where id = migration.ref(sc.cityid)) city,
       (select name from ref where id = migration.ref(sc.stateid)) state,
       migration.txt(sc.pincode) pincode,
       coalesce(migration.txt(s.contactnumber), migration.txt(sc.companycontactno)) phone,
       coalesce(migration.txt(s.emailid), migration.txt(sc.companyemailid)) email,
       migration.txt(s.contactperson) contact_person, migration.txt(sc.name) company_name,
       (select name from ref where id = migration.ref(sc.bankid)) bank_name,
       migration.txt(sc.bankbranch) branch, migration.txt(sc.bankaccountno) account_no,
       migration.txt(sc.bankaccountname) account_name, migration.txt(sc.bankifci) ifsc,
       migration.num(s.creditdays) credit_days, migration.num(s.minmargin) minm, migration.num(s.maxmargin) maxm,
       migration.num(s.discountpercentage) disc,
       coalesce(migration.bool(s.isactive), true) active, migration.utc(s.createdon) created,
       migration.utc(s.modifiedon) modified,
       (select t.name from legacy.transport t where migration.int(t.id) = migration.ref(sc.preferredtransportid)) transport
from legacy.supplier s left join legacy.suppliercompany sc on sc.supplierid = s.id;

alter table sup add column gstin text;
update sup set gstin = gst_raw where gst_raw ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$';

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'suppliercompany', id::text, 'review', 'GSTIN is not a valid 15-character GSTIN; left blank on the supplier.',
       jsonb_build_object('supplier', name, 'value', gst_raw)
from sup where gstin is null and gst_raw not in ('', '0');   -- "0" is how legacy wrote "none"

create temp table sup_base as select coalesce(max(id), 0) max_id from suppliers;

insert into suppliers (name, gstin, pan, state, state_code, address, phone, email, bank, aliases, created_at)
select s.name, s.gstin, coalesce(s.pan, substring(s.gstin from 3 for 10)), s.state,
       left(s.gstin, 2),
       concat_ws(E'\n', s.address, concat_ws(' - ', s.city, s.pincode)),
       s.phone, s.email,
       json_strip_nulls(json_build_object('name', s.bank_name, 'account_no', s.account_no,
                                          'ifsc', s.ifsc, 'branch', s.branch,
                                          'account_name', s.account_name)),
       case when s.company_name is not null and upper(s.company_name) <> upper(s.name)
            then json_build_array(s.company_name) else '[]'::json end,
       coalesce(s.created, now())
from sup s order by s.id;

-- The new rows' ids follow the insert order above (legacy id order), so the
-- n-th new supplier is the n-th legacy one. Names are not unique enough to
-- join on, which is why the order is used instead.
insert into migration.map_supplier
select s.id, x.new_id from (select id, row_number() over (order by id) rn from sup) s
join (select id new_id, row_number() over (order by id) rn from suppliers
      where id > (select max_id from sup_base)) x on x.rn = s.rn;

do $$ begin
  if (select count(*) from migration.map_supplier m join suppliers s on s.id = m.new_id
      join sup on sup.id = m.old_id where s.name <> sup.name) > 0 then
    raise exception 'supplier id map is misaligned';
  end if;
end $$;

-- the ERP's commercial terms, kept against the supplier's code (master_defs SUPPLIER)
insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'supplier', coalesce(s.code, 'SUP-' || s.id), s.name,
       jsonb_strip_nulls(jsonb_build_object(
         'code', coalesce(s.code, 'SUP-' || s.id), 'gstin', s.gstin, 'name', s.name,
         'company_reg_name', coalesce(s.company_name, s.name), 'contact_person', s.contact_person,
         'contact_no', s.phone, 'address', s.address, 'city', s.city, 'state', s.state,
         'pincode', s.pincode, 'email', s.email, 'transport', s.transport,
         'min_discount_pct', nullif(s.disc, 0), 'margin_min', nullif(s.minm, 0),
         'margin_max', nullif(s.maxm, 0), 'payment_credit_days', nullif(s.credit_days, 0),
         'active', s.active, 'legacy_supplier_id', s.id))::json,
       '{}'::json, '{}'::json, s.active, coalesce(s.created, now()), coalesce(s.modified, now())
from sup s
where not exists (select 1 from sup s2 where coalesce(s2.code, 'SUP-' || s2.id) = coalesce(s.code, 'SUP-' || s.id)
                  and s2.id < s.id)
order by s.id;

insert into migration.duplicates(entity, match_key, old_ids, new_ids, resolution)
select 'supplier', 'gstin=' || gstin, array_agg(id::text || ':' || name order by id),
       array_agg(m.new_id order by id), 'kept_separate'
from sup join migration.map_supplier m on m.old_id = sup.id
where gstin is not null group by gstin having count(*) > 1;

insert into migration.duplicates(entity, match_key, old_ids, new_ids, resolution)
select 'supplier', 'name=' || upper(name), array_agg(id::text || case when active then '' else ' (inactive)' end order by id),
       array_agg(m.new_id order by id), 'kept_separate'
from sup join migration.map_supplier m on m.old_id = sup.id
group by upper(name) having count(*) > 1;

insert into migration.duplicates(entity, match_key, old_ids, new_ids, resolution)
select 'supplier', 'code=' || code, array_agg(id::text || ':' || name order by id),
       array_agg(m.new_id order by id), 'kept_separate (the supplier master record holds the first code)'
from sup join migration.map_supplier m on m.old_id = sup.id
where code is not null group by code having count(*) > 1;

-- ---------------------------------------------------------------------------
--  Agents and transporters — ESSA's tables are unique by NAME, so legacy rows
--  with the same name are the same agent / transporter and map to one row.
-- ---------------------------------------------------------------------------
create table migration.map_agent (old_id bigint primary key, new_id int not null);
create table migration.map_transport (old_id bigint primary key, new_id int not null);

insert into agents (name, phone, created_at)
select distinct on (upper(btrim(a.name))) btrim(a.name), migration.txt(a.contactno),
       coalesce(migration.utc(a.createdon), now())
from legacy.agent a where migration.txt(a.name) is not null
order by upper(btrim(a.name)), migration.int(a.id);

insert into migration.map_agent
select migration.int(a.id), g.id from legacy.agent a join agents g on upper(g.name) = upper(btrim(a.name));

insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'agent', null, g.name,
       jsonb_strip_nulls(jsonb_build_object(
         'agent_type', 'Agent', 'name', g.name, 'contact_person', migration.txt(a.contactperson),
         'contact_no', migration.txt(a.contactno), 'email', migration.txt(a.emailid),
         'address', migration.txt(a.address), 'pincode', migration.txt(a.pincode),
         'pan', migration.txt(a.pan), 'gst', migration.txt(a.gst),
         'commission_amt', nullif(migration.num(a.commissionamount), 0),
         'commission_pct', nullif(migration.num(a.commissionpercentage), 0),
         'bank', (select name from ref where id = migration.ref(a.agentbankid)),
         'branch', migration.txt(a.agentbankbranch), 'bank_account_name', migration.txt(a.agentbankaccountname),
         'ifsc', migration.txt(a.agentbankifci), 'account_no', migration.txt(a.agentbankaccountno),
         'active', coalesce(migration.bool(a.isactive), true)))::json,
       '{}'::json, '{}'::json, coalesce(migration.bool(a.isactive), true),
       coalesce(migration.utc(a.createdon), now()), coalesce(migration.utc(a.modifiedon), now())
from (select distinct on (upper(btrim(name))) * from legacy.agent where migration.txt(name) is not null
      order by upper(btrim(name)), migration.int(id)) a
join agents g on upper(g.name) = upper(btrim(a.name));

insert into transports (name, phone, created_at)
select distinct on (upper(btrim(t.name))) btrim(t.name), migration.txt(t.contactno),
       coalesce(migration.utc(t.createdon), now())
from legacy.transport t where migration.txt(t.name) is not null
order by upper(btrim(t.name)), migration.int(t.id);

insert into migration.map_transport
select migration.int(t.id), x.id from legacy.transport t join transports x on upper(x.name) = upper(btrim(t.name));

insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'transport', null, x.name,
       jsonb_strip_nulls(jsonb_build_object(
         'name', x.name, 'contact_person', migration.txt(t.contactperson),
         'contact_no', migration.txt(t.contactno), 'email', migration.txt(t.emailid),
         'address', migration.txt(t.address),
         'city', (select name from ref where id = migration.ref(t.cityid)),
         'state', (select name from ref where id = migration.ref(t.stateid)),
         'pincode', migration.txt(t.pincode), 'pan', migration.txt(t.pan), 'gst', migration.txt(t.gst),
         'bank', (select name from ref where id = migration.ref(t.bankid)),
         'branch', migration.txt(t.bankbranch), 'bank_account_name', migration.txt(t.bankaccountname),
         'ifsc', migration.txt(t.bankifci), 'account_no', migration.txt(t.bankaccountno),
         'active', coalesce(migration.bool(t.isactive), true)))::json,
       '{}'::json, '{}'::json, coalesce(migration.bool(t.isactive), true),
       coalesce(migration.utc(t.createdon), now()), coalesce(migration.utc(t.modifiedon), now())
from (select distinct on (upper(btrim(name))) * from legacy.transport where migration.txt(name) is not null
      order by upper(btrim(name)), migration.int(id)) t
join transports x on upper(x.name) = upper(btrim(t.name));

insert into migration.duplicates(entity, match_key, old_ids, resolution)
select 'agent', 'name=' || upper(btrim(name)), array_agg(id order by migration.int(id)), 'merged (ESSA agents are unique by name)'
from legacy.agent group by upper(btrim(name)) having count(*) > 1;
insert into migration.duplicates(entity, match_key, old_ids, resolution)
select 'transport', 'name=' || upper(btrim(name)), array_agg(id order by migration.int(id)), 'merged (ESSA transports are unique by name)'
from legacy.transport group by upper(btrim(name)) having count(*) > 1;

-- ---------------------------------------------------------------------------
--  Employees
-- ---------------------------------------------------------------------------
create table migration.map_employee (old_id bigint primary key, new_id int not null, name text);

insert into master_records (master, code, name, data, grids, matrix, active, created_at, updated_at)
select 'employee', null, btrim(e.name),
       jsonb_strip_nulls(jsonb_build_object(
         'employee_code', coalesce(migration.txt(e.employeecode), 'EMP-' || e.id),
         'name', btrim(e.name), 'surname', migration.txt(e.surname),
         'contact_no', migration.txt(e.contactno), 'email', migration.txt(e.emailid),
         'date_of_birth', migration.iso(e.dateofbirth), 'date_of_joining', migration.iso(e.dateofjoining),
         'department', (select name from ref where id = migration.ref(e.departmentid)),
         'section', (select name from ref where id = migration.ref(e.sectionid)),
         'designation', (select name from ref where id = migration.ref(e.desingnationid)),
         'working_location', (select name from ref where id = migration.ref(e.locationid)),
         'floor', (select name from ref where id = migration.ref(e.floorid)),
         'allow_system', migration.bool(e.allowsystemaccess), 'username', migration.txt(e.username),
         'active', coalesce(migration.bool(e.isactive), true) and not coalesce(migration.bool(e.hasleft), false),
         'legacy_employee_id', migration.int(e.id)))::json,
       '{}'::json, '{}'::json,
       coalesce(migration.bool(e.isactive), true) and not coalesce(migration.bool(e.hasleft), false),
       coalesce(migration.utc(e.createdon), now()), coalesce(migration.utc(e.modifiedon), now())
from legacy.employee e
where migration.txt(e.name) is not null and upper(btrim(e.name)) <> 'UNSPECIFIED'
order by migration.int(e.id);

insert into migration.map_employee
select (r.data::jsonb ->> 'legacy_employee_id')::bigint, r.id, r.name
from master_records r where r.master = 'employee';

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'employee', e.id, 'excluded', 'Legacy placeholder employee ("UNSPECIFIED" / no name); not a person.',
       jsonb_build_object('name', e.name)
from legacy.employee e
where migration.txt(e.name) is null or upper(btrim(e.name)) = 'UNSPECIFIED';

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'employee', e.id, 'review', 'Date of joining is blank in the legacy record; ESSA''s Employee form requires it.',
       jsonb_build_object('name', e.name)
from legacy.employee e join migration.map_employee m on m.old_id = migration.int(e.id)
where migration.iso(e.dateofjoining) is null;
