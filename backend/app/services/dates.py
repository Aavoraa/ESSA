"""
One date format, everywhere: ISO `YYYY-MM-DD`.

Dates arrive in this system in whatever form the page they came from used. A
supplier prints 31/07/2026, a register page is written 31-7-26, an e-invoice
carries 2026-07-31, and a human types whatever they type. They were all stored
verbatim, and that quietly broke three things:

  * **Range search.** `GET /api/lr/search?date_from=` compares in SQL — a plain
    string comparison on `recv_date`. That is only correct on ISO text: with
    "31/07/2026" in the column, "01/08/2026" sorts *before* it and a July–August
    filter returns the wrong rows. No error, just a shorter list than the truth.
  * **Conflict detection.** `lr_link` flags a register row whose invoice date
    disagrees with the linked invoice. Two spellings of the same day compared as
    text disagree, so a correct pairing gets reported as a mismatch.
  * **Ageing.** `payments._parse_date` knows three formats; anything else falls to
    None and the bill silently loses its "days outstanding".

So dates are normalised as they are written. Anything readable becomes ISO;
anything unreadable is **left exactly as it came** rather than dropped, because a
date this module cannot parse is still evidence of what was on the page, and
silently blanking a supplier's own figure is worse than storing it oddly.

AMBIGUITY: 03/04/2026 is the 3rd of April here, not the 4th of March. Every
supplier and register in this business writes day-first, and the reference
recordings are Indian. Where the first number is above 12 the reading is forced
anyway; where a year appears first the order is unambiguous. Only genuinely
ambiguous day/month pairs use the day-first assumption, and that assumption is
stated here rather than buried in a format list.
"""
import datetime as dt
import functools
import re

ISO = "%Y-%m-%d"

#: Tried in order. Day-first before month-first — see the note above.
_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",          # unambiguous, year leading
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",          # the common Indian forms
    "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y",
    "%d %b %y", "%d-%b-%y", "%d %B %y", "%d-%B-%y",   # "15-Dec-26", off a register
    "%b %d %Y", "%B %d %Y",
    "%d%m%Y",
)

#: A day/month pair we would otherwise read day-first, but cannot be one.
_MONTH_FIRST = ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y")

_CLEAN_RE = re.compile(r"[‐-―−]")     # assorted unicode dashes
_SEP_RUN_RE = re.compile(r"\s+")


def to_iso(value):
    """`YYYY-MM-DD`, or None when the text is not a date this can read.

    None is the honest answer for "3l/07/26" (a lowercase L for a 1, straight off
    an OCR pass). The caller decides what to do with it; this does not guess."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()

    s = _SEP_RUN_RE.sub(" ", _CLEAN_RE.sub("-", str(value)).strip())
    if not s:
        return None
    for fmt in _FORMATS:
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        return _sane(d)
    # 13/04/2026 style is already handled day-first above; this catches a genuine
    # month-first source, which only parses when the day part is over 12
    for fmt in _MONTH_FIRST:
        try:
            return _sane(dt.datetime.strptime(s, fmt).date())
        except ValueError:
            continue
    return None


def _sane(d):
    """Reject a year no purchase ledger will ever carry.

    A two-digit year and a mis-OCR'd digit both produce dates like 0026-07-31,
    which parse perfectly and are nonsense. Better to hand back None and keep the
    raw text than to file a document under the wrong century."""
    return d.isoformat() if 1990 <= d.year <= 2099 else None


def normalise(value):
    """ISO if it can be read, otherwise the value untouched.

    The form used at every write point: it upgrades what it understands and never
    destroys what it doesn't."""
    return to_iso(value) or value


def normalise_fields(payload, *keys):
    """Normalise the named keys of a dict in place, and hand it back.

    Keys that are absent are left absent — this must never invent a blank date on
    a partial update, where a missing key means "don't touch" rather than "clear"."""
    if not isinstance(payload, dict):
        return payload
    for k in keys:
        if k in payload and payload[k] not in (None, ""):
            payload[k] = normalise(payload[k])
    return payload


def parse(value):
    """The value as a `date`, or None. For arithmetic — ageing, sorting."""
    if isinstance(value, str):
        return _parse_text(value)
    iso = to_iso(value)
    return dt.date.fromisoformat(iso) if iso else None


@functools.lru_cache(maxsize=4096)
def _parse_text(value):
    # A string always reads as the same date, and the ageing reports parse the
    # same few hundred invoice dates tens of thousands of times per call.
    iso = to_iso(value)
    return dt.date.fromisoformat(iso) if iso else None


def today():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------------------
#  Reading one back
# ---------------------------------------------------------------------------
#: What a person reads. ISO is the STORAGE format and stays that way — see the
#: three things at the top of this file that only work because it is — but a date
#: is never SHOWN that way. Every register page, supplier invoice, cheque and
#: delivery note in this business is written day-first, so that is the one form
#: every screen, export and printed label uses. The frontend's `readableDate`
#: produces exactly this; the two must agree, or a CSV disagrees with the table
#: it was downloaded from.
DISPLAY = "%d-%m-%Y"


def to_display(value, blank=""):
    """`DD-MM-YYYY`, or the value untouched when it is not a date.

    Untouched, not blank: a register cell may hold text no parser could read, and
    that text is still evidence of what was on the page. The same rule `normalise`
    keeps on the way in, kept on the way out."""
    if value in (None, ""):
        return blank
    iso = to_iso(value)
    if not iso:
        # a stored timestamp — `2026-07-31T09:14:22` — is a date with a time on it
        iso = to_iso(str(value)[:10])
    return dt.date.fromisoformat(iso).strftime(DISPLAY) if iso else value


def display_cell(value):
    """A date hiding in a value that could be anything — a report cell, a CSV
    field. Only a full ISO date is rewritten: a quantity, a rate and an invoice
    code all have to come back as themselves."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not _ISO_CELL_RE.match(s):
        return value
    return to_display(s[:10], blank=value)


_ISO_CELL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")
