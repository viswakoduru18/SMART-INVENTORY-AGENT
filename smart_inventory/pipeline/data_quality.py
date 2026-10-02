"""Data-quality controls (architecture doc 6.3). ERROR failures stop decision publishing."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..models import BounceEvent, DataQualityResult, InventoryBatch, OrderLine, Sku


def run_checks(db: Session, today: date) -> list[dict]:
    results: list[dict] = []

    def add(check: str, passed: bool, severity: str, detail: str) -> None:
        results.append({"run_date": today, "check": check, "passed": passed, "severity": severity, "detail": detail[:600]})

    since = datetime.combine(today - timedelta(days=7), datetime.min.time())
    blank = db.scalar(select(func.count()).select_from(BounceEvent).where(
        BounceEvent.ts >= since, or_(BounceEvent.reason_code.is_(None), BounceEvent.reason_code == "")
    )) or 0
    add("bounce_reason_code_present", blank == 0, "ERROR", f"{blank} bounce(s) in the last 7 days without a reason code")

    snap = db.scalar(select(func.max(InventoryBatch.snapshot_date)).where(InventoryBatch.snapshot_date <= today))
    if snap is None:
        add("inventory_snapshot_present", False, "ERROR", "No inventory snapshot loaded")
    else:
        stale = (today - snap).days
        add("inventory_snapshot_fresh", stale <= 1, "ERROR", f"Latest snapshot {snap} ({stale} day(s) old)")
        missing = db.scalar(select(func.count()).select_from(InventoryBatch).where(
            InventoryBatch.snapshot_date == snap,
            or_(InventoryBatch.batch.is_(None), InventoryBatch.batch == "", InventoryBatch.expiry.is_(None)),
        )) or 0
        add("batch_and_expiry_on_all_stock_rows", missing == 0, "ERROR", f"{missing} stock row(s) missing batch or expiry")
        neg = db.scalar(select(func.count()).select_from(InventoryBatch).where(InventoryBatch.snapshot_date == snap, InventoryBatch.qty < 0)) or 0
        add("no_negative_stock", neg == 0, "WARN", f"{neg} negative stock row(s)")

    last_order = db.scalar(select(func.max(OrderLine.ts)))
    if last_order is None:
        add("order_history_present", False, "ERROR", "No order history loaded")
    else:
        lag = (today - last_order.date()).days
        add("order_feed_fresh", lag <= 2, "ERROR", f"Latest order line {last_order:%Y-%m-%d %H:%M} ({lag} day(s) before run date)")
        first = db.scalar(select(func.min(OrderLine.ts)))
        depth = (today - first.date()).days if first else 0
        add("history_depth_12m", depth >= 365, "WARN", f"{depth} days of order history (Phase 0 gate needs 12+ months)")

    orphan = db.scalar(select(func.count()).select_from(OrderLine).where(
        OrderLine.ts >= since, OrderLine.sku_id.not_in(select(Sku.sku_id))
    )) or 0
    add("order_lines_reference_known_skus", orphan == 0, "WARN", f"{orphan} recent order line(s) reference unknown SKUs")

    db.execute(delete(DataQualityResult).where(DataQualityResult.run_date == today))
    for r in results:
        db.add(DataQualityResult(**r))
    db.flush()
    return results


def blocking_failures(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["passed"] and r["severity"] == "ERROR"]
