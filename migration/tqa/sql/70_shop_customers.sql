-- 70 · Shop: companies (billing identities) and customers
--
-- The shop's `companies` are whose GSTIN goes on a bill. sync_locations (step
-- 65) created the shop's default company from its own config; it is brought in
-- line with ESSA's business 1 (the legal entity legacy company 1 raised its
-- bills as), and ESSA's other business gets its own company. Each location is
-- filed under the company its ESSA store belongs to.

update shop.companies c set
  name = b.legal_name, gstin = coalesce(b.gstin, c.gstin), address = coalesce(b.address, c.address),
  state_code = coalesce(b.state_code, left(b.gstin, 2), c.state_code), phone = coalesce(b.phone, c.phone),
  is_default = true, active = true
from businesses b
where b.is_default and c.id = (select id from shop.companies where is_default order by id limit 1);

insert into shop.companies (name, gstin, address, state_code, phone, is_default, active)
select b.legal_name, b.gstin, b.address, coalesce(b.state_code, left(b.gstin, 2)), b.phone, false, b.active
from businesses b
where not b.is_default and not exists (select 1 from shop.companies c where upper(c.name) = upper(b.legal_name));

create table migration.map_shop_location (store_id int primary key, location_id int not null, company_id int);

insert into migration.map_shop_location
select s.id, l.id, c.id
from stores s
join shop.locations l on lower(l.name) = lower(btrim(s.name))
join businesses b on b.id = s.business_id
join shop.companies c on upper(c.name) = upper(b.legal_name);

update shop.locations l set company_id = m.company_id
from migration.map_shop_location m where m.location_id = l.id;

-- ---------------------------------------------------------------------------
--  Customers
-- ---------------------------------------------------------------------------
-- Kept one-for-one. 367 rows share a mobile number with another customer — a
-- family on one phone as often as a duplicate — so they are listed for review
-- rather than merged: bills reference each id.
create table migration.map_customer (old_id bigint primary key, new_id int not null);

create temp table cust as
select migration.int(c.id) id, btrim(c.name) name,
       nullif(regexp_replace(coalesce(c.mobileno, ''), '\D', '', 'g'), '') phone,
       migration.txt(c.emailid) email,
       migration.txt(concat_ws(', ', migration.txt(c.address), migration.txt(c.area), migration.txt(c.pincode))) address,
       upper(migration.txt(c.gstno)) gstin, migration.num(c.loyelty) loyalty,
       migration.ts(c.dateofbirth)::date dob, migration.ts(c.marriagedate)::date anniversary,
       coalesce(migration.utc(c.createdon), now()) created
from legacy.customer c;

create temp table cust_base as select coalesce(max(id), 0) max_id from shop.customers;

insert into shop.customers (name, phone, email, address, gstin, state_code, loyalty_points, total_spent,
                            dob, anniversary, created_at)
select coalesce(nullif(name, ''), 'Customer ' || id), left(phone, 32), left(email, 128), left(address, 256),
       case when gstin ~ '^[0-9]{2}[A-Z0-9]{13}$' then gstin end,
       case when gstin ~ '^[0-9]{2}[A-Z0-9]{13}$' then left(gstin, 2) else '33' end,
       coalesce(loyalty, 0), 0, dob, anniversary, created
from cust order by id;

insert into migration.map_customer
select a.id, b.id from (select id, row_number() over (order by id) rn from cust) a
join (select id, row_number() over (order by id) rn from shop.customers
      where id > (select max_id from cust_base)) b on b.rn = a.rn;

do $$ begin
  if (select count(*) from migration.map_customer) <> (select count(*) from cust) then
    raise exception 'customer map incomplete';
  end if;
end $$;

insert into migration.duplicates(entity, match_key, old_ids, new_ids, resolution)
select 'customer', 'phone=' || phone, array_agg(c.id::text || ':' || c.name order by c.id),
       array_agg(m.new_id order by c.id), 'kept_separate'
from cust c join migration.map_customer m on m.old_id = c.id
where phone is not null group by phone having count(*) > 1;
