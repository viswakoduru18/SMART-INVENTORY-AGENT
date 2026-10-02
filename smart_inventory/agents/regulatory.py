"""Regulatory Alert agent: CDSCO / state FDA NSQ, spurious-drug and recall notices -> compliance holds (P0).

Claude extracts product, manufacturer, batch and reason from the notice text.
Safety asymmetry: a missed recall is far worse than an unnecessary hold, but a
wrong hold still blocks sales. So:
  - batch numbers that exist in our own stock for a matched SKU -> hold applied immediately
    (batch-level, all warehouses) and procurement is alerted;
  - SKU matches without a batch in our stock -> returned as PROPOSED holds for a human to confirm.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..integrations import notifications
from ..models import ComplianceHold, InventoryBatch, Sku
from . import llm
from .supplier_parser import match_sku

SYSTEM = """You extract drug-quality regulatory alerts for a pharmaceutical distributor in India. Input is the text of a CDSCO or \
state drug-controller notice (Not of Standard Quality list, spurious-drug alert, recall, ban/suspension), often a table pasted from PDF.

Return one entry per product/batch line. product_name as written (brand + strength); composition if stated; manufacturer as \
stated (the "manufactured by" firm, not the sampling location); batch exactly as written (keep letters, digits, slashes); \
reason short (e.g. "NSQ - dissolution failure", "spurious", "recall"). Use null for anything not stated. Never invent batches."""

SCHEMA = {
    "type": "object",
    "properties": {
        "issuer": {"type": ["string", "null"]},
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "composition": {"type": ["string", "null"]},
                    "manufacturer": {"type": ["string", "null"]},
                    "batch": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["product_name", "composition", "manufacturer", "batch", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["issuer", "alerts"],
    "additionalProperties": False,
}


def _norm_batch(b: str | None) -> str:
    return "".join(ch for ch in (b or "").upper() if ch.isalnum())


def process_alert(db: Session, text: str, source: str, today: date) -> dict[str, Any]:
    resp = llm.create(effort="low", max_tokens=8000, system=SYSTEM,
                      messages=[{"role": "user", "content": f"<notice source=\"{source}\">\n{text}\n</notice>"}],
                      output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    if resp.stop_reason == "refusal":
        raise llm.LLMUnavailable("model declined to parse this notice")
    data = json.loads(llm.text_of(resp))
    skus = list(db.scalars(select(Sku)))
    snap = list(db.scalars(select(InventoryBatch).where(InventoryBatch.qty > 0)))
    applied, proposed, unmatched = [], [], []
    for a in data.get("alerts", []):
        sku, score, why = match_sku(skus, a["product_name"], a.get("composition"), a.get("manufacturer"), cutoff=0.45)
        if sku is None:
            unmatched.append({**a, "reason_unmatched": why})
            continue
        reason = f"{source}: {a.get('reason') or 'regulatory alert'}"[:300]
        in_stock = [b for b in snap if b.sku_id == sku.sku_id and a.get("batch") and _norm_batch(b.batch) == _norm_batch(a["batch"])]
        if in_stock:
            hold = ComplianceHold(sku_id=sku.sku_id, warehouse_id=None, batch=in_stock[0].batch, reason=reason, source=source)
            db.add(hold)
            for b in in_stock:
                b.on_hold = True
            db.flush()
            applied.append({**a, "alert_batch": a.get("batch"), "hold_id": hold.id, "sku_id": sku.sku_id, "sku_name": sku.name,
                            "batch": in_stock[0].batch, "qty_blocked": sum(b.qty for b in in_stock),
                            "warehouses": sorted({b.warehouse_id for b in in_stock})})
        else:
            proposed.append({**a, "sku_id": sku.sku_id, "sku_name": sku.name, "match_score": score, "batch_in_our_stock": False})
    if applied:
        body = "COMPLIANCE HOLD applied: " + "; ".join(f"{x['sku_name']} batch {x['batch']} ({x['qty_blocked']:.0f} units)" for x in applied)
        notifications.send(db, "whatsapp", get_settings().procurement_whatsapp, "procurement_briefing", ref=f"reg-{today}", body=body[:1000])
    result = {"issuer": data.get("issuer"), "holds_applied": applied, "proposed_holds": proposed, "unmatched": unmatched}
    llm.record(db, "regulatory", text[:20000], llm.AgentResult(text=json.dumps(result, default=str)[:20000]))
    return result
