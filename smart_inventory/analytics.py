"""Gold-layer queries: KPIs and the four management views (Inventory, Bounce, Purchase, Margin).

Shared by the REST API, the web console and the Claude agents so every surface
reports the same numbers.
"""
from __future__ import annotations

import functools
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .utils import to_py
from .models import (
    BounceEvent,
    BounceProfile,
    BufferPolicy,
    DataQualityResult,
    DecisionLog,
    Forecast,
    ForecastAccuracy,
    InventoryBatch,
    JobRun,
    OrderLine,
    PriceOffer,
    Sku,
    SkuClassDaily,
    SourcingRequest,
    StockDecision,
    SuggestedPO,
    Supplier,
)


def _clean(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return to_py(fn(*args, **kwargs))
    return wrapper


def _df(db: Session, stmt) -> pd.DataFrame:
    res = db.execute(stmt)
    return pd.DataFrame(res.fetchall(), columns=list(res.keys()))


def latest_run_date(db: Session) -> date | None:
    return db.scalar(select(func.max(DecisionLog.date)))


def resolve_date(db: Session, day: date | None) -> date:
    return day or latest_run_date(db) or date.today()


def _wh(stmt, col, warehouse_id: str | None):
    return stmt.where(col == warehouse_id) if warehouse_id else stmt


def _r(v: Any, n: int = 2) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return round(float(v), n)


# ------------------------------------------------------------------- stock
def stock_frame(db: Session, day: date, warehouse_id: str | None = None) -> pd.DataFrame:
    snap = db.scalar(select(func.max(InventoryBatch.snapshot_date)).where(InventoryBatch.snapshot_date <= day))
    if snap is None:
        return pd.DataFrame(columns=["sku_id", "warehouse_id", "batch", "expiry", "qty", "cost", "inward_date", "on_hold", "value", "age", "dte"])
    stmt = _wh(select(InventoryBatch.sku_id, InventoryBatch.warehouse_id, InventoryBatch.batch, InventoryBatch.expiry, InventoryBatch.qty,
                      InventoryBatch.cost, InventoryBatch.inward_date, InventoryBatch.on_hold, InventoryBatch.supplier_id)
               .where(InventoryBatch.snapshot_date == snap), InventoryBatch.warehouse_id, warehouse_id)
    df = _df(db, stmt)
    if df.empty:
        return df.assign(value=[], age=[], dte=[])
    df["value"] = df.qty * df.cost
    df["age"] = [(day - d).days if d else 0 for d in df.inward_date]
    df["dte"] = [(e - day).days if e else 9999 for e in df.expiry]
    return df


def class_frame(db: Session, day: date, warehouse_id: str | None = None) -> pd.DataFrame:
    d = db.scalar(select(func.max(SkuClassDaily.date)).where(SkuClassDaily.date <= day))
    stmt = _wh(select(SkuClassDaily.sku_id, SkuClassDaily.warehouse_id, SkuClassDaily.business_class, SkuClassDaily.candidate_class)
               .where(SkuClassDaily.date == d), SkuClassDaily.warehouse_id, warehouse_id)
    return _df(db, stmt)


# -------------------------------------------------------------------- KPIs
@_clean
def kpis(db: Session, day: date | None = None, warehouse_id: str | None = None) -> dict[str, Any]:
    day = resolve_date(db, day)
    since30 = datetime.combine(day - timedelta(days=30), datetime.min.time())
    end = datetime.combine(day, datetime.min.time())
    lines = db.scalar(_wh(select(func.count()).select_from(OrderLine).where(OrderLine.ts >= since30, OrderLine.ts < end), OrderLine.warehouse_id, warehouse_id)) or 0
    b = _df(db, _wh(select(BounceEvent.outcome, BounceEvent.value_lost, BounceEvent.retailer_id)
                    .where(BounceEvent.ts >= since30, BounceEvent.ts < end), BounceEvent.warehouse_id, warehouse_id))
    final = int((b.outcome == "final").sum()) if not b.empty else 0
    recovered = int((b.outcome == "recovered").sum()) if not b.empty else 0
    asks = lines + final
    sales = _df(db, _wh(select(OrderLine.sku_id, OrderLine.qty, OrderLine.price, OrderLine.discount_pct)
                        .where(OrderLine.ts >= since30, OrderLine.ts < end), OrderLine.warehouse_id, warehouse_id))
    cost = dict(db.execute(select(Sku.sku_id, Sku.cost)).all())
    revenue = cogs = 0.0
    if not sales.empty:
        revenue = float((sales.qty * sales.price * (1 - sales.discount_pct / 100)).sum())
        cogs = float((sales.qty * sales.sku_id.map(cost).fillna(0)).sum())
    st = stock_frame(db, day, warehouse_id)
    stock_value = float(st.value.sum()) if not st.empty else 0.0
    daily_cogs = cogs / 30 if cogs else 0.0
    dec = _df(db, _wh(select(DecisionLog.status, DecisionLog.overridden_by).where(DecisionLog.date >= day - timedelta(days=30)),
                      DecisionLog.warehouse_id, warehouse_id))
    po = _df(db, _wh(select(SuggestedPO.status).where(SuggestedPO.date >= day - timedelta(days=30)), SuggestedPO.warehouse_id, warehouse_id))
    reviewed = po[po.status.isin(["APPROVED", "PUSHED", "REJECTED"])] if not po.empty else po
    cls = _df(db, _wh(select(SkuClassDaily.flags).where(SkuClassDaily.date == day), SkuClassDaily.warehouse_id, warehouse_id))
    switched = int(sum(1 for f in cls["flags"] if any(x in ("CONFIRMED_SWITCH", "IMMEDIATE_SWITCH") for x in (f or [])))) if not cls.empty else 0
    acc = _df(db, _wh(select(ForecastAccuracy.sku_class, ForecastAccuracy.method, ForecastAccuracy.role, ForecastAccuracy.wape, ForecastAccuracy.bias)
                      .where(ForecastAccuracy.date == day), ForecastAccuracy.warehouse_id, warehouse_id))
    dq = _df(db, select(DataQualityResult.check, DataQualityResult.passed, DataQualityResult.severity).where(DataQualityResult.run_date == day))
    expiry_risk = float(st[(st.dte <= 90)].value.sum()) if not st.empty else 0.0
    return {
        "run_date": day.isoformat(),
        "warehouse_id": warehouse_id or "ALL",
        "availability": {
            "fill_rate_line_30d": _r(lines / asks if asks else None, 4),
            "bounce_rate_30d": _r(final / asks if asks else None, 4),
            "bounce_recovery_rate_30d": _r(recovered / (recovered + final) if recovered + final else None, 4),
            "order_lines_30d": lines,
            "final_bounces_30d": final,
            "revenue_lost_30d": _r(b.loc[b.outcome == "final", "value_lost"].sum() if not b.empty else 0.0),
            "retailers_affected_30d": int(b.loc[b.outcome == "final", "retailer_id"].nunique()) if not b.empty else 0,
        },
        "inventory": {
            "stock_value": _r(stock_value),
            "inventory_days": _r(stock_value / daily_cogs if daily_cogs else None, 1),
            "working_capital_pct_of_30d_turnover": _r(stock_value / revenue if revenue else None, 4),
            "near_expiry_value_90d": _r(expiry_risk),
        },
        "margin": {
            "revenue_30d": _r(revenue),
            "gross_margin_pct_30d": _r((revenue - cogs) / revenue * 100 if revenue else None),
        },
        "model_health": {
            "forecast_accuracy": acc.where(pd.notna(acc), None).to_dict("records") if not acc.empty else [],
            "class_switches_today": switched,
            "po_acceptance_rate_30d": _r((reviewed.status != "REJECTED").mean() if len(reviewed) else None, 4),
            "override_rate_30d": _r(dec.overridden_by.notna().mean() if len(dec) else None, 4),
        },
        "platform": {
            "data_quality_pass_rate": _r(dq.passed.mean() if len(dq) else None, 4),
            "data_quality_failures": dq[~dq.passed].to_dict("records") if len(dq) else [],
            "last_jobs": job_status(db, day),
        },
    }


def job_status(db: Session, day: date | None = None) -> list[dict[str, Any]]:
    stmt = select(JobRun).order_by(JobRun.id.desc()).limit(20)
    if day:
        stmt = select(JobRun).where(JobRun.run_date == day).order_by(JobRun.id.desc()).limit(20)
    return [{"job": j.job, "status": j.status, "started_at": j.started_at.isoformat(), "finished_at": j.finished_at.isoformat() if j.finished_at else None,
             "detail": {k: v for k, v in (j.detail or {}).items() if k != "trace"}} for j in db.scalars(stmt)]


# --------------------------------------------------------------- Inventory
@_clean
def inventory_view(db: Session, day: date | None = None, warehouse_id: str | None = None, limit: int = 25) -> dict[str, Any]:
    day = resolve_date(db, day)
    st = stock_frame(db, day, warehouse_id)
    cl = class_frame(db, day, warehouse_id)
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    by_class = []
    if not cl.empty:
        val = st.groupby(["sku_id", "warehouse_id"]).value.sum() if not st.empty else pd.Series(dtype=float)
        cl["stock_value"] = [float(val.get((r.sku_id, r.warehouse_id), 0.0)) for r in cl.itertuples()]
        g = cl.groupby("business_class").agg(skus=("sku_id", "size"), stock_value=("stock_value", "sum"),
                                            with_stock=("stock_value", lambda s: int((s > 0).sum())))
        by_class = [{"class": k, "skus": int(v.skus), "skus_with_stock": int(v.with_stock), "stock_value": _r(v.stock_value)} for k, v in g.iterrows()]
    ageing = []
    near = []
    if not st.empty:
        bins = [(0, 30), (31, 60), (61, 90), (91, 180), (181, 100000)]
        for lo, hi in bins:
            m = st[(st.age >= lo) & (st.age <= hi)]
            ageing.append({"bucket": f"{lo}-{hi}" if hi < 100000 else f"{lo}+", "value": _r(m.value.sum()), "batches": int(len(m))})
        ne = st[st.dte <= 180].sort_values("dte")
        near = [{"sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id, "batch": r.batch,
                 "expiry": r.expiry.isoformat() if r.expiry else None, "days_to_expiry": int(r.dte), "qty": r.qty, "value": _r(r.value)}
                for r in ne.head(limit).itertuples()]
    slow_val = sum(c["stock_value"] or 0 for c in by_class if c["class"] in ("SLOW", "NON_MOVING", "SPORADIC"))
    return {
        "run_date": day.isoformat(),
        "by_class": sorted(by_class, key=lambda c: -(c["stock_value"] or 0)),
        "ageing": ageing,
        "near_expiry": near,
        "near_expiry_value_90d": _r(st[st.dte <= 90].value.sum()) if not st.empty else 0.0,
        "near_expiry_value_180d": _r(st[st.dte <= 180].value.sum()) if not st.empty else 0.0,
        "working_capital_blocked": _r(st.value.sum()) if not st.empty else 0.0,
        "working_capital_in_slow_nonmoving": _r(slow_val),
        "on_hold_value": _r(st[st.on_hold.astype(bool)].value.sum()) if not st.empty else 0.0,
    }


# ------------------------------------------------------------------ Bounce
@_clean
def bounce_view(db: Session, day: date | None = None, warehouse_id: str | None = None, limit: int = 25) -> dict[str, Any]:
    day = resolve_date(db, day)
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    last_bounce_day = db.scalar(_wh(select(func.max(BounceEvent.ts)), BounceEvent.warehouse_id, warehouse_id))
    today_start = datetime.combine(last_bounce_day.date(), datetime.min.time()) if last_bounce_day else datetime.combine(day, datetime.min.time())
    todays = _df(db, _wh(select(BounceEvent.bounce_id, BounceEvent.sku_id, BounceEvent.retailer_id, BounceEvent.qty, BounceEvent.reason_code,
                                BounceEvent.outcome, BounceEvent.value_lost, BounceEvent.ts)
                         .where(BounceEvent.ts >= today_start), BounceEvent.warehouse_id, warehouse_id))
    prof = _df(db, _wh(select(*BounceProfile.__table__.columns).where(BounceProfile.date == day), BounceProfile.warehouse_id, warehouse_id))
    sd = _df(db, _wh(select(StockDecision.sku_id, StockDecision.warehouse_id, StockDecision.decision, StockDecision.target_stock_qty,
                            StockDecision.margin_captured, StockDecision.carrying_cost, StockDecision.expiry_risk_cost, StockDecision.reason)
                     .where(StockDecision.date == day), StockDecision.warehouse_id, warehouse_id))
    top, repeat, stocking = [], [], []
    if not prof.empty:
        for r in prof.sort_values(["bounces_90", "value_lost_90"], ascending=False).head(limit).itertuples():
            top.append({"sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id, "bounces_30": r.bounces_30,
                        "bounces_60": r.bounces_60, "bounces_90": r.bounces_90, "retailers": r.distinct_retailers_90,
                        "value_lost_90": _r(r.value_lost_90), "pattern": r.demand_pattern, "spread": r.retailer_spread,
                        "external_availability": r.external_availability, "recovery_rate": _r(r.sourcing_success_rate, 3)})
        repeat = [t for t in top if t["pattern"] == "REGULAR"]
    if not sd.empty:
        cand = sd[sd.decision.isin(["STOCK", "LIMITED_SAFETY_STOCK"])].sort_values("margin_captured", ascending=False)
        stocking = [{"sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id, "decision": r.decision,
                     "target_stock_qty": r.target_stock_qty, "benefit_per_month": _r(r.margin_captured),
                     "cost_per_month": _r(r.carrying_cost + r.expiry_risk_cost), "reason": r.reason} for r in cand.head(limit).itertuples()]
    queue = sourcing_queue(db, warehouse_id=warehouse_id, limit=limit)
    return {
        "run_date": day.isoformat(),
        "today": {
            "date": today_start.date().isoformat(),
            "bounces": int(len(todays)),
            "final": int((todays.outcome == "final").sum()) if not todays.empty else 0,
            "recovered": int((todays.outcome == "recovered").sum()) if not todays.empty else 0,
            "pending": int((todays.outcome == "pending").sum()) if not todays.empty else 0,
            "revenue_lost": _r(todays.value_lost.sum()) if not todays.empty else 0.0,
            "retailers_affected": int(todays.retailer_id.nunique()) if not todays.empty else 0,
            "by_reason": todays.reason_code.value_counts().to_dict() if not todays.empty else {},
        },
        "revenue_lost_90d": _r(prof.value_lost_90.sum()) if not prof.empty else 0.0,
        "decision_mix": sd.decision.value_counts().to_dict() if not sd.empty else {},
        "top_bounced": top,
        "repeat_bounced": repeat,
        "recommended_for_stocking": stocking,
        "sourcing_queue": queue,
    }


@_clean
def sourcing_queue(db: Session, status: str | None = None, warehouse_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    stmt = select(SourcingRequest)
    if status:
        stmt = stmt.where(SourcingRequest.status == status)
    else:
        stmt = stmt.where(SourcingRequest.status.in_(["OPEN", "HELD", "PURCHASE_TASK"]))
    stmt = _wh(stmt, SourcingRequest.warehouse_id, warehouse_id).order_by(
        (SourcingRequest.value * SourcingRequest.retailer_importance).desc()).limit(limit)
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    return [{"id": r.id, "sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id, "retailer_id": r.retailer_id,
             "qty": r.qty, "status": r.status, "value": r.value, "priority_score": _r(r.value * r.retailer_importance),
             "eta_hours": r.eta_hours, "failure_reason": r.failure_reason, "best_offer": (r.ranked_offers or [None])[0],
             "created_at": r.created_at.isoformat()} for r in db.scalars(stmt)]


# ---------------------------------------------------------------- Purchase
@_clean
def purchase_view(db: Session, day: date | None = None, warehouse_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    day = resolve_date(db, day)
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    sup_names = dict(db.execute(select(Supplier.supplier_id, Supplier.name)).all())
    fc = _df(db, _wh(select(Forecast.sku_id, Forecast.warehouse_id, Forecast.horizon, Forecast.p50, Forecast.p90, Forecast.sku_class)
                     .where(Forecast.date == day), Forecast.warehouse_id, warehouse_id))
    ptr = {s.sku_id: s.selling_base * (1 - s.normal_discount_pct / 100) for s in db.scalars(select(Sku))}
    po = _df(db, _wh(select(*SuggestedPO.__table__.columns).where(SuggestedPO.date == day), SuggestedPO.warehouse_id, warehouse_id))
    buf = _df(db, _wh(select(BufferPolicy.buffer_qty, BufferPolicy.safety_stock).where(BufferPolicy.date == day), BufferPolicy.warehouse_id, warehouse_id))
    lines = []
    if not po.empty:
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        po = po.assign(_r=po.priority.map(rank)).sort_values(["_r", "line_value"], ascending=[True, False])
        for r in po.head(limit).itertuples():
            lines.append({"po_draft_id": r.po_draft_id, "sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id,
                          "class": r.sku_class, "current_stock": r.current_stock, "open_po": r.open_po_qty, "forecast_1d": r.forecast_1d,
                          "forecast_2d": r.forecast_2d, "safety_buffer": r.buffer_qty, "suggested_qty": r.qty, "unit_cost": r.unit_cost,
                          "line_value": r.line_value, "supplier_id": r.supplier_id, "supplier": sup_names.get(r.supplier_id),
                          "priority": r.priority, "reason_code": r.reason_code, "constraints": r.constraints_applied, "status": r.status,
                          "erp_ref": r.erp_ref})
    f1 = fc[fc.horizon == 1] if not fc.empty else fc
    f2 = fc[fc.horizon == 2] if not fc.empty else fc
    return {
        "run_date": day.isoformat(),
        "tomorrow_demand_units": _r(f1.p50.sum()) if not fc.empty else 0.0,
        "tomorrow_demand_value": _r((f1.p50 * f1.sku_id.map(ptr).fillna(0)).sum()) if not fc.empty else 0.0,
        "two_day_demand_units": _r(f2.p50.sum()) if not fc.empty else 0.0,
        "two_day_demand_value": _r((f2.p50 * f2.sku_id.map(ptr).fillna(0)).sum()) if not fc.empty else 0.0,
        "forecast_skus": int(f1.sku_id.nunique()) if not fc.empty else 0,
        "safety_stock_units": _r(buf.safety_stock.sum()) if not buf.empty else 0.0,
        "po_lines": int(len(po)),
        "po_value": _r(po.line_value.sum()) if not po.empty else 0.0,
        "po_value_by_priority": po.groupby("priority").line_value.sum().round(2).to_dict() if not po.empty else {},
        "po_by_status": po.status.value_counts().to_dict() if not po.empty else {},
        "lines_without_gated_supplier": int(sum("NO_GATED_SUPPLIER" in (c or []) for c in po.constraints_applied)) if not po.empty else 0,
        "lines": lines,
    }


# ------------------------------------------------------------------ Margin
@_clean
def margin_view(db: Session, day: date | None = None, warehouse_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    day = resolve_date(db, day)
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    off = _df(db, _wh(select(*PriceOffer.__table__.columns).where(PriceOffer.date == day), PriceOffer.warehouse_id, warehouse_id))
    dec = _df(db, _wh(select(DecisionLog.sku_id, DecisionLog.warehouse_id, DecisionLog.inputs_snapshot, DecisionLog.action)
                      .where(DecisionLog.date == day), DecisionLog.warehouse_id, warehouse_id))
    cost = dict(db.execute(select(Sku.sku_id, Sku.cost)).all())
    leakage, liquidation, special = [], [], []
    total_leak = 0.0
    if not dec.empty:
        for r in dec.itertuples():
            snap = r.inputs_snapshot or {}
            if snap.get("leakage_30d"):
                total_leak += snap["leakage_30d"]
                leakage.append({"sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id,
                                "current_discount": snap.get("current_discount"), "recommended_discount": snap.get("recommended_discount"),
                                "leakage_30d": _r(snap["leakage_30d"])})
    if not off.empty:
        off["value_at_cost"] = off.qty * off.sku_id.map(cost).fillna(0)
        for r in off[off.kind == "SPECIAL_LOT"].sort_values("value_at_cost", ascending=False).head(limit).itertuples():
            item = {"offer_id": r.offer_id, "sku_id": r.sku_id, "name": names.get(r.sku_id), "warehouse_id": r.warehouse_id, "batch": r.batch,
                    "tier": r.tier, "current_discount": r.normal_discount_pct, "recommended_discount": r.discount_pct, "term_flag": r.term_flag,
                    "qty": r.qty, "value_at_cost": _r(r.value_at_cost), "net_price": r.net_price, "status": r.status, "reason": r.reason}
            (liquidation if r.tier.startswith("LIQUIDATE") else special).append(item)
    return {
        "run_date": day.isoformat(),
        "margin_leakage_fast_30d": _r(total_leak),
        "leakage": sorted(leakage, key=lambda x: -(x["leakage_30d"] or 0))[:limit],
        "slow_moving_eligible_value": _r(off[(off.kind == "SPECIAL_LOT") & ~off.tier.str.startswith("LIQUIDATE")].value_at_cost.sum()) if not off.empty else 0.0,
        "liquidation_opportunity_value": _r(off[off.tier.str.startswith("LIQUIDATE")].value_at_cost.sum()) if not off.empty else 0.0,
        "return_to_supplier_value": _r(off[off.kind == "RETURN_TO_SUPPLIER"].value_at_cost.sum()) if not off.empty else 0.0,
        "price_tests_proposed": int((off.kind == "PRICE_TEST").sum()) if not off.empty else 0,
        "offers_by_status": off.status.value_counts().to_dict() if not off.empty else {},
        "special_lots": special,
        "liquidation_lots": liquidation,
        "returns": off[off.kind == "RETURN_TO_SUPPLIER"][["offer_id", "sku_id", "warehouse_id", "batch", "qty", "reason"]].head(limit).to_dict("records") if not off.empty else [],
    }


# ---------------------------------------------------------------- SKU 360
def find_skus(db: Session, query: str, limit: int = 10) -> list[dict[str, Any]]:
    q = f"%{query.strip()}%"
    rows = db.scalars(select(Sku).where(or_(Sku.sku_id.ilike(q), Sku.name.ilike(q), Sku.composition.ilike(q))).limit(limit))
    return [{"sku_id": s.sku_id, "name": s.name, "composition": s.composition, "mrp": s.mrp} for s in rows]


@_clean
def sku_profile(db: Session, sku_id: str, warehouse_id: str | None = None, day: date | None = None) -> dict[str, Any] | None:
    sku = db.get(Sku, sku_id)
    if sku is None:
        return None
    day = resolve_date(db, day)

    def rows(model, *where, order=None, n=10):
        stmt = select(*model.__table__.columns).where(*where)
        if warehouse_id and hasattr(model, "warehouse_id"):
            stmt = stmt.where(model.warehouse_id == warehouse_id)
        if order is not None:
            stmt = stmt.order_by(order)
        return _df(db, stmt.limit(n)).astype(object).where(lambda d: pd.notna(d), None).to_dict("records")

    return {
        "sku": {c.name: getattr(sku, c.name) for c in Sku.__table__.columns},
        "decision": rows(DecisionLog, DecisionLog.sku_id == sku_id, DecisionLog.date == day),
        "class_history": rows(SkuClassDaily, SkuClassDaily.sku_id == sku_id, order=SkuClassDaily.date.desc(), n=14),
        "forecast": rows(Forecast, Forecast.sku_id == sku_id, Forecast.date == day),
        "buffer": rows(BufferPolicy, BufferPolicy.sku_id == sku_id, BufferPolicy.date == day),
        "bounce_profile": rows(BounceProfile, BounceProfile.sku_id == sku_id, BounceProfile.date == day),
        "stock_decision": rows(StockDecision, StockDecision.sku_id == sku_id, StockDecision.date == day),
        "suggested_po": rows(SuggestedPO, SuggestedPO.sku_id == sku_id, SuggestedPO.date == day),
        "offers": rows(PriceOffer, PriceOffer.sku_id == sku_id, PriceOffer.date == day),
        "batches": stock_frame(db, day, warehouse_id).query("sku_id == @sku_id").drop(columns=["value"]).astype(object).to_dict("records"),
        "sourcing": [r for r in sourcing_queue(db, warehouse_id=warehouse_id, limit=200) if r["sku_id"] == sku_id],
    }


@_clean
def decisions(db: Session, day: date | None = None, warehouse_id: str | None = None, action: str | None = None,
              status: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    day = resolve_date(db, day)
    stmt = select(DecisionLog).where(DecisionLog.date == day)
    if warehouse_id:
        stmt = stmt.where(DecisionLog.warehouse_id == warehouse_id)
    if action:
        stmt = stmt.where(DecisionLog.action == action)
    if status:
        stmt = stmt.where(DecisionLog.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    names = dict(db.execute(select(Sku.sku_id, Sku.name)).all())
    items = [{
        "id": d.id, "date": d.date.isoformat(), "sku_id": d.sku_id, "name": names.get(d.sku_id), "warehouse_id": d.warehouse_id,
        "action": d.action, "priority": d.priority, "secondary_tags": d.secondary_tags, "sku_class": d.sku_class,
        "reason_code": d.reason_code, "reason_text": d.reason_text, "status": d.status, "autonomy_mode": d.autonomy_mode,
        "overridden_by": d.overridden_by, "override_action": d.override_action, "override_reason": d.override_reason,
        "inputs_snapshot": d.inputs_snapshot, "version": d.version,
    } for d in db.scalars(stmt.order_by(DecisionLog.priority, DecisionLog.sku_id).limit(limit).offset(offset))]
    by_action = dict(db.execute(select(DecisionLog.action, func.count()).where(DecisionLog.date == day).group_by(DecisionLog.action)).all())
    return {"run_date": day.isoformat(), "total": total, "by_action": by_action, "items": items}
