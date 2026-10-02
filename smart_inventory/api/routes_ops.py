"""Real-time integration API: events, availability, checkout offers, GRB check, sourcing, suppliers, holds."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import analytics
from ..agents import llm, supplier_parser
from ..config import get_policy
from ..engines import pricing, sourcing
from ..engines.compliance import effective_price, gate_supplier_offer
from ..integrations import get_connector
from ..models import (
    BounceEvent,
    ComplianceHold,
    IngestedEvent,
    InventoryBatch,
    OrderLine,
    RetailerSkuInteraction,
    Sku,
    SourcingRequest,
    Supplier,
    SupplierOffer,
    TermFlag,
)
from .deps import Principal, business_today, get_db, require

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------ events
class Event(BaseModel):
    event_id: str = Field(description="Idempotency key, unique per event across channels")
    schema_version: str = "1"
    type: Literal["order_line", "bounce_candidate", "search", "cart_add", "ask"]
    ts: datetime | None = None
    retailer_id: str
    sku_id: str
    warehouse_id: str
    qty: float = 1.0
    price: float | None = None
    discount_pct: float | None = None
    channel: str | None = None
    order_id: str | None = None
    line_id: str | None = None
    term_flag: Literal["NORMAL", "SPECIAL_NON_RETURNABLE"] = "NORMAL"
    offer_id: str | None = None


def _process_event(db: Session, e: Event, today: date) -> dict[str, Any]:
    if db.get(IngestedEvent, e.event_id):
        return {"event_id": e.event_id, "status": "duplicate"}
    ts = e.ts.replace(tzinfo=None) if e.ts else datetime.utcnow()
    out: dict[str, Any] = {"event_id": e.event_id, "status": "accepted"}
    if e.type == "order_line":
        sku = db.get(Sku, e.sku_id)
        if e.term_flag == TermFlag.SPECIAL_NON_RETURNABLE.value and not e.offer_id:
            raise HTTPException(422, "special-term order lines must carry the offer_id shown at checkout")
        db.add(OrderLine(
            order_id=e.order_id or e.event_id, line_id=e.line_id or "1", retailer_id=e.retailer_id, sku_id=e.sku_id,
            warehouse_id=e.warehouse_id, qty=e.qty, price=e.price if e.price is not None else (sku.selling_base if sku else 0.0),
            discount_pct=e.discount_pct if e.discount_pct is not None else (sku.normal_discount_pct if sku else 0.0),
            channel=e.channel, term_flag=e.term_flag, offer_id=e.offer_id, ts=ts,
        ))
    elif e.type == "bounce_candidate":
        bounce_id = f"BNC-{e.event_id}"
        db.add(BounceEvent(bounce_id=bounce_id, order_line_id=f"{e.order_id}-{e.line_id}" if e.order_id else None, sku_id=e.sku_id,
                           retailer_id=e.retailer_id, warehouse_id=e.warehouse_id, qty=e.qty, reason_code="NOT_IN_STOCK",
                           sourcing_attempted=True, outcome="pending", ts=ts))
        db.flush()
        try:
            req = sourcing.open_request(db, get_policy(), e.sku_id, e.warehouse_id, e.retailer_id, e.qty, today,
                                        bounce_id=bounce_id, order_line_ref=f"{e.order_id}-{e.line_id}" if e.order_id else None)
            out.update(sourcing_request_id=req.id, sourcing_status=req.status, eta_hours=req.eta_hours, reason=req.failure_reason,
                       retailer_message=_retailer_state(req))
        except Exception as exc:  # fail-open: never block the portal
            log.exception("sourcing failed for %s", e.event_id)
            b = db.get(BounceEvent, bounce_id)
            b.outcome, b.reason_code = "final", "EXTERNAL_SOURCING_FAILED"
            out.update(sourcing_status="ERROR", retailer_message="NOT_AVAILABLE", error=str(exc))
    else:
        db.add(RetailerSkuInteraction(event_id=e.event_id, retailer_id=e.retailer_id, sku_id=e.sku_id, warehouse_id=e.warehouse_id,
                                      event_type=e.type, ts=ts))
    db.add(IngestedEvent(event_id=e.event_id, event_type=e.type, schema_version=e.schema_version))
    return out


def _retailer_state(req: SourcingRequest) -> str:
    return {"HELD": "AVAILABLE_ON_REQUEST", "OPEN": "CHECKING_SUPPLIERS", "FAILED": "NOT_AVAILABLE"}.get(req.status, req.status)


@router.post("/events", tags=["events"])
def ingest_events(events: list[Event] | Event, db: Session = Depends(get_db), _: Principal = Depends(require("portal", "erp"))):
    """Ingest order / search / cart / bounce events from portal, app and WhatsApp (same schema for all channels)."""
    items = events if isinstance(events, list) else [events]
    today = business_today()
    results = []
    for e in items:
        try:
            results.append(_process_event(db, e, today))
            db.commit()
        except IntegrityError:
            db.rollback()
            results.append({"event_id": e.event_id, "status": "duplicate"})
    return {"results": results}


# ------------------------------------------------------------ availability
@router.get("/availability", tags=["portal"])
def availability(sku_id: str, warehouse_id: str, qty: float = 1, retailer_id: str | None = None,
                 db: Session = Depends(get_db), _: Principal = Depends(require("portal", "procurement"))):
    """Stock + sourcing ETA for the portal. Never errors: on internal failure it fails open (state UNKNOWN)."""
    try:
        sku = db.get(Sku, sku_id)
        if sku is None:
            raise HTTPException(404, "unknown sku")
        if sourcing.is_held(db, sku_id, warehouse_id):
            return {"sku_id": sku_id, "warehouse_id": warehouse_id, "state": "COMPLIANCE_HOLD", "available_qty": 0}
        live = None
        try:
            live = get_connector().live_stock(sku_id, warehouse_id)
        except Exception:  # noqa: BLE001
            live = None
        if live is None:
            snap = db.scalar(select(func.max(InventoryBatch.snapshot_date)))
            live = db.scalar(select(func.coalesce(func.sum(InventoryBatch.qty), 0)).where(
                InventoryBatch.sku_id == sku_id, InventoryBatch.warehouse_id == warehouse_id,
                InventoryBatch.snapshot_date == snap, InventoryBatch.on_hold.is_(False))) or 0.0
        alternatives = [
            {"sku_id": s.sku_id, "name": s.name} for s in db.scalars(select(Sku).where(
                Sku.composition == sku.composition, Sku.sku_id != sku_id, Sku.active.is_(True)).limit(5))
        ] if sku.composition else []
        out = {"sku_id": sku_id, "warehouse_id": warehouse_id, "available_qty": float(live),
               "same_composition_alternatives": alternatives,
               "alternatives_note": "Information only. Substitution is the pharmacist's decision and is never automatic."}
        if live >= qty:
            return {**out, "state": "IN_STOCK"}
        offers = [o for o in sourcing.find_offers(db, sku, warehouse_id, qty, get_policy(), business_today(), live_api=False)
                  if o["gate_status"] == "PASS" and o["available_qty"] > 0]
        db.rollback()  # find_offers annotates gate status; nothing to persist from a read
        if offers:
            return {**out, "state": "AVAILABLE_ON_REQUEST", "eta_hours": offers[0]["eta_hours"]}
        return {**out, "state": "NOT_AVAILABLE"}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("availability failed")
        return {"sku_id": sku_id, "warehouse_id": warehouse_id, "state": "UNKNOWN", "fail_open": True, "error": str(exc)}


# ------------------------------------------------------- checkout and GRB
@router.get("/offers", tags=["portal"])
def checkout_offers(retailer_id: str, sku_id: str, warehouse_id: str, db: Session = Depends(get_db),
                    _: Principal = Depends(require("portal", "procurement"))):
    """Two transparent options at checkout: Normal discount + normal GRB terms, or Special discount + non-returnable."""
    try:
        return pricing.checkout_options(db, get_policy(), retailer_id, sku_id, warehouse_id, business_today())
    except KeyError:
        raise HTTPException(404, "unknown sku") from None


@router.get("/returns/check", tags=["erp"])
def returns_check(order_id: str, line_id: str = "1", db: Session = Depends(get_db), _: Principal = Depends(require("erp", "portal"))):
    """Called by the ERP credit-note / GRB flow: special-term lines must be blocked."""
    return pricing.grb_allowed(db, order_id, line_id)


# --------------------------------------------------------------- sourcing
class SourcingIn(BaseModel):
    sku_id: str
    warehouse_id: str
    qty: float = 1
    retailer_id: str | None = None


@router.post("/sourcing/requests", tags=["sourcing"])
def open_sourcing(body: SourcingIn, db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "portal"))):
    req = sourcing.open_request(db, get_policy(), body.sku_id, body.warehouse_id, body.retailer_id, body.qty, business_today())
    db.commit()
    return _req_out(req)


@router.get("/sourcing/queue", tags=["sourcing"])
def sourcing_queue(status: str | None = None, warehouse_id: str | None = None, limit: int = 100,
                   db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "management"))):
    return analytics.sourcing_queue(db, status, warehouse_id, limit)


@router.get("/sourcing/requests/{request_id}", tags=["sourcing"])
def get_sourcing(request_id: str, db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "portal"))):
    req = db.get(SourcingRequest, request_id)
    if req is None:
        raise HTTPException(404, "not found")
    return _req_out(req)


@router.post("/sourcing/requests/{request_id}/{action}", tags=["sourcing"])
def act_sourcing(request_id: str, action: Literal["confirm_purchase", "fulfilled", "cancel", "retry"],
                 db: Session = Depends(get_db), p: Principal = Depends(require("procurement"))):
    req = db.get(SourcingRequest, request_id)
    if req is None:
        raise HTTPException(404, "not found")
    try:
        if action == "retry":
            sourcing.evaluate_request(db, get_policy(), req, business_today(), final_if_none=False)
        else:
            sourcing.resolve_request(db, req, action, p.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return _req_out(req)


def _req_out(req: SourcingRequest) -> dict[str, Any]:
    return {"id": req.id, "sku_id": req.sku_id, "warehouse_id": req.warehouse_id, "retailer_id": req.retailer_id, "qty": req.qty,
            "status": req.status, "retailer_state": _retailer_state(req), "eta_hours": req.eta_hours, "failure_reason": req.failure_reason,
            "value": req.value, "ranked_offers": req.ranked_offers}


# --------------------------------------------------------------- suppliers
class OfferIn(BaseModel):
    supplier_id: str
    sku_id: str
    price: float
    scheme: str | None = None
    available_qty: float | None = None
    batch: str | None = None
    expiry: date | None = None
    eta_hours: float | None = None


@router.post("/supplier-offers", tags=["suppliers"])
def add_offer(body: OfferIn, db: Session = Depends(get_db), _: Principal = Depends(require("procurement"))):
    """Manual offer entry by procurement (fallback adapter). Gated immediately; open requests re-evaluated."""
    sup, sku = db.get(Supplier, body.supplier_id), db.get(Sku, body.sku_id)
    if sku is None:
        raise HTTPException(404, "unknown sku")
    eff = effective_price(body.price, body.scheme)
    gate = gate_supplier_offer({"batch": body.batch, "expiry": body.expiry, "effective_price": eff, "price": body.price},
                               sourcing._supplier_dict(sup) if sup else None, sourcing._sku_dict(sku), business_today(),
                               get_policy().section("sourcing"), held=sourcing.is_held(db, sku.sku_id, None))
    o = SupplierOffer(**body.model_dump(), effective_price=eff, source="manual", gate_status=gate.status, gate_failures=gate.failures)
    db.add(o)
    db.flush()
    resolved = sourcing.reevaluate_for_sku(db, get_policy(), sku.sku_id, business_today())
    db.commit()
    return {"offer_id": o.id, "gate_status": gate.status, "gate_failures": gate.failures, "effective_price": eff,
            "sourcing_requests_resolved": resolved}


class SupplierMessage(BaseModel):
    text: str = Field(min_length=3, max_length=50000)
    supplier_id: str | None = None
    channel: str = "whatsapp"


@router.post("/supplier-offers/parse", tags=["suppliers", "agents"])
def parse_offer(body: SupplierMessage, db: Session = Depends(get_db), _: Principal = Depends(require("procurement"))):
    """Claude parses a supplier WhatsApp / email / price list into gated offers."""
    try:
        out = supplier_parser.parse_and_store(db, get_policy(), body.text, body.supplier_id, business_today(), body.channel)
    except llm.LLMUnavailable as exc:
        db.commit()
        raise HTTPException(503, f"Claude unavailable: {exc}. Enter the offer manually via POST /api/v1/supplier-offers.") from exc
    db.commit()
    return out


class InboundWhatsApp(BaseModel):
    from_: str = Field(alias="from")
    text: str
    message_id: str | None = None


@router.post("/webhooks/whatsapp", tags=["suppliers"])
def whatsapp_inbound(body: InboundWhatsApp, db: Session = Depends(get_db), _: Principal = Depends(require("portal", "erp"))):
    """Inbound WhatsApp (Gupshup / AiSensy webhook). Supplier replies are parsed into offers; retailer CALLBACK replies are logged."""
    sup = db.scalar(select(Supplier).where(Supplier.contact == body.from_))
    if sup is not None:
        try:
            out = supplier_parser.parse_and_store(db, get_policy(), body.text, sup.supplier_id, business_today(), "whatsapp")
            db.commit()
            return {"handled_as": "supplier_offer", **out}
        except llm.LLMUnavailable as exc:
            db.commit()
            return {"handled_as": "supplier_offer", "queued_for_manual_entry": True, "reason": str(exc)}
    db.add(RetailerSkuInteraction(event_id=body.message_id or f"wa-{uuid.uuid4().hex[:12]}", retailer_id=body.from_, sku_id="UNKNOWN",
                                  event_type="ask", ts=datetime.utcnow()))
    db.commit()
    return {"handled_as": "retailer_message", "logged": True}


# -------------------------------------------------------------- compliance
class HoldIn(BaseModel):
    sku_id: str
    warehouse_id: str | None = None
    batch: str | None = None
    reason: str
    source: str = "manual"


@router.get("/compliance/holds", tags=["compliance"])
def list_holds(db: Session = Depends(get_db), _: Principal = Depends(require("procurement", "management"))):
    return [{c.name: getattr(h, c.name) for c in ComplianceHold.__table__.columns}
            for h in db.scalars(select(ComplianceHold).where(ComplianceHold.active.is_(True)))]


@router.post("/compliance/holds", tags=["compliance"])
def add_hold(body: HoldIn, db: Session = Depends(get_db), _: Principal = Depends(require("procurement"))):
    """Recall / quarantine / licence hold (CDSCO, state FDA, QC). SKU-level holds become P0 and block sale + sourcing."""
    h = ComplianceHold(**body.model_dump())
    db.add(h)
    if body.batch:
        for b in db.scalars(select(InventoryBatch).where(InventoryBatch.sku_id == body.sku_id, InventoryBatch.batch == body.batch)):
            b.on_hold = True
    db.commit()
    return {"id": h.id, "active": True}


@router.delete("/compliance/holds/{hold_id}", tags=["compliance"])
def release_hold(hold_id: int, db: Session = Depends(get_db), _: Principal = Depends(require("procurement"))):
    h = db.get(ComplianceHold, hold_id)
    if h is None:
        raise HTTPException(404, "not found")
    h.active = False
    db.commit()
    return {"id": h.id, "active": False}
