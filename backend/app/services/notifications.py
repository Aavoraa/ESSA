"""The notification centre: every queue in the warehouse, as one list.

The dashboard already answers "what is waiting on someone", but only for whoever
opens the dashboard. This is the same question asked the other way round — the
list comes to you, it says how long each thing has been waiting, and it
remembers what you have already seen.

**Notices are derived, not logged.** Almost nothing here is an event; it is a
condition — four GRNs in draft, eleven lines dead for six months. Filing an
event every time a check ran would refile the same four drafts hourly. So each
rule reads the current state and returns one notice or nothing, and the only
thing stored is the acknowledgement (models.NotificationState).

**Read means read at a number.** Acknowledging "4 drafts" stores 4. At five it
is unread again, because a fifth is news; at three it stays quiet, because a
queue that shrank needs nobody chased. This is what makes a bell that can be
cleared and still be trusted.

**A clear queue produces no notice at all.** A centre that lists "0 invoices to
review" trains people to skim past it, and then they skim past the one that says
eleven.
"""
import datetime as dt

from sqlalchemy.orm import selectinload

from .. import models
from . import payments as pay_svc
from . import shortages as short_svc
from . import dead_stock as ds_svc

LEVELS = ["critical", "warn", "info"]

#: how a level reads on screen — set once here so the desktop bell, the dashboard
#: and the phone can never colour the same notice three different ways
LEVEL_META = {
    "critical": {"dot": "🔥", "label": "Critical"},
    "warn": {"dot": "🔴", "label": "Needs attention"},
    "info": {"dot": "🟠", "label": "For information"},
}


def _money(v):
    return f"₹ {float(v or 0):,.2f}"


# --------------------------------------------------------------- the rules ---
# Each rule is (key, module, builder). `module` is the tab the notice opens —
# a notification you cannot act on from is a notification nobody thanks you for.
# A builder returns None when its queue is clear.
#
# `ctx` is scratch shared by one pass. It exists for the dead-stock bands: three
# rules read the same answer, and that answer is a walk of every product plus a
# read of the till. Without it, opening the bell would run that walk three times
# to print three lines of the same report.

def _dead_bands(db, ctx):
    if "dead_bands" not in ctx:
        ctx["dead_bands"] = ds_svc.alert_levels(db)
    return ctx["dead_bands"]


def _r_documents(db, ctx):
    n = db.query(models.Document).filter(models.Document.status == "needs_review").count()
    if not n:
        return None
    return dict(level="warn", count=n,
                title=f"{n} invoice{'s' if n != 1 else ''} to review",
                body="Read, but something did not reconcile — confirm or correct them.")


def _r_grn_draft(db, ctx):
    rows = db.query(models.Purchase).filter(models.Purchase.status == "draft").all()
    if not rows:
        return None
    value = sum(float(p.grand_total or 0) for p in rows)
    return dict(level="warn", count=len(rows), value=value,
                title=f"{len(rows)} GRN{'s' if len(rows) != 1 else ''} in draft",
                body=f"{_money(value)} counted but not posted — the goods are not stock yet.")


def _r_shortages(db, ctx):
    # Every shortage on a GRN line, read directly — the same set as walking each
    # purchase's lines, without loading every line of every GRN to find them.
    shorts = [sh for sh in db.query(models.GrnShortage)
                             .join(models.PurchaseLine,
                                   models.PurchaseLine.id == models.GrnShortage.line_id)
                             .join(models.Purchase,
                                   models.Purchase.id == models.PurchaseLine.purchase_id)
              if sh.claimable and short_svc.status_of(db, sh) in ("open", "part-claimed")]
    if not shorts:
        return None
    value = sum(short_svc.value(sh) for sh in shorts)
    return dict(level="warn", count=len(shorts), value=value,
                title=f"{len(shorts)} open shortage claim{'s' if len(shorts) != 1 else ''}",
                body=f"{_money(value)} billed and not delivered — waive it or raise a debit note.")


def _r_lr_pending(db, ctx):
    n = db.query(models.LREntry).filter(
        (models.LREntry.received_by == None) | (models.LREntry.received_by == "")).count()  # noqa: E711
    if not n:
        return None
    return dict(level="warn", count=n,
                title=f"{n} consignment{'s' if n != 1 else ''} not received",
                body="In the register, but nobody has signed for the packages.")


def _r_lr_unlinked(db, ctx):
    n = db.query(models.LREntry).filter(
        (models.LREntry.matched == False) | (models.LREntry.matched == None)).count()  # noqa: E711,E712
    if not n:
        return None
    return dict(level="info", count=n,
                title=f"{n} LR row{'s' if n != 1 else ''} without an invoice",
                body="No supplier invoice has been matched to them yet.")


def _r_transit(db, ctx):
    # lines loaded with the notes: `total_qty` reads them, one query per note otherwise
    rows = (db.query(models.StockOutward).options(selectinload(models.StockOutward.lines))
              .filter(models.StockOutward.status == "posted").all())
    if not rows:
        return None
    qty = sum(o.total_qty for o in rows)
    return dict(level="warn", count=len(rows), value=None,
                title=f"{len(rows)} transfer{'s' if len(rows) != 1 else ''} in transit",
                body=f"{qty:g} pcs dispatched that no destination has checked in.")


def _r_outward_draft(db, ctx):
    n = db.query(models.StockOutward).filter(models.StockOutward.status == "draft").count()
    if not n:
        return None
    return dict(level="info", count=n,
                title=f"{n} outward dispatch{'es' if n != 1 else ''} in draft",
                body="Packed on screen but not sent — stock has not left.")


def _r_returns(db, ctx):
    n = db.query(models.PurchaseReturn).filter(models.PurchaseReturn.status == "draft").count()
    if not n:
        return None
    return dict(level="info", count=n,
                title=f"{n} purchase return{'s' if n != 1 else ''} in draft",
                body="Debit notes started against a GRN but not raised.")


def _r_overdue(db, ctx):
    bills = [b for b in pay_svc.pending_bills(db, None) if (b.get("days") or 0) > 30]
    if not bills:
        return None
    total = sum(float(b["outstanding"] or 0) for b in bills)
    oldest = max((b.get("days") or 0) for b in bills)
    return dict(level="critical", count=len(bills), value=total,
                title=f"{len(bills)} supplier bill{'s' if len(bills) != 1 else ''} over 30 days",
                body=f"{_money(total)} outstanding, the oldest {oldest} days old.")


def _r_detail(db, ctx):
    n = db.query(models.Product).filter(
        models.Product.stock_qty > 0,
        (models.Product.detailed == False) | (models.Product.detailed == None)).count()  # noqa: E711,E712
    if not n:
        return None
    return dict(level="info", count=n,
                title=f"{n} product{'s' if n != 1 else ''} awaiting physical detail",
                body="Colour, size and fit not recorded — they cannot be labelled or sold on properly.")


def _dead_stock_rule(level_key, notice_level):
    """The three dead-stock bands, off the one read the module already does."""
    def build(db, ctx):
        band = next((a for a in _dead_bands(db, ctx) if a["level"] == level_key), None)
        if not band:
            return None
        return dict(level=notice_level, count=band["lines"], value=band["stock_value"],
                    title=f"{band['lines']} product line{'s' if band['lines'] != 1 else ''} — {band['title'].lower()}",
                    body=f"{band['qty']:g} pcs, {_money(band['stock_value'])} of capital — {band['note']}.")
    return build


RULES = [
    ("payments.overdue", "payments", _r_overdue),
    ("deadstock.critical", "deadstock", _dead_stock_rule("critical", "critical")),
    ("deadstock.dead", "deadstock", _dead_stock_rule("dead", "warn")),
    ("documents.review", "documents", _r_documents),
    ("grn.draft", "purchases", _r_grn_draft),
    ("grn.shortage", "purchases", _r_shortages),
    ("lr.pending", "lr", _r_lr_pending),
    ("outward.transit", "inward", _r_transit),
    ("deadstock.approaching", "deadstock", _dead_stock_rule("approaching", "info")),
    ("lr.unlinked", "lr", _r_lr_unlinked),
    ("outward.draft", "outward", _r_outward_draft),
    ("returns.draft", "returns", _r_returns),
    ("inventory.detail", "inventory", _r_detail),
]

_ORDER = {key: i for i, (key, _, _) in enumerate(RULES)}
_FAILED = object()          # a rule that raised: its queue goes unreported this pass


# ---------------------------------------------------------------- the feed ---

def _state(db, key, create=False):
    st = db.query(models.NotificationState).filter(
        models.NotificationState.key == key).first()
    if st is None and create:
        st = models.NotificationState(key=key, read_level=0.0)
        db.add(st)
        db.flush()
    return st


def _age(since, now=None):
    """'3 days' — how long this has been waiting, in the coarsest true unit.

    Clamped at zero: `now` is taken before the rules run, so a queue first seen
    DURING this pass is stamped a few milliseconds later than it. Left signed,
    timedelta normalises that to minus one day plus 23 hours, and every brand
    new notice announces itself as having waited most of a day."""
    if not since:
        return None
    delta = max(dt.timedelta(0), (now or dt.datetime.utcnow()) - since)
    days = delta.days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return "just now"


def collect(db, include_muted=False):
    """Every open queue, newest state, with what has been read of it.

    Writes as it reads — a queue seen for the first time gets a `first_seen`, so
    "waiting 6 days" is answerable later. That is the only write, and it is the
    difference between a list of counts and a list somebody can prioritise."""
    now = dt.datetime.utcnow()
    ctx = {}                      # scratch shared by this pass; see _dead_bands
    out = []
    # The dead-stock rules are asked LAST: on a dashboard the dead-stock rows
    # they read are being built on another thread at the same moment, and the
    # other rules' reads fit in that wait instead of queueing behind it. Only
    # the asking moves — the notices are handled in RULES order below and sorted
    # by it at the end, so the feed is the same.
    built = {}
    for key, module, build in sorted(RULES, key=lambda r: r[0].startswith("deadstock.")):
        try:
            built[key] = build(db, ctx)
        except Exception:
            # one rule failing is one queue unreported, not a dead bell — the
            # centre exists to carry the other twelve
            built[key] = _FAILED
    for key, module, build in RULES:
        notice = built[key]
        if notice is _FAILED:
            continue
        st = _state(db, key)
        if notice is None:
            # the queue cleared: forget what was read, so the next occurrence is
            # news again rather than being compared against an old acknowledgement
            if st is not None:
                db.delete(st)
            continue
        if st is None:
            st = _state(db, key, create=True)
        if st.muted and not include_muted:
            continue
        count = float(notice.get("count") or 0)
        unread = count > float(st.read_level or 0)
        out.append({
            "key": key, "module": module,
            "level": notice["level"], "dot": LEVEL_META[notice["level"]]["dot"],
            "title": notice["title"], "body": notice["body"],
            "count": notice.get("count"), "value": notice.get("value"),
            "unread": unread, "muted": bool(st.muted),
            "first_seen": st.first_seen.isoformat() if st.first_seen else None,
            "waiting": _age(st.first_seen, now),
            "read_at": st.read_at.isoformat() if st.read_at else None,
            "read_by": st.read_by,
        })
    db.commit()
    out.sort(key=lambda n: (not n["unread"], _ORDER.get(n["key"], 99)))
    return out


def feed(db):
    notices = collect(db)
    unread = [n for n in notices if n["unread"]]
    return {
        "notices": notices,
        "counts": {
            "total": len(notices),
            "unread": len(unread),
            **{lvl: len([n for n in unread if n["level"] == lvl]) for lvl in LEVELS},
        },
        "checked_at": dt.datetime.utcnow().isoformat(),
        "recipients": [recipient_out(r) for r in
                       db.query(models.NotificationRecipient).filter(
                           models.NotificationRecipient.active == True).all()],  # noqa: E712
    }


def mark_read(db, keys, by=None):
    """Acknowledge these queues AT THEIR CURRENT COUNT."""
    live = {n["key"]: n for n in collect(db, include_muted=True)}
    now = dt.datetime.utcnow()
    for key in keys or []:
        n = live.get(key)
        if not n:
            continue
        st = _state(db, key, create=True)
        st.read_level = float(n.get("count") or 0)
        st.read_at = now
        st.read_by = by
    db.commit()
    return feed(db)


def mark_all(db, by=None):
    return mark_read(db, [n["key"] for n in collect(db, include_muted=True)], by=by)


def set_muted(db, key, muted, by=None):
    if key not in _ORDER:
        raise KeyError(key)
    st = _state(db, key, create=True)
    st.muted = bool(muted)
    st.read_by = by or st.read_by
    db.commit()
    return feed(db)


def muted_keys(db):
    """The muted list, with their titles — so a queue somebody silenced months
    ago can be found and turned back on."""
    titles = {key: key for key, _, _ in RULES}
    live = {n["key"]: n["title"] for n in collect(db, include_muted=True)}
    rows = db.query(models.NotificationState).filter(
        models.NotificationState.muted == True).all()  # noqa: E712
    return [{"key": s.key, "title": live.get(s.key) or titles.get(s.key, s.key),
             "open_now": s.key in live} for s in rows]


# ----------------------------------------------------------- the recipients --

def recipient_out(r):
    return {"id": r.id, "name": r.name, "mobile": r.mobile, "role": r.role,
            "levels": r.levels or LEVELS, "active": bool(r.active)}


def clean_mobile(value):
    """Keep what was dialled, drop what was decoration.

    Deliberately not validated into a shape: this warehouse's numbers are typed
    with +91, with a 0, with spaces and sometimes as two numbers in one box.
    Rejecting those would lose the roster to protect a format nothing yet reads —
    delivery is in-app today. Digits and the few characters a dialler accepts
    survive; everything else goes."""
    if not value:
        return None
    kept = "".join(c for c in str(value) if c.isdigit() or c in "+-/, ")
    return " ".join(kept.split()) or None
