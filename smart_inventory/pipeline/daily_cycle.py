"""Daily decision cycle (architecture doc 4.3). Idempotent: safe to re-run for the same date.

sync -> data quality -> gate offers -> E1 signal -> E2 classify -> E3 forecast
-> E6 bounce-to-stock -> E4 buffers + PO -> E7 pricing -> E8 orchestrator -> (publish)

Re-runs replace today's machine proposals but never touch rows a human has
already approved, rejected, overridden or that were pushed to the ERP.
"""
from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from ..config import Policy, get_policy
from ..integrations import get_connector
from ..integrations.base import ERPConnector
from ..models import (
    BounceProfile,
    BufferPolicy,
    DecisionLog,
    Forecast,
    ForecastAccuracy,
    JobRun,
    PriceOffer,
    SkuClassDaily,
    StockDecision,
    SuggestedPO,
)
from ..engines import bounce_to_stock, classifier, forecast, orchestrator, pricing, replenishment, signal, sourcing
from ..engines.data import load_working_set
from ..utils import to_py
from . import data_quality, ingest

log = logging.getLogger(__name__)
HUMAN_PO_STATES = ("APPROVED", "REJECTED", "PUSHED")
HUMAN_OFFER_STATES = ("APPROVED", "REJECTED", "PUBLISHED")


class StageError(RuntimeError):
    pass


def _job(db: Session, name: str, run_date: date, fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any]:
    job = JobRun(job=name, run_date=run_date, status="RUNNING", detail={})
    db.add(job)
    db.flush()
    t0 = time.perf_counter()
    try:
        detail = fn() or {}
    except Exception as exc:
        # the transaction may be aborted (Postgres): roll back, then record the failure on its own
        db.rollback()
        failed = JobRun(job=name, run_date=run_date, status="FAILED", finished_at=datetime.utcnow(),
                        detail={"error": str(exc)[:2000], "trace": traceback.format_exc()[-2000:], "seconds": round(time.perf_counter() - t0, 2)})
        db.add(failed)
        db.commit()
        raise StageError(f"{name} failed: {exc}") from exc
    job.status = detail.pop("_status", "SUCCESS")
    job.detail = to_py({**detail, "seconds": round(time.perf_counter() - t0, 2)})
    job.finished_at = datetime.utcnow()
    db.flush()
    return job.detail


def _insert(db: Session, model, rows: list[dict[str, Any]]) -> None:
    if rows:
        db.execute(insert(model), to_py(rows))


def _replace(db: Session, model, today: date, rows: list[dict[str, Any]]) -> int:
    db.execute(delete(model).where(model.date == today))
    _insert(db, model, rows)
    return len(rows)


def previous_classes(db: Session, today: date) -> dict[tuple[str, str], dict[str, Any]]:
    last = db.scalar(select(func.max(SkuClassDaily.date)).where(SkuClassDaily.date < today))
    if last is None:
        return {}
    return {
        (r.sku_id, r.warehouse_id): {"business_class": r.business_class, "candidate_class": r.candidate_class, "candidate_since": r.candidate_since}
        for r in db.scalars(select(SkuClassDaily).where(SkuClassDaily.date == last))
    }


def run_daily_cycle(
    db: Session,
    today: date,
    erp: ERPConnector | None = None,
    policy: Policy | None = None,
    sync: bool = True,
    full_sync: bool = False,
    publish: bool = False,
    enforce_dq: bool = True,
) -> dict[str, Any]:
    erp = erp or get_connector()
    policy = policy or get_policy()
    summary: dict[str, Any] = {"run_date": today.isoformat(), "policy_version": policy.version}
    ctx: dict[str, Any] = {}

    if sync:
        summary["sync"] = _job(db, "erp_sync", today, lambda: ingest.sync_all(db, erp, today, full=full_sync))

    def dq():
        res = data_quality.run_checks(db, today)
        blocking = data_quality.blocking_failures(res)
        ctx["dq_blocking"] = blocking
        return {"checks": len(res), "failed": [r["check"] for r in res if not r["passed"]],
                "_status": "FAILED" if blocking else ("WARN" if any(not r["passed"] for r in res) else "SUCCESS")}
    summary["data_quality"] = _job(db, "data_quality", today, dq)
    if enforce_dq and ctx.get("dq_blocking"):
        summary["halted"] = "blocking data-quality failures: " + ", ".join(r["check"] for r in ctx["dq_blocking"])
        db.commit()
        return summary
    db.commit()

    summary["offer_gates"] = _job(db, "offer_gates", today, lambda: sourcing.gate_all_offers(db, policy, today))
    ws = load_working_set(db, today, history_days=int(policy.get("classification.history_days", 180)) + 20)

    def e1():
        profiles = signal.compute_bounce_profiles(ws, policy)
        ctx["profiles"] = signal.bounce_profile_index(profiles)
        return {"bounce_profiles": _replace(db, BounceProfile, today, profiles)}
    summary["signal"] = _job(db, "e1_signal", today, e1)

    def e2():
        rows = classifier.classify(ws, policy, ctx["profiles"], previous_classes(db, today))
        ctx["class_rows"] = {(r["sku_id"], r["warehouse_id"]): r for r in rows}
        ctx["classes"] = {k: r["business_class"] for k, r in ctx["class_rows"].items()}
        counts = pd.Series(list(ctx["classes"].values())).value_counts().to_dict() if rows else {}
        return {"classified": _replace(db, SkuClassDaily, today, rows), "by_class": counts,
                "pending_switches": sum(1 for r in rows if r["candidate_class"])}
    summary["classification"] = _job(db, "e2_classification", today, e2)

    def e3():
        rows, acc = forecast.run_forecasts(ws, policy, ctx["classes"], ctx["profiles"])
        idx: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
        for r in rows:
            idx.setdefault((r["sku_id"], r["warehouse_id"]), {})[r["horizon"]] = r
        ctx["forecasts"], ctx["accuracy"] = idx, acc
        live = _score_live(db, ws, today)
        db.execute(delete(ForecastAccuracy).where(ForecastAccuracy.date == today, ForecastAccuracy.role != "live"))
        db.execute(delete(ForecastAccuracy).where(ForecastAccuracy.date == today - timedelta(days=1), ForecastAccuracy.role == "live"))
        _insert(db, ForecastAccuracy, acc + live)
        return {"forecasts": _replace(db, Forecast, today, rows),
                "champions": {f"{a['warehouse_id']}/{a['sku_class']}": f"{a['method']} wape={a['wape']:.3f}" if a["wape"] is not None else a["method"]
                              for a in acc if a["role"] == "champion"}}
    summary["forecast"] = _job(db, "e3_forecast", today, e3)

    def e6():
        rows = bounce_to_stock.run_bounce_to_stock(ws, policy, ctx["profiles"])
        ctx["stock_decisions"] = {(r["sku_id"], r["warehouse_id"]): r for r in rows}
        return {"stock_decisions": _replace(db, StockDecision, today, rows),
                "by_decision": pd.Series([r["decision"] for r in rows]).value_counts().to_dict() if rows else {}}
    summary["bounce_to_stock"] = _job(db, "e6_bounce_to_stock", today, e6)

    def e4():
        buffers, pos = replenishment.run_replenishment(ws, policy, ctx["classes"], ctx["forecasts"], ctx["profiles"], ctx["stock_decisions"])
        ctx["buffers"] = {(r["sku_id"], r["warehouse_id"]): r for r in buffers}
        ctx["pos"] = pos
        return {"buffers": _replace(db, BufferPolicy, today, buffers), "po_lines": len(pos),
                "deferred_by_wc_cap": sum(1 for p in pos if p["status"] == "DEFERRED")}
    summary["replenishment"] = _job(db, "e4_replenishment", today, e4)

    def e7():
        offers, signals = pricing.run_pricing(ws, policy, ctx["classes"], ctx["forecasts"])
        ctx["price_signals"] = signals
        locked = set(db.scalars(select(PriceOffer.offer_id).where(PriceOffer.date == today, PriceOffer.status.in_(HUMAN_OFFER_STATES))))
        db.execute(delete(PriceOffer).where(PriceOffer.date == today, PriceOffer.status.not_in(HUMAN_OFFER_STATES)))
        new = [o for o in offers if o["offer_id"] not in locked]
        _insert(db, PriceOffer, new)
        return {"offers": len(new), "kinds": pd.Series([o["kind"] for o in offers]).value_counts().to_dict() if offers else {}}
    summary["pricing"] = _job(db, "e7_pricing", today, e7)

    def e8():
        decisions = orchestrator.run_orchestrator(ws, policy, ctx["class_rows"], ctx["forecasts"], ctx["buffers"], ctx["profiles"],
                                                  ctx["stock_decisions"], ctx["pos"], ctx["price_signals"], ctx["accuracy"])
        # persist POs (after orchestrator so AUTO approvals are captured), preserving human decisions
        locked_po = set(db.scalars(select(SuggestedPO.po_draft_id).where(SuggestedPO.date == today, SuggestedPO.status.in_(HUMAN_PO_STATES))))
        db.execute(delete(SuggestedPO).where(SuggestedPO.date == today, SuggestedPO.status.not_in(HUMAN_PO_STATES)))
        new_pos = [p for p in ctx["pos"] if p["po_draft_id"] not in locked_po]
        _insert(db, SuggestedPO, [{**p, "approved_by": p.get("approved_by")} for p in new_pos])
        locked = set(db.execute(select(DecisionLog.sku_id, DecisionLog.warehouse_id).where(
            DecisionLog.date == today, DecisionLog.status.in_(["OVERRIDDEN"]) | DecisionLog.overridden_by.is_not(None)
        )).all())
        db.execute(delete(DecisionLog).where(DecisionLog.date == today, DecisionLog.overridden_by.is_(None), DecisionLog.status != "OVERRIDDEN"))
        new = [d for d in decisions if (d["sku_id"], d["warehouse_id"]) not in locked]
        _insert(db, DecisionLog, new)
        return {"decisions": len(new), "by_action": pd.Series([d["action"] for d in decisions]).value_counts().to_dict() if decisions else {},
                "auto_approved_pos": sum(1 for p in new_pos if p["status"] == "APPROVED")}
    summary["orchestrator"] = _job(db, "e8_orchestrator", today, e8)
    db.commit()

    if publish:
        summary["publish"] = _job(db, "publish", today, lambda: publish_approved(db, today, erp, policy))
        db.commit()
    return summary


def _score_live(db: Session, ws, today: date) -> list[dict[str, Any]]:
    y = today - timedelta(days=1)
    prev = pd.DataFrame([
        {"sku_id": f.sku_id, "warehouse_id": f.warehouse_id, "horizon": f.horizon, "p50": f.p50, "sku_class": f.sku_class}
        for f in db.scalars(select(Forecast).where(Forecast.date == y, Forecast.horizon == 1))
    ])
    if prev.empty:
        return []
    dd = ws.daily_demand(2)
    actual = dd[dd.day == pd.Timestamp(y)] if not dd.empty else dd
    return forecast.score_live_forecasts(prev, actual, y, None)


def publish_approved(db: Session, today: date, erp: ERPConnector, policy: Policy) -> dict[str, Any]:
    """Push approved PO drafts and price rules to the ERP. Never runs in SHADOW mode."""
    mode = str(policy.get("autonomy.mode", "ASSIST")).upper()
    if mode == "SHADOW":
        return {"skipped": "SHADOW mode: nothing is written to the ERP"}
    pushed, failed, rules = 0, [], 0
    for po in db.scalars(select(SuggestedPO).where(SuggestedPO.status == "APPROVED")):
        try:
            push_po(db, po, erp)
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{po.po_draft_id}: {exc}")
    for offer in db.scalars(select(PriceOffer).where(PriceOffer.status == "APPROVED", PriceOffer.kind == "SPECIAL_LOT")):
        try:
            push_offer(db, offer, erp)
            rules += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{offer.offer_id}: {exc}")
    return {"pos_pushed": pushed, "price_rules_published": rules, "failures": failed, "_status": "WARN" if failed else "SUCCESS"}


def push_po(db: Session, po: SuggestedPO, erp: ERPConnector) -> None:
    ref = erp.push_po_draft({
        "warehouse_id": po.warehouse_id, "supplier_id": po.supplier_id, "sku_id": po.sku_id, "qty": po.qty,
        "unit_cost": po.unit_cost, "reason_code": f"{po.reason_code} ({po.po_draft_id})",
    })
    po.erp_ref, po.status = ref, "PUSHED"


def push_offer(db: Session, offer: PriceOffer, erp: ERPConnector) -> None:
    ref = erp.push_price_rule({
        "sku_id": offer.sku_id, "warehouse_id": offer.warehouse_id, "batch": offer.batch, "discount_pct": offer.discount_pct,
        "term_flag": offer.term_flag, "valid_from": offer.valid_from, "valid_to": offer.valid_to,
    })
    offer.erp_ref, offer.status = ref, "PUBLISHED"
