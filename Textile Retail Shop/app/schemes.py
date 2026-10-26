"""Sales promotion schemes — which goods and bills qualify, and what they get.

Reads the scheme rows in app/models.py (SalesScheme and its FILTER and BENEFIT
rows, written by the Promotions screen) and answers, for a bill:

    which schemes apply, to which lines, and what they take off

It decides nothing about money on its own say-so at the till: the counter asks
`evaluate_bill` to SHOW what the bill earns, and `checkout` asks it again, from
the lines it is about to write, and that is the answer that bills. A page that
could send its own scheme discount could give the shop's goods away.

WHEN A SCHEME PAYS OUT (`applicable`)
-------------------------------------
* On Same Bill      — at the counter, as a discount on the qualifying lines.
* On Bill (Addon)   — the same, but on top of whatever else the line has (it
                      stacks, where two Same Bill schemes do not: the line gets
                      the better of those).
* On Settlement     — when the bill is paid, off the bill as a whole.
* On Next Bill      — when the bill is paid, as a coupon for the next visit.
* On Next Day Bill  — the same, but the coupon is good from tomorrow.
* On Simulation     — never at the till; only on the scheme screen's Simulate.

FILTERS
-------
Item filters say which LINES qualify. A line qualifies when it matches at least
one include filter (Product Attribute, Invoice, Barcode, Marked Item, Barcode
Prefix) — or there are none — and matches no exclude filter (Excluded Barcode /
Marker / Product Attribute, and Excluded Discount: a line already discounted by
hand). Bill filters gate the whole scheme: Bill/Bill Item Value (the bill must
come to between two amounts) and Agreement Code (the bill must carry one of the
codes, entered as the discount card's P.Code).

MODE AND THE QTY / RATE CONDITION
---------------------------------
Every benefit row carries the condition it was added under: Qty between (the
qualifying pieces) and Rate between (inclusive; 0 at the top = no limit). The
scheme's Mode says what the Rate is measured on:

* On Bill Item  — each item's own price with GST: only items priced inside the
                  range count, and the benefit is on them.
* On Bill Value — the qualifying goods' total: the bill must come to inside
                  the range, and the benefit is on all of them.

The rows are tiers: the one with the highest Qty From that is met is the one
that pays.

BENEFITS
--------
* Discount In Percentage  — Value: % off the qualifying items; Additional: a
                            further % on top.
* Discount In Fixed Value — Value: ₹ off each qualifying piece; ticked Flat:
                            ₹ off the qualifying goods once.
* Reward Points           — Value: loyalty points the customer earns, credited
                            when the bill is paid (a customer must be on it).
* Discount In Qty         — Qty From: how many to buy (X); Value: how many come
                            free (Y). In every X+Y pieces the cheapest Y are free.
* Combo Offer             — Value: ₹ off every complete set of Qty From pieces;
                            ticked Flat: ₹ off once, however many sets.
* Discounted Item         — Item: the barcode / SKU that gets the discount;
                            Value: its % off — one piece per complete set of Qty
                            From qualifying pieces, if it is on the bill.
* Coupon for next bill    — Value: the coupon's worth in ₹; Additional: days
                            valid (30 when empty). Issued when the bill is paid.
"""
import json
from datetime import date, timedelta

from app import db, vouchers
from app.models import (Coupon, CouponCampaign, Customer, LoyaltyTxn, SalesScheme,
                        SalesSchemeUse, SCHEME_APPLICABLE, SCHEME_FILTER_MODES)

#: Every timing a till can meet — all but Simulation.
SCHEME_APPLICABLE_CODES = [(c, l) for c, l in SCHEME_APPLICABLE if c != "simulation"]

#: Item filters that let a line in, and those that keep it out.
INCLUDE_MODES = ("product_attr", "invoice", "barcode", "marker", "barcode_prefix")
EXCLUDE_MODES = ("ex_barcode", "ex_marker", "ex_discount", "ex_product_attr")

#: The product fields a Product Attribute filter can name. (key, label, field)
ATTRIBUTES = (
    ("category", "Product", None),          # the category's name
    ("brand", "Brand", "brand"),
    ("material", "Material", "fabric"),
    ("pattern", "Pattern", "pattern"),
    ("colour", "Colour", "color"),
    ("style", "Style", "style"),
    ("size", "Size", "size"),
    ("type", "Type", "product_type"),
    ("sleeve", "Sleeve", "sleeve"),
    ("fit", "Fit", "fit"),
)

#: The Mode a scheme starts on for each timing — the screen picks it when
#: Applicable is chosen, and it can be changed.
DEFAULT_MODE = {
    "same_bill": "bill_item", "addon": "bill_item", "simulation": "bill_item",
    "settlement": "bill_value", "next_bill": "bill_value", "next_day_bill": "bill_value",
}

#: Benefits that pay out when the bill is paid, whatever the scheme's timing.
PAID_LATER = ("coupon", "points")

AT_THE_COUNTER = ("same_bill", "addon")
AT_SETTLEMENT = ("settlement", "next_bill", "next_day_bill")


def _norm(v):
    return str(v or "").strip().upper()


def _data(f):
    try:
        d = json.loads(f.data or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _in_range(v, lo, hi):
    return (not lo or v + 1e-9 >= lo) and (not hi or v - 1e-9 <= hi)


def combine(*pcts):
    """Several % discounts, one on top of the other, as one %."""
    keep = 1.0
    for p in pcts:
        keep *= 1 - min(100.0, max(0.0, float(p or 0))) / 100.0
    return round((1 - keep) * 100, 4)


class Line:
    """One paid line of a bill, as the engine reads it.

    `price` is the unit price before ANY discount (pre-tax), `manual_pct` the
    discount the counter gave it by hand, and `charged` what it comes to with
    GST after that discount — what a scheme's discount comes off.
    """

    def __init__(self, idx, product, qty, price, gst, manual_pct=0.0):
        self.idx, self.product = idx, product
        self.qty, self.price = float(qty or 0), float(price or 0)
        self.gst = float(gst if gst is not None else (product.gst_rate if product else 0) or 0)
        self.manual_pct = float(manual_pct or 0)
        self.unit_gross = self.price * (1 + self.gst / 100.0)
        self.charged = self.qty * self.unit_gross * (1 - self.manual_pct / 100.0)


# --------------------------------------------------------------------------
# which schemes, which lines
# --------------------------------------------------------------------------

def live_schemes(stages, company_id=None, location_id=None, when=None):
    """Active schemes running today at this company/location, at these stages."""
    when = when or date.today()
    rows = (SalesScheme.query.filter(SalesScheme.active.is_(True),
                                     SalesScheme.applicable.in_(stages))
            .order_by(SalesScheme.id).all())
    return [s for s in rows if s.runs_on(when)
            and (not s.company_id or not company_id or s.company_id == company_id)
            and (not s.location_id or not location_id or s.location_id == location_id)]


def _attr_value(product, key, field):
    if field is None:
        return product.category.name if product.category else ""
    return getattr(product, field, None) or ""


def _match_item(f, line):
    """Does this one filter row pick out this line?"""
    p, d = line.product, _data(f)
    if p is None:
        return False
    codes = {_norm(p.barcode), _norm(p.sku)} - {""}
    if f.mode in ("product_attr", "ex_product_attr"):
        named = [(k, fld) for k, _label, fld in ATTRIBUTES if _norm(d.get(k))]
        return bool(named) and all(_norm(_attr_value(p, k, fld)) == _norm(d.get(k))
                                   for k, fld in named)
    if f.mode == "invoice":
        checks = [(d.get("supplier"), p.supplier), (d.get("invoice"), p.purchase_invoice),
                  (d.get("design_code"), p.design_no),
                  (d.get("invoice_date"), p.purchase_date.isoformat() if p.purchase_date else "")]
        named = [(want, have) for want, have in checks if _norm(want)]
        return bool(named) and all(_norm(want) == _norm(have) for want, have in named)
    if f.mode in ("barcode", "ex_barcode"):
        return bool(codes & {_norm(c) for c in d.get("codes") or []})
    if f.mode in ("marker", "ex_marker"):
        return _norm(p.marker) != "" and _norm(p.marker) in {_norm(m) for m in d.get("markers") or []}
    if f.mode == "barcode_prefix":
        return any(c.startswith(_norm(pre)) for c in codes for pre in d.get("prefixes") or [] if _norm(pre))
    if f.mode == "ex_discount":
        return line.manual_pct > 0
    return False


def eligible(scheme, lines, bill_value, agreement_code=None):
    """The lines this scheme is for — or [] when a bill filter shuts it out."""
    for f in scheme.filters:
        d = _data(f)
        if f.mode == "bill_value":
            if not _in_range(bill_value, float(d.get("min") or 0), float(d.get("max") or 0)):
                return []
        elif f.mode == "agreement_code":
            if _norm(agreement_code) not in {_norm(c) for c in d.get("codes") or []}:
                return []
    inc = [f for f in scheme.filters if f.mode in INCLUDE_MODES]
    exc = [f for f in scheme.filters if f.mode in EXCLUDE_MODES]
    return [l for l in lines
            if (not inc or any(_match_item(f, l) for f in inc))
            and not any(_match_item(f, l) for f in exc)]


# --------------------------------------------------------------------------
# what they get
# --------------------------------------------------------------------------

class Award:
    """What one scheme gives one bill: a % per line, and/or an amount."""

    def __init__(self, scheme, benefit):
        self.scheme, self.benefit = scheme, benefit
        self.line_pct = {}          # idx -> % off that line
        self.amount = 0.0           # ₹ with GST, what it is worth
        self.coupon_value = 0.0     # ₹ of the coupon a Coupon benefit issues
        self.points = 0.0           # loyalty points a Reward Points benefit gives

    def to_json(self):
        return {"scheme_id": self.scheme.id, "code": self.scheme.code,
                "name": self.scheme.name, "applicable": self.scheme.applicable,
                "applicable_label": self.scheme.applicable_label,
                "benefit": self.benefit.mode if self.benefit else None,
                "lines": {str(k): v for k, v in self.line_pct.items()},
                "amount": round(self.amount, 2),
                "coupon_value": round(self.coupon_value, 2),
                "points": round(self.points, 2)}


def _tier(scheme, elig):
    """The benefit row that pays, and the lines it pays on.

    On Bill Item the Rate condition picks out the items priced inside it; On
    Bill Value it is the qualifying goods' total that has to be inside it.
    """
    by_value = (scheme.mode or "bill_item") == "bill_value"
    for b in sorted(scheme.benefits, key=lambda b: -(b.qty_from or 0)):
        if by_value:
            rows = list(elig)
            if not _in_range(sum(l.charged for l in rows), b.price_from, b.price_to):
                continue
        else:
            rows = [l for l in elig if _in_range(l.unit_gross, b.price_from, b.price_to)]
        qty = sum(l.qty for l in rows)
        if rows and _in_range(qty, b.qty_from, b.qty_to):
            return b, rows
    return None, []


def award(scheme, lines, bill_value, agreement_code=None):
    """What `scheme` gives this bill, or None."""
    elig = eligible(scheme, lines, bill_value, agreement_code)
    if not elig:
        return None
    b, rows = _tier(scheme, elig)
    if b is None:
        return None
    a = Award(scheme, b)
    value, extra = float(b.value or 0), float(b.additional or 0)
    if b.mode == "discount_pct":
        pct = combine(value, extra)
        for l in rows:
            a.line_pct[l.idx] = pct
    elif b.mode == "flat":
        base = sum(l.charged for l in rows)
        if base <= 0:
            return None
        pct = combine(min(100.0, value / base * 100), extra)
        for l in rows:
            a.line_pct[l.idx] = pct
    elif b.mode == "free_item":
        buy, free = max(1, int(b.qty_from or 0)), int(value)
        units = int(sum(l.qty for l in rows) + 1e-9)
        n_free = (units // (buy + free)) * free if free > 0 else 0
        if n_free <= 0:
            return None
        # the cheapest pieces go free, a line at a time
        for l in sorted(rows, key=lambda l: l.unit_gross):
            if n_free <= 0:
                break
            take = min(n_free, int(l.qty + 1e-9))
            if take > 0:
                a.line_pct[l.idx] = round(take / l.qty * 100, 4)
                n_free -= take
    elif b.mode == "fixed":
        if b.flat:
            base = sum(l.charged for l in rows)
            if base <= 0:
                return None
            pct = min(100.0, value / base * 100)
            for l in rows:
                a.line_pct[l.idx] = round(pct, 4)
        else:
            for l in rows:
                if l.unit_gross > 0:
                    a.line_pct[l.idx] = round(min(100.0, value / l.unit_gross * 100), 4)
    elif b.mode == "combo":
        size = max(1, int(b.qty_from or 0) or 1)
        sets = int(sum(l.qty for l in rows) + 1e-9) // size
        if sets <= 0:
            return None
        base = sum(l.charged for l in rows)
        off = value if b.flat else value * sets
        if base <= 0:
            return None
        pct = min(100.0, off / base * 100)
        for l in rows:
            a.line_pct[l.idx] = round(pct, 4)
    elif b.mode == "discounted_item":
        code = _norm(b.item_code)
        size = max(1, int(b.qty_from or 0) or 1)
        sets = int(sum(l.qty for l in rows) + 1e-9) // size
        target = next((l for l in lines if l.product is not None and code
                       and code in {_norm(l.product.barcode), _norm(l.product.sku)}), None)
        if sets <= 0 or target is None:
            return None
        pieces = min(sets, int(target.qty + 1e-9))
        if pieces <= 0:
            return None
        a.line_pct[target.idx] = round(min(100.0, value) * pieces / target.qty, 4)
        rows = rows + [target]
    elif b.mode == "points":
        a.points = value
    elif b.mode == "coupon":
        a.coupon_value = value
    else:
        return None
    by_idx = {l.idx: l for l in rows}
    a.amount = sum(by_idx[i].charged * p / 100.0 for i, p in a.line_pct.items())
    return a


def evaluate_bill(lines, company_id=None, location_id=None, agreement_code=None, when=None):
    """What the counter's schemes do to these lines.

    Returns ({idx: {"pct", "scheme_id", "names"}}, [Award]). Of the Same Bill
    schemes a line gets the best one; Addon schemes stack on top of it.
    """
    bill_value = sum(l.charged for l in lines)
    best, addons, awards = {}, {}, []
    for s in live_schemes(AT_THE_COUNTER, company_id, location_id, when):
        a = award(s, lines, bill_value, agreement_code)
        if a is None or not a.line_pct:
            continue
        awards.append(a)
        for idx, pct in a.line_pct.items():
            if s.applicable == "addon":
                addons.setdefault(idx, []).append((s, pct))
            elif pct > best.get(idx, (None, 0))[1]:
                best[idx] = (s, pct)
    out = {}
    for idx in set(best) | set(addons):
        main = best.get(idx)
        extra = addons.get(idx, [])
        pcts = ([main[1]] if main else []) + [p for _s, p in extra]
        first = main[0] if main else extra[0][0]
        out[idx] = {"pct": combine(*pcts), "scheme_id": first.id,
                    "names": ([main[0].name] if main else []) + [s.name for s, _p in extra]}
    return out, awards


def preview_later(lines, company_id=None, location_id=None, agreement_code=None, when=None):
    """What the bill being built will get when it is paid — for the counter to
    say out loud: the On Settlement discounts and the coupons for next time."""
    bill_value = sum(l.charged for l in lines)
    out = []
    for s in live_schemes(AT_SETTLEMENT + AT_THE_COUNTER, company_id, location_id, when):
        a = award(s, lines, bill_value, agreement_code)
        if a is None:
            continue
        if s.applicable in AT_THE_COUNTER:
            if a.benefit.mode in PAID_LATER and (a.coupon_value > 0.005 or a.points > 0.005):
                a.amount = 0.0
                out.append(a)
        elif a.amount > 0.005 or a.coupon_value > 0.005 or a.points > 0.005:
            out.append(a)
    return out


def agreement_schemes(code, company_id=None, location_id=None, when=None):
    """The live schemes that ask for this agreement code."""
    want = _norm(code)
    if not want:
        return []
    out = []
    for s in live_schemes([c for c, _l in SCHEME_APPLICABLE_CODES], company_id, location_id, when):
        for f in s.filters:
            if f.mode == "agreement_code" and want in {_norm(c) for c in _data(f).get("codes") or []}:
                out.append(s)
                break
    return out


# --------------------------------------------------------------------------
# when the bill is paid
# --------------------------------------------------------------------------

def invoice_lines(inv):
    """A raised bill's paid lines, as the engine reads them."""
    out = []
    for n, it in enumerate(inv.items):
        if it.promo_role == "reward":
            continue
        before = it.list_price if it.list_price is not None else (it.unit_price or 0)
        # The hand-given part of the line's discount: all of it, less any scheme's.
        manual = 0.0
        if it.list_price:
            total_keep = (it.unit_price or 0) / it.list_price
            scheme_keep = 1 - (it.scheme_pct or 0) / 100.0
            manual = max(0.0, (1 - total_keep / scheme_keep) * 100) if scheme_keep > 0 else 0.0
        l = Line(n, it.product, it.quantity, before, it.gst_rate, manual)
        # what the line actually came to, scheme and all
        l.charged = (it.line_total or 0) + (it.tax_amount or 0)
        out.append(l)
    return out


def settlement_awards(inv, when=None):
    """What On Settlement / Next Bill / Next Day Bill schemes give this bill."""
    lines = invoice_lines(inv)
    out = []
    for s in live_schemes(AT_SETTLEMENT + AT_THE_COUNTER, inv.company_id, inv.location_id, when):
        a = award(s, lines, inv.total or 0, inv.agreement_code)
        if a is None:
            continue
        if s.applicable in AT_THE_COUNTER:
            # the counter already took its discount; what is left to pay out at
            # settlement is a coupon or reward points
            if a.benefit.mode in PAID_LATER and (a.coupon_value > 0.005 or a.points > 0.005):
                a.amount = 0.0
                out.append(a)
        elif a.amount > 0.005 or a.coupon_value > 0.005 or a.points > 0.005:
            out.append(a)
    return out


def settlement_discount(awards, total):
    """₹ off the bill now, from the On Settlement awards, never more than it is."""
    # A Coupon benefit is always a coupon, never money off now — its `amount` is 0.
    off = sum(a.amount for a in awards if a.scheme.applicable == "settlement")
    return round(min(max(0.0, off), total or 0), 2)


def _campaign_for(benefit, scheme, worth, days):
    """The coupon campaign a scheme's coupon is issued from.

    A coupon is worth what its campaign says, so a Coupon benefit — one fixed
    value — has one campaign, made the first time and kept in step with the
    row. A Next Bill scheme's coupon is worth what THIS bill earned, which is
    different every time, so each gets a campaign of its own: sharing one and
    changing its value would change every coupon already in a customer's hand.
    """
    name = f"{scheme.code} · {scheme.name}"[:128]
    if benefit.mode == "coupon":
        c = benefit.campaign if benefit.campaign_id else None
        if c is None:
            c = CouponCampaign(name=name, kind="amount", uses_per_coupon=1,
                               issue_at_settlement=False, active=True)
            db.session.add(c)
            db.session.flush()
            benefit.campaign_id = c.id
        c.value, c.valid_days = worth, days
        return c
    c = CouponCampaign(name=name, kind="amount", value=worth, valid_days=days,
                       uses_per_coupon=1, issue_at_settlement=False, active=True)
    db.session.add(c)
    db.session.flush()
    return c


def pay_out(inv, awards, user_id=None, today=None):
    """Record the settlement-stage schemes on a bill being paid.

    The On Settlement discount has already been taken off the total by the
    caller; this writes what each scheme did, and issues the coupons the Next
    Bill / Next Day Bill schemes (and every Coupon benefit) earned. Returns the
    coupons issued.
    """
    today = today or date.today()
    issued = []
    for a in awards:
        s, b = a.scheme, a.benefit
        coupon = None
        if b.mode == "points":
            # credited to the customer on the bill — a walk-in earns nothing
            cust = Customer.query.get(inv.customer_id) if inv.customer_id else None
            if cust is not None and a.points > 0:
                cust.loyalty_points = (cust.loyalty_points or 0) + a.points
                db.session.add(LoyaltyTxn(customer_id=cust.id, points=a.points,
                                          reason=f"scheme {s.code}", invoice_id=inv.id))
                db.session.add(SalesSchemeUse(scheme_id=s.id, invoice_id=inv.id,
                                              stage="settlement", benefit_value=a.points))
            continue
        worth = a.coupon_value if b.mode == "coupon" else (
            round(a.amount, 2) if s.applicable in ("next_bill", "next_day_bill") else 0.0)
        if worth > 0.005:
            days = max(1, int(b.additional or 0) if b.mode == "coupon" and b.additional else 30)
            camp = _campaign_for(b, s, worth, days)
            start = today + timedelta(days=1) if s.applicable == "next_day_bill" else today
            coupon = Coupon(code=vouchers._new_code("SC"), campaign_id=camp.id,
                            customer_id=inv.customer_id, valid_from=start,
                            valid_to=start + timedelta(days=days - 1),
                            uses_allowed=1, issued_via="scheme",
                            issued_invoice_id=inv.id, created_by_id=user_id)
            db.session.add(coupon)
            db.session.flush()
            issued.append(coupon)
        db.session.add(SalesSchemeUse(
            scheme_id=s.id, invoice_id=inv.id,
            stage="next_bill" if s.applicable in ("next_bill", "next_day_bill") else "settlement",
            benefit_value=round(worth if coupon else a.amount, 2),
            coupon_id=coupon.id if coupon else None))
    return issued


def describe(scheme):
    """One line a person can read about what a scheme does."""
    parts = []
    for b in scheme.benefits:
        v, x = b.value or 0, b.additional or 0
        what = {"discount_pct": f"{v:g}% off" + (f" + {x:g}%" if x else ""),
                "fixed": f"₹{v:g} off" + (" (flat)" if b.flat else " a piece"),
                "points": f"{v:g} reward points",
                "combo": f"₹{v:g} off " + ("once" if b.flat else f"every {int(b.qty_from or 1)}"),
                "discounted_item": f"{v:g}% off {b.item_code or 'an item'}",
                "flat": f"₹{v:g} off" + (f" + {x:g}%" if x else ""),
                "free_item": f"buy {int(b.qty_from or 1)} get {int(v)} free",
                "coupon": f"₹{v:g} coupon"}.get(b.mode, b.mode)
        rng = []
        if b.qty_from or b.qty_to:
            rng.append(f"qty {b.qty_from or 0:g}–{b.qty_to:g}" if b.qty_to else f"qty {b.qty_from:g}+")
        if b.price_from or b.price_to:
            rng.append(f"₹{b.price_from or 0:g}–{b.price_to:g}" if b.price_to else f"₹{b.price_from:g}+")
        parts.append(what + (f" ({', '.join(rng)})" if rng else ""))
    return "; ".join(parts) or "no benefit set"


def filter_label(mode):
    return dict(SCHEME_FILTER_MODES).get(mode, mode)


def filter_summary(f):
    """What one filter row says, for the screen's middle table."""
    d = _data(f) if not isinstance(f, dict) else f.get("data", {})
    mode = f.mode if not isinstance(f, dict) else f.get("mode")
    if mode in ("product_attr", "ex_product_attr"):
        return ", ".join(f"{label}: {d[k]}" for k, label, _f in ATTRIBUTES if d.get(k))
    if mode == "invoice":
        return ", ".join(f"{k.replace('_', ' ').title()}: {d[k]}"
                         for k in ("supplier", "invoice", "design_code", "invoice_date") if d.get(k))
    if mode in ("barcode", "ex_barcode", "agreement_code"):
        codes = d.get("codes") or []
        return f"{len(codes)} code(s): " + ", ".join(codes[:5]) + ("…" if len(codes) > 5 else "")
    if mode in ("marker", "ex_marker"):
        return ", ".join(d.get("markers") or [])
    if mode == "barcode_prefix":
        return ", ".join(d.get("prefixes") or [])
    if mode == "bill_value":
        return f"₹{d.get('min') or 0} – " + (f"₹{d['max']}" if d.get("max") else "no limit")
    if mode == "ex_discount":
        return "items already discounted by hand"
    return ""
