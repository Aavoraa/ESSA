"""Company, location and counter — who is billing, from where, at which till.

A bill has to say who raised it. Until now the shop had exactly one answer, in
config: SHOP_NAME and SHOP_GSTIN, printed on every invoice. That is right for one
company trading from one room, and wrong the moment there are two — two
registrations file two returns, and a month's sales that cannot be split between
them is a month's work to unpick.

So the entity is picked at the till and remembered on the invoice. Three levels,
because they answer three different questions:

    Company   whose GSTIN is on the bill        → prints, and splits the returns
    Location  which branch it was sold from     → the warehouse's own list
    Counter   which till at that branch         → whose drawer it belongs in

LOCATIONS ARE NOT A SECOND MASTER. The warehouse already knows these places: it
dispatches stock to them by name, and its transport register names the branch a
consignment is forwarded to. A shop keeping its own spelling of the same branch
cannot be asked the one question worth asking across the two halves — what did we
send there, and what did they sell. So the names are read from the warehouse and
kept in step, exactly as app/master_categories does with the category master.

Nothing here writes to the warehouse. It is read, as everything else in this shop
reads it: through the engine app/warehouse_items already builds, which knows how
to reach a SQLite file on a warehouse PC and a Postgres database on a deployment.
"""
from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app import db
# Hoisted, and it has to be. Every `from app import …` inside this shop must run
# while the package swap in backend/app/pos_mount is still in effect — at request
# time `app` is the WAREHOUSE's package again, and a late import silently reaches
# for the wrong one. `_warehouse_locations` gets away with importing lazily
# because it only ever runs at startup; `stores_of_warehouse` below runs on every
# picker draw, which is a request.
from app import warehouse_items as _wh
from app.models import Company, Counter, Floor, Location

#: The counter every location starts with. A shop with one till should not have
#: to invent a name for it before it can bill anything.
DEFAULT_COUNTER = "Counter 1"

#: Names that are not places. The warehouse's location list carries NONE as "do
#: not forward this consignment anywhere", which is an answer, not a branch.
NOT_A_PLACE = {"", "none", "-none-", "n/a", "na", "nil"}


def default_company():
    """The company a till bills as before anybody chooses one.

    Seeded from the shop's own config, so an install that has always had one
    company keeps printing exactly what it printed before and never meets the
    picker as a decision. A second company is then somebody adding one, which is
    the moment the choice becomes real.
    """
    row = Company.query.filter_by(is_default=True).first()
    if row:
        return row
    row = Company.query.order_by(Company.id).first()
    if row:
        return row
    cfg = current_app.config
    row = Company(
        name=cfg.get("SHOP_NAME") or "Shop",
        gstin=cfg.get("SHOP_GSTIN") or None,
        address=cfg.get("SHOP_ADDRESS") or None,
        state_code=cfg.get("SHOP_STATE_CODE") or None,
        phone=cfg.get("SHOP_PHONE") or None,
        is_default=True, active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _warehouse_locations():
    """Branch names the warehouse knows, or [] if it cannot be reached.

    The MASTER list only — the one somebody maintains, behind LR Entry's
    auto-transfer branch. Not the destinations stock has actually been dispatched
    to, and that is a deliberate refusal: `to_destination` is free text and the
    warehouse dispatches to customers as well as to branches. A delivery to a
    person would become a "branch" here, and then transfers.sync_transfers would
    take those pieces into shop stock — inventing goods on a shelf out of goods
    that went out of the door.

    The cost of being strict is a branch that has been shipped to but never added
    to the master: its transfers are left alone until somebody adds it. That is
    the right way round. Stock that has not arrived anywhere is a question
    somebody asks; stock that arrived somewhere it never went is a figure nobody
    questions.
    """
    wh = _wh                  # bound at import — see transfers.py
    con = wh._connect()
    if con is None:
        return []
    names = []
    try:
        # rows come back as dicts keyed by column name — see warehouse_items._Result
        #
        # `stores` first: it is the warehouse's system of record now, and unlike
        # the option list it says whether a branch is still OPEN. A store closed
        # upstairs should stop being offered as a place to bill from, and the
        # option list cannot express that — it is a list of strings.
        #
        # Filtered in Python, not in the WHERE clause. `active` is a BOOLEAN on
        # the deployment's Postgres and an INTEGER on the warehouse PC's SQLite,
        # and `active = 1` is a type error on the first while `active IS NOT
        # FALSE` is a syntax error on the second. A NULL — a row written by hand
        # — counts as open, which is how this behaved when the source was a list
        # of strings with no status at all.
        try:
            names += [r["name"] for r in con.execute(
                "SELECT name, active FROM stores").fetchall()
                if r.get("name") and (r.get("active") is None or r.get("active"))]
        except SQLAlchemyError:
            # A warehouse that predates the locations tables. It mirrors every
            # active store into the option list anyway (see the warehouse's
            # services/locations.mirror_to_options), so this fallback is also
            # what keeps a half-upgraded pair of deployments working: whichever
            # side is older, the names still arrive.
            pass
        sql = ("SELECT value FROM master_options "
               "WHERE kind = 'auto_transfer_location'")
        try:
            names += [r["value"] for r in con.execute(sql).fetchall() if r.get("value")]
        except SQLAlchemyError:
            pass                  # an older warehouse without that table
    finally:
        con.close()
    seen, out = set(), []
    for n in names:
        n = " ".join(str(n).split())
        if n.lower() in NOT_A_PLACE or n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(n)
    return sorted(out)


def sync_locations():
    """Bring the warehouse's places in, additively, and give each one a till.

    Additive on purpose. A name the warehouse no longer lists is left alone if
    anything is filed under it — a branch that closed still has last year's bills
    against it, and deleting the row to tidy a dropdown would orphan them. Only an
    unused, warehouse-sourced row is retired, and even then by deactivating it
    rather than deleting it.

    Returns (added, retired) for the caller to report or ignore.
    """
    known = _warehouse_locations()
    if not known:
        return 0, 0

    company = default_company()
    have = {loc.name.lower(): loc for loc in Location.query.all()}
    added = 0
    for name in known:
        row = have.get(name.lower())
        if row is None:
            row = Location(name=name, company_id=company.id, local=False, active=True)
            db.session.add(row)
            db.session.flush()
            added += 1
        elif not row.active:
            row.active = True         # it is on the warehouse's list again
        if not Counter.query.filter_by(location_id=row.id).first():
            db.session.add(Counter(name=DEFAULT_COUNTER, location_id=row.id))

    retired = 0
    lowered = {n.lower() for n in known}
    for loc in Location.query.filter_by(local=False, active=True).all():
        if loc.name.lower() in lowered:
            continue
        from app.models import Invoice
        if Invoice.query.filter_by(location_id=loc.id).first():
            continue                  # bills are filed under it; it stays
        loc.active = False
        retired += 1

    db.session.commit()
    sync_places()
    return added, retired


def sync_places():
    """Bring the warehouse's FLOORS and TILLS in, under the branches above.

    The warehouse's Locations screen is the master for the whole chain —
    warehouse → store → floor → till — and this is the shop reading it, exactly
    as `sync_locations` above reads the stores. It is not a second master: a
    floor is created upstairs, and the prefix it carries is what the till here
    numbers a bill with (see app/billing_numbers.py).

    MATCHED ON THE WAREHOUSE'S OWN ID, not on the name. Stores are matched by
    name because that is all the older option list ever gave; a floor and a till
    both have a real id, so renaming "Ground Floor" upstairs renames it here
    instead of creating a second one and leaving the counter on the first.

    ADDITIVE, like everything else the shop reads. A row the warehouse no longer
    lists is deactivated, never deleted — a till that closed still has last
    week's bills against it. And a till the shop made for itself (`wh_id` null —
    the "Counter 1" every branch gets so it can bill at all) is left alone
    entirely: it is not the warehouse's to retire, and it may be the only till
    the branch has.
    """
    con = _wh._connect()
    if con is None:
        return {"floors": 0, "counters": 0, "retired": 0}
    try:
        try:
            floors = con.execute(
                "SELECT f.id AS id, f.name AS name, f.prefix AS prefix, "
                "       f.sort_order AS sort_order, f.active AS active, "
                "       s.name AS store "
                "FROM floors f JOIN stores s ON s.id = f.store_id").fetchall()
        except SQLAlchemyError:
            return {"floors": 0, "counters": 0, "retired": 0}   # older warehouse
        try:
            tills = con.execute(
                "SELECT t.id AS id, t.name AS name, t.active AS active, "
                "       t.floor_id AS floor_id, s.name AS store "
                "FROM pos_terminals t JOIN stores s ON s.id = t.store_id").fetchall()
        except SQLAlchemyError:
            tills = []
    finally:
        con.close()

    def key(name):
        return " ".join(str(name or "").split()).lower()

    here = {key(l.name): l for l in Location.query.all()}
    made_f = made_c = retired = 0

    # `active` is filtered in Python for the same reason as everywhere else in
    # this file: it is BOOLEAN on Postgres and INTEGER on SQLite, and no single
    # WHERE clause is valid on both.
    seen_floors, by_wh_floor = set(), {}
    for row in floors:
        loc = here.get(key(row.get("store")))
        if loc is None:
            continue                  # a store this shop has not taken in
        wh_id = row.get("id")
        mine = Floor.query.filter_by(wh_id=wh_id).first()
        if mine is None:
            # A floor the shop had already created by hand under the same name
            # is ADOPTED rather than duplicated — otherwise a shop that set its
            # floors up locally and then had them added upstairs would end up
            # with two of each and tills split between them.
            mine = Floor.query.filter_by(location_id=loc.id,
                                         name=str(row.get("name") or "")).first()
            if mine is None:
                mine = Floor(location_id=loc.id, name=str(row.get("name") or ""))
                db.session.add(mine)
                made_f += 1
            mine.wh_id = wh_id
        mine.location_id = loc.id
        mine.name = str(row.get("name") or mine.name)
        # A floor with no prefix upstairs bills on the shop's plain series; the
        # column is not nullable here, so that is stored as the empty string and
        # billing_numbers.series_for reads it as "not set".
        mine.prefix = (str(row.get("prefix") or "").strip().upper())
        mine.sort_order = int(row.get("sort_order") or 0)
        mine.local = False
        mine.active = bool(row.get("active") is None or row.get("active"))
        db.session.flush()
        by_wh_floor[wh_id] = mine.id
        seen_floors.add(wh_id)

    for row in tills:
        loc = here.get(key(row.get("store")))
        if loc is None:
            continue
        wh_id = row.get("id")
        mine = Counter.query.filter_by(wh_id=wh_id).first()
        if mine is None:
            mine = Counter.query.filter_by(location_id=loc.id,
                                           name=str(row.get("name") or "")).first()
            if mine is None:
                mine = Counter(location_id=loc.id, name=str(row.get("name") or ""))
                db.session.add(mine)
                made_c += 1
            mine.wh_id = wh_id
        mine.location_id = loc.id
        mine.name = str(row.get("name") or mine.name)
        mine.floor_id = by_wh_floor.get(row.get("floor_id"))
        mine.active = bool(row.get("active") is None or row.get("active"))

    # Retire what the warehouse has stopped listing — its rows only, and only by
    # switching them off.
    for f in Floor.query.filter(Floor.wh_id.isnot(None),
                                Floor.active.is_(True)).all():
        if f.wh_id not in seen_floors:
            f.active = False
            retired += 1
    seen_tills = {row.get("id") for row in tills}
    for c in Counter.query.filter(Counter.wh_id.isnot(None),
                                  Counter.active.is_(True)).all():
        if c.wh_id not in seen_tills:
            c.active = False
            retired += 1

    db.session.commit()
    return {"floors": made_f, "counters": made_c, "retired": retired}


#: Where the "which warehouse opened this till" scope is kept. In the SESSION,
#: like the counter choice beside it: it is a property of the frame somebody is
#: looking at, and it has to survive navigation inside that frame — the till
#: moves between its own screens and only the first of them carries the
#: parameter that set it.
SCOPE_KEY = "warehouse_scope"


def stores_of_warehouse(warehouse_id):
    """Branch names one warehouse supplies, read from the warehouse's own tables.

    None — not an empty list — when the question cannot be answered: no warehouse
    was named, the warehouse database is unreachable, or it predates the stores
    table. None means "do not narrow", which keeps every till working exactly as
    it did rather than emptying its branch list because a lookup failed.

    An empty SET is a real answer and a different one: this warehouse supplies no
    shops yet.
    """
    if not warehouse_id:
        return None
    con = _wh._connect()
    if con is None:
        return None
    try:
        # Positional `?` with a TUPLE — that is the only shape
        # warehouse_items._Con.execute understands; it rewrites the markers into
        # named parameters itself. A dict passed here is silently mis-bound.
        rows = con.execute(
            "SELECT name, active FROM stores WHERE warehouse_id = ?",
            (int(warehouse_id),)).fetchall()
    except SQLAlchemyError:
        return None               # a warehouse without the locations tables
    finally:
        con.close()
    # `active` is filtered in Python for the same reason as in
    # _warehouse_locations: it is BOOLEAN on Postgres and INTEGER on SQLite, and
    # no single WHERE clause is valid on both.
    return {" ".join(str(r["name"]).split()).lower() for r in rows
            if r.get("name") and (r.get("active") is None or r.get("active"))}


def current_scope():
    """The warehouse this till was opened from, if the frame said so."""
    try:
        from flask import session
        raw = session.get(SCOPE_KEY)
    except RuntimeError:          # outside a request
        return None
    return int(raw) if str(raw or "").strip().isdigit() else None


def picker_options():
    """Everything the till's Company / Location / Counter dialog draws itself from.

    Narrowed to the branches of the warehouse this till was opened from, when it
    was opened from one. A counter at Erode has no business billing as a Karur
    shop, and a picker that offers it is the only thing standing between a
    cashier and a sale filed against the wrong branch's stock.
    """
    locations = Location.query.filter_by(active=True).order_by(Location.name).all()
    allowed = stores_of_warehouse(current_scope())
    if allowed is not None:
        locations = [l for l in locations
                     if " ".join((l.name or "").split()).lower() in allowed]
    keep = {l.id for l in locations}
    return {
        "companies": [{"id": c.id, "name": c.name, "gstin": c.gstin or ""}
                      for c in Company.query.filter_by(active=True)
                                            .order_by(Company.name).all()],
        "locations": [{"id": l.id, "name": l.name, "company_id": l.company_id}
                      for l in locations],
        # The storeys of each branch, and the bill prefix each one bills on. The
        # prefix travels with the option because the till SHOWS it — a cashier
        # picking "First Floor" should see TF before they take a rupee, not
        # discover it on the printed bill.
        "floors": [{"id": f.id, "name": f.name, "prefix": f.prefix,
                    "location_id": f.location_id}
                   for f in Floor.query.filter_by(active=True)
                                       .order_by(Floor.sort_order, Floor.name).all()
                   if allowed is None or f.location_id in keep],
        "counters": [{"id": x.id, "name": x.name, "location_id": x.location_id,
                      "floor_id": x.floor_id}
                     for x in Counter.query.filter_by(active=True)
                                           .order_by(Counter.name).all()
                     # a till at a branch this warehouse does not supply is not
                     # offered either — it would be a counter with no location
                     if allowed is None or x.location_id in keep],
    }


def resolve(company_id=None, location_id=None, floor_id=None, counter_id=None):
    """The four rows a till has chosen, each None if it has not chosen one.

    A counter that does not belong to the chosen location is dropped rather than
    kept, and so is a location that does not belong to the chosen company: a
    till that reads "ESSA GARMENTS / TIRUPUR / Counter 2" where Counter 2 is at
    another branch is worse than one that reads nothing, because it looks
    answered.

    The FLOOR is the one level that can also be worked out rather than chosen. A
    till belongs to a storey, so a session naming only the counter still knows
    which floor it is on — and that is the ordinary case, because the mapping is
    what decides the bill prefix and the cashier should not have to restate it.
    An explicit floor that disagrees with the till's own is dropped, for the same
    reason as everything else here: better to ask again than to look answered and
    print the wrong series.
    """
    company = db.session.get(Company, company_id) if company_id else None
    location = db.session.get(Location, location_id) if location_id else None
    floor = db.session.get(Floor, floor_id) if floor_id else None
    counter = db.session.get(Counter, counter_id) if counter_id else None
    # A branch outside this till's warehouse is dropped the same way, and for the
    # same reason: a session that still names Karur after the frame was opened
    # from Erode would keep billing against Karur's stock while the screen around
    # it says Erode. Dropped rather than corrected — the picker then asks, which
    # is the only honest thing to do when the answer has become impossible.
    allowed = stores_of_warehouse(current_scope())
    if location is not None and allowed is not None \
            and " ".join((location.name or "").split()).lower() not in allowed:
        location, floor, counter = None, None, None
    if location is not None and company is not None and location.company_id \
            and location.company_id != company.id:
        location, floor, counter = None, None, None
    if floor is not None and (location is None or floor.location_id != location.id):
        floor = None
    if counter is not None and (location is None or counter.location_id != location.id):
        counter = None
    if counter is not None and floor is not None and counter.floor_id \
            and counter.floor_id != floor.id:
        # The till says it is on another storey. The till is the one standing in
        # the building, so the chosen floor is what gives way.
        floor = None
    if floor is None and counter is not None and counter.floor_id:
        floor = db.session.get(Floor, counter.floor_id)      # mapped, not picked
    return company, location, floor, counter
