"""The same JSON a route's return value becomes, written without the detour.

A route that returns a list or dict has it passed through FastAPI's
`jsonable_encoder` — a pure-Python walk that visits every value to convert the
handful that are not JSON already (dates, decimals, models) — and then through
`json.dumps`. For a list of thousands of rows the walk is most of the response
time: 19 s of the 22 s spent encoding the 363K-product list, where `json.dumps`
alone takes 2.4 s.

`json_response` calls `json.dumps` with the same settings Starlette's
JSONResponse renders with, handing it `jsonable_encoder` for exactly the values
it cannot write itself. Plain values are written the same either way, so the
bytes are the same (checked byte for byte on the inventory, GRN, outward,
payments, returns, suppliers, LR, dead-stock and audit lists). Anything
`json.dumps` refuses outright — a dict keyed by something other than a string
or number — takes FastAPI's own path, unchanged.
"""
import functools
import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


def returns_json(route):
    """`@returns_json(router.get("/x"))` in place of `@router.get("/x")`.

    Registers the endpoint so its answer is written by `json_response`, and
    leaves the module's name bound to the plain function — other code that calls
    it for the data (the dashboard summary calls the outward and LR lists) gets
    the data, as before. The parameters FastAPI injects are read off the plain
    function (`functools.wraps`), so the route's inputs are unchanged."""
    def register(fn):
        @functools.wraps(fn)
        def endpoint(*args, **kwargs):
            out = fn(*args, **kwargs)
            return out if isinstance(out, Response) else json_response(out)
        route(endpoint)
        return fn
    return register


def json_response(content, status_code: int = 200):
    try:
        body = json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None,
                          separators=(",", ":"), default=jsonable_encoder)
    except (TypeError, ValueError):
        return JSONResponse(jsonable_encoder(content), status_code=status_code)
    return Response(body.encode("utf-8"), status_code=status_code,
                    media_type="application/json")
