-- 30 · Products (ESSA's stock item: one SKU per legacy ITEM × DESIGN)
--
-- Legacy keeps three levels: `products` (the group — MENS-SHIRT), `items` (a
-- variant — group × brand × size × style × material × colour…), and `stock`
-- (one barcoded piece, TQ315689, carrying its design number and price).
-- ESSA's Product is the variant INCLUDING the design number: design_no is an
-- identity attribute of the garment catalogue (catalogue_attributes), so two
-- designs of the same item are two SKUs in ESSA, exactly as a GRN posted in
-- ESSA would create them. A piece's CURRENT item/design is used (448K pieces
-- were re-classified after their GRN — legacy `productchange`).
--
-- The legacy piece table is typed once here into migration.piece; every later
-- step (GRNs, piece codes, transfers, sales) reads that instead of re-parsing.

create table migration.piece as
select migration.int(s.id) id,
       btrim(s.barcode) barcode,
       migration.int(s.itemid) item_id,
       coalesce(migration.txt(s.designid), '') design,
       migration.ref(s.invoiceitemid) grn_line_id,
       coalesce(migration.num(s.qty), 0) qty,
       coalesce(migration.num(s.transfered), 0) transferred,
       coalesce(migration.num(s.purchasereturnqty), 0) returned,
       coalesce(migration.bool(s.isactive), true) active,
       coalesce(migration.bool(s.hasstock), false) has_stock,
       migration.num(s.price) price,
       nullif(migration.num(s.displaymrp), 0) mrp,
       migration.num(s.buyingprice) cost,
       migration.ref(s.supplierid) supplier_id,
       migration.txt(s.hsncode) hsn,
       migration.txt(s.printingname) printing_name,
       migration.txt(s.lrreference) lr_reference,
       migration.utc(s.createdon) created
from legacy.stock s;
alter table migration.piece add primary key (id);
create index on migration.piece (item_id, design);
create index on migration.piece (grn_line_id);
create index on migration.piece (barcode);

-- GRN lines typed once as well (step 40 builds purchase lines from these)
create table migration.grn_line as
select migration.int(l.id) id, migration.int(l.lrinvoiceid) invoice_id,
       migration.int(l.itemid) item_id, coalesce(migration.txt(l.designid), '') design,
       coalesce(migration.num(l.buyingqty), 0) qty, migration.num(l.buyingprice) rate,
       nullif(migration.num(l.mrp), 0) mrp, nullif(migration.num(l.sellingprice), 0) sale_price,
       migration.num(l.amount) amount, migration.txt(l.hsncode) hsn,
       migration.num(l.purchasetaxpercentage) tax_pct, migration.num(l.purchasetax) tax,
       coalesce(migration.bool(l.isactive), true) active, migration.int(l.sno) sno,
       migration.utc(l.createdon) created
from legacy.lrinvoiceitems l;
alter table migration.grn_line add primary key (id);
create index on migration.grn_line (invoice_id);
create index on migration.grn_line (item_id, design);
analyze migration.piece;
analyze migration.grn_line;

-- the variant attributes, resolved once
create temp table item as
select migration.int(i.id) id, migration.int(i.productid) product_id, migration.txt(i.code) code,
       migration.txt(i.brandname) brand, migration.txt(i.sizename) size, migration.txt(i.colourname) color,
       migration.txt(i.patternname) pattern, migration.txt(i.stylename) style,
       migration.txt(i.materialname) material, migration.txt(i.typename) product_type,
       migration.txt(fit.name) fit, migration.txt(slv.name) sleeve,
       coalesce(migration.txt(i.sellingname), migration.txt(i.productname)) selling_name,
       migration.utc(i.createdon) created
from legacy.items i
left join legacy.referencelist fit on fit.type = 'FIT' and fit.id = i.fitid
left join legacy.referencelist slv on slv.type = 'SLEEVE' and slv.id = i.sleeveid;
create index on item(id);

-- ---- the product set --------------------------------------------------------
-- Every (item, design) a real barcoded piece carries. ESSA mints a Product only
-- for goods that became stock, and so does this: a GRN line that never produced
-- a piece stays on its purchase as a document line (step 40) without inventing
-- a stock item that never held anything. (Most such lines are empty rows from
-- the legacy system's own Aug-2022 go-live import.)
create table migration.map_product (
  item_id bigint not null, design text not null, new_id int, sku text,
  primary key (item_id, design));

insert into migration.map_product (item_id, design)
select distinct item_id, design from migration.piece where item_id is not null;

insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'stock', null, 'excluded', 'Pieces with no item id cannot be a product; not migrated.',
       jsonb_build_object('pieces', count(*))
from migration.piece where item_id is null having count(*) > 0;

-- what each product is: latest piece's prices, latest GRN's cost and supplier
create temp table prod_src as
select m.item_id, m.design,
       lp.price sale_price, coalesce(lp.mrp, lg.mrp, lp.price) mrp, coalesce(lp.hsn, lg.hsn) hsn,
       coalesce(lp.cost, lg.rate) last_rate, lp.supplier_id, lp.printing_name,
       least(fp.created, fg.created) first_seen
from migration.map_product m
left join lateral (select * from migration.piece p where p.item_id = m.item_id and p.design = m.design
                   order by p.created desc nulls last, p.id desc limit 1) lp on true
left join lateral (select min(created) created from migration.piece p
                   where p.item_id = m.item_id and p.design = m.design) fp on true
left join lateral (select * from migration.grn_line g where g.item_id = m.item_id and g.design = m.design
                   order by g.created desc nulls last, g.id desc limit 1) lg on true
left join lateral (select min(created) created from migration.grn_line g
                   where g.item_id = m.item_id and g.design = m.design) fg on true;

-- SKUs in ESSA's own format (numbering.DOCS["sku"]: ESSA- + 5-digit padding),
-- oldest product first so the numbers read in the order the goods arrived
create temp table prod as
select ps.*, i.*,
       -- lpad TRUNCATES past its width, so pad to at least 5, never to exactly 5
       'ESSA-' || migration.pad(row_number() over (order by ps.first_seen nulls last, ps.item_id, ps.design), 5) sku
from prod_src ps left join item i on i.id = ps.item_id;

-- a variant whose item row is gone cannot be described
insert into migration.exceptions(entity, old_id, kind, reason, detail)
select 'items', item_id::text, 'review', 'Pieces/GRN lines reference an item id that is not in the legacy item table; product created from the piece data alone.',
       jsonb_build_object('design', design)
from prod where id is null;

insert into products (catalogue_id, sku, barcode, description, hsn, uom, unit_type, pieces_per_unit,
                      mrp, primary_supplier_id, stock_qty, avg_cost, last_rate, created_at,
                      color, size, pattern, fit, product_type, material, design_no,
                      brand, style, sleeve, category, category_section,
                      sale_price, sale_discount_pct, detailed)
select (select id from catalogues where is_default limit 1),
       p.sku, null,
       btrim(concat_ws(' ', coalesce(p.printing_name, p.selling_name, mc.name, 'ITEM ' || p.item_id),
                            nullif(p.design, ''))),
       coalesce(p.hsn, mc.hsn), 'PCS', 'PCS', 1.0,
       p.mrp, (select new_id from migration.map_supplier where old_id = p.supplier_id),
       0, coalesce(p.last_rate, 0), p.last_rate, coalesce(p.first_seen, now()),
       p.color, p.size, p.pattern, p.fit, p.product_type, p.material, nullif(p.design, ''),
       p.brand, p.style, p.sleeve, mc.name, mc.section,
       p.sale_price,
       case when p.mrp > 0 and p.sale_price is not null and p.sale_price < p.mrp
            then round((1 - p.sale_price / p.mrp) * 100, 2) else 0 end,
       false
from prod p left join migration.map_category mc on mc.old_id = p.product_id
order by p.sku;

update migration.map_product m set new_id = x.id, sku = x.sku
from prod p join products x on x.sku = p.sku
where m.item_id = p.item_id and m.design = p.design;

do $$ begin
  if exists (select 1 from migration.map_product where new_id is null) then
    raise exception 'unmapped products remain';
  end if;
end $$;

-- a piece's product, resolved once
alter table migration.piece add column product_id int;
update migration.piece p set product_id = m.new_id
from migration.map_product m where m.item_id = p.item_id and m.design = p.design;
create index on migration.piece (product_id);
