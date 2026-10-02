"""E5 Sourcing Engine.

Bounced SKU -> search supplier network -> availability -> price/scheme comparison
-> compliance gates (hard) -> ranked offers -> purchase task, and the retailer
sees "Available on request, ETA N hours" instead of "Product Not Available".

Supplier adapters: live API (suppliers with api_url), price lists synced from
the ERP, offers parsed by the Claude supplier-message agent from WhatsApp /
email, and manual entry by procurement. Unapproved or non-compliant sources
never enter the automated flow.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Policy
from ..integrations import notifications
from ..models import BounceEvent, ComplianceHold, Retailer, Sku, SourcingRequest, Supplier, SupplierOffer
from .compliance import effective_price, gate_supplier_offer

log = logging.getLogger(__name__)


def _supplier_dict(s: Supplier) -> dict[str, Any]:
    return {c.name: getattr(s, c.name) for c in Supplier.__table__.columns}


def _sku_dict(s: Sku) -> dict[str, Any]:
    return {c.name: getattr(s, c.name) for c in Sku.__table__.columns}


def is_held(db: Session, sku_id: str, warehouse_id: str | None) -> bool:
    """Active SKU-level compliance hold (all batches) for this warehouse or all warehouses."""
    for h_wh in db.scalars(select(ComplianceHold.warehouse_id).where(
        ComplianceHold.sku_id == sku_id, ComplianceHold.active.is_(True), ComplianceHold.batch.is_(None)
    )):
        if h_wh is None or h_wh == warehouse_id:
            return True
    return False


def gate_all_offers(db: Session, policy: Policy, today: date) -> dict[str, int]:
    """Nightly: evaluate every stored offer against the hard gates."""
    suppliers = {s.supplier_id: _supplier_dict(s) for s in db.scalars(select(Supplier))}
    skus = {s.sku_id: _sku_dict(s) for s in db.scalars(select(Sku))}
    held = {h.sku_id for h in db.scalars(select(ComplianceHold).where(ComplianceHold.active.is_(True), ComplianceHold.batch.is_(None)))}
    cfg = policy.section("sourcing")
    counts = {"PASS": 0, "FAIL": 0}
    updates = []
    for o in db.scalars(select(SupplierOffer)):
        sku = skus.get(o.sku_id)
        if sku is None:
            res_status, failures = "FAIL", ["SKU_UNKNOWN"]
        else:
            res = gate_supplier_offer({"batch": o.batch, "expiry": o.expiry, "effective_price": o.effective_price, "price": o.price},
                                      suppliers.get(o.supplier_id), sku, today, cfg, held=o.sku_id in held)
            res_status, failures = res.status, res.failures
        counts[res_status] += 1
        updates.append({"_id": o.id, "gate_status": res_status, "gate_failures": failures})
    for u in updates:
        db.execute(update(SupplierOffer).where(SupplierOffer.id == u["_id"]).values(gate_status=u["gate_status"], gate_failures=u["gate_failures"]))
    db.flush()
    return counts


def query_supplier_api(supplier: Supplier, sku: Sku, qty: float, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Live availability from a supplier that exposes an API. Expected response: list of offers."""
    if not supplier.api_url:
        return []
    try:
        resp = httpx.get(supplier.api_url, params={"sku_id": sku.sku_id, "composition": sku.composition, "qty": qty}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("offers", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("supplier %s API failed: %s", supplier.supplier_id, exc)
        return []


def find_offers(db: Session, sku: Sku, warehouse_id: str, qty: float, policy: Policy, today: date, live_api: bool = True) -> list[dict[str, Any]]:
    """Ranked, gated offers for a SKU. Each item: offer fields + landed cost, margin, eta, gate status."""
    cfg = policy.section("sourcing")
    fresh_after = datetime.utcnow() - timedelta(hours=float(cfg.get("offer_freshness_hours", 48)))
    suppliers = {s.supplier_id: s for s in db.scalars(select(Supplier))}
    held = is_held(db, sku.sku_id, warehouse_id)
    rows = list(db.scalars(select(SupplierOffer).where(SupplierOffer.sku_id == sku.sku_id, SupplierOffer.ts >= fresh_after)))
    if live_api:
        for s in suppliers.values():
            if s.channel == "api" and s.api_url and s.approved:
                for raw in query_supplier_api(s, sku, qty):
                    price = float(raw.get("price") or 0)
                    offer = SupplierOffer(
                        supplier_id=s.supplier_id, sku_id=sku.sku_id, price=price, scheme=raw.get("scheme"),
                        effective_price=effective_price(price, raw.get("scheme")), available_qty=float(raw.get("available_qty") or 0),
                        batch=raw.get("batch"), expiry=_parse_date(raw.get("expiry")), eta_hours=raw.get("eta_hours"),
                        source="api", gate_status="PENDING", gate_failures=[],
                    )
                    db.add(offer)
                    rows.append(offer)
        db.flush()
    sell = sku.selling_base * (1 - (sku.normal_discount_pct or 0) / 100)
    ranked = []
    for o in rows:
        sup = suppliers.get(o.supplier_id)
        res = gate_supplier_offer({"batch": o.batch, "expiry": o.expiry, "effective_price": o.effective_price, "price": o.price},
                                  _supplier_dict(sup) if sup else None, _sku_dict(sku), today, cfg, held=held)
        o.gate_status, o.gate_failures = res.status, res.failures
        eta = o.eta_hours if o.eta_hours is not None else (sup.lead_time_days * 24 if sup else float(cfg.get("default_eta_hours", 6)))
        ranked.append({
            "offer_id": o.id, "supplier_id": o.supplier_id, "supplier_name": sup.name if sup else None,
            "price": o.price, "scheme": o.scheme, "landed_cost": o.effective_price, "available_qty": o.available_qty or 0.0,
            "batch": o.batch, "expiry": o.expiry.isoformat() if o.expiry else None, "eta_hours": float(eta),
            "margin_per_unit": round(sell - o.effective_price, 2), "gate_status": res.status, "gate_failures": res.failures,
            "can_fill": (o.available_qty or 0) >= qty, "source": o.source,
        })
    ranked.sort(key=lambda r: (r["gate_status"] != "PASS", not r["can_fill"], r["available_qty"] <= 0, r["landed_cost"], r["eta_hours"]))
    return ranked


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def open_request(
    db: Session,
    policy: Policy,
    sku_id: str,
    warehouse_id: str,
    retailer_id: str | None,
    qty: float,
    today: date,
    bounce_id: str | None = None,
    order_line_ref: str | None = None,
    notify: bool = True,
) -> SourcingRequest:
    sku = db.get(Sku, sku_id)
    retailer = db.get(Retailer, retailer_id) if retailer_id else None
    req = SourcingRequest(
        id=f"SRC-{uuid.uuid4().hex[:12]}", bounce_id=bounce_id, order_line_ref=order_line_ref, sku_id=sku_id,
        warehouse_id=warehouse_id, retailer_id=retailer_id, qty=qty, status="OPEN",
        retailer_importance=1.5 if retailer and retailer.is_top_account else 1.0,
        value=round(qty * (sku.selling_base * (1 - sku.normal_discount_pct / 100)) if sku else 0.0, 2),
    )
    db.add(req)
    if sku is None:
        _fail(db, req, "EXTERNAL_SOURCING_FAILED", notify=False)
        return req
    evaluate_request(db, policy, req, today, notify=notify)
    return req


def evaluate_request(db: Session, policy: Policy, req: SourcingRequest, today: date, notify: bool = True, final_if_none: bool = True) -> SourcingRequest:
    sku = db.get(Sku, req.sku_id)
    if is_held(db, req.sku_id, req.warehouse_id):
        _fail(db, req, "COMPLIANCE_HOLD", notify=notify)
        return req
    ranked = find_offers(db, sku, req.warehouse_id, req.qty, policy, today)
    req.ranked_offers = ranked[:10]
    winners = [r for r in ranked if r["gate_status"] == "PASS" and r["available_qty"] > 0]
    if winners:
        best = winners[0]
        req.status, req.best_offer_id, req.eta_hours = "HELD", best["offer_id"], best["eta_hours"]
        _set_bounce(db, req, outcome="recovered", reason="NOT_IN_STOCK")
        if notify:
            retailer = db.get(Retailer, req.retailer_id) if req.retailer_id else None
            notifications.send(db, "whatsapp", retailer.whatsapp if retailer else req.retailer_id, "sourced_eta", ref=req.id,
                               retailer=retailer.name if retailer else req.retailer_id, sku=sku.name, eta=int(round(best["eta_hours"])))
            req.retailer_notified = True
        return req
    any_stock_anywhere = any(r["available_qty"] > 0 for r in ranked)
    reason = "EXTERNAL_SOURCING_FAILED" if any_stock_anywhere else "SUPPLY_SHORTAGE"
    req.failure_reason = reason
    if not final_if_none:
        return req  # re-check of an already open request: keep waiting
    if _outreach(db, sku, req):
        return req  # stays OPEN until a supplier replies or the sweep expires it
    _fail(db, req, reason, notify=notify)
    return req


def _outreach(db: Session, sku: Sku, req: SourcingRequest) -> int:
    """Ask approved WhatsApp/email suppliers for availability (template, no LLM). Returns #inquiries."""
    if req.status != "OPEN":
        return 0
    already = db.scalar(select(SourcingRequest.id).where(
        SourcingRequest.sku_id == req.sku_id, SourcingRequest.status == "OPEN", SourcingRequest.id != req.id
    ))
    if already:
        return 1  # an inquiry for this SKU is already out; piggyback on it
    sent = 0
    for s in db.scalars(select(Supplier).where(Supplier.approved.is_(True), Supplier.channel.in_(["whatsapp", "email"]))):
        notifications.send(db, s.channel if s.channel == "whatsapp" else "email", s.contact, "supplier_inquiry", ref=req.id,
                           supplier=s.name, sku=sku.name, composition=sku.composition or "", qty=int(req.qty), warehouse=req.warehouse_id)
        sent += 1
    return sent


def _fail(db: Session, req: SourcingRequest, reason: str, notify: bool) -> None:
    req.status, req.failure_reason = "FAILED", reason
    _set_bounce(db, req, outcome="final", reason=reason)
    if notify and not req.retailer_notified:
        retailer = db.get(Retailer, req.retailer_id) if req.retailer_id else None
        sku = db.get(Sku, req.sku_id)
        notifications.send(db, "whatsapp", retailer.whatsapp if retailer else req.retailer_id, "not_available", ref=req.id,
                           retailer=retailer.name if retailer else req.retailer_id, sku=sku.name if sku else req.sku_id,
                           reason=reason.replace("_", " ").lower())
        req.retailer_notified = True


def _set_bounce(db: Session, req: SourcingRequest, outcome: str, reason: str) -> None:
    if not req.bounce_id:
        return
    b = db.get(BounceEvent, req.bounce_id)
    if b is None:
        return
    b.outcome, b.reason_code, b.sourcing_attempted = outcome, reason, True
    b.value_lost = req.value if outcome == "final" else 0.0


def expire_open_requests(db: Session, policy: Policy, today: date, max_age_hours: float = 2.0) -> int:
    """Sweep: requests still OPEN after outreach become final bounces (retailer told once)."""
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    n = 0
    for req in db.scalars(select(SourcingRequest).where(SourcingRequest.status == "OPEN", SourcingRequest.created_at <= cutoff)):
        evaluate_request(db, policy, req, today, final_if_none=False)
        if req.status == "OPEN":
            _fail(db, req, req.failure_reason or "EXTERNAL_SOURCING_FAILED", notify=True)
        n += 1
    return n


def reevaluate_for_sku(db: Session, policy: Policy, sku_id: str, today: date) -> list[str]:
    """A new offer arrived (e.g. supplier replied on WhatsApp): retry open requests for that SKU."""
    done = []
    for req in db.scalars(select(SourcingRequest).where(SourcingRequest.sku_id == sku_id, SourcingRequest.status == "OPEN")):
        evaluate_request(db, policy, req, today, final_if_none=False)
        if req.status == "HELD":
            done.append(req.id)
    return done


def resolve_request(db: Session, req: SourcingRequest, action: str, by: str) -> SourcingRequest:
    if action == "confirm_purchase" and req.status == "HELD":
        req.status = "PURCHASE_TASK"
    elif action == "fulfilled" and req.status in ("HELD", "PURCHASE_TASK"):
        req.status = "FULFILLED"
    elif action == "cancel" and req.status in ("OPEN", "HELD", "PURCHASE_TASK"):
        req.status = "CANCELLED"
        _set_bounce(db, req, outcome="final", reason="EXTERNAL_SOURCING_FAILED")
    else:
        raise ValueError(f"cannot {action} a request in status {req.status}")
    return req
