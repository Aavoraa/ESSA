"""Reuse of an expensive read within ONE read-only request — and never beyond it.

The Command Centre and the notification feed are each built out of services
that were written to stand alone: the dead-stock count, the dead-stock alerts
and the notification rules each read the till's sales and the product ages for
themselves. Correct, and on a single screen load that meant the same 2-million
line aggregation running four times over, returning the same answer each time.

`memo(key, compute)` runs `compute` once per key per request and hands the same
result to every later caller in that request. The scope is deliberately tiny:

  * GET requests only. A request that writes may change the very figures being
    read, so it always reads fresh.
  * The request's own lifetime. Nothing survives into the next request, so there
    is no staleness to reason about and nothing to invalidate — two calls inside
    one request were always meant to see the same data anyway.
  * Outside a request (scripts, tests, background work) it is a plain call.

Callers must treat a memoised result as read-only, which every current caller
already does.
"""
import contextvars
import threading

_store = contextvars.ContextVar("essa_request_memo", default=None)

# A dashboard builds its sections on several threads at once (see
# command_center._run_groups), all sharing the request's store. Two sections
# asking for the same read at the same moment must still cause ONE read: the
# first starts it and the second waits for that answer rather than running the
# same 2-million-line aggregation alongside it.
_LOCK = ("__request_memo_lock__",)


class _Pending:
    __slots__ = ("done", "value", "error")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None


def _lock(store):
    lock = store.get(_LOCK)
    if lock is None:
        lock = store.setdefault(_LOCK, threading.Lock())   # atomic on a dict
    return lock


def memo(key, compute):
    store = _store.get()
    if store is None:
        return compute()
    with _lock(store):
        entry = store.get(key, _MISSING)
        mine = entry is _MISSING
        if mine:
            entry = _Pending()
            store[key] = entry
    if not isinstance(entry, _Pending):
        return entry
    if not mine:
        entry.done.wait()
        if entry.error is not None:
            return compute()            # the first attempt failed: try for ourselves
        return entry.value
    try:
        entry.value = compute()
    except BaseException as exc:
        entry.error = exc
        with _lock(store):
            if store.get(key) is entry:
                del store[key]
        raise
    finally:
        entry.done.set()
    with _lock(store):
        store[key] = entry.value
    return entry.value


_MISSING = object()


def remember(key, value):
    """Record a by-product of one read for a later caller in the same request."""
    store = _store.get()
    if store is not None:
        store[key] = value


def peek(key, default=None):
    """A value already known in this request, or `default` — never computes."""
    store = _store.get()
    if store is None:
        return default
    value = store.get(key, default)
    return default if isinstance(value, _Pending) else value


class RequestMemoMiddleware:
    """Pure ASGI, so it adds no task or body buffering of its own."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "GET" \
                or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        token = _store.set({})
        try:
            return await self.app(scope, receive, send)
        finally:
            _store.reset(token)
