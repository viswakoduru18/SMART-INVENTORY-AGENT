"""Procurement console + management dashboard + agents + admin API."""
from __future__ import annotations

import datetime as dt
import threading
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import analytics
from ..agents import explainer, insights, llm, operations
from ..config import Policy, get_policy, set_policy_override
from ..db import session_scope
from ..integrations import get_connector
from ..models import Action, AgentRun, DataQualityResult, DecisionLog, Notification, PriceOffer, SuggestedPO
from ..pipeline.daily_cycle import push_offer, push_po, run_daily_cycle
from .deps import Principal, business_today, get_db, plan_date, require

router = APIRouter(prefix="/api/v1")
_cycle_lock = threading.Lock()


def _shadow() -> bool:
    return str(get_policy().get("autonomy.mode", "ASSIST")).upper() == "SHADOW"


# ---------------------------------------------------------------- decisions
@router.get("/decisions", tags=["decisions"])
def list_decisions(date: dt.date | None = None, warehouse_id: str | None = None, action: str | None = None, status: str | None = None,
                   limit: int = 200, offset: int = 0, db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "management"))):
    """Orchestrator output: one primary action per SKU x warehouse for the day."""
    return analytics.decisions(db, date, warehouse_id, action, status, min(limit, 1000), offset)


class OverrideIn(BaseModel):
    action: Literal["COMPLIANCE_HOLD", "LIQUIDATE", "SOURCE", "STOCK", "DISCOUNT", "SELL", "DONT_STOCK"]
    reason: str = Field(min_length=3, max_length=400)
    by: str | None = None


@router.post("/decisions/{decision_id}/override", tags=["decisions"])
def override(decision_id: int, body: OverrideIn, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    """Approve / reject with reason. Overrides are a core metric: they show where the rules are wrong."""
    d = db.get(DecisionLog, decision_id)
    if d is None:
        raise HTTPException(404, "not found")
    d.overridden_by, d.override_action, d.override_reason, d.status = body.by or p.name, body.action, body.reason, "OVERRIDDEN"
    if d.action == Action.STOCK.value and body.action != Action.STOCK.value:
        for po in db.scalars(select(SuggestedPO).where(SuggestedPO.date == d.date, SuggestedPO.sku_id == d.sku_id,
                                                       SuggestedPO.warehouse_id == d.warehouse_id, SuggestedPO.status.in_(["DRAFT", "DEFERRED"]))):
            po.status, po.note, po.approved_by = "REJECTED", f"decision override: {body.reason}"[:400], body.by or p.name
    db.commit()
    return {"id": d.id, "status": d.status, "override_action": d.override_action}


@router.post("/decisions/{decision_id}/approve", tags=["decisions"])
def approve_decision(decision_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    d = db.get(DecisionLog, decision_id)
    if d is None:
        raise HTTPException(404, "not found")
    d.status, d.overridden_by = "APPROVED", None
    d.outcome = {**(d.outcome or {}), "approved_by": p.name}
    db.commit()
    return {"id": d.id, "status": d.status}


# --------------------------------------------------------------- PO drafts
@router.get("/po-drafts", tags=["purchase"])
def list_po(date: dt.date | None = None, warehouse_id: str | None = None, status: str | None = None, limit: int = 500,
            db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "management"))):
    day = analytics.resolve_date(db, date)
    stmt = select(SuggestedPO).where(SuggestedPO.date == day)
    if warehouse_id:
        stmt = stmt.where(SuggestedPO.warehouse_id == warehouse_id)
    if status:
        stmt = stmt.where(SuggestedPO.status == status)
    return [{c.name: getattr(p, c.name) for c in SuggestedPO.__table__.columns} for p in db.scalars(stmt.limit(limit))]


class POApprove(BaseModel):
    qty: float | None = Field(default=None, ge=0, description="Edited quantity; omit to accept the suggestion")
    supplier_id: str | None = None
    by: str | None = None


def _approve_po(db: Session, po: SuggestedPO, body: POApprove, who: str) -> dict[str, Any]:
    if po.status not in ("DRAFT", "DEFERRED"):
        raise HTTPException(409, f"PO {po.po_draft_id} is {po.status}")
    notes = []
    if body.qty is not None and body.qty != po.qty:
        notes.append(f"qty edited {po.qty:g}->{body.qty:g}")
        po.qty, po.line_value = body.qty, round(body.qty * po.unit_cost, 2)
    if body.supplier_id and body.supplier_id != po.supplier_id:
        notes.append(f"supplier {po.supplier_id}->{body.supplier_id}")
        po.supplier_id = body.supplier_id
    if not po.supplier_id:
        raise HTTPException(422, "choose a supplier before approving (no gated supplier was found)")
    po.status, po.approved_by = "APPROVED", body.by or who
    if notes:
        po.note = "; ".join(notes)
    if not _shadow():
        push_po(db, po, get_connector())
    return {"po_draft_id": po.po_draft_id, "status": po.status, "erp_ref": po.erp_ref, "note": po.note}


@router.post("/po-drafts/{po_id}/approve", tags=["purchase"])
def approve_po(po_id: str, body: POApprove | None = None, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    """Approve (optionally edit) a draft PO; pushed to the ERP as a DRAFT purchase order (not in SHADOW mode)."""
    po = db.get(SuggestedPO, po_id)
    if po is None:
        raise HTTPException(404, "not found")
    out = _approve_po(db, po, body or POApprove(), p.name)
    db.commit()
    return out


class BulkApprove(BaseModel):
    po_draft_ids: list[str]
    by: str | None = None


@router.post("/po-drafts/approve-bulk", tags=["purchase"])
def approve_bulk(body: BulkApprove, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    results = []
    for pid in body.po_draft_ids:
        po = db.get(SuggestedPO, pid)
        if po is None:
            results.append({"po_draft_id": pid, "error": "not found"})
            continue
        try:
            results.append(_approve_po(db, po, POApprove(by=body.by), p.name))
        except HTTPException as exc:
            results.append({"po_draft_id": pid, "error": exc.detail})
    db.commit()
    return {"results": results}


class Reject(BaseModel):
    reason: str = Field(min_length=3, max_length=400)
    by: str | None = None


@router.post("/po-drafts/{po_id}/reject", tags=["purchase"])
def reject_po(po_id: str, body: Reject, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    po = db.get(SuggestedPO, po_id)
    if po is None:
        raise HTTPException(404, "not found")
    if po.status not in ("DRAFT", "DEFERRED"):
        raise HTTPException(409, f"PO is {po.status}")
    po.status, po.note, po.approved_by = "REJECTED", body.reason, body.by or p.name
    db.commit()
    return {"po_draft_id": po.po_draft_id, "status": po.status}


# ------------------------------------------------------------ price offers
@router.get("/price-offers", tags=["pricing"])
def list_price_offers(date: dt.date | None = None, status: str | None = None, kind: str | None = None, warehouse_id: str | None = None,
                      limit: int = 500, db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "management"))):
    day = analytics.resolve_date(db, date)
    stmt = select(PriceOffer).where(PriceOffer.date == day)
    for col, val in ((PriceOffer.status, status), (PriceOffer.kind, kind), (PriceOffer.warehouse_id, warehouse_id)):
        if val:
            stmt = stmt.where(col == val)
    return [{c.name: getattr(o, c.name) for c in PriceOffer.__table__.columns} for o in db.scalars(stmt.limit(limit))]


@router.post("/price-offers/{offer_id}/approve", tags=["pricing"])
def approve_offer(offer_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    """Approve a special lot / price test / supplier return. Special lots are published to the ERP with the term flag."""
    o = db.get(PriceOffer, offer_id)
    if o is None:
        raise HTTPException(404, "not found")
    if o.status != "PROPOSED":
        raise HTTPException(409, f"offer is {o.status}")
    o.status, o.approved_by = "APPROVED", p.name
    if o.kind == "SPECIAL_LOT" and not _shadow():
        push_offer(db, o, get_connector())
    db.commit()
    return {"offer_id": o.offer_id, "status": o.status, "erp_ref": o.erp_ref}


@router.post("/price-offers/{offer_id}/reject", tags=["pricing"])
def reject_offer(offer_id: str, body: Reject, db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    o = db.get(PriceOffer, offer_id)
    if o is None:
        raise HTTPException(404, "not found")
    o.status, o.approved_by, o.reason = "REJECTED", body.by or p.name, f"{o.reason} | rejected: {body.reason}"[:400]
    db.commit()
    return {"offer_id": o.offer_id, "status": o.status}


# --------------------------------------------------------------- dashboard
MGMT = ("management", "procurement")


@router.get("/dashboard/kpis", tags=["dashboard"])
def d_kpis(date: dt.date | None = None, warehouse_id: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.kpis(db, date, warehouse_id)


@router.get("/dashboard/inventory", tags=["dashboard"])
def d_inventory(date: dt.date | None = None, warehouse_id: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.inventory_view(db, date, warehouse_id)


@router.get("/dashboard/bounce", tags=["dashboard"])
def d_bounce(date: dt.date | None = None, warehouse_id: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.bounce_view(db, date, warehouse_id)


@router.get("/dashboard/purchase", tags=["dashboard"])
def d_purchase(date: dt.date | None = None, warehouse_id: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.purchase_view(db, date, warehouse_id)


@router.get("/dashboard/margin", tags=["dashboard"])
def d_margin(date: dt.date | None = None, warehouse_id: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.margin_view(db, date, warehouse_id)


@router.get("/skus/search", tags=["dashboard"])
def sku_search(q: str, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT, "portal"))):
    return analytics.find_skus(db, q, 20)


@router.get("/skus/{sku_id}", tags=["dashboard"])
def sku_detail(sku_id: str, warehouse_id: str | None = None, date: dt.date | None = None, db: Session = Depends(get_db),
               _: Principal = Depends(require(*MGMT))):
    out = analytics.sku_profile(db, sku_id, warehouse_id, date)
    if out is None:
        raise HTTPException(404, "unknown sku")
    return out


# ------------------------------------------------------------------ agents
class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    date: dt.date | None = None


@router.post("/agents/ask", tags=["agents"])
def agent_ask(body: Ask, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    """Natural-language question over the dashboards (English or Telugu). Read-only."""
    try:
        out = insights.ask(db, body.question, body.date)
    except llm.LLMUnavailable as exc:
        db.commit()
        raise HTTPException(503, f"Claude unavailable: {exc}") from exc
    db.commit()
    return out


@router.get("/agents/explain/{decision_id}", tags=["agents"])
def agent_explain(decision_id: int, lang: Literal["en", "te"] = "en", db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    try:
        out = explainer.explain(db, decision_id, lang)
    except KeyError:
        raise HTTPException(404, "decision not found") from None
    db.commit()
    return out


class OpsRun(BaseModel):
    run_cycle: bool = False
    notify: bool = False
    task: str | None = None
    date: dt.date | None = None


@router.post("/agents/ops/run", tags=["agents"])
def agent_ops(body: OpsRun, db: Session = Depends(get_db), _: Principal = Depends(require("procurement"))):
    """Operations agent: optionally runs the cycle, sweeps sourcing, writes and sends the briefing."""
    day = body.date or (plan_date() if body.run_cycle else analytics.resolve_date(db, None))
    if body.run_cycle and not _cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "a decision cycle is already running")
    try:
        return operations.run(db, day, get_connector(), run_cycle=body.run_cycle, notify=body.notify, task=body.task)
    finally:
        if body.run_cycle:
            _cycle_lock.release()


@router.get("/agents/runs", tags=["agents"])
def agent_runs(agent: str | None = None, limit: int = 50, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(min(limit, 200))
    if agent:
        stmt = stmt.where(AgentRun.agent == agent)
    return [{c.name: getattr(r, c.name) for c in AgentRun.__table__.columns} for r in db.scalars(stmt)]


@router.get("/agents/status", tags=["agents"])
def agent_status(_: Principal = Depends(require(*MGMT))):
    from ..config import get_settings
    s = get_settings()
    return {"llm_available": llm.available(), "model": s.llm_model, "effort": s.llm_effort, "fallbacks": s.llm_fallbacks,
            "agents": ["supplier_parser", "explainer", "insights", "operations"]}


# ------------------------------------------------------------------- admin
class CycleIn(BaseModel):
    date: dt.date | None = None
    sync: bool = True
    full_sync: bool = False
    publish: bool = False
    enforce_data_quality: bool = True
    background: bool = False


def _run_cycle_bg(day: date, body: CycleIn) -> None:
    try:
        with session_scope() as s:
            run_daily_cycle(s, day, erp=get_connector(), sync=body.sync, full_sync=body.full_sync, publish=body.publish,
                            enforce_dq=body.enforce_data_quality)
    finally:
        _cycle_lock.release()


@router.post("/admin/run-cycle", tags=["admin"])
def admin_run_cycle(body: CycleIn, tasks: BackgroundTasks, db: Session = Depends(get_db), _: Principal = Depends(require())):
    day = body.date or plan_date()
    if not _cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "a decision cycle is already running")
    if body.background:
        tasks.add_task(_run_cycle_bg, day, body)
        return {"accepted": True, "run_date": day.isoformat()}
    try:
        return run_daily_cycle(db, day, erp=get_connector(), sync=body.sync, full_sync=body.full_sync, publish=body.publish,
                               enforce_dq=body.enforce_data_quality)
    finally:
        _cycle_lock.release()


@router.get("/admin/jobs", tags=["admin"])
def admin_jobs(date: dt.date | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return analytics.job_status(db, date)


@router.get("/admin/data-quality", tags=["admin"])
def admin_dq(date: dt.date | None = None, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    day = analytics.resolve_date(db, date)
    return [{c.name: getattr(r, c.name) for c in DataQualityResult.__table__.columns}
            for r in db.scalars(select(DataQualityResult).where(DataQualityResult.run_date == day))]


@router.get("/admin/notifications", tags=["admin"])
def admin_notifications(limit: int = 100, db: Session = Depends(get_db), _: Principal = Depends(require(*MGMT))):
    return [{c.name: getattr(n, c.name) for c in Notification.__table__.columns}
            for n in db.scalars(select(Notification).order_by(Notification.id.desc()).limit(min(limit, 500)))]


@router.get("/admin/policy", tags=["admin"])
def admin_policy(_: Principal = Depends(require(*MGMT))):
    return get_policy().data


class AutonomyIn(BaseModel):
    mode: Literal["SHADOW", "ASSIST", "AUTO"]


@router.put("/admin/policy/autonomy", tags=["admin"])
def admin_autonomy(body: AutonomyIn, _: Principal = Depends(require())):
    """Switch graduated autonomy at runtime (persist the change in config/policy.yaml for restarts)."""
    data = dict(get_policy().data)
    data["autonomy"] = {**data.get("autonomy", {}), "mode": body.mode}
    set_policy_override(Policy(data))
    return {"mode": body.mode, "note": "runtime override; update config/policy.yaml to persist"}


@router.get("/admin/erp/health", tags=["admin"])
def erp_health(_: Principal = Depends(require())):
    return get_connector().health()


@router.get("/meta", tags=["admin"])
def meta(db: Session = Depends(get_db)):
    """Unauthenticated metadata for the console bootstrap (no business data)."""
    return {"business_today": business_today().isoformat(), "plan_date": plan_date().isoformat(),
            "latest_run_date": (analytics.latest_run_date(db) or business_today()).isoformat(),
            "autonomy_mode": get_policy().get("autonomy.mode"), "policy_version": get_policy().version,
            "connector": get_connector().name}
