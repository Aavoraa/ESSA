"""A customer's GSTIN, and what it makes of a bill.

A bill to a customer who gives a GSTIN is a B2B sale; with none it is B2C. That
is the whole rule, and it decides which section of the GST return the bill lands
in (B2B is reported invoice by invoice, against the buyer's GSTIN), so a GSTIN is
checked properly before it is accepted rather than stored as typed:

  * the shape: 2-digit state, 10-character PAN, entity number, "Z", check digit
  * the check digit itself (the GSTN's base-36 checksum), which catches the one
    mistyped character a shape check lets through

The first two digits are the buyer's state, so a GSTIN also settles the customer's
state code — and with it whether the bill is inter-state (IGST) or not.
"""
import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _check_char(first14):
    total = 0
    for i, ch in enumerate(first14):
        v = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _CHARS[(36 - total % 36) % 36]


def clean(raw):
    """(gstin, error). Blank is fine — the GSTIN is optional — and gives ("", None)."""
    g = re.sub(r"\s+", "", raw or "").upper()
    if not g:
        return "", None
    if len(g) != 15 or not _SHAPE.match(g):
        return None, "That GSTIN is not in the right format (15 characters, e.g. 33AAAAA0000A1Z9)."
    if not 1 <= int(g[:2]) <= 38 and g[:2] not in ("97", "99"):
        return None, f"That GSTIN starts with {g[:2]}, which is not a state code."
    if _check_char(g[:14]) != g[14]:
        return None, "That GSTIN's last character does not match — check it for a typing mistake."
    return g, None


def state_code(gstin):
    """The state a GSTIN is registered in, e.g. "33" for Tamil Nadu."""
    return gstin[:2] if gstin else None
