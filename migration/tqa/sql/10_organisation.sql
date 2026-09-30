-- 10 · Businesses → Warehouses → Stores → Floors → POS terminals
--
-- Source: legacy.company (2 rows) and legacy.referencelist (LOCATION / FLOOR /
-- COUNTER). Which floor and till belong to which store is not stored on the
-- floor/counter rows themselves; it is read off the bills (bill.locationid,
-- floorid, counterid), which is where the legacy system actually used them.
-- The bill-number prefix of each floor (TG/TF/TS/TT…) is likewise read off the
-- bill numbers billed from it — it is what ESSA's Floor.prefix is for.

-- ---- businesses -----------------------------------------------------------
-- Legacy company 1 IS the entity ESSA was configured as (ESSA_COMPANY_NAME,
-- seeded as business ESSA): match on legal name, fill only blanks.
create table migration.map_company (old_id bigint primary key, new_id int not null);

insert into migration.map_company
select migration.int(c.id), b.id
from legacy.company c
join businesses b on upper(b.legal_name) = upper(btrim(c.name))
where migration.int(c.id) = 1;

update businesses b set
  email = coalesce(b.email, migration.txt(c.emailid)),
  address = coalesce(b.address, migration.txt(c.address)),
  pincode = coalesce(b.pincode, migration.txt(c.pincode)),
  phone = coalesce(b.phone, migration.txt(c.contactno)),
  pan = coalesce(b.pan, migration.txt(c.pan))
from legacy.company c join migration.map_company m on m.old_id = migration.int(c.id)
where b.id = m.new_id;

-- Any other legacy company is a separate entity in the legacy books (company 2
-- raised every Prozone bill and 122 purchase invoices), so it becomes its own
-- ESSA business rather than being folded into company 1.
insert into businesses (uuid, code, name, legal_name, gstin, pan, address, pincode, phone,
                        email, country, currency, timezone, fy_start_month, is_default, active, created_at)
select gen_random_uuid()::text,
       coalesce(migration.txt(c.code), 'LEGACY-CO' || c.id),
       initcap(btrim(c.name)), btrim(c.name), migration.txt(c.gstno), migration.txt(c.pan),
       migration.txt(c.address), migration.txt(c.pincode), migration.txt(c.contactno),
       migration.txt(c.emailid), 'India', 'INR', 'Asia/Kolkata', 4, false,
       coalesce(migration.bool(c.isactive), true), coalesce(migration.utc(c.createdon), now())
from legacy.company c
where migration.int(c.id) not in (select old_id from migration.map_company)
order by migration.int(c.id);

insert into migration.map_company
select migration.int(c.id), b.id from legacy.company c
join businesses b on b.legal_name = btrim(c.name)
where migration.int(c.id) not in (select old_id from migration.map_company);

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'company', c.id, 'review',
       'Created as a separate ESSA business (legacy keeps it apart from company 1). Confirm it is a distinct legal entity and set its GSTIN.',
       jsonb_build_object('name', c.name, 'essa_business_code', b.code)
from legacy.company c join migration.map_company m on m.old_id = migration.int(c.id)
join businesses b on b.id = m.new_id where m.old_id <> 1;

-- ---- locations ------------------------------------------------------------
create table migration.map_location (
  old_id bigint primary key, kind text not null, new_id int not null, name text);

create temp table loc as
select migration.int(r.id) id, btrim(r.code) code, btrim(r.name) name,
       coalesce(migration.bool(r.isactive), true) active,
       coalesce((migration.txt(r.additionalinfo)::jsonb ->> 'iswarehouse')::boolean, false) is_wh,
       migration.utc(r.createdon) created
from legacy.referencelist r where r.type = 'LOCATION';

-- warehouses: what the legacy system itself flags as a warehouse
insert into warehouses (name, code, catalogue_id, business_id, uuid, kind, loc_type, active, created_at)
select l.name, l.code, (select id from catalogues where is_default limit 1),
       (select new_id from migration.map_company where old_id = 1),
       gen_random_uuid()::text, 'Central', 'Garments', l.active, coalesce(l.created, now())
from loc l where l.is_wh order by l.id;

insert into migration.map_location
select l.id, 'warehouse', w.id, w.name from loc l join warehouses w on w.name = l.name where l.is_wh;

-- stores: every other legacy location. Their business is the company that
-- billed from them (Prozone → company 2); a store with no bills is company 1's.
create temp table loc_company as
select migration.int(locationid) loc, migration.int(companyid) company, count(*) n
from legacy.bill group by 1, 2;

insert into stores (warehouse_id, name, code, business_id, loc_type, active, created_at)
select (select new_id from migration.map_location where kind = 'warehouse' order by old_id limit 1),
       l.name, l.code,
       coalesce((select m.new_id from loc_company lc join migration.map_company m on m.old_id = lc.company
                 where lc.loc = l.id order by lc.n desc limit 1),
                (select new_id from migration.map_company where old_id = 1)),
       'Garments', l.active, coalesce(l.created, now())
from loc l where not l.is_wh order by l.id;

insert into migration.map_location
select l.id, 'store', s.id, s.name from loc l join stores s on s.name = l.name where not l.is_wh;

-- ---- floors ---------------------------------------------------------------
-- store + bill prefix per legacy floor, from the sales bills billed on it
-- (returns are numbered RET…, not on the floor series, so they are left out)
create temp table floor_use as
select migration.int(b.floorid) floor_id, migration.int(b.locationid) loc,
       substring(b.billno from '^([A-Z]+)[0-9]{2}[-/]') prefix, count(*) n
from legacy.bill b
where migration.bool(b.isreturn) is false and b.billno !~ '^RET'
group by 1, 2, 3;

create temp table floor_home as
select distinct on (floor_id) floor_id, loc, prefix
from floor_use where floor_id is not null order by floor_id, n desc;

-- A floor nobody billed from is still placed when its NAME says where it is:
-- "L2-FIRST" is a floor of the store whose legacy code is L2. No prefix — it
-- has never numbered a bill.
insert into floor_home
select migration.int(r.id), l.id, null
from legacy.referencelist r
join loc l on not l.is_wh and upper(btrim(r.name)) like upper(l.code) || '-%'
where r.type = 'FLOOR' and migration.int(r.id) not in (select floor_id from floor_home);

create table migration.map_floor (old_id bigint primary key, new_id int not null);

insert into floors (store_id, name, prefix, sort_order, active, created_at)
select ml.new_id, btrim(r.name), fh.prefix,
       case upper(btrim(r.name)) when 'GROUND' then 0 when 'FIRST' then 1 when 'SECOND' then 2
            when 'THIRD' then 3 else 9 end,
       coalesce(migration.bool(r.isactive), true), coalesce(migration.utc(r.createdon), now())
from legacy.referencelist r
join floor_home fh on fh.floor_id = migration.int(r.id)
join migration.map_location ml on ml.old_id = fh.loc and ml.kind = 'store'
where r.type = 'FLOOR' and migration.txt(r.name) is not null
order by migration.int(r.id);

insert into migration.map_floor
select migration.int(r.id), f.id from legacy.referencelist r
join floor_home fh on fh.floor_id = migration.int(r.id)
join migration.map_location ml on ml.old_id = fh.loc and ml.kind = 'store'
join floors f on f.store_id = ml.new_id and f.name = btrim(r.name)
where r.type = 'FLOOR';

-- floors never billed from cannot be placed in a store from the data
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'referencelist:FLOOR', r.id, 'unmatched',
       'Floor has no bills, so the store it belongs to cannot be determined; not created.',
       jsonb_build_object('name', r.name, 'code', r.code)
from legacy.referencelist r
where r.type = 'FLOOR' and migration.int(r.id) not in (select old_id from migration.map_floor);

-- ---- POS terminals (legacy counters) ---------------------------------------
create temp table counter_home as
select distinct on (counter_id) counter_id, loc, floor_id from (
  select migration.int(counterid) counter_id, migration.int(locationid) loc,
         migration.int(floorid) floor_id, count(*) n
  from legacy.bill group by 1, 2, 3) x
where counter_id is not null order by counter_id, n desc;

create table migration.map_counter (old_id bigint primary key, new_id int not null);

insert into pos_terminals (store_id, name, code, business_id, floor_id, active, created_at)
select ml.new_id, btrim(r.name), btrim(r.code),
       (select business_id from stores where id = ml.new_id),
       mf.new_id, coalesce(migration.bool(r.isactive), true), coalesce(migration.utc(r.createdon), now())
from legacy.referencelist r
join counter_home ch on ch.counter_id = migration.int(r.id)
join migration.map_location ml on ml.old_id = ch.loc and ml.kind = 'store'
left join migration.map_floor mf on mf.old_id = ch.floor_id
where r.type = 'COUNTER'
order by migration.int(r.id);

insert into migration.map_counter
select migration.int(r.id), t.id from legacy.referencelist r
join pos_terminals t on t.code = btrim(r.code)
where r.type = 'COUNTER';

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'referencelist:COUNTER', r.id, 'unmatched',
       'Counter has no bills, so the store it stands in cannot be determined; not created.',
       jsonb_build_object('name', r.name)
from legacy.referencelist r
where r.type = 'COUNTER' and migration.int(r.id) not in (select old_id from migration.map_counter);
