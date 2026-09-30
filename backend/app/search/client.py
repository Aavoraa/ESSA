"""The one Elasticsearch client, created on first use.

Nothing else in ESSA imports the elasticsearch package: a deployment without it
installed, or with ELASTICSEARCH_ENABLED off, gets `client() is None` and every
caller takes its PostgreSQL path.
"""
import logging
import threading
import time

from . import config

log = logging.getLogger("uvicorn.error")
_client = None
_lock = threading.Lock()

#: While Elasticsearch is unreachable, reads do not ask it at all. A closed port
#: on Windows takes ~2 s to refuse a connection, and with retries and several
#: reads per screen that was ~14 s on every dashboard before PostgreSQL
#: answered. So a failure marks it down for DOWN_SECONDS, and the background
#: sync — which checks it every few seconds anyway — marks it up again the
#: moment it answers. Reads never wait on a dead server.
DOWN_SECONDS = 60.0
_down_until = 0.0
_down_reason = ""


def mark_down(exc=None):
    global _down_until, _down_reason
    first = time.time() >= _down_until
    _down_until = time.time() + DOWN_SECONDS
    _down_reason = f"{type(exc).__name__}: {str(exc)[:160]}" if exc else "unreachable"
    if first:
        log.warning("search: Elasticsearch unreachable — reads use PostgreSQL until it answers again (%s)",
                    _down_reason)


def mark_up():
    global _down_until
    if _down_until:
        log.info("search: Elasticsearch answering again")
    _down_until = 0.0


def is_down():
    return time.time() < _down_until


def usable():
    """The client for a READ — or None when Elasticsearch is off or known to be
    down, so the caller takes its PostgreSQL path at once. Reads made with it
    should not retry (see `for_reads`)."""
    if is_down():
        return None
    return client()


def for_reads(es):
    """The same client with a short timeout and no retries: a read that cannot
    be answered quickly is answered by PostgreSQL instead."""
    return es.options(request_timeout=min(config.REQUEST_TIMEOUT, 3), max_retries=0, retry_on_timeout=False)


def client():
    """The shared client, or None when Elasticsearch is not configured here."""
    global _client
    if not config.ENABLED:
        return None
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            try:
                from elasticsearch import Elasticsearch
            except ImportError:
                log.warning("ELASTICSEARCH_ENABLED is set but the elasticsearch package "
                            "is not installed — using PostgreSQL only")
                return None
            kw = dict(request_timeout=config.REQUEST_TIMEOUT, retry_on_timeout=True,
                      max_retries=2, verify_certs=config.VERIFY_CERTS)
            if config.CA_CERTS:
                kw["ca_certs"] = config.CA_CERTS
            if config.API_KEY:
                kw["api_key"] = config.API_KEY
            elif config.USERNAME:
                kw["basic_auth"] = (config.USERNAME, config.PASSWORD)
            _client = Elasticsearch(config.URL, **kw)
    return _client


def health():
    """What the status screen and `es_admin.py health` print. Never raises."""
    es = client()
    if es is None:
        return {"enabled": config.ENABLED, "reachable": False,
                "reason": "disabled" if not config.ENABLED else "client unavailable"}
    if is_down():
        return {"enabled": True, "reachable": False, "url": config.URL,
                "reason": "marked down: " + _down_reason}
    try:
        h = for_reads(es).cluster.health()
        return {"enabled": True, "reachable": True, "status": h.get("status"),
                "nodes": h.get("number_of_nodes"), "url": config.URL}
    except Exception as exc:                            # noqa: BLE001 — reported, not raised
        mark_down(exc)
        return {"enabled": True, "reachable": False, "url": config.URL,
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
