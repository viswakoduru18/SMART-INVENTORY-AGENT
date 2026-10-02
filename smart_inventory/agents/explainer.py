"""Decision Explainer agent: "why was a PO raised for SKU X?" in English or Telugu.

Reads the decision log (reason code + inputs snapshot); never changes a decision.
Falls back to a deterministic template when Claude is unavailable.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import DecisionLog, Sku
from . import llm

SYSTEM = """You explain inventory decisions made by Acintyo's deterministic decision engine to the procurement team of a \
pharmaceutical distributor. You are given the decision record: the primary action, its priority, reason code, the engine's \
own reason text and the exact inputs snapshot it used.

Explain in plain language why this action was chosen, citing the actual numbers from the snapshot (stock on hand, forecast, \
buffer, bounces, discounts). Say what procurement should do next and what would change the decision. Keep it under 150 words. \
Do not invent numbers that are not in the record. Do not recommend different quantities or prices from those in the record; \
if the inputs look wrong, say which input to check instead.

Action meanings: COMPLIANCE_HOLD blocks sale and sourcing; LIQUIDATE moves near-expiry stock on special non-returnable terms; \
SOURCE arranges live retailer demand from outside suppliers; STOCK means a purchase order draft; DISCOUNT offers a higher \
discount on slow stock with non-returnable terms; SELL means healthy at normal price; DONT_STOCK blocks auto-reorder."""

LANG = {"en": "English", "te": "Telugu (use Telugu script; keep SKU names, numbers and terms like PO, MOQ in English)"}


def decision_record(db: Session, d: DecisionLog) -> dict[str, Any]:
    sku = db.get(Sku, d.sku_id)
    return {
        "date": d.date.isoformat(), "sku_id": d.sku_id, "sku_name": sku.name if sku else None, "composition": sku.composition if sku else None,
        "warehouse_id": d.warehouse_id, "action": d.action, "priority": d.priority, "secondary_tags": d.secondary_tags,
        "class": d.sku_class, "reason_code": d.reason_code, "reason_text": d.reason_text, "inputs": d.inputs_snapshot,
        "status": d.status, "autonomy_mode": d.autonomy_mode, "rule_and_model_version": d.version,
    }


def template_explanation(rec: dict[str, Any]) -> str:
    i = rec["inputs"] or {}
    parts = [f"{rec['sku_name'] or rec['sku_id']} ({rec['warehouse_id']}): {rec['action']} [{rec['priority']}] - {rec['reason_text']}."]
    parts.append(f"Class {i.get('class')}; on hand {i.get('on_hand')}; forecast tomorrow {i.get('forecast_1d')}, 2-day {i.get('forecast_2d')}; "
                 f"buffer {i.get('buffer_qty')}.")
    if i.get("bounces_30_60_90"):
        parts.append(f"Bounces 30/60/90d: {i['bounces_30_60_90']}, pattern {i.get('demand_pattern')}, market {i.get('external_availability')}.")
    if i.get("po_qty"):
        parts.append(f"Suggested PO {i['po_qty']} from {i.get('supplier_id') or 'no gated supplier'}.")
    return " ".join(parts)


def explain(db: Session, decision_id: int, lang: str = "en") -> dict[str, Any]:
    d = db.get(DecisionLog, decision_id)
    if d is None:
        raise KeyError(decision_id)
    rec = decision_record(db, d)
    try:
        resp = llm.create(
            effort="low", max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": f"Explain this decision in {LANG.get(lang, 'English')}.\n\n"
                                                  f"<decision>\n{json.dumps(rec, default=str, indent=1)}\n</decision>"}],
        )
        text = llm.text_of(resp) if resp.stop_reason != "refusal" else template_explanation(rec)
        usage = getattr(resp, "usage", None)
        llm.record(db, "explainer", f"decision {decision_id} ({lang})", llm.AgentResult(
            text=text, input_tokens=getattr(usage, "input_tokens", 0) or 0, output_tokens=getattr(usage, "output_tokens", 0) or 0))
        return {"decision_id": decision_id, "lang": lang, "explanation": text, "source": "claude", "decision": rec}
    except llm.LLMUnavailable as exc:
        return {"decision_id": decision_id, "lang": "en", "explanation": template_explanation(rec), "source": "template",
                "note": str(exc), "decision": rec}
