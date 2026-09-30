"""Request and SQL timing — off unless ESSA_PERF_LOG is set.

With ESSA_PERF_LOG=1 every API request writes one line to the server log:

    PERF GET /api/command/overview 200 total=1.84s sql=0.93s/41q py=0.91s
         slowest: 0.31s SELECT products.id … | 0.12s SELECT …

and, when ESSA_PERF_FILE names a path, one JSON object per request to that file
(for before/after comparisons). With the variable unset nothing is registered:
no listener on the engine, no middleware, no cost.

Timing lives in a contextvar set per request. Sync routes run in a threadpool
that copies the request's context, so every statement a route issues — from
the router, a service, or a lazy relationship load — is counted against it.
"""
import contextvars
import json
import logging
import os
import time

ENABLED = (os.environ.get("ESSA_PERF_LOG") or "").strip().lower() in ("1", "true", "yes", "on")
PERF_FILE = (os.environ.get("ESSA_PERF_FILE") or "").strip() or None
log = logging.getLogger("uvicorn.error")

_current = contextvars.ContextVar("essa_perf", default=None)


def install(app, engine):
    if not ENABLED:
        return
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, cursor, statement, params, context, executemany):
        conn.info.setdefault("essa_perf_t0", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn, cursor, statement, params, context, executemany):
        stack = conn.info.get("essa_perf_t0")
        if not stack:
            return
        dt = time.perf_counter() - stack.pop()
        rec = _current.get()
        if rec is None:
            return
        rec["n"] += 1
        rec["sql"] += dt
        # statements grouped by template: a template run hundreds of times in one
        # request is an N+1 loop, whatever each run costs
        key = " ".join(statement.split())[:160]
        agg = rec["shapes"].setdefault(key, [0, 0.0])
        agg[0] += 1
        agg[1] += dt
        slow = rec["slow"]
        if len(slow) < 5 or dt > slow[-1][0]:
            slow.append((dt, " ".join(statement.split())[:220]))
            slow.sort(key=lambda s: -s[0])
            del slow[5:]

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, record):
        rec = _current.get()
        if rec is not None:
            rec["connects"] += 1

    @app.middleware("http")
    async def _perf(request, call_next):
        if not request.url.path.startswith(("/api/", "/pos/")):
            return await call_next(request)
        rec = {"n": 0, "sql": 0.0, "slow": [], "connects": 0, "shapes": {}}
        token = _current.set(rec)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            total = time.perf_counter() - t0
            _current.reset(token)
            path = request.url.path + (("?" + request.url.query) if request.url.query else "")
            slow = " | ".join(f"{d:.2f}s {s}" for d, s in rec["slow"][:3])
            log.info("PERF %s %s %s total=%.2fs sql=%.2fs/%dq conn=%d py=%.2fs  slowest: %s",
                     request.method, path, status, total, rec["sql"], rec["n"], rec["connects"],
                     max(0.0, total - rec["sql"]), slow)
            if PERF_FILE:
                try:
                    with open(PERF_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "method": request.method, "path": path, "status": status,
                            "total": round(total, 3), "sql": round(rec["sql"], 3),
                            "queries": rec["n"], "connects": rec["connects"],
                            "slowest": [[round(d, 3), s] for d, s in rec["slow"]],
                            "repeated": [[n, round(t, 3), k] for k, (n, t) in sorted(
                                rec["shapes"].items(), key=lambda kv: -kv[1][0])[:8]]}) + "\n")
                except OSError:
                    pass
        return response
