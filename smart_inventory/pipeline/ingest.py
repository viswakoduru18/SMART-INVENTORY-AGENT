"""ERP -> platform sync (L1 ingestion). Reference data is full-refreshed, facts are incremental."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from ..integrations.base import ERPConnector
from ..models import (
    BounceEvent,
    InventoryBatch,
    OpenPO,
    OrderLine,
    Retailer,
    Sku,
    Supplier,
    SupplierOffer,
    Warehouse,
)
from ..engines.compliance import effective_price

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- coercion
def to_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_int(v: Any, default: int = 0) -> int:
    return int(round(to_float(v, default)))


def to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def to_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%Y", "%Y-%m"):
        try:
            d = datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
            return d
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        d = to_date(v)
        return datetime.combine(d, datetime.min.time()) if d else None


def _str(v: Any) -> str | None:
    return None if v is None or v == "" else str(v).strip()


# ---------------------------------------------------------------- upserts
def _merge_all(db: Session, model: type, rows: list[dict[str, Any]]) -> int:
    for row in rows:
        db.merge(model(**row))
    return len(rows)


def sync_reference(db: Session, erp: ERPConnector) -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["warehouses"] = _merge_all(db, Warehouse, [
        {"warehouse_id": _str(r["warehouse_id"]), "name": _str(r.get("name")) or _str(r["warehouse_id"]),
         "working_capital_cap_inr": to_float(r.get("working_capital_cap_inr"), 0) or None}
        for r in erp.fetch("warehouses") if r.get("warehouse_id")
    ])
    counts["skus"] = _merge_all(db, Sku, [
        {
            "sku_id": _str(r["sku_id"]), "name": _str(r.get("name")) or _str(r["sku_id"]),
            "composition": _str(r.get("composition")), "pack": _str(r.get("pack")),
            "pack_size": max(1, to_int(r.get("pack_size"), 1)), "mrp": to_float(r.get("mrp")),
            "ptr": to_float(r.get("ptr")) or None, "cost": to_float(r.get("cost")),
            "schedule": _str(r.get("schedule")), "manufacturer": _str(r.get("manufacturer")),
            "shelf_life_days": to_int(r.get("shelf_life_days"), 730) or 730, "moq": max(1, to_int(r.get("moq"), 1)),
            "normal_discount_pct": to_float(r.get("normal_discount_pct"), 8.0),
            "margin_floor_pct": to_float(r.get("margin_floor_pct"), 2.0),
            "price_ceiling": to_float(r.get("price_ceiling")) or None,
        }
        for r in erp.fetch("skus") if r.get("sku_id")
    ])
    counts["retailers"] = _merge_all(db, Retailer, [
        {"retailer_id": _str(r["retailer_id"]), "name": _str(r.get("name")) or _str(r["retailer_id"]),
         "city": _str(r.get("city")), "is_top_account": to_bool(r.get("is_top_account")),
         "credit_status": _str(r.get("credit_status")), "drug_licence_no": _str(r.get("drug_licence_no")),
         "whatsapp": _str(r.get("whatsapp"))}
        for r in erp.fetch("retailers") if r.get("retailer_id")
    ])
    counts["suppliers"] = _merge_all(db, Supplier, [
        {"supplier_id": _str(r["supplier_id"]), "name": _str(r.get("name")) or _str(r["supplier_id"]),
         "approved": to_bool(r.get("approved")), "licence_valid_until": to_date(r.get("licence_valid_until")),
         "gst_compliant": to_bool(r.get("gst_compliant")), "lead_time_days": to_float(r.get("lead_time_days"), 1.0),
         "lead_time_std_days": to_float(r.get("lead_time_std_days"), 0.3), "return_rights": to_bool(r.get("return_rights")),
         "channel": _str(r.get("channel")) or "manual", "contact": _str(r.get("contact")), "api_url": _str(r.get("api_url"))}
        for r in erp.fetch("suppliers") if r.get("supplier_id")
    ])
    db.flush()
    return counts


def sync_facts(db: Session, erp: ERPConnector, today: date, full: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    last_ts = None if full else db.scalar(select(func.max(OrderLine.ts)))
    since = (last_ts - timedelta(days=2)) if last_ts else None

    # order lines: insert new (order_id, line_id) only
    existing = set()
    if since:
        existing = set(db.execute(select(OrderLine.order_id, OrderLine.line_id).where(OrderLine.ts >= since)).all())
    new_lines = []
    for r in erp.fetch("order_lines", since=since):
        key = (_str(r.get("order_id")), _str(r.get("line_id")) or "1")
        ts = to_dt(r.get("ts"))
        if not key[0] or not r.get("sku_id") or ts is None or key in existing:
            continue
        existing.add(key)
        new_lines.append({
            "order_id": key[0], "line_id": key[1], "retailer_id": _str(r.get("retailer_id")) or "UNKNOWN",
            "sku_id": _str(r["sku_id"]), "warehouse_id": _str(r.get("warehouse_id")) or "DEFAULT",
            "qty": to_float(r.get("qty")), "price": to_float(r.get("price")), "discount_pct": to_float(r.get("discount_pct")),
            "channel": _str(r.get("channel")), "term_flag": _str(r.get("term_flag")) or "NORMAL", "ts": ts,
        })
    if new_lines:
        db.execute(insert(OrderLine), new_lines)
    counts["order_lines"] = len(new_lines)

    # bounces: upsert by bounce_id, compute value lost
    last_b = None if full else db.scalar(select(func.max(BounceEvent.ts)))
    since_b = (last_b - timedelta(days=2)) if last_b else None
    sku_price = {s.sku_id: (s.selling_base, s.normal_discount_pct) for s in db.scalars(select(Sku))}
    existing_b = set()
    if since_b:
        existing_b = set(db.scalars(select(BounceEvent.bounce_id).where(BounceEvent.ts >= since_b)))
    new_b = []
    for r in erp.fetch("bounces", since=since_b):
        bid = _str(r.get("bounce_id"))
        ts = to_dt(r.get("ts"))
        if not bid or bid in existing_b or not r.get("sku_id") or ts is None:
            continue
        existing_b.add(bid)
        sku_id = _str(r["sku_id"])
        qty = to_float(r.get("qty"), 1.0) or 1.0
        outcome = _str(r.get("outcome")) or "final"
        base, disc = sku_price.get(sku_id, (0.0, 0.0))
        new_b.append({
            "bounce_id": bid, "order_line_id": _str(r.get("order_line_id")), "sku_id": sku_id,
            "retailer_id": _str(r.get("retailer_id")) or "UNKNOWN", "warehouse_id": _str(r.get("warehouse_id")) or "DEFAULT",
            "qty": qty, "reason_code": _str(r.get("reason_code")) or "",
            "sourcing_attempted": to_bool(r.get("sourcing_attempted")), "outcome": outcome,
            "value_lost": round(qty * base * (1 - disc / 100), 2) if outcome == "final" else 0.0, "ts": ts,
        })
    if new_b:
        db.execute(insert(BounceEvent), new_b)
    counts["bounces"] = len(new_b)

    # inventory: replace today's snapshot (history of previous days retained)
    db.execute(delete(InventoryBatch).where(InventoryBatch.snapshot_date == today))
    inv = [
        {
            "warehouse_id": _str(r.get("warehouse_id")) or "DEFAULT", "sku_id": _str(r["sku_id"]),
            "batch": _str(r.get("batch")) or "", "expiry": to_date(r.get("expiry")), "qty": to_float(r.get("qty")),
            "cost": to_float(r.get("cost")), "inward_date": to_date(r.get("inward_date")),
            "supplier_id": _str(r.get("supplier_id")), "on_hold": to_bool(r.get("on_hold")), "snapshot_date": today,
        }
        for r in erp.fetch("inventory") if r.get("sku_id") and to_float(r.get("qty")) > 0
    ]
    if inv:
        db.execute(insert(InventoryBatch), inv)
    counts["inventory_batches"] = len(inv)

    db.execute(delete(OpenPO))
    pos = [
        {"po_id": _str(r.get("po_id")) or "", "warehouse_id": _str(r.get("warehouse_id")) or "DEFAULT",
         "sku_id": _str(r["sku_id"]), "supplier_id": _str(r.get("supplier_id")), "qty": to_float(r.get("qty")),
         "expected_date": to_date(r.get("expected_date"))}
        for r in erp.fetch("open_pos") if r.get("sku_id")
    ]
    if pos:
        db.execute(insert(OpenPO), pos)
    counts["open_pos"] = len(pos)

    # supplier price lists become (ungated) offers; the sourcing engine gates them
    db.execute(delete(SupplierOffer).where(SupplierOffer.source == "price_list"))
    now = datetime.utcnow()
    offers = []
    for r in erp.fetch("supplier_prices"):
        if not r.get("sku_id") or not r.get("supplier_id"):
            continue
        price = to_float(r.get("price"))
        offers.append({
            "supplier_id": _str(r["supplier_id"]), "sku_id": _str(r["sku_id"]), "price": price,
            "scheme": _str(r.get("scheme")), "effective_price": effective_price(price, _str(r.get("scheme"))),
            "available_qty": to_float(r.get("available_qty"), 0.0), "batch": _str(r.get("batch")),
            "expiry": to_date(r.get("expiry")), "eta_hours": None, "source": "price_list",
            "gate_status": "PENDING", "gate_failures": [], "ts": now,
        })
    if offers:
        db.execute(insert(SupplierOffer), offers)
    counts["supplier_offers"] = len(offers)
    db.flush()
    return counts


def sync_all(db: Session, erp: ERPConnector, today: date, full: bool = False) -> dict[str, int]:
    counts = sync_reference(db, erp)
    counts.update(sync_facts(db, erp, today, full=full))
    log.info("ERP sync complete: %s", counts)
    return counts
