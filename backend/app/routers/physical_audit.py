"""Physical Stock Audit — the counted stocktake, over HTTP.

One screen, so the endpoints are shaped for one screen: the filter panel asks
`/options` for what it may offer and `/preview` for how much a filter set would
cover, the grid asks `/{id}` for its rows, and everything that changes a count
takes the count's id. Nothing here moves stock except `/apply`, which needs an
approved count and a change reason before it will.

See services/physical_audit for why a count is not an adjustment, and why this
is a second audit rather than a bigger version of the scan-through next door.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..fastjson import returns_json
from ..services import physical_audit as audit, scope

router = APIRouter(prefix="/api/physical-audit", tags=["physical-audit"])


# --- what the screen sends -------------------------------------------------
class OpenIn(BaseModel):
    note: Optional[str] = None
    #: The left panel, verbatim. Kept as a free dict rather than twelve optional
    #: fields because the panel is driven by the catalogue's attributes and a
    #: warehouse that trades sarees offers different ones — a fixed schema here
    #: would have to be edited every time a business line is added.
    filters: Optional[dict] = None


class PreviewIn(BaseModel):
    filters: Optional[dict] = None


class CountIn(BaseModel):
    qty: Optional[float] = None
    note: Optional[str] = None


class ScanIn(BaseModel):
    code: str
    qty: Optional[float] = 1


class UploadRow(BaseModel):
    code: str
    qty: Optional[float] = 1


class UploadIn(BaseModel):
    rows: List[UploadRow]


class StatusIn(BaseModel):
    status: str


class ApplyIn(BaseModel):
    reason: Optional[str] = None


class ReasonIn(BaseModel):
    change_reason: Optional[str] = None


# --- helpers ---------------------------------------------------------------
def _who(request: Request):
    """Who is counting — set on the request by the auth middleware."""
    return (getattr(request.state, "user", None) or {}).get("username")


def _counting_warehouse(db, wid):
    """Which building is being counted.

    A count is physical: somebody walked one warehouse's racks. A blank is
    resolved only when the answer is not a guess — one warehouse on file means
    there IS only one — and otherwise refused, because filing Erode's count
    against Karur is the kind of wrong that is found months later, if ever.
    """
    if wid:
        return wid
    ids = [w.id for w in db.query(models.Warehouse).limit(2).all()]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise HTTPException(400, "There are no warehouses yet — add one first.")
    raise HTTPException(400, "Say which warehouse is being counted — this company "
                             "has more than one, and a count belongs to the "
                             "building whose shelves were walked.")


def _audit_or_404(db, audit_id) -> models.PhysicalAudit:
    row = db.get(models.PhysicalAudit, audit_id)
    if not row:
        raise HTTPException(404, "that count does not exist")
    return row


def _line_or_404(audit, line_id) -> models.PhysicalAuditLine:
    line = next((l for l in audit.lines if l.id == line_id), None)
    if line is None:
        raise HTTPException(404, "that row is not on this count")
    return line


def _guard(fn, *args, **kwargs):
    """Run a service call, turning its refusals into 400s with the reason.

    The service raises AuditError with a sentence written for a person; this is
    the one place it becomes a status code, so no endpoint can lose the wording
    by rephrasing it.
    """
    try:
        return fn(*args, **kwargs)
    except audit.AuditError as exc:
        raise HTTPException(400, str(exc))


# --- choosing what to count ------------------------------------------------
@returns_json(router.get("/options"))
def options(db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """What the filter panel may offer, for this warehouse's own stock."""
    return audit.filter_options(db, _counting_warehouse(db, wid))


@router.post("/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """How much a filter set covers, before anybody commits to counting it.

    Opening a count over the wrong filters is expensive to undo — it is a
    document with a number — so the panel says "4,312 items, 18,004 pieces"
    first. The Search button on the screen is this.
    """
    return audit.preview(db, _counting_warehouse(db, wid), body.filters or {})


# --- the count itself ------------------------------------------------------
@router.get("/current")
def current(db: Session = Depends(get_db),
            wid: Optional[int] = Depends(scope.current)):
    """The count in progress here, or null — what the screen asks on opening."""
    row = audit.current(db, _counting_warehouse(db, wid))
    return audit.audit_out(db, row) if row else None


@router.post("/open")
def open_audit(body: OpenIn, request: Request, db: Session = Depends(get_db),
               wid: Optional[int] = Depends(scope.current)):
    wid = _counting_warehouse(db, wid)
    row = _guard(audit.open_audit, db, wid, by=_who(request), note=body.note,
                 filters=body.filters)
    db.commit()
    db.refresh(row)
    return audit.audit_out(db, row)


@router.get("")
def list_audits(limit: int = 50, db: Session = Depends(get_db),
                wid: Optional[int] = Depends(scope.current)):
    """Past counts of this warehouse, newest first."""
    q = scope.physical_audits(db.query(models.PhysicalAudit), wid)
    rows = q.order_by(models.PhysicalAudit.id.desc()).limit(
        max(1, min(limit, 500))).all()
    return [audit.audit_out(db, r, with_lines=False) for r in rows]


@router.get("/{audit_id}")
def get_audit(audit_id: int, db: Session = Depends(get_db)):
    return audit.audit_out(db, _audit_or_404(db, audit_id))


@router.patch("/{audit_id}")
def set_reason(audit_id: int, body: ReasonIn, db: Session = Depends(get_db)):
    """The Change Reason box at the foot of the screen.

    Saved as it is typed rather than only at apply time, so the reason survives
    the count being put down on Monday and approved on Thursday by somebody else.
    """
    row = _audit_or_404(db, audit_id)
    row.change_reason = (body.change_reason or "").strip()[:256] or None
    db.commit()
    return audit.audit_out(db, row, with_lines=False)


@router.post("/{audit_id}/lines/{line_id}/count")
def count_line(audit_id: int, line_id: int, body: CountIn, request: Request,
               db: Session = Depends(get_db)):
    """Type what is on the shelf for one row. A blank qty clears it.

    Clearing is the same endpoint rather than a second one because the screen's
    Count box is one control: emptying it means "nobody has counted this", which
    is a real state and not the same as zero — see services/physical_audit.clear.
    """
    row = _audit_or_404(db, audit_id)
    line = _line_or_404(row, line_id)
    if body.qty is None:
        _guard(audit.clear, db, row, line)
    else:
        _guard(audit.record, db, row, line, body.qty, by=_who(request),
               note=body.note)
    db.commit()
    return {"line": audit.line_out(line), "totals": audit.totals(db, row)}


@router.delete("/{audit_id}/lines/{line_id}")
def drop_line(audit_id: int, line_id: int, db: Session = Depends(get_db)):
    """Take an uncounted row off the count — the Remove half of Direct Add/Remove."""
    row = _audit_or_404(db, audit_id)
    line = _line_or_404(row, line_id)
    _guard(audit.remove_line, db, row, line)
    db.commit()
    return {"ok": True, "totals": audit.totals(db, row)}


@router.post("/{audit_id}/scan")
def scan(audit_id: int, body: ScanIn, request: Request,
         db: Session = Depends(get_db)):
    """One tag off the barcode row at the top of the grid. Scanning ADDS."""
    row = _audit_or_404(db, audit_id)
    line, message = _guard(audit.scan, db, row, body.code, qty=body.qty or 1,
                           by=_who(request))
    db.commit()
    return {"line": audit.line_out(line), "message": message,
            "totals": audit.totals(db, row)}


@router.post("/{audit_id}/add/{product_id}")
def add_line(audit_id: int, product_id: int, request: Request,
             db: Session = Depends(get_db)):
    """Put an item on the count by hand, for a tag that will not scan."""
    row = _audit_or_404(db, audit_id)
    product = db.get(models.Product, product_id)
    if product is None:
        raise HTTPException(404, "product not found")
    line = _guard(audit.add_product, db, row, product, by=_who(request))
    db.commit()
    return {"line": audit.line_out(line), "totals": audit.totals(db, row)}


@router.post("/{audit_id}/upload")
def upload(audit_id: int, body: UploadIn, request: Request,
           db: Session = Depends(get_db)):
    """Counts off a handheld's file. Quantities REPLACE, so a re-run is safe."""
    row = _audit_or_404(db, audit_id)
    result = _guard(audit.upload, db, row,
                    [r.model_dump() for r in body.rows], by=_who(request))
    db.commit()
    result["totals"] = audit.totals(db, row)
    return result


@router.post("/{audit_id}/synchronize")
def synchronize(audit_id: int, db: Session = Depends(get_db)):
    """Re-read the books for uncounted rows, and pull in stock received since."""
    row = _audit_or_404(db, audit_id)
    result = _guard(audit.synchronize, db, row)
    db.commit()
    db.refresh(row)
    return {**result, "audit": audit.audit_out(db, row)}


# --- the way through -------------------------------------------------------
@router.post("/{audit_id}/status")
def set_status(audit_id: int, body: StatusIn, request: Request,
               db: Session = Depends(get_db)):
    row = _audit_or_404(db, audit_id)
    _guard(audit.set_status, db, row, body.status, by=_who(request))
    db.commit()
    db.refresh(row)
    return audit.audit_out(db, row, with_lines=False)


@router.post("/{audit_id}/apply")
def apply(audit_id: int, body: ApplyIn, request: Request,
          db: Session = Depends(get_db)):
    """Write the approved variance into the ledger. Once, and only once."""
    row = _audit_or_404(db, audit_id)
    result = _guard(audit.apply, db, row, by=_who(request), reason=body.reason)
    db.commit()
    db.refresh(row)
    return {**result, "audit": audit.audit_out(db, row, with_lines=False)}
