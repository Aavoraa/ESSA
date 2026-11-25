"""Super-admin only: test and inspect an outside PostgreSQL database (read-only).

The role check is made before any of this runs, by security.POLICY
(``^/api/admin/database`` -> superadmin). See services/extdb for the safety rules.

    POST /api/admin/database/test            test typed or saved settings, step by step
    GET  /api/admin/database/status          environments, connection state, live probe
    POST /api/admin/database/config          save one environment's settings
    POST /api/admin/database/active          switch the active environment
    POST /api/admin/database/connect         open the read-only pool for a saved environment
    POST /api/admin/database/disconnect      close it
    POST /api/admin/database/write-approval  record / revoke an administrator's write approval
    GET  /api/admin/database/inspect         schemas and tables (no rows, ever)
    GET  /api/admin/database/inspect/table   one table's columns, keys and relationships
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..services import extdb

router = APIRouter(prefix="/api/admin/database", tags=["admin-database"])


class Settings(BaseModel):
    environment: str
    host: str = Field("", max_length=253)
    port: int = 5432
    database: str = Field("", max_length=63)
    user: str = Field("", max_length=63)
    password: Optional[str] = Field(None, max_length=1024)
    sslmode: str = "prefer"
    timeout_ms: int = 5000
    pool_max: int = 5
    confirm: Optional[str] = Field(None, max_length=200)     # production: the database name


class EnvAction(BaseModel):
    environment: str
    confirm: Optional[str] = Field(None, max_length=200)


class WriteApproval(BaseModel):
    environment: str
    approve: bool
    confirm: Optional[str] = Field(None, max_length=200)


def _who(request: Request) -> str:
    return (getattr(request.state, "user", None) or {}).get("username", "?")


def _checked(body: Settings) -> dict:
    try:
        return extdb.validate(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))


def _env(env: str) -> str:
    if env not in extdb.ENVIRONMENTS:
        raise HTTPException(422, "Unknown environment.")
    return env


@router.post("/test")
def test(body: Settings):
    p = _checked(body)
    try:
        extdb.check_production(p["environment"], p["database"], body.confirm)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    return extdb.test(p, extdb.password_for_test(p))


@router.get("/status")
def status():
    return extdb.status()


@router.post("/config")
def save(body: Settings, request: Request):
    p = _checked(body)
    try:
        return extdb.save_profile(p, _who(request))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/active")
def set_active(body: EnvAction):
    try:
        extdb.set_active(_env(body.environment), body.confirm)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return extdb.status()


@router.post("/connect")
def connect(body: EnvAction):
    try:
        return extdb.connect(_env(body.environment), body.confirm)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/disconnect")
def disconnect():
    return {"disconnected": extdb.disconnect()}


@router.post("/write-approval")
def write_approval(body: WriteApproval, request: Request):
    try:
        return extdb.set_write_approval(_env(body.environment), body.approve, body.confirm, _who(request))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


def _inspection(fn, *args):
    import psycopg2
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except psycopg2.Error as e:
        msg = extdb._clean(str(e), None).strip().splitlines()[0][:300]
        raise HTTPException(502, f"The database refused the read: {msg}")


@router.get("/inspect")
def inspect(schema: Optional[str] = Query(None, max_length=63)):
    return _inspection(extdb.inspect_tables, schema or None)


@router.get("/inspect/table")
def inspect_table(schema: str = Query(..., min_length=1, max_length=63),
                  table: str = Query(..., min_length=1, max_length=63)):
    return _inspection(extdb.inspect_table, schema, table)
