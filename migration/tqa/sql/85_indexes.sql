-- 85 · Indexes for real data volumes
--
-- Postgres does not index a foreign key by itself (SQLite's query planner was
-- forgiving about it at demo sizes). ESSA and the shop filter by these columns
-- constantly — every GRN's lines, every invoice's items, every payment's
-- allocations — and on 800K GRN lines / 2M bill lines an unindexed filter is a
-- full scan per lookup. This adds a plain b-tree index on every single-column
-- foreign key that has none, in both schemas. Indexes change no data and no
-- behaviour; they only change how fast the same rows are found.

do $$
declare r record;
begin
  for r in
    select n.nspname sch, cl.relname tbl, a.attname col
    from pg_constraint c
    join pg_class cl on cl.oid = c.conrelid
    join pg_namespace n on n.oid = cl.relnamespace
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
    where c.contype = 'f' and array_length(c.conkey, 1) = 1
      and n.nspname in ('public', 'shop')
      and not exists (select 1 from pg_index i where i.indrelid = c.conrelid and i.indkey[0] = c.conkey[1])
  loop
    execute format('create index if not exists %I on %I.%I (%I)',
                   left('ix_fk_' || r.tbl || '_' || r.col, 63), r.sch, r.tbl, r.col);
  end loop;
end $$;

-- the shop's reports window everything by bill date
create index if not exists ix_shop_invoices_invoice_date on shop.invoices (invoice_date);
create index if not exists ix_shop_credit_notes_created_at on shop.credit_notes (created_at);

analyze;
