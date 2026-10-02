"""E4 Buffer and Suggested PO Engine.

Per SKU x warehouse:
    SS              = z(service_level) * sqrt(LT * var_d + mean_d^2 * var_LT)
    Required_buffer = SS + bounce_risk_uplift            (uplift only when sourcing is Difficult)
    Suggested_PO    = max(0, forecast_over_cover_days + Required_buffer - on_hand - open_PO)
then constrained by MOQ, pack size, shelf-life vs sell-through, supplier
availability and a per-warehouse working-capital cap (when the cap binds,
lines are ranked by priority, then margin earned per rupee of inventory).

Business example (spec section 7) reproduces exactly:
    Product A: stock 40, 2-day forecast 55, buffer 10 -> purchase 25
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import pandas as pd

from ..config import Policy
from ..models import SkuClass
from .data import WorkingSet


def z_for(service_level: float) -> float:
    if service_level <= 0:
        return 0.0
    return float(NormalDist().inv_cdf(min(service_level, 0.999)))


def safety_stock(z: float, lead_time: float, demand_mean: float, demand_std: float, lead_time_std: float) -> float:
    return z * math.sqrt(max(lead_time, 0) * demand_std**2 + demand_mean**2 * lead_time_std**2)


def suggested_qty(cover_demand: float, buffer: float, on_hand: float, open_po: float) -> float:
    return max(0.0, cover_demand + buffer - on_hand - open_po)


def apply_lot_rules(qty: float, pack_size: int, moq: int) -> tuple[float, list[str]]:
    applied = []
    if qty <= 0:
        return 0.0, applied
    pack = max(int(pack_size or 1), 1)
    rounded = math.ceil(qty / pack) * pack
    if rounded != qty:
        applied.append("PACK_ROUNDING")
    if rounded < (moq or 1):
        rounded = float(moq)
        applied.append("MOQ")
    return float(rounded), applied


def usable_on_hand(ws: WorkingSet, policy: Policy) -> pd.Series:
    inv = ws.inventory
    if inv.empty:
        return pd.Series(dtype=float)
    excl = int(policy.get("replenishment.near_expiry_days_excluded_from_on_hand", 30))
    cutoff = ws.today + pd.Timedelta(days=excl)
    ok = ~inv.on_hold.astype(bool) & (inv.expiry.isna() | (pd.to_datetime(inv.expiry) > pd.Timestamp(cutoff)))
    return inv[ok].groupby(["sku_id", "warehouse_id"]).qty.sum()


def best_offer(offers: pd.DataFrame, qty: float) -> dict[str, Any] | None:
    """Cheapest gated offer that can fill the qty; else cheapest gated offer with any stock."""
    if offers.empty:
        return None
    ok = offers[(offers.gate_status == "PASS") & (offers.available_qty.fillna(0) > 0)]
    if ok.empty:
        return None
    full = ok[ok.available_qty >= qty]
    pick = (full if not full.empty else ok).sort_values(["effective_price", "lead_time_days"]).iloc[0]
    return pick.to_dict()


def run_replenishment(
    ws: WorkingSet,
    policy: Policy,
    classes: dict[tuple[str, str], str],
    forecasts: dict[tuple[str, str], dict[int, dict[str, Any]]],
    profiles: dict[tuple[str, str], dict[str, Any]],
    stock_decisions: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cfg = policy.section("replenishment")
    service = cfg.get("service_levels", {})
    cover_cfg = cfg.get("cover_days", {})
    on_hand = usable_on_hand(ws, policy)
    open_po = ws.open_pos.groupby(["sku_id", "warehouse_id"]).qty.sum() if not ws.open_pos.empty else pd.Series(dtype=float)
    offers = ws.offers.copy()
    if not offers.empty:
        lt = {k: v.get("lead_time_days", 1.0) for k, v in ws.supplier_index.items()}
        offers["lead_time_days"] = offers.supplier_id.map(lt).fillna(1.0)
    offers_by_sku = dict(tuple(offers.groupby("sku_id"))) if not offers.empty else {}

    candidates = {
        k for k, c in classes.items()
        if c in (SkuClass.FAST.value, SkuClass.MEDIUM.value, SkuClass.CRITICAL_HARD_TO_SOURCE.value)
    } | {k for k, d in stock_decisions.items() if d["decision"] in ("STOCK", "LIMITED_SAFETY_STOCK")}

    buffers, pos = [], []
    for key in sorted(candidates):
        sku_id, wh = key
        sku = ws.sku_index.get(sku_id)
        if sku is None or ws.held(sku_id, wh):
            continue
        cls = classes.get(key, SkuClass.SPORADIC.value)
        sd = stock_decisions.get(key)
        f = forecasts.get(key, {})
        f1 = float(f.get(1, {}).get("p50", 0.0))
        f2 = float(f.get(2, {}).get("p50", 0.0))
        rate = float(f.get(1, {}).get("daily_rate", 0.0))
        std = float(f.get(1, {}).get("daily_std", 0.0))
        sl = float(service.get(cls, 0.0))
        if sd and sd["decision"] in ("STOCK", "LIMITED_SAFETY_STOCK"):
            sl = max(sl, float(cfg.get("stocking_candidate_service_level", 0.9)))
            if rate == 0 and sd["monthly_demand_units"] > 0:
                rate = sd["monthly_demand_units"] / 30.0
                std = math.sqrt(rate)
        sku_offers = offers_by_sku.get(sku_id, pd.DataFrame())
        pre = best_offer(sku_offers, 1)
        supplier = ws.supplier_index.get(pre["supplier_id"]) if pre else None
        lt = float(supplier["lead_time_days"]) if supplier else 1.0
        lt_std = float(supplier["lead_time_std_days"]) if supplier else 0.5
        z = z_for(sl)
        ss = safety_stock(z, lt, rate, std, lt_std)
        prof = profiles.get(key)
        uplift = 0.0
        if prof and prof["external_availability"] in ("DIFFICULT", "SUPPLY_SHORTAGE"):
            uplift = prof["bounced_qty_90"] / 90.0 * float(cfg.get("bounce_uplift_days", 1.0))
        min_stock = float(sd["target_stock_qty"]) if sd and sd["decision"] in ("STOCK", "LIMITED_SAFETY_STOCK") else 0.0
        buffer = ss + uplift
        cover_days = max(float(cover_cfg.get(cls, 2)), math.ceil(lt))
        cover = f1 if cover_days <= 1 else f2 + max(cover_days - 2, 0) * rate
        oh = float(on_hand.get(key, 0.0))
        opo = float(open_po.get(key, 0.0))
        buffers.append({
            "sku_id": sku_id, "warehouse_id": wh, "date": ws.today, "service_level": sl, "z": round(z, 3),
            "cover_days": cover_days, "lead_time_days": lt, "safety_stock": round(ss, 2), "bounce_uplift": round(uplift, 2),
            "min_stock": min_stock, "buffer_qty": round(buffer, 2),
        })
        raw = suggested_qty(cover, buffer, oh, opo)
        raw = max(raw, min_stock - oh - opo)
        if raw <= 0.0:
            continue
        qty, applied = apply_lot_rules(raw, int(sku.get("pack_size") or 1), int(sku.get("moq") or 1))
        offer = best_offer(sku_offers, qty)
        if offer and offer["available_qty"] < qty:
            qty = float(offer["available_qty"])
            applied.append("SUPPLIER_AVAILABILITY")
        # shelf life vs expected sell-through
        residual = ((offer["expiry"] - ws.today).days if offer and offer.get("expiry") else int(sku.get("shelf_life_days") or 730))
        max_sell = rate * residual * float(cfg.get("min_shelf_life_sell_through_ratio", 0.5)) if rate > 0 else qty
        if qty > max_sell and max_sell > 0:
            qty = float(math.floor(max_sell))
            applied.append("SHELF_LIFE_SELL_THROUGH")
        if qty <= 0:
            continue
        unit_cost = float(offer["effective_price"]) if offer else float(sku.get("cost") or 0)
        sell = float(sku.get("ptr") or sku["mrp"] * 0.8) * (1 - float(sku.get("normal_discount_pct") or 0) / 100)
        stockout = oh <= 0 and (f1 > 0 or (prof and prof["bounces_30"] > 0))
        if stockout or cls == SkuClass.CRITICAL_HARD_TO_SOURCE.value:
            priority = "HIGH"
        elif oh < buffer or (sd and sd["decision"] == "STOCK"):
            priority = "MEDIUM"
        else:
            priority = "LOW"
        if cls == SkuClass.CRITICAL_HARD_TO_SOURCE.value:
            reason = "CRITICAL_COVER"
        elif sd and sd["decision"] in ("STOCK", "LIMITED_SAFETY_STOCK") and cls not in (SkuClass.FAST.value, SkuClass.MEDIUM.value):
            reason = "STOCKING_CANDIDATE"
        elif stockout:
            reason = "STOCKOUT_RISK"
        else:
            reason = "BELOW_COVER_PLUS_BUFFER"
        if offer is None:
            applied.append("NO_GATED_SUPPLIER")
        pos.append({
            "po_draft_id": f"PO-{ws.today:%Y%m%d}-{wh}-{sku_id}", "date": ws.today, "warehouse_id": wh,
            "supplier_id": offer["supplier_id"] if offer else None, "sku_id": sku_id, "sku_class": cls,
            "current_stock": oh, "open_po_qty": opo, "forecast_1d": round(f1, 2), "forecast_2d": round(f2, 2),
            "cover_demand": round(cover, 2), "buffer_qty": round(buffer, 2), "raw_qty": round(raw, 2), "qty": qty,
            "unit_cost": round(unit_cost, 2), "line_value": round(qty * unit_cost, 2),
            "margin_per_rupee": round((sell - unit_cost) / unit_cost, 4) if unit_cost else 0.0,
            "priority": priority, "reason_code": reason, "constraints_applied": applied, "status": "DRAFT",
        })

    # working-capital cap per warehouse
    caps = policy.get("replenishment.working_capital_cap_inr", {}) or {}
    wh_caps = {}
    if not ws.warehouses.empty:
        wh_caps = {r.warehouse_id: r.working_capital_cap_inr for r in ws.warehouses.itertuples()}
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    for wh in {p["warehouse_id"] for p in pos}:
        cap = caps.get(wh) or wh_caps.get(wh) or caps.get("default")
        if not cap:
            continue
        lines = sorted((p for p in pos if p["warehouse_id"] == wh), key=lambda p: (rank[p["priority"]], -p["margin_per_rupee"]))
        spent = 0.0
        for p in lines:
            if spent + p["line_value"] > float(cap):
                p["status"] = "DEFERRED"
                p["constraints_applied"] = p["constraints_applied"] + ["WORKING_CAPITAL_CAP"]
            else:
                spent += p["line_value"]
    return buffers, pos
