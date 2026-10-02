"""E8 Decision Orchestrator: engines propose, the orchestrator disposes.

Exactly one primary action per SKU x warehouse per day, highest priority wins:
    P0 COMPLIANCE_HOLD  recall / quarantine / licence issue      -> block sale and sourcing
    P1 LIQUIDATE        near-expiry + low movement (after return check)
    P2 SOURCE           open demand, zero stock, supplier route
    P3 STOCK            below cover + buffer, or stocking candidate -> PO draft
    P4 DISCOUNT         slow-moving with inventory on hand
    P5 SELL             healthy at normal price (margin-leakage tag if relevant)
    P6 DONT_STOCK       one-time demand, or slow with zero inventory -> block auto-reorder
Every decision stores the inputs snapshot + hash, rule/model versions, reason
code, autonomy mode and (later) who approved/overrode it and the outcome.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd

from ..config import Policy
from ..models import Action, SkuClass
from .data import WorkingSet
from .forecast import MODEL_VERSION

ENGINE_VERSION = "engines-1.0"
PRIORITY = {a.value: f"P{i}" for i, a in enumerate(Action)}
LOW_MOVEMENT = {SkuClass.SLOW.value, SkuClass.SPORADIC.value, SkuClass.NON_MOVING.value}


def _hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def resolve(candidates: list[tuple[str, str, str]]) -> tuple[str, str, str, list[str]]:
    """candidates: (action, reason_code, reason_text). Returns primary + secondary tags."""
    ordered = sorted(candidates, key=lambda c: list(PRIORITY).index(c[0]))
    primary = ordered[0]
    secondary = sorted({c[0] for c in ordered[1:] if c[0] != primary[0]})
    return primary[0], primary[1], primary[2], secondary


def auto_gate(policy: Policy, accuracy: list[dict[str, Any]]) -> dict[tuple[str, str], bool]:
    """(warehouse, class) -> True when the champion back-test WAPE is inside the gate."""
    max_wape = float(policy.get("autonomy.auto_po_gate_max_wape", 0.35))
    out = {}
    for a in accuracy:
        if a["role"] == "champion" and a["wape"] is not None:
            out[(a["warehouse_id"], a["sku_class"])] = a["wape"] <= max_wape
    return out


def run_orchestrator(
    ws: WorkingSet,
    policy: Policy,
    classes: dict[tuple[str, str], dict[str, Any]],
    forecasts: dict[tuple[str, str], dict[int, dict[str, Any]]],
    buffers: dict[tuple[str, str], dict[str, Any]],
    profiles: dict[tuple[str, str], dict[str, Any]],
    stock_decisions: dict[tuple[str, str], dict[str, Any]],
    pos: list[dict[str, Any]],
    price_signals: dict[tuple[str, str], dict[str, Any]],
    accuracy: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mode = str(policy.get("autonomy.mode", "ASSIST")).upper()
    auto_classes = set(policy.get("autonomy.auto_po_classes", []) or [])
    auto_max = float(policy.get("autonomy.auto_po_max_value_inr", 50000))
    gates = auto_gate(policy, accuracy)
    version = f"{policy.version}|{MODEL_VERSION}|{ENGINE_VERSION}"
    po_by_key = {(p["sku_id"], p["warehouse_id"]): p for p in pos}

    inv = ws.inventory
    on_hand = inv[~inv.on_hold.astype(bool)].groupby(["sku_id", "warehouse_id"]).qty.sum() if not inv.empty else pd.Series(dtype=float)
    held_batches = inv[inv.on_hold.astype(bool)].groupby(["sku_id", "warehouse_id"]).qty.sum() if not inv.empty else pd.Series(dtype=float)
    stock_value = (inv.assign(v=inv.qty * inv.cost).groupby(["sku_id", "warehouse_id"]).v.sum() if not inv.empty else pd.Series(dtype=float))
    open_src = set()
    if not ws.sourcing.empty:
        s = ws.sourcing[ws.sourcing.status.isin(["OPEN", "HELD", "PURCHASE_TASK"])]
        open_src = set(zip(s.sku_id, s.warehouse_id))

    universe = set(classes) | set(price_signals) | set(po_by_key) | set(stock_decisions) | open_src
    rows = []
    for key in sorted(universe):
        sku_id, wh = key
        if sku_id not in ws.sku_index:
            continue
        c = classes.get(key, {})
        cls = c.get("business_class", SkuClass.SPORADIC.value)
        f = forecasts.get(key, {})
        b = buffers.get(key, {})
        p = profiles.get(key)
        sd = stock_decisions.get(key)
        po = po_by_key.get(key)
        sig = price_signals.get(key, {})
        oh = float(on_hand.get(key, 0.0))
        tags = list(sig.get("tags", [])) + list(c.get("flags", []))
        cands: list[tuple[str, str, str]] = []

        if ws.held(sku_id, wh):
            cands.append(("COMPLIANCE_HOLD", "P0_COMPLIANCE_HOLD", "Active recall / quarantine / licence hold: sale and sourcing blocked"))
        if float(held_batches.get(key, 0.0)) > 0:
            tags.append("BATCH_ON_HOLD")
        if sig.get("action") == "LIQUIDATE":
            cands.append(("LIQUIDATE", "P1_NEAR_EXPIRY_LOW_MOVEMENT",
                          f"Near-expiry / at-risk stock worth Rs{sig.get('eligible_value', 0):,.0f}: special lot at {sig.get('recommended_discount', 0):.1f}%"))
        if key in open_src:
            cands.append(("SOURCE", "P2_OPEN_SOURCING_REQUEST", "Live retailer demand being sourced externally"))
        elif oh <= 0 and p and p["bounces_30"] > 0 and sd and sd["decision"] == "SOURCE_ON_DEMAND":
            cands.append(("SOURCE", "P2_SOURCE_ON_DEMAND", f"Zero stock, {p['bounces_30']} bounce(s) in 30d, market can supply: source on demand"))
        if po and po["status"] in ("DRAFT", "DEFERRED"):
            cands.append(("STOCK", f"P3_{po['reason_code']}",
                          f"Suggested PO {po['qty']:.0f} = cover {po['cover_demand']:.1f} + buffer {po['buffer_qty']:.1f} "
                          f"- on hand {po['current_stock']:.0f} - open PO {po['open_po_qty']:.0f}"
                          + (" (deferred: working-capital cap)" if po["status"] == "DEFERRED" else "")))
        if sig.get("action") == "DISCOUNT":
            cands.append(("DISCOUNT", "P4_SLOW_MOVING_WITH_STOCK",
                          f"Slow-moving stock Rs{sig.get('eligible_value', 0):,.0f}: offer {sig.get('recommended_discount', 0):.1f}% on Special / Non-Returnable terms"))
        if (sd and sd["decision"] == "DONT_STOCK") or (cls in LOW_MOVEMENT and oh <= 0):
            cands.append(("DONT_STOCK", "P6_NO_REORDER",
                          "One-time demand: do not stock" if sd and sd["decision"] == "DONT_STOCK"
                          else f"{cls} with zero inventory: do not reorder unless demand appears"))
        if not cands or oh > 0:
            leak = sig.get("leakage_value_30d")
            if leak:
                tags.append("MARGIN_LEAKAGE")
            cands.append(("SELL", "P5_HEALTHY" if not leak else "P5_HEALTHY_MARGIN_LEAKAGE",
                          "Healthy at normal price" + (f"; discount leakage Rs{leak:,.0f}/30d, price test proposed" if leak else "")))

        action, code, text, secondary = resolve(cands)
        status = "SHADOW" if mode == "SHADOW" else "PROPOSED"
        if action == "STOCK" and po and po["status"] == "DRAFT" and mode == "AUTO" and cls in auto_classes \
                and gates.get((wh, cls), False) and po["line_value"] <= auto_max and po["supplier_id"]:
            po["status"] = "APPROVED"
            po["approved_by"] = "AUTO"
            status = "AUTO_EXECUTED"
        if action in ("SELL", "DONT_STOCK") and mode != "SHADOW":
            status = "APPROVED"  # no write-back needed; recorded for audit
        snapshot = {
            "class": cls, "base_class": c.get("base_class"), "adi": c.get("adi"), "cv2": c.get("cv2"),
            "on_hand": oh, "stock_value": round(float(stock_value.get(key, 0.0)), 2),
            "forecast_1d": f.get(1, {}).get("p50"), "forecast_2d": f.get(2, {}).get("p50"),
            "forecast_method": f.get(1, {}).get("method"), "buffer_qty": b.get("buffer_qty"), "service_level": b.get("service_level"),
            "po_qty": po["qty"] if po else None, "po_status": po["status"] if po else None, "supplier_id": po["supplier_id"] if po else None,
            "bounces_30_60_90": [p["bounces_30"], p["bounces_60"], p["bounces_90"]] if p else None,
            "demand_pattern": p["demand_pattern"] if p else None, "external_availability": p["external_availability"] if p else None,
            "stock_decision": sd["decision"] if sd else None, "current_discount": sig.get("current_discount"),
            "recommended_discount": sig.get("recommended_discount"), "leakage_30d": sig.get("leakage_value_30d"),
        }
        rows.append({
            "date": ws.today, "sku_id": sku_id, "warehouse_id": wh, "action": action, "priority": PRIORITY[action],
            "secondary_tags": sorted(set(secondary + tags)), "sku_class": cls, "reason_code": code, "reason_text": text[:600],
            "inputs_snapshot": snapshot, "inputs_hash": _hash(snapshot), "version": version, "autonomy_mode": mode, "status": status,
        })
    return rows
