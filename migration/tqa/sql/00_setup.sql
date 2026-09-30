-- 00 · migration bookkeeping + value parsers
--
-- Everything the migration needs to remember lives in schema `migration`:
-- old→new id maps (one table per entity), the exception log, and duplicate
-- groups. ESSA's own tables are never altered.
--
-- The legacy export writes booleans as t/f, "empty" as '' or 'null', and
-- "no date" as 0001-01-01 / 1901-01-01. These helpers turn those into real
-- NULLs and typed values, so nothing downstream has to know the export's habits.

create schema if not exists migration;

create or replace function migration.txt(v text) returns text
language sql immutable as $$
  select case when v is null then null
              when btrim(v) in ('', 'null', 'NULL') then null
              else btrim(v) end $$;

create or replace function migration.num(v text) returns numeric
language sql immutable as $$
  select case when migration.txt(v) ~ '^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$'
              then migration.txt(v)::numeric end $$;

create or replace function migration.int(v text) returns bigint
language sql immutable as $$
  select case when migration.txt(v) ~ '^-?[0-9]+$' then migration.txt(v)::bigint end $$;

-- 0 is the legacy "no reference" id everywhere
create or replace function migration.ref(v text) returns bigint
language sql immutable as $$
  select nullif(migration.int(v), 0) $$;

create or replace function migration.ts(v text) returns timestamp
language sql immutable as $$
  select case when migration.txt(v) ~ '^\d{4}-\d{2}-\d{2}'
               and left(migration.txt(v), 4)::int >= 1950
              then migration.txt(v)::timestamp end $$;

-- ESSA keeps business dates as ISO strings (services/dates)
create or replace function migration.iso(v text) returns text
language sql immutable as $$
  select to_char(migration.ts(v), 'YYYY-MM-DD') $$;

create or replace function migration.bool(v text) returns boolean
language sql immutable as $$
  select case lower(btrim(coalesce(v, ''))) when 't' then true when 'true' then true
              when 'f' then false when 'false' then false end $$;

-- Legacy timestamps are local (IST). ESSA stores created_at in UTC
-- (models.now = utcnow; config.BUSINESS_UTC_OFFSET_MINUTES = 330).
create or replace function migration.utc(v text) returns timestamp
language sql immutable as $$
  select migration.ts(v) - interval '330 minutes' $$;

-- zero-padded to at least `width` digits (numbering.render's rule); lpad alone
-- would cut 100000 down to 10000
create or replace function migration.pad(n bigint, width int) returns text
language sql immutable as $$
  select lpad(n::text, greatest(width, length(n::text)), '0') $$;

create table if not exists migration.run_log (
  step text primary key, started_at timestamptz, finished_at timestamptz, note text);

create table if not exists migration.exceptions (
  id bigserial primary key,
  entity text not null,          -- legacy table
  old_id text,                   -- legacy id
  kind text not null,            -- excluded | unmatched | review | adjusted | failed
  reason text not null,
  detail jsonb,
  logged_at timestamptz default now());
create index if not exists exceptions_entity on migration.exceptions(entity, kind);

create table if not exists migration.duplicates (
  id bigserial primary key,
  entity text not null,
  match_key text not null,       -- what the records share
  old_ids text[] not null,
  new_ids int[],
  resolution text not null);     -- kept_separate | merged
