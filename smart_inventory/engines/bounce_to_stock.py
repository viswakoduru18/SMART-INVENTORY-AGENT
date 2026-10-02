"""E6 Bounce-to-Stock Decision: Stock / Limited safety stock / Source-on-demand / Don't stock.

Rule table (architecture doc E6):
    Repeated demand + repeated bounce        -> STOCK (minimum inventory)
    Rare demand + easy external availability -> SOURCE_ON_DEMAND
    Rare demand + difficult availability     -> LIMITED_SAFETY_STOCK if the economics hold
    One-time demand                          -> DONT_STOCK

Economic test (monthly, INR). Note: the architecture draft lists sourcing-delay
cost on the cost side; it is a cost of NOT stocking, so it is counted here as a
benefit of stocking:
    benefit = margin on asks we would otherwise lose (weighted by retailer
              importance and retention) + sourcing overhead avoided on asks
              sourcing would have recovered
    cost    = carrying cost + expected expiry/GRB write-off of the stocked qty
    STOCK only if benefit > cost
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from ..config import Policy
from .data import WorkingSet


def decide(pattern: str, external: str, economic_pass: bool) -> tuple[str, str]:
    if pattern == "ONE_TIME":
        return "DONT_STOCK", "One-time demand: never stock"
    if pattern == "REGULAR":
        return "STOCK", "Repeated demand with repeated bounces: hold minimum inventory"
    if external in ("EASY", "UNKNOWN"):
        return "SOURCE_ON_DEMAND", "Rare demand and the market can supply: source just-in-time"
    if economic_pass:
        return "LIMITED_SAFETY_STOCK", "Rare demand, hard to source, and stocking pays for itself"
    return "SOURCE_ON_DEMAND", "Rare demand and hard to source, but stocking does not cover carrying + expiry risk"


def economics(
    monthly_units: float,
    unit_price: float,
    unit_cost: float,
    sourcing_success: float | None,
    importance: float,
    stock_days: float,
    shelf_life_days: float,
    has_return_rights: bool,
    cfg: dict[str, Any],
) -> dict[str, float]:
    unit_margin = max(unit_price - unit_cost, 0.0)
    success = 0.5 if sourcing_success is None else sourcing_success
    retention = float(cfg.get("expected_retention_factor", 0.6))
    lost_margin = monthly_units * (1 - success) * unit_margin * retention * importance
    sourcing_delay_cost = monthly_units * success * unit_price * float(cfg.get("sourcing_overhead_pct", 1.5)) / 100
    daily = monthly_units / 30.0
    target = max(math.ceil(daily * stock_days), 1) if monthly_units > 0 else 0
    value = target * unit_cost
    carrying = value * float(cfg.get("carrying_cost_annual_pct", 18.0)) / 100 / 12
    usable_days = max(shelf_life_days * 0.5, 30)
    unsold_frac = max(0.0, 1 - (daily * usable_days) / target) if target else 0.0
    expiry_risk = 0.0 if has_return_rights else value * unsold_frac / (usable_days / 30)
    margin_captured = lost_margin + sourcing_delay_cost
    return {
        "target_stock_qty": float(target),
        "margin_captured": round(margin_captured, 2),
        "carrying_cost": round(carrying, 2),
        "expiry_risk_cost": round(expiry_risk, 2),
        "sourcing_delay_cost": round(sourcing_delay_cost, 2),
        "economic_pass": margin_captured > carrying + expiry_risk,
    }


def run_bounce_to_stock(ws: WorkingSet, policy: Policy, profiles: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    cfg = policy.section("bounce_to_stock")
    today = pd.Timestamp(ws.today)
    o90 = ws.orders[ws.orders.ts >= today - pd.Timedelta(days=90)] if not ws.orders.empty else ws.orders
    fulfilled = o90.groupby(["sku_id", "warehouse_id"]).qty.sum() if not o90.empty else pd.Series(dtype=float)
    sup = ws.supplier_index
    return_rights_skus: set[str] = set()
    if not ws.offers.empty:
        for row in ws.offers[["sku_id", "supplier_id"]].drop_duplicates().itertuples():
            if sup.get(row.supplier_id, {}).get("return_rights"):
                return_rights_skus.add(row.sku_id)
    out = []
    for key, p in profiles.items():
        sku = ws.sku_index.get(key[0])
        if sku is None:
            continue
        monthly = (float(fulfilled.get(key, 0.0)) + p["bounced_qty_90"] - _recovered_qty(p) + p["suppressed_demand_est"]) / 3.0
        price = float(sku.get("ptr") or sku["mrp"] * 0.8) * (1 - float(sku.get("normal_discount_pct") or 0) / 100)
        importance = 1.0 + (float(cfg.get("top_retailer_weight", 1.5)) - 1.0) * (p["top_account_asks"] / max(p["bounces_90"], 1))
        stock_days = float(cfg.get("minimum_stock_days", 3)) if p["demand_pattern"] == "REGULAR" else float(cfg.get("limited_stock_days", 7))
        econ = economics(monthly, price, float(sku.get("cost") or 0), p["sourcing_success_rate"], importance, stock_days,
                         float(sku.get("shelf_life_days") or 730), key[0] in return_rights_skus, cfg)
        decision, reason = decide(p["demand_pattern"], p["external_availability"], econ["economic_pass"])
        if decision in ("SOURCE_ON_DEMAND", "DONT_STOCK"):
            econ["target_stock_qty"] = 0.0
        out.append({
            "sku_id": key[0], "warehouse_id": key[1], "date": ws.today, "decision": decision,
            "monthly_demand_units": round(monthly, 2), "reason": (
                f"{reason}. pattern={p['demand_pattern']}, external={p['external_availability']}, "
                f"bounces 30/60/90={p['bounces_30']}/{p['bounces_60']}/{p['bounces_90']}, retailers={p['distinct_retailers_90']}, "
                f"benefit Rs{econ['margin_captured']:.0f}/mo vs cost Rs{econ['carrying_cost'] + econ['expiry_risk_cost']:.0f}/mo"
            )[:400],
            **econ,
        })
    return out


def _recovered_qty(p: dict[str, Any]) -> float:
    # recovered bounces already appear as fulfilled order lines; avoid double counting
    if p["bounces_90"] == 0:
        return 0.0
    return p["bounced_qty_90"] * p["recovered_90"] / p["bounces_90"]
