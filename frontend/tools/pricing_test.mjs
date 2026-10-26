// Checks the line-item pricing chain: cost → sell price → MRP.
//
//     node frontend/tools/pricing_test.mjs
//
// Money, and two percentages that are easy to mix up — the buffer over the cost
// and the buffer over the shelf price are different numbers spanning different
// pairs of prices, and swapping them produces figures that look plausible on
// every line. Worth a test that states the arithmetic in the terms the trade
// uses rather than in the terms the code does.
//
// recalcLine and its helpers are lifted out of App.jsx by source rather than
// imported: the file is a React module and pulling it in would need a bundler
// and a DOM for four pure functions.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(HERE, '..', 'src', 'App.jsx'), 'utf8');

// The pricing helpers are one contiguous block in App.jsx — nf and r2 above it,
// then fromMrp through recalcLine — so they are sliced out by their boundary
// lines rather than parsed. Anything that moves them breaks this loudly, which
// is the right failure: the test is only meaningful against the real code.
function slice(fromLine, toLine) {
  const lines = SRC.split('\n');
  const a = lines.findIndex(l => l.startsWith(fromLine));
  const b = lines.findIndex(l => l.startsWith(toLine));
  if (a < 0 || b < 0 || b <= a) {
    throw new Error(`could not slice ${fromLine} … ${toLine} — has App.jsx been reorganised?`);
  }
  return lines.slice(a, b).join('\n');
}

const { recalcLine } = new Function(
  slice('const nf = (v) => {', 'function taxBase(')
  + '\nreturn { recalcLine }'
)();

// "Apply all" itself, lifted the same way. It has two branches — a price goes
// through recalcLine, a brand or an HSN is set as typed — and which branch a
// column takes is exactly the sort of thing that is right until somebody adds a
// column, and wrong from then on.
const { applyAll, ready } = new Function(
  // `num` sits at the top of App.jsx, outside the arithmetic block below, and
  // fillReady leans on it to tell "0" from "abc"
  "const num = (v) => (v === '' || v == null ? null : isNaN(+v) ? v : +v)\n"
  + slice('const nf = (v) => {', 'function taxBase(')
  + '\n' + slice('const ITEM_FILL = {', 'const ITEM_CALC =')
  + '\nlet items = [], fill = {}'
  + '\nconst setItems = (v) => { items = v }'
  + '\n' + slice('  const [fill, setFill] = useState({})', '  const fillCell =')
             .replace('const [fill, setFill] = useState({})', '')
  + '\nreturn {'
  + '  applyAll: (list, key, value) => {'
  + '    items = list; fill = { [key]: value }; applyFill(key); return items },'
  + '  ready: (key, value) => { fill = { [key]: value }; return fillReady(key) },'
  + '}'
)();

let fails = 0;
const check = (ok, label, detail = '') => {
  if (ok) { console.log(`  ok    ${label}`); return; }
  fails++; console.error(`  FAIL  ${label}${detail ? ' — ' + detail : ''}`);
};
const near = (a, b, tol = 0.02) => a != null && Math.abs(a - b) < tol;
// shop prices are worked out to the nearest 10 (5 and above goes up)
const r10 = (v) => Math.round(Math.round(v * 100) / 1000) * 10;

// ---------------------------------------------------------------------------
console.log('the worked example: 320 → +40% → 448, tagged 450 → +25% → 560');

// goods arrive with a cost and no tag; both buffers are the shop's decision
let row = { qty: 1, rate: 320, buffer_pct: 40 };
row = recalcLine(row, 'buffer_pct', {});
check(near(row.sale_price, 450), 'rate 320 + 40% = 448, sold at 450', `got ${row.sale_price}`);

row = recalcLine({ ...row, mrp_buffer_pct: 25 }, 'mrp_buffer_pct', row);
check(near(row.mrp, 560), 'sell 450 + 25% = 562.5, tagged MRP 560', `got ${row.mrp}`);

console.log('\nthe percentages that follow from those prices:');
check(near(row.sale_discount_pct, 19.64), 'sale disc = (560−450)/560 = 19.64%', `got ${row.sale_discount_pct}`);
check(near(row.discount_pct, 42.86), 'purchase disc = (560−320)/560 = 42.86%', `got ${row.discount_pct}`);

console.log('\na printed MRP is not overwritten:');
let printed = recalcLine({ qty: 1, rate: 320, mrp: 995, buffer_pct: 40, mrp_buffer_pct: 25 },
                         'buffer_pct', {});
check(printed.mrp === 995, 'invoice MRP 995 stands when the buffer moves', `got ${printed.mrp}`);
check(near(printed.sale_price, 450), 'the sell price is still priced off cost', `got ${printed.sale_price}`);

console.log('\n…but typing the MRP buffer is an instruction, and wins:');
const retagged = recalcLine({ ...printed, mrp_buffer_pct: 25 }, 'mrp_buffer_pct', printed);
check(near(retagged.mrp, 560), 'typed MRP buffer re-prices the tag to 560', `got ${retagged.mrp}`);

console.log('\ntyping a sell price back-solves the buffer, and is not overwritten:');
let typed = recalcLine({ qty: 1, rate: 320, buffer_pct: 40, sale_price: 480 }, 'sale_price',
                       { rate: 320, buffer_pct: 40, sale_price: 448 });
check(near(typed.sale_price, 480), 'the typed 480 survives', `got ${typed.sale_price}`);
check(near(typed.buffer_pct, 50), 'buffer restated as (480−320)/320 = 50%', `got ${typed.buffer_pct}`);

console.log('\nthe purchase side still works from the tag down:');
let bill = recalcLine({ qty: 2, mrp: 995, discount_pct: 34.67 }, 'discount_pct', {});
check(near(bill.rate, 650, 0.05), 'MRP 995 − 34.67% = rate 650 (the % is rounded)', `got ${bill.rate}`);
check(near(bill.amount, 1300, 0.10), 'qty 2 × 650 = 1300 (the % is rounded)', `got ${bill.amount}`);

console.log('\nround trip — a rounded price reads its buffer back:');
const back = recalcLine({ qty: 1, rate: 320, sale_price: 448 }, 'rate', {});
check(near(bufferOf(448, 320), 40), '448 against 320 is 40%');
function bufferOf(top, base) { return Math.round((top / base - 1) * 10000) / 100; }

console.log('');
console.log('filling a column applies to every line, and re-prices each one:');
// what "Apply all" does: the same recalcLine each row would get if the number
// had been typed into it by hand
const invoice = [
  { qty: 1, rate: 320 }, { qty: 5, rate: 290 }, { qty: 15, rate: 205 },
];
const withBuffer = invoice.map((it) => recalcLine({ ...it, buffer_pct: 40 }, 'buffer_pct', it));
check(withBuffer.every((r) => near(r.sale_price, r10(r.rate * 1.4))),
  'every line priced at cost + 40%',
  withBuffer.map((r) => r.rate + '->' + r.sale_price).join(' '));

const withMrp = withBuffer.map((it) => recalcLine({ ...it, mrp_buffer_pct: 25 }, 'mrp_buffer_pct', it));
check(withMrp.every((r) => near(r.mrp, r10(r.sale_price * 1.25))),
  'every MRP is sell + 25%',
  withMrp.map((r) => r.sale_price + '->' + r.mrp).join(' '));
check(near(withMrp[0].sale_price, 450) && near(withMrp[0].mrp, 560),
  'the worked line still reads 320 / 450 / 560',
  withMrp[0].rate + ' / ' + withMrp[0].sale_price + ' / ' + withMrp[0].mrp);
check(withMrp.every((r) => near(r.amount, r.qty * r.rate)),
  'line amounts are untouched by a pricing fill',
  withMrp.map((r) => r.amount).join(' '));

console.log('');
console.log('…and Apply all itself, which takes one of two paths per column:');
{
  const bill = [{ qty: 1, rate: 320, hsn: '620520' },
                { qty: 5, rate: 290, hsn: '' },
                { qty: 15, rate: 205, hsn: '620462' }];

  // a text column is set as typed, and touches none of the money
  const hsned = applyAll(bill, 'hsn', ' 620520 ');
  check(hsned.every((r) => r.hsn === '620520'),
    'one HSN reaches every line, trimmed', hsned.map((r) => r.hsn).join(' '));
  check(hsned.every((r, i) => r.rate === bill[i].rate && r.sale_price === undefined),
    'and a text fill re-prices nothing');
  check(applyAll(bill, 'uom', 'Pc').every((r) => r.uom === 'Pc'), 'so does one unit');
  check(applyAll(bill, 'brand', 'COSM').every((r) => r.brand === 'COSM'), 'and one brand');

  // a price column goes through the whole chain, against each line's own cost
  const buffered = applyAll(bill, 'buffer_pct', '40');
  check(buffered.every((r) => near(r.sale_price, r10(r.rate * 1.4))),
    'a buffer fill re-prices each line off its OWN cost',
    buffered.map((r) => r.rate + '->' + r.sale_price).join(' '));
  check(applyAll(bill, 'rate', '500').every((r) => near(r.amount, r.qty * 500)),
    'and a rate fill carries the amount with it');

  // blank does nothing rather than clearing the column
  check(!ready('hsn', '') && !ready('hsn', '   '), 'a blank text fill is not ready');
  check(!ready('buffer_pct', '') && !ready('buffer_pct', 'abc'),
    'nor a blank or unreadable number');
  check(ready('buffer_pct', '0'), 'but zero IS a number somebody may mean');
  check(applyAll(bill, 'hsn', '  ')[1].hsn === '', 'so a blank leaves the column alone');
}

console.log('');
console.log('the GRN fill row lines up with the headings it fills:');
{
  // The invoice grid's fill row is generated from ITEM_COLS and cannot drift.
  // The GRN's is hand-written, because its headings are — and a fill row one
  // cell short silently slides every box one column to the left, which puts an
  // MRP box over Discount % and is not obvious from looking at it.
  const grn = SRC.slice(SRC.indexOf('<th>Product</th>'));
  const headEnd = grn.indexOf('</tr>');
  const head = grn.slice(0, headEnd);
  const fillStart = grn.indexOf('<tr className="fillrow">');
  const fill = grn.slice(fillStart, grn.indexOf('</tr>', fillStart));
  const cells = (s) => (s.match(/<th[\s/>]/g) || []).length;
  check(fillStart > 0, 'the GRN table has a fill row at all');
  check(cells(head) === cells(fill),
    'and it has one cell per heading',
    `${cells(head)} headings, ${cells(fill)} fill cells`);
  // the five that are editable, and nothing else
  // category is set on the invoice's line items now, not on the GRN
  for (const k of ['unit_type', 'mrp', 'sale_discount_pct', 'sale_price']) {
    check(fill.includes(`gfillCell('${k}'`), `  ${k} can be filled`);
  }
  for (const k of ['description', 'hsn', 'qty', 'rate', 'amount']) {
    check(!fill.includes(`gfillCell('${k}'`),
      `  ${k} cannot — it is what the supplier billed`);
  }
}

console.log(fails ? `\n${fails} failing` : '\nall passing');
process.exit(fails ? 1 : 0);
