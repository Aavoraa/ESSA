"""Index definitions, and the alias arrangement that lets them be rebuilt safely.

Each logical index is an ALIAS (`essa-sales`) pointing at one versioned index
(`essa-sales-v1-20260925t0930`). A rebuild creates a new versioned index, fills
and validates it, then moves the alias in one atomic call — readers never see a
half-built index, and the previous one is kept until the next rebuild.

Mappings are explicit (`dynamic: strict`): ids are integers, amounts doubles,
dates dates; nothing is analysed text, because nothing here is searched as
words — it is filtered and aggregated.
"""
import datetime as dt

from . import config

SALES = "sales"
RETURNS = "sales-returns"
META = "meta"

SPECS = {
    SALES: {
        # one document per (bill, warehouse product); `_id` "<invoice_id>:<wid>"
        "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"},
        "mappings": {"dynamic": "strict", "properties": {
            "invoice_id": {"type": "long"},
            "wid": {"type": "long"},                  # warehouse product id (shop products.warehouse_id)
            "qty": {"type": "double"},                # Σ invoice_items.quantity on this bill
            "amount": {"type": "double"},             # Σ invoice_items.line_total on this bill
            "invoice_date": {"type": "date", "format": "strict_date_optional_time||epoch_millis"},
            "location_id": {"type": "long"},
            "live": {"type": "boolean"},              # COALESCE(payment_status,'paid') <> 'cancelled'
        }},
    },
    RETURNS: {
        # one document per (credit note, warehouse product); `_id` "<credit_note_id>:<wid>"
        "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"},
        "mappings": {"dynamic": "strict", "properties": {
            "credit_note_id": {"type": "long"},
            "wid": {"type": "long"},
            "qty": {"type": "double"},                # Σ credit_note_items.quantity
            "amount": {"type": "double"},             # Σ credit_note_items.line_total
            "created_at": {"type": "date", "format": "strict_date_optional_time||epoch_millis"},
        }},
    },
    META: {
        # sync state, one document per logical index — kept in Elasticsearch so
        # PostgreSQL's schema is not touched
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {"dynamic": "strict", "properties": {
            "state": {"type": "object", "enabled": False},
        }},
    },
}


def alias(name):
    return f"{config.PREFIX}{name}"


def versioned_name(name):
    stamp = dt.datetime.utcnow().strftime("%Y%m%dt%H%M%S")
    return f"{config.PREFIX}{name}-v1-{stamp}"


def current_index(es, name):
    """The concrete index the alias points at, or None."""
    try:
        got = es.indices.get_alias(name=alias(name))
    except Exception:                                   # noqa: BLE001 — NotFound: no alias yet
        return None
    return next(iter(got), None)


def create(es, name):
    """A new, empty versioned index for `name`; the alias is not moved."""
    spec = SPECS[name]
    index = versioned_name(name)
    es.indices.create(index=index, settings=spec["settings"], mappings=spec["mappings"])
    return index


def point_alias(es, name, index):
    """Move the alias to `index` atomically; returns the index it left."""
    old = current_index(es, name)
    actions = [{"add": {"index": index, "alias": alias(name)}}]
    if old and old != index:
        actions.insert(0, {"remove": {"index": old, "alias": alias(name)}})
    es.indices.update_aliases(actions=actions)
    return old


def ensure_meta(es):
    if current_index(es, META) is None:
        point_alias(es, META, create(es, META))
