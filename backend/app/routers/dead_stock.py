"""Dead Stock & Clearance — the register, the ladder, the campaigns.

Five screens are served from here and they are all the same read: the dashboard
is the register totalled, the summary is it grouped, the cash impact is it
valued, and the alerts are it counted at three thresholds. Only the campaigns
write anything, and what they write is a plan — never stock. See
services/dead_stock.py for why that boundary is where it is.
"""
import datetime as dt
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..fastjson import returns_json
from .. import models
from ..services import dead_stock as ds

router = APIRouter(prefix="/api/dead-stock", tags=["dead-stock"])


# ------------------------------------------------------------------ read -----

@returns_json(router.get("/register"))
def register(q: str = "", bucket: str = "", category: str = "", supplier: str = "",
             size: str = "", status: str = "dead",
             min_value: Optional[float] = None, min_qty: Optional[float] = None,
             db: Session = Depends(get_db)):
    """The Dead Stock Register, filtered. `status=all` shows healthy stock too —
    the same read, which is what lets someone check a line they expected to see."""
    return ds.register(db, q=q, bucket=bucket, category=category, supplier=supplier,
                       size=size, status=status, min_value=min_value, min_qty=min_qty)


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Dashboard, Clearance Summary and Cash Impact — one read, grouped three ways."""
    return ds.summary(db)


@router.get("/alerts")
def alerts(db: Session = Depends(get_db)):
    """Approaching / dead / critical, with what each band is worth."""
    return ds.alerts(db)


@router.get("/rules")
def get_rules():
    return {"rules": ds.get_rules(), "defaults": ds.DEFAULT_RULES, "actions": ds.ACTIONS}


class RulesIn(BaseModel):
    buckets: Optional[List[Dict[str, Any]]] = None
    approaching_days: Optional[int] = None
    dead_after_days: Optional[int] = None
    critical_days: Optional[int] = None
    stock_turns: Optional[float] = None
    gross_margin_pct: Optional[float] = None


@router.put("/rules")
def put_rules(body: RulesIn):
    """Change the ladder or the assumptions. Applies to every screen at once —
    the discount is never stored on a product, only on a campaign line that has
    already been approved at it."""
    return {"rules": ds.save_rules(body.model_dump(exclude_none=True))}


# ------------------------------------------------------------- campaigns -----

class CampaignIn(BaseModel):
    name: str
    starts_on: Optional[str] = None
    ends_on: Optional[str] = None
    note: Optional[str] = None
    created_by: Optional[str] = None
    #: products to open the campaign with. Empty is allowed — a worksheet can be
    #: created first and filled from the register afterwards.
    product_ids: List[int] = []
    #: {product_id: "Clear Now"} for anything decided while selecting
    actions: Dict[str, str] = {}


class CampaignPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    starts_on: Optional[str] = None
    ends_on: Optional[str] = None
    note: Optional[str] = None


class LinePatch(BaseModel):
    qty: Optional[float] = None
    discount_pct: Optional[float] = None
    action: Optional[str] = None
    note: Optional[str] = None


class AddLines(BaseModel):
    product_ids: List[int]
    actions: Dict[str, str] = {}


def _get(db, campaign_id) -> models.ClearanceCampaign:
    c = db.get(models.ClearanceCampaign, campaign_id)
    if not c:
        raise HTTPException(404, "Clearance worksheet not found")
    return c


def _rows_by_id(db):
    return {r["product_id"]: r for r in ds.product_rows(db)}


def _add(db, campaign, product_ids, actions):
    """Add products to a worksheet, at today's age and today's ladder.

    A product already on the sheet is skipped rather than doubled: the line
    carries the whole holding, so a second one would promise the same garments
    twice and every total on the campaign would be wrong."""
    rows = _rows_by_id(db)
    have = {l.product_id for l in campaign.lines}
    added, skipped = 0, 0
    for pid in product_ids:
        if pid in have:
            skipped += 1
            continue
        row = rows.get(pid)
        if not row:
            skipped += 1          # gone, or no longer in stock
            continue
        db.add(models.ClearanceLine(campaign_id=campaign.id,
                                    **ds.line_from_row(row, actions.get(str(pid), "Review"))))
        have.add(pid)
        added += 1
    return added, skipped


@router.post("/campaigns")
def create_campaign(body: CampaignIn, db: Session = Depends(get_db)):
    today = dt.date.today()
    c = models.ClearanceCampaign(
        name=(body.name or "").strip() or f"Clearance {today.isoformat()}",
        starts_on=body.starts_on or today.isoformat(),
        # a month, which is what a markdown run is given before it is judged
        ends_on=body.ends_on or (today + dt.timedelta(days=30)).isoformat(),
        note=body.note, created_by=body.created_by, status="draft")
    db.add(c)
    db.flush()
    added, skipped = _add(db, c, body.product_ids, body.actions)
    db.commit()
    db.refresh(c)
    return {**ds.campaign_out(db, c), "added": added, "skipped": skipped}


@router.get("/campaigns")
def list_campaigns(status: str = "all", db: Session = Depends(get_db)):
    q = db.query(models.ClearanceCampaign)
    if status and status != "all":
        q = q.filter(models.ClearanceCampaign.status == status)
    rows = q.order_by(models.ClearanceCampaign.id.desc()).all()
    return [ds.campaign_out(db, c) for c in rows]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    return ds.campaign_out(db, _get(db, campaign_id))


@router.patch("/campaigns/{campaign_id}")
def patch_campaign(campaign_id: int, body: CampaignPatch, db: Session = Depends(get_db)):
    c = _get(db, campaign_id)
    data = body.model_dump(exclude_none=True)
    if "status" in data and data["status"] not in ds.CAMPAIGN_STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(ds.CAMPAIGN_STATUSES)}")
    for k, v in data.items():
        setattr(c, k, v)
    # closing is a date, not just a word: a campaign closed today stops counting
    # today's sales tomorrow, and one reopened starts counting again
    if data.get("status") == "closed":
        c.closed_at = dt.datetime.utcnow()
        c.ends_on = c.ends_on or dt.date.today().isoformat()
    elif "status" in data:
        c.closed_at = None
    db.commit()
    db.refresh(c)
    return ds.campaign_out(db, c)


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = _get(db, campaign_id)
    db.delete(c)                      # lines cascade; no stock is touched
    db.commit()
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/lines")
def add_lines(campaign_id: int, body: AddLines, db: Session = Depends(get_db)):
    c = _get(db, campaign_id)
    added, skipped = _add(db, c, body.product_ids, body.actions)
    db.commit()
    db.refresh(c)
    return {**ds.campaign_out(db, c), "added": added, "skipped": skipped}


@router.patch("/campaigns/{campaign_id}/lines/{line_id}")
def patch_line(campaign_id: int, line_id: int, body: LinePatch,
               db: Session = Depends(get_db)):
    line = db.get(models.ClearanceLine, line_id)
    if not line or line.campaign_id != campaign_id:
        raise HTTPException(404, "Line not found on this worksheet")
    data = body.model_dump(exclude_none=True)
    if "action" in data and data["action"] not in ds.ACTIONS:
        raise HTTPException(400, f"action must be one of {', '.join(ds.ACTIONS)}")
    for k, v in data.items():
        setattr(line, k, v)
    # a hand-set discount re-prices the line it was set on, so the sheet's own
    # arithmetic still adds up — the ladder suggests, the person decides
    if "discount_pct" in data or "qty" in data:
        base = float(line.mrp or line.cost_price or 0)
        line.clearance_price = round(base * (1 - float(line.discount_pct or 0) / 100.0), 2)
        line.expected_realisation = round(line.clearance_price * float(line.qty or 0), 2)
    db.commit()
    db.refresh(line)
    return ds.campaign_out(db, line.campaign)


@router.delete("/campaigns/{campaign_id}/lines/{line_id}")
def delete_line(campaign_id: int, line_id: int, db: Session = Depends(get_db)):
    line = db.get(models.ClearanceLine, line_id)
    if not line or line.campaign_id != campaign_id:
        raise HTTPException(404, "Line not found on this worksheet")
    campaign = line.campaign
    db.delete(line)
    db.commit()
    db.refresh(campaign)
    return ds.campaign_out(db, campaign)
