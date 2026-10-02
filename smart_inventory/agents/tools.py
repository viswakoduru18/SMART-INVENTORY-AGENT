"""Tool definitions shared by the Insights and Operations agents.

Read tools are side-effect free. Operations tools can run jobs and sweep the
sourcing queue, but none can approve a PO, change a price or alter a quantity:
those stay with deterministic engines and human approvers.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_policy
from ..models import DataQualityResult, PriceOffer, SuggestedPO
from .llm import Tool

WH = {"type": ["string", "null"], "description": "Warehouse id (e.g. HYD01, NMB01) or null for all"}
LIMIT = {"type": "integer", "description": "Max rows to return (1-50)"}


def _lim(n: int | None, default: int = 15) -> int:
    return max(1, min(int(n or default), 50))


def read_tools(db: Session, day: date | None) -> list[Tool]:
    return [
        Tool("get_kpis", "Headline KPIs: fill rate, bounce rate, recovery rate, inventory days, working capital, gross margin, "
             "forecast accuracy (WAPE/bias by class), override rate, data-quality status.",
             {"warehouse_id": WH}, ["warehouse_id"], lambda warehouse_id: analytics.kpis(db, day, warehouse_id)),
        Tool("inventory_intelligence", "Stock by SKU class, ageing buckets, near-expiry batches, working capital blocked.",
             {"warehouse_id": WH, "limit": LIMIT}, ["warehouse_id", "limit"],
             lambda warehouse_id, limit: analytics.inventory_view(db, day, warehouse_id, _lim(limit))),
        Tool("bounce_intelligence", "Today's bounces, top and repeat-bounced SKUs, revenue lost, stocking candidates, sourcing queue.",
             {"warehouse_id": WH, "limit": LIMIT}, ["warehouse_id", "limit"],
             lambda warehouse_id, limit: analytics.bounce_view(db, day, warehouse_id, _lim(limit))),
        Tool("purchase_intelligence", "Tomorrow and 2-day forecast totals, safety stock, suggested PO lines with priority and supplier.",
             {"warehouse_id": WH, "limit": LIMIT}, ["warehouse_id", "limit"],
             lambda warehouse_id, limit: analytics.purchase_view(db, day, warehouse_id, _lim(limit))),
        Tool("margin_intelligence", "Discount leakage on fast movers, slow stock eligible for special offers, liquidation and supplier-return opportunities.",
             {"warehouse_id": WH, "limit": LIMIT}, ["warehouse_id", "limit"],
             lambda warehouse_id, limit: analytics.margin_view(db, day, warehouse_id, _lim(limit))),
        Tool("find_sku", "Search SKUs by id, product name or composition/molecule.",
             {"query": {"type": "string"}}, ["query"], lambda query: analytics.find_skus(db, query)),
        Tool("sku_profile", "Everything about one SKU: today's decision, class history, forecast, buffer, bounces, PO, offers, batches.",
             {"sku_id": {"type": "string"}, "warehouse_id": WH}, ["sku_id", "warehouse_id"],
             lambda sku_id, warehouse_id: analytics.sku_profile(db, sku_id, warehouse_id, day) or {"error": "unknown sku"}),
        Tool("list_decisions", "Today's decisions filtered by action (COMPLIANCE_HOLD, LIQUIDATE, SOURCE, STOCK, DISCOUNT, SELL, DONT_STOCK).",
             {"action": {"type": ["string", "null"]}, "warehouse_id": WH, "limit": LIMIT}, ["action", "warehouse_id", "limit"],
             lambda action, warehouse_id, limit: {k: v for k, v in analytics.decisions(db, day, warehouse_id, action, None, _lim(limit)).items()}),
        Tool("sourcing_queue", "Open / held sourcing requests ranked by value x retailer importance.",
             {"status": {"type": ["string", "null"]}, "warehouse_id": WH, "limit": LIMIT}, ["status", "warehouse_id", "limit"],
             lambda status, warehouse_id, limit: analytics.sourcing_queue(db, status, warehouse_id, _lim(limit))),
    ]


def ops_tools(db: Session, day: date, erp) -> list[Tool]:
    from ..engines import sourcing
    from ..pipeline.daily_cycle import run_daily_cycle

    def run_cycle(sync: bool, publish: bool) -> dict[str, Any]:
        return run_daily_cycle(db, day, erp=erp, sync=sync, publish=publish)

    def dq() -> list[dict[str, Any]]:
        return [{"check": r.check, "passed": r.passed, "severity": r.severity, "detail": r.detail}
                for r in db.scalars(select(DataQualityResult).where(DataQualityResult.run_date == day))]

    def pending() -> dict[str, Any]:
        pos = list(db.scalars(select(SuggestedPO).where(SuggestedPO.date == day, SuggestedPO.status.in_(["DRAFT", "DEFERRED"]))))
        offers = list(db.scalars(select(PriceOffer).where(PriceOffer.date == day, PriceOffer.status == "PROPOSED")))
        return {"po_drafts": len(pos), "po_value": round(sum(p.line_value for p in pos), 2),
                "high_priority_pos": sum(1 for p in pos if p.priority == "HIGH"),
                "price_offers_awaiting_approval": len(offers), "autonomy_mode": get_policy().get("autonomy.mode")}

    def retry_sourcing() -> dict[str, Any]:
        from ..models import SourcingRequest
        resolved = []
        for sku_id in {r.sku_id for r in db.scalars(select(SourcingRequest).where(SourcingRequest.status == "OPEN"))}:
            resolved += sourcing.reevaluate_for_sku(db, get_policy(), sku_id, day)
        return {"resolved": resolved}

    def expire_sourcing(max_age_hours: float) -> dict[str, Any]:
        return {"expired": sourcing.expire_open_requests(db, get_policy(), day, max_age_hours=max(0.5, float(max_age_hours)))}

    return [
        Tool("run_daily_cycle", "Run the deterministic decision cycle for the run date (ERP sync, data quality, engines E1-E8). "
             "publish=true also pushes already-approved PO drafts and price rules to the ERP (never in SHADOW mode).",
             {"sync": {"type": "boolean"}, "publish": {"type": "boolean"}}, ["sync", "publish"], run_cycle),
        Tool("data_quality_results", "Today's data-quality check results.", {}, [], dq),
        Tool("job_status", "Recent batch job runs with status and timings.", {}, [], lambda: analytics.job_status(db, day)),
        Tool("pending_approvals", "Counts of PO drafts and price offers waiting for human approval.", {}, [], pending),
        Tool("retry_open_sourcing", "Re-check every OPEN sourcing request against the latest supplier offers.", {}, [], retry_sourcing),
        Tool("expire_stale_sourcing", "Close OPEN sourcing requests older than max_age_hours as final bounces and notify retailers once.",
             {"max_age_hours": {"type": "number"}}, ["max_age_hours"], expire_sourcing),
    ]
