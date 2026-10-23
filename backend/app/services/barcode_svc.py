"""
QR + label service.

A product has ONE identifier of ours: the `sku` (ESSA-00001). It is what the SKU
column shows, what prints under the QR on a label, and what a scan resolves to.

`Product.barcode` still exists but is no longer ours to issue. It holds the code
the SUPPLIER printed, when they print one, so `inventory.match_product` can key a
re-buy onto the product it already created — which is what stops one item's
weighted-average cost splitting across two records. It is never presented as this
product's code, and nothing generates it. We used to mint an internal EAN-13 there
so the label could carry a 1D stripe; once the label went QR-only that second
number bought nothing and gave two answers to "what is this product's code".

Labels and the Inventory list are QR-only:

  * QR — carries the whole product record (see qr_payload), so a phone camera or
    2D imager gets every attribute in one scan, and still works with no network.
    Owning the label alone lets it print bigger, which is what makes it scan
    first time in warehouse light.

The trade-off this accepts: a pure 1D laser handheld cannot read a 2D code at
all, so scanners that aren't 2D imagers can only be used by keying in the printed
SKU. `barcode_svg` is kept (and still served at /barcode.svg) for anyone who
needs a stripe back — it renders whatever code it is handed.

The QR payload is compact JSON, not a URL: a URL would need the server reachable
and would carry no attributes of its own. Scanning resolves the embedded `sku`
against the live database so details are always current, and falls back to the
attributes baked into the code when offline — which is the point of putting them
there. `parse_qr_payload` + `resolve` accept a scanned payload anywhere a bare
code is accepted, so nothing downstream needs to know which symbology was used.
"""
import io
import json
import re
from .. import models

try:
    import barcode as _barcode
    from barcode.writer import SVGWriter
    _HAVE_BARCODE = True
except Exception:                      # pragma: no cover
    _HAVE_BARCODE = False

try:
    import segno as _segno
    _HAVE_QR = True
except Exception:                      # pragma: no cover
    _HAVE_QR = False


def _ean13_check_digit(d12: str) -> str:
    s = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12))
    return str((10 - s % 10) % 10)


def internal_ean13(product_id: int) -> str:
    """Deterministic internal EAN-13 for a product id (prefix 2 = in-store).

    No longer issued — kept so a barcode minted by an earlier build can still be
    recognised as ours rather than mistaken for a supplier's code."""
    body = "2" + f"{product_id:011d}"          # 12 digits
    return body + _ean13_check_digit(body)     # 13 digits


def _next_sku(db) -> str:
    """One SKU rule, not two. This used to be a second copy of the format string
    in services/inventory — the kind of duplication that drifts silently."""
    from . import inventory
    return inventory._next_sku(db)


def assign_identifiers(db, product) -> dict:
    """Give a product its SKU. Returns {sku, barcode, generated:[...]}.

    The SKU is the only identifier we issue. We used to also mint an internal
    EAN-13 so the label could carry a 1D stripe; the label is QR-only now, and
    the QR resolves on `id`/`sku`, so a second number of our own bought nothing
    and gave two answers to "what is this product's code".

    `product.barcode` is untouched here on purpose. It holds the code the
    SUPPLIER printed, when they print one, and `inventory.match_product` keys a
    re-buy on it — which is what keeps one product's weighted-average cost in one
    place. It is never shown as our code and never generated."""
    generated = []
    if not product.sku:
        product.sku = _next_sku(db)
        generated.append("sku")
    if product.id is None:
        db.flush()
    db.flush()
    return {"sku": product.sku, "barcode": product.barcode, "generated": generated}


# ---- QR payload -------------------------------------------------------------
#
# Positional pipe-delimited, NOT JSON. This is a physical constraint, not a style
# preference: a QR's module count grows with its payload, and the printed module
# size is what decides whether a phone camera can read it. The same record as
# JSON runs ~262 bytes -> a 65-module (version 12) symbol -> 0.25mm per module on
# a 17mm label, below the ~0.33mm a phone needs in warehouse light. Dropping the
# key names and quotes roughly halves it to ~130 bytes -> ~45 modules -> 0.4mm+,
# which scans first time. JSON would be the nicer format if the label were bigger.
#
# `E1` is the schema tag. Fields are append-only: a new attribute goes on the END,
# never in the middle, so a label printed last year still decodes correctly.
QR_SCHEMA = 1
QR_TAG = "E1"

# payload position -> (dict key, Product attribute). ORDER IS THE FORMAT.
QR_ORDER = [
    ("id", "id"), ("sku", "sku"), ("bc", "barcode"), ("d", "description"),
    ("hsn", "hsn"), ("uom", "uom"), ("mrp", "mrp"), ("sp", "sale_price"),
    ("cat", "category"), ("sec", "category_section"), ("ty", "product_type"),
    ("sz", "size"), ("col", "color"), ("pat", "pattern"), ("fit", "fit"),
    ("mat", "material"), ("dsn", "design_no"),
    # appended, never inserted — a label printed before these existed decodes
    # exactly as it did, because every field it carried is still in the same place
    ("brd", "brand"), ("sty", "style"), ("slv", "sleeve"),
]


def _num(v):
    """Trim pointless decimals — '499' not '499.0' saves bytes on every label."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _qesc(v):
    """Escape a value for the pipe-delimited QR payload."""
    return _num(v).replace("\\", "\\\\").replace("|", "\\|")


def _split_escaped(s):
    """Split on unescaped '|' only, honouring backslash escapes."""
    out, cur, esc = [], [], False
    for ch in s:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def qr_payload(p) -> str:
    """The complete product record, as the compact string printed into the QR.

    Blank attributes collapse to an empty field, so a sparsely-detailed product
    produces a small, low-density code."""
    vals = []
    for _, attr in QR_ORDER:
        v = getattr(p, attr, None)
        vals.append("" if v in (None, "") else _qesc(v))
    # trailing empties carry no information — drop them to shrink the symbol
    while vals and vals[-1] == "":
        vals.pop()
    return "|".join([QR_TAG] + vals)


# ---- per-piece (unit) labels -------------------------------------------------
#
# A third tag, `EU1`, for the code on an individual garment. It carries its own
# unit code AND the sku it belongs to, so one scan answers both "which piece" and
# "which product" — and `resolve()` below returns the product for it, which is what
# keeps every existing scan point (GRN linking, lookup, outward) working when
# someone scans a piece label instead of a shelf one.
UNIT_TAG = "EU1"
UNIT_ORDER = [
    ("u", "code"), ("sku", "_sku"), ("id", "product_id"), ("d", "_description"),
    ("sz", "_size"), ("col", "_color"), ("mrp", "_mrp"),
]


def _unit_field(u, attr):
    if not attr.startswith("_"):
        return getattr(u, attr, None)
    p = u.product
    return getattr(p, attr[1:], None) if p else None


def unit_qr_payload(u) -> str:
    """What one garment's QR carries: which piece it is, which SKU it belongs to,
    and enough of the record to read it with no network."""
    vals = []
    for _, attr in UNIT_ORDER:
        v = _unit_field(u, attr)
        vals.append("" if v in (None, "") else _qesc(v))
    while vals and vals[-1] == "":
        vals.pop()
    return "|".join([UNIT_TAG] + vals)


def parse_unit_payload(text):
    """Decode a scanned per-piece payload, or None if it isn't one."""
    if not text:
        return None
    s = str(text).strip()
    if not s.startswith(UNIT_TAG + "|"):
        return None
    parts = _split_escaped(s)[1:]
    d = {"kind": "unit"}
    for (key, _), val in zip(UNIT_ORDER, parts):
        if val != "":
            d[key] = val
    return d


def unit_qr_svg(u, scale: int = 3) -> str:
    return qr_svg(unit_qr_payload(u), scale=scale)


def unit_qr_png(u, scale: int = 6) -> bytes:
    return qr_png(unit_qr_payload(u), scale=scale)


def _resolve_unit_row(db, code):
    """The ProductUnit a scanned piece label refers to, or None."""
    payload = parse_unit_payload(code)
    if payload:
        code = payload.get("u") or ""
    code = str(code or "").strip()
    if not code:
        return None
    return db.query(models.ProductUnit).filter(
        models.ProductUnit.code.ilike(code)).first()


def _unit_label_card(u) -> str:
    """One piece's tag. Identical to the SKU tag except that the code is this
    garment's own — which is the whole point of it."""
    p = u.product
    g = lambda a: getattr(p, a, None) if p else None       # noqa: E731
    return _garment_card(
        unit_qr_svg(u, scale=3), u.code, g("sku"), g("description"),
        g("size"), g("color"), g("material"), g("category"),
        f"₹{p.mrp:g}" if p and p.mrp else "", _unit_note(p))


def unit_labels_sheet(units) -> str:
    """Print-ready sheet of PER-PIECE labels.

    Same shape as the SKU label so a picker sees one kind of garment tag, but the
    code under each QR is that piece's own — which is the whole point: eight
    garments off one SKU leave the warehouse with eight distinguishable tags
    instead of eight copies of the same one."""
    return _garment_sheet("Piece labels", "piece label",
                          [_unit_label_card(u) for u in units])


# ---- bundle labels ----------------------------------------------------------
#
# A carton's label carries its OWN tag, `EB1`, not the product one. The two answer
# different questions — "which box is this?" versus "which garment is this?" — and
# a scanner that cannot tell them apart would happily dispatch a carton as if it
# were one shirt. The tag makes that impossible: `resolve()` below returns products
# and refuses a bundle payload outright, so the mistake surfaces as "that is a
# bundle code" instead of silently moving the wrong stock.
BUNDLE_TAG = "EB1"
BUNDLE_PREFIX = "ESSA-B-"


def _is_bundle_code(code) -> bool:
    return str(code or "").strip().upper().startswith(BUNDLE_PREFIX)

BUNDLE_ORDER = [
    ("code", "code"), ("d", "description"), ("qty", "qty"), ("uom", "uom"),
    ("items", "item_count"), ("grn", "grn_no"), ("inv", "invoice_number"),
    ("loc", "location"),
]


def bundle_qr_payload(b) -> str:
    """What a carton's QR carries: enough to identify the box on a rack with no
    network — what is in it, how many, and which receipt it came from."""
    vals = []
    for _, attr in BUNDLE_ORDER:
        v = getattr(b, attr, None)
        vals.append("" if v in (None, "") else _qesc(v))
    while vals and vals[-1] == "":
        vals.pop()
    return "|".join([BUNDLE_TAG] + vals)


def bundle_qr_svg(b, scale: int = 3) -> str:
    return qr_svg(bundle_qr_payload(b), scale=scale)


def bundle_qr_png(b, scale: int = 6) -> bytes:
    return qr_png(bundle_qr_payload(b), scale=scale)


def parse_bundle_payload(text):
    """Decode a scanned BUNDLE payload, or None if it isn't one."""
    if not text:
        return None
    s = str(text).strip()
    if not s.startswith(BUNDLE_TAG + "|"):
        return None
    parts = _split_escaped(s)[1:]
    d = {"kind": "bundle"}
    for (key, _), val in zip(BUNDLE_ORDER, parts):
        if val != "":
            d[key] = val
    return d


def parse_qr_payload(text):
    """Decode a scanned PRODUCT payload into {key: value}, or None if it isn't one.

    Accepts the compact `E1|...` form and the earlier JSON form, so labels printed
    before the format changed still scan. Deliberately forgiving: anything
    unrecognised returns None and the caller treats the input as a bare code."""
    if not text:
        return None
    s = str(text).strip()

    if s.startswith(QR_TAG + "|"):
        parts = _split_escaped(s)[1:]
        d = {"v": QR_SCHEMA}
        for (key, _), val in zip(QR_ORDER, parts):
            if val != "":
                d[key] = val
        return d

    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
        except Exception:
            return None
        return d if isinstance(d, dict) and "v" in d else None

    return None


# The quiet zone is part of the symbol, not decoration. ISO/IEC 18004 requires four
# clear modules on every side; a decoder uses that margin to find the symbol's edge
# at all, so a code printed with less is not a slightly worse code, it is one a
# scanner may never locate. It lives here rather than in each stylesheet so it
# travels with the symbol everywhere it is drawn — sheet, screen, phone PNG.
QUIET_ZONE = 4

#: the root width/height, with an optional CSS unit — segno writes bare pixels,
#: python-barcode writes millimetres
_SVG_HEAD_RE = re.compile(
    r'<svg([^>]*?)\bwidth="([\d.]+)(mm|cm|in|pt|pc|px)?"\s+'
    r'height="([\d.]+)(mm|cm|in|pt|pc|px)?"')

#: CSS absolute units, in px — the user-unit space a viewBox is measured in
_UNIT_PX = {"": 1.0, "px": 1.0, "mm": 96 / 25.4, "cm": 960 / 25.4,
            "in": 96.0, "pt": 96 / 72, "pc": 16.0}


def _scalable(svg: str) -> str:
    """Give a symbol a viewBox and crisp edges.

    Neither generator emits one. An SVG without a viewBox has no mapping from
    user units to its viewport, so CSS `width: 32mm` resizes the viewport and
    leaves the drawing at its intrinsic size — and because the outermost svg
    clips by default, the overflow is silently cut off rather than scaled. A
    41-module symbol rendered at scale 3 is 135px of drawing inside a 98px box:
    the outer quarter of the code, finder pattern and all, simply is not there.
    It looks like a QR and cannot be decoded.

    The viewBox is in USER UNITS, which are px — so a root sized in millimetres
    (python-barcode writes `width="33.000mm"`, and its bars are placed in mm too)
    has to be converted, or the box describes a region a quarter the size of the
    drawing and clips exactly as badly.

    `crispEdges` turns off antialiasing on the module boundaries. At label sizes a
    softened edge costs real contrast, and contrast is what a phone camera in
    warehouse light has least of."""
    if "viewBox" in svg:
        return svg
    m = _SVG_HEAD_RE.search(svg)
    if not m:
        return svg
    attrs, w, wu, h, hu = m.groups()
    vw = float(w) * _UNIT_PX.get(wu or "", 1.0)
    vh = float(h) * _UNIT_PX.get(hu or "", 1.0)
    return svg.replace(
        m.group(0),
        f'<svg{attrs}width="{w}{wu or ""}" height="{h}{hu or ""}" '
        f'viewBox="0 0 {vw:g} {vh:g}" '
        f'shape-rendering="crispEdges" preserveAspectRatio="xMidYMid meet"',
        1)


def qr_svg(payload: str, scale: int = 3) -> str:
    """Render a payload as an inline SVG QR code.

    Error correction 'M' (~15% recoverable) is the retail-label default: enough to
    survive a scuffed or partly peeled sticker without inflating the module count
    the way 'Q'/'H' would."""
    payload = str(payload or "")
    if _HAVE_QR and payload:
        try:
            qr = _segno.make_qr(payload, error="m")
            # segno writes bytes, so this needs a BytesIO — a StringIO raises.
            # svgns=True keeps the xmlns, which the /qr.svg response requires and
            # which is harmless when the same markup is inlined into a label.
            buf = io.BytesIO()
            qr.save(buf, kind="svg", scale=scale, border=QUIET_ZONE,
                    xmldecl=False, svgns=True)
            return _scalable(buf.getvalue().decode())
        except Exception:
            pass
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" '
            'viewBox="0 0 80 80">'
            '<rect width="80" height="80" fill="#eee"/>'
            '<text x="6" y="44" font-family="monospace" font-size="9">no QR lib</text></svg>')


def qr_module_size_mm(payload: str, box_mm: float) -> float:
    """How many millimetres one module gets when this payload is drawn at `box_mm`.

    The number that decides whether a label scans. Roughly 0.33mm is the floor for
    a phone camera in warehouse light; the sizes chosen for the sheets below keep
    the worst realistic payload comfortably above it. Exposed so a test can assert
    that rather than trusting a comment."""
    if not (_HAVE_QR and payload):
        return 0.0
    n = _segno.make_qr(str(payload), error="m").symbol_size(border=QUIET_ZONE)[0]
    return box_mm / n if n else 0.0


def product_qr_svg(p, scale: int = 3) -> str:
    return qr_svg(qr_payload(p), scale=scale)


def qr_png(payload: str, scale: int = 6) -> bytes:
    """The same QR as a PNG. Returns b"" when the QR library is unavailable.

    Exists for the warehouse phone app: React Native's <Image> renders PNG/JPEG
    and cannot draw an SVG without pulling in a native renderer, so the label a
    picker checks on screen has to arrive as raster. Same payload and the same
    'M' error correction as the printed label — it is the identical code, only
    rendered for a screen rather than a sticker."""
    payload = str(payload or "")
    if _HAVE_QR and payload:
        try:
            buf = io.BytesIO()
            _segno.make_qr(payload, error="m").save(
                buf, kind="png", scale=scale, border=QUIET_ZONE)
            return buf.getvalue()
        except Exception:
            pass
    return b""


def product_qr_png(p, scale: int = 6) -> bytes:
    return qr_png(qr_payload(p), scale=scale)


def resolve(db, code):
    """Fetch a product by QR payload, barcode, sku, or numeric id — the
    system-wide lookup.

    A scanned QR resolves by the identifiers inside it (id, then sku, then
    barcode) so the caller always gets the LIVE record rather than the snapshot
    printed on the label."""
    if code is None:
        return None
    code = str(code).strip()
    if not code:
        return None

    # a carton is not a garment: never let a bundle label resolve to one of the
    # products inside it, however tempting the fallbacks below would make it
    if parse_bundle_payload(code) or _is_bundle_code(code):
        return None

    # a per-piece code IS a garment — the one whose SKU it hangs off. Resolving it
    # here is what lets every existing scan point (GRN linking, lookup, outward)
    # accept a label off an individual item without knowing units exist.
    unit = _resolve_unit_row(db, code)
    if unit:
        return unit.product

    payload = parse_qr_payload(code)
    if payload:
        for getter in (lambda: db.get(models.Product, int(payload["id"]))
                       if str(payload.get("id", "")).isdigit() else None,
                       lambda: db.query(models.Product).filter(
                           models.Product.sku == payload.get("sku")).first()
                       if payload.get("sku") else None,
                       lambda: db.query(models.Product).filter(
                           models.Product.barcode == payload.get("bc")).first()
                       if payload.get("bc") else None):
            try:
                p = getter()
            except Exception:
                p = None
            if p:
                return p
        return None

    p = db.query(models.Product).filter(models.Product.barcode == code).first()
    if p:
        return p
    p = db.query(models.Product).filter(models.Product.sku == code).first()
    if p:
        return p
    if code.isdigit():
        p = db.get(models.Product, int(code))
        if p:
            return p
    # case-insensitive sku fallback
    return db.query(models.Product).filter(
        models.Product.sku.ilike(code)).first()


def barcode_svg(code: str) -> str:
    """Render a code as an SVG barcode. 13-digit numeric → EAN-13, else Code128.
    Falls back to a plain text SVG if the barcode lib is unavailable."""
    code = str(code or "").strip()
    if _HAVE_BARCODE and code:
        try:
            sym = "ean13" if (code.isdigit() and len(code) in (12, 13)) else "code128"
            klass = _barcode.get_barcode_class(sym)
            obj = klass(code, writer=SVGWriter())
            buf = io.BytesIO()
            obj.write(buf, options={"module_height": 12.0, "font_size": 8,
                                    "text_distance": 3.0, "quiet_zone": 2.0})
            svg = buf.getvalue().decode()
            # strip the XML prolog so it can be inlined inside HTML
            i = svg.find("<svg")
            # and give it a viewBox, for the same reason the QR gets one: the
            # designer draws this into a box of its own choosing, and without
            # one the stripe is cut off at whatever the box happens to be
            return _scalable(svg[i:] if i >= 0 else svg)
        except Exception:
            pass
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="40">' \
           f'<text x="4" y="24" font-family="monospace" font-size="14">{code}</text></svg>'


def _hesc(s) -> str:
    """Escape for HTML text — descriptions carry & and < from supplier bills."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _unit_note(p) -> str:
    """"PAIR · 2 pcs" — but only when one of these is more than one thing.

    A tag on a pillow cover has to say it covers a pair, or the person holding it
    counts two articles where the system counts one. A handkerchief needs no such
    note: the tag is on the handkerchief."""
    if not p:
        return ""
    per = float(getattr(p, "pieces_per_unit", 1) or 1)
    code = getattr(p, "unit_type", None) or getattr(p, "uom", None)
    if per <= 1 or not code:
        return ""
    return f"{code} · {per:g} pcs"


def _garment_card(qr_svg_markup, code, sku, description, size, color, material,
                  category, mrp, unit="") -> str:
    """One garment tag. The SKU label and the per-piece label differ only in the
    code beneath the symbol, so they are built here and the warehouse only ever
    learns to read one thing.

    Text is capped generously and then clipped by CSS rather than by character
    count: only the browser knows how wide 'Cotton Saree' actually renders, and a
    count that guesses either truncates a name that would have fitted or lets one
    through that doesn't.

    `sku` and `code` are separate on purpose. On a piece tag they differ — the foot
    says which product this is (ESSA-00002) and the line under the symbol says
    which of its garments (ESSA-00002-101) — and losing either leaves someone
    holding a tag that answers only half the question."""
    bits = [b for b in (size, color, material, unit) if b]
    # size first and in bold — on a rack it is the field someone reads before any
    # other, and it is the one a mis-pick turns on
    meta = ""
    if bits:
        head, rest = bits[0], bits[1:]
        meta = f"<b>{_hesc(head)}</b>" + (" · " + _hesc(" · ".join(str(b) for b in rest)) if rest else "")
    return f"""
    <div class="label">
      <div class="head">
        <div class="txt">
          <div class="name">{_hesc((description or '')[:48])}</div>
          <div class="meta">{meta}</div>
          <div class="cat">{_hesc((category or '')[:34])}</div>
          <div class="foot">
            <span class="sku">{_hesc(sku or code or '')}</span>
            <span class="mrp">{_hesc(mrp)}</span>
          </div>
        </div>
        <div class="qr">
          {qr_svg_markup}
          <div class="code">{_hesc(code or '')}</div>
        </div>
      </div>
    </div>"""


def _label_card(p) -> str:
    # our SKU, never p.barcode — that is the supplier's number, and printing it
    # under our QR would put someone else's code on our label
    return _garment_card(
        product_qr_svg(p, scale=3), p.sku or str(p.id), p.sku, p.description,
        p.size, p.color, p.material, p.category,
        f"₹{p.mrp:g}" if p.mrp else "", _unit_note(p))


def bundle_labels_sheet(bundles) -> str:
    """Print-ready sheet of CARTON labels — one per bundle, bigger than a garment
    tag because it is read across a rack rather than held in the hand.

    Deliberately shows the mix (10 S · 20 M · 15 L) rather than a single size: the
    whole point of the carton label is to say what is inside without opening it."""
    def card(b):
        prods = b.products
        mix = " · ".join(
            f"{(s.qty or 0):g} {s.variant_label or '—'}"
            for s in (b.line.splits if b.line and b.line.is_split else [])) \
            or f"{(b.qty or 0):g} {_hesc(b.uom or 'PCS')}"
        skus = ", ".join(p.sku for p in prods if p.sku)
        return f"""
    <div class="blabel">
      <div class="top">
        <div class="txt">
          <div class="bname">{_hesc((b.description or '')[:40])}</div>
          <div class="qty">{(b.qty or 0):g} {_hesc(b.uom or 'PCS')}
            <span class="items">· {b.item_count} item{'' if b.item_count == 1 else 's'}</span></div>
          <div class="mix">{_hesc(mix)}</div>
          <div class="grn">GRN {_hesc(b.grn_no or '—')} · Inv {_hesc(b.invoice_number or '—')}</div>
          <div class="grn">{_hesc(b.supplier.name if b.supplier else '')}</div>
        </div>
        <div class="bqr">
          {bundle_qr_svg(b, scale=3)}
          <div class="bcode">{_hesc(b.code)}</div>
        </div>
      </div>
      <div class="loc">LOCATION: {_hesc(b.location or '________________')}</div>
      <div class="skus">{_hesc(skus[:90])}</div>
    </div>"""

    cards = "".join(card(b) for b in bundles)
    n = len(bundles)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Bundle labels ({n})</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 10px; color: #000; }}
  .bar {{ margin: 0 0 10px; }}
  .bar button {{ padding: 8px 16px; font-size: 14px; cursor: pointer; }}
  .hint {{ font-size: 12px; color: #555; margin: 0 0 10px; }}
  .sheet {{ display: flex; flex-wrap: wrap; gap: 4mm; }}
  .blabel {{ width: 110mm; box-sizing: border-box; padding: 4mm 5mm; background: #fff;
             border: .6mm solid #000; border-radius: 2mm;
             page-break-inside: avoid; break-inside: avoid; }}
  .top {{ display: flex; align-items: stretch; gap: 5mm; }}
  .txt {{ flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }}
  /* clipped, every one of them — a carton description off a supplier bill is as
     long as the supplier felt like making it */
  .bname, .mix, .grn, .skus {{
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .bname {{ font-size: 15px; font-weight: 700; line-height: 1.25; min-height: 19px; }}
  .qty {{ font-size: 22px; font-weight: 800; margin-top: 1mm; line-height: 1.1; }}
  .items {{ font-size: 12px; font-weight: 600; color: #444; }}
  .mix {{ font-size: 10.5px; color: #222; margin-top: 1.2mm; min-height: 13px; }}
  .grn {{ font-size: 10.5px; color: #444; margin-top: .8mm; min-height: 13px; }}
  /* 40mm: read across a rack rather than in the hand, and the payload is smaller
     than a garment's, so every module gets ~1mm — legible at arm's length and
     forgiving of a carton that has been dragged along a shelf edge */
  .bqr {{ flex: 0 0 {BUNDLE_QR_MM}mm; display: flex; flex-direction: column;
          align-items: center; background: #fff; }}
  .bqr svg {{ width: {BUNDLE_QR_MM}mm; height: {BUNDLE_QR_MM}mm; display: block; }}
  .bcode {{ font-family: "Courier New", monospace; font-size: 11px; font-weight: 700;
            text-align: center; margin-top: 1.2mm; letter-spacing: -.2px; }}
  .loc {{ margin-top: 2.5mm; border-top: .3mm dashed #999; padding-top: 1.5mm;
          font-size: 13px; font-weight: 700; letter-spacing: .5px; }}
  .skus {{ font-family: "Courier New", monospace; font-size: 8.5px; color: #666;
           margin-top: 1mm; }}
  @media print {{
    .bar, .hint {{ display: none; }}
    * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style></head><body>
  <div class="bar"><button onclick="window.print()">🖨 Print {n} bundle label{'s' if n != 1 else ''}</button></div>
  <div class="hint">Print at 100% / “Actual size”.</div>
  <div class="sheet">{cards}</div>
</body></html>"""


def labels_sheet(products) -> str:
    """A print-ready HTML sheet of product labels (browser → Print).

    Each label carries the QR (full record, for phone/2D scanning) plus the code
    in human-readable digits underneath, for keying in by hand."""
    return _garment_sheet("Product labels", "label",
                          [_label_card(p) for p in products])


#: Printed size of a garment tag's QR. 32mm keeps the worst realistic payload
#: (a long supplier description with every attribute filled — 53 modules plus the
#: 4-module quiet zone) at ~0.52mm per module, and a typical one at ~0.65mm. The
#: floor for a phone camera in warehouse light is around 0.33mm, so this leaves
#: room for thermal-printer bleed and a scuffed sticker rather than sitting on the
#: limit. `test` asserts it; see qr_module_size_mm.
GARMENT_QR_MM = 32
#: Read across a rack rather than in the hand, and carrying a smaller payload.
BUNDLE_QR_MM = 40


def _garment_sheet(title, noun, cards) -> str:
    """The shared garment-tag sheet: same stylesheet whether the code under each QR
    is a SKU or one piece's own, so the warehouse only ever learns one label.

    LAYOUT RULES, all of them about the symbol staying readable:

    * The QR is a fixed column of its own and the details never share it. Every
      text row is clipped with an ellipsis — a single un-clipped row (the old
      `.meta` had no overflow rule) is enough to push a long "S · Orange · Cotton"
      into the code and take the label with it.
    * The 4-module quiet zone is inside the symbol; the flex gap adds ~4mm more of
      guaranteed white beside it, and the QR box paints itself white so a tinted
      label stock or a background rule cannot creep into the margin.
    * Rows have fixed minimum heights, so a product with no category and a product
      with one produce the same card and a sheet of them lines up in a grid — which
      is what makes a page of labels cuttable.
    """
    cards = "".join(cards)
    n = len(cards.split('<div class="label">')) - 1
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title} ({n})</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 10px; color: #000; }}
  .bar {{ margin: 0 0 10px; }}
  .bar button {{ padding: 8px 16px; font-size: 14px; cursor: pointer; }}
  .hint {{ font-size: 12px; color: #555; margin: 0 0 10px; }}
  .sheet {{ display: flex; flex-wrap: wrap; gap: 4mm; }}
  .label {{ width: 76mm; box-sizing: border-box; padding: 3.5mm 4mm;
            border: 1px solid #999; border-radius: 1.5mm; background: #fff;
            page-break-inside: avoid; break-inside: avoid; }}
  /* the QR column and the text column, side by side and never overlapping */
  .head {{ display: flex; align-items: stretch; gap: 4mm; }}
  .txt {{ flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }}
  /* every row clipped: content varies per product, the label must not */
  .name, .meta, .cat, .sku, .mrp {{
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .name {{ font-size: 13px; font-weight: 700; line-height: 1.25; min-height: 16px;
           letter-spacing: -.1px; }}
  .meta {{ font-size: 11px; color: #222; line-height: 1.3; min-height: 14px;
           margin-top: 1mm; }}
  .cat {{ font-size: 9.5px; color: #555; font-family: "Courier New", monospace;
          line-height: 1.3; min-height: 12px; margin-top: .8mm; letter-spacing: -.2px; }}
  /* pinned to the foot of the text column, which stretches to the QR's height —
     so SKU and MRP sit on one baseline across every label on the sheet */
  .foot {{ display: flex; align-items: baseline; justify-content: space-between;
           gap: 3mm; margin-top: auto; padding-top: 1.5mm;
           border-top: .4mm solid #ddd; }}
  .sku {{ font-family: "Courier New", monospace; font-size: 11px; font-weight: 700;
          letter-spacing: -.2px; }}
  .mrp {{ font-size: 13px; font-weight: 700; flex: 0 0 auto; }}
  /* white behind the symbol so nothing can tint the quiet zone */
  .qr {{ flex: 0 0 {GARMENT_QR_MM}mm; display: flex; flex-direction: column;
         align-items: center; background: #fff; }}
  .qr svg {{ width: {GARMENT_QR_MM}mm; height: {GARMENT_QR_MM}mm; display: block; }}
  /* clear of the quiet zone — this text must not be the first thing a decoder
     finds when it looks for the symbol's lower edge */
  .code {{ font-family: "Courier New", monospace; font-size: 9px; font-weight: 700;
           text-align: center; letter-spacing: -.2px; margin-top: 1.2mm;
           max-width: {GARMENT_QR_MM}mm; overflow: hidden; white-space: nowrap; }}
  @media print {{
    .bar, .hint {{ display: none; }}
    .label {{ border-color: #000; }}
    /* keep the white QR backing when the browser strips backgrounds */
    * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style></head><body>
  <div class="bar"><button onclick="window.print()">🖨 Print {n} {noun}{'s' if n != 1 else ''}</button></div>
  <div class="hint">Print at 100% / “Actual size”.</div>
  <div class="sheet">{cards}</div>
</body></html>"""
