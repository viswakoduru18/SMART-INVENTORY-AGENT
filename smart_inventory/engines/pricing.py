"""E7 Pricing and Liquidation Engine.

Fast movers: detect margin leakage (discount given above the cohort benchmark).
    Never cut blindly: emit a PRICE_TEST proposal on a retailer cohort with a
    holdout, measured on volume and wallet share, not margin alone.
Slow / non-moving / near-expiry stock, per batch, in recovery order:
    1. supplier return / expiry-credit rights  -> RETURN_TO_SUPPLIER (no discount wasted)
    2. tiered special-lot discount by days-to-expiry and ageing, Special /
       Non-Returnable terms, capped by margin floor (liquidation tiers may go to
       the liquidation recovery floor) and never above MRP / statutory ceiling
    3. below minimum residual shelf life -> not sellable, write-off / return flag
Checkout: retailer sees Normal (normal returns) vs Special (non-returnable), with
a per-retailer cap on special-term exposure.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Policy
from ..models import OrderLine, PriceOffer, Sku, SkuClass, TermFlag
from .compliance import max_discount_pct, min_discount_pct
from .data import WorkingSet

LOW_MOVEMENT = {SkuClass.SLOW.value, SkuClass.NON_MOVING.value, SkuClass.SPORADIC.value}


def pick_tier(tiers: list[dict[str, Any]], days_to_expiry: int, age_days: int, low_movement: bool, at_risk: bool) -> dict[str, Any] | None:
    for t in tiers:
        if "max_days_to_expiry" in t:
            if days_to_expiry <= int(t["max_days_to_expiry"]) and (low_movement or at_risk):
                return t
        elif low_movement and age_days >= int(t.get("min_age_days", 0)):
            return t
    return None


def run_pricing(
    ws: WorkingSet,
    policy: Policy,
    classes: dict[tuple[str, str], str],
    forecasts: dict[tuple[str, str], dict[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    cfg = policy.section("pricing")
    tiers = cfg.get("tiers", [])
    min_sale_life = int(cfg.get("min_residual_shelf_life_days_for_sale", 30))
    return_window = int(cfg.get("supplier_return_window_days", 120))
    valid_days = 14
    offers: list[dict[str, Any]] = []
    signals: dict[tuple[str, str], dict[str, Any]] = {}

    # ---------------- margin leakage on fast movers
    today = pd.Timestamp(ws.today)
    o30 = ws.orders[ws.orders.ts >= today - pd.Timedelta(days=30)] if not ws.orders.empty else ws.orders
    if not o30.empty:
        o30 = o30.assign(gross=o30.qty * o30.price)
        g = o30.groupby(["sku_id", "warehouse_id"]).apply(
            lambda d: pd.Series({"gross": d.gross.sum(), "disc": (d.gross * d.discount_pct).sum() / max(d.gross.sum(), 1e-9)}),
            include_groups=False,
        )
        bench = float(cfg.get("fast_target_discount_pct", 8.0))
        tol = float(cfg.get("fast_leakage_tolerance_pct", 0.5))
        for (sku_id, wh), r in g.iterrows():
            if classes.get((sku_id, wh)) != SkuClass.FAST.value:
                continue
            leak = r.disc - bench - tol
            sig = signals.setdefault((sku_id, wh), {})
            sig.update(current_discount=round(float(r.disc), 2), recommended_discount=bench)
            if leak > 0:
                sku = ws.sku_index.get(sku_id, {})
                value = float(r.gross) * (r.disc - bench) / 100
                sig.update(leakage_value_30d=round(value, 2), leakage_pct=round(float(r.disc - bench), 2))
                offers.append({
                    "offer_id": f"OFR-{ws.today:%Y%m%d}-{wh}-{sku_id}-TEST", "date": ws.today, "sku_id": sku_id, "warehouse_id": wh,
                    "batch": None, "tier": "PRICE_TEST", "kind": "PRICE_TEST",
                    "normal_discount_pct": round(float(r.disc), 2), "discount_pct": max(bench, min_discount_pct(sku, cfg)),
                    "term_flag": TermFlag.NORMAL.value, "qty": 0.0, "net_price": 0.0, "valid_from": ws.today,
                    "valid_to": ws.today + timedelta(days=28), "cohort": "TEST_50PCT_WITH_HOLDOUT", "status": "PROPOSED",
                    "reason": f"Fast mover: avg discount {r.disc:.1f}% vs cohort benchmark {bench:.1f}%; leakage Rs{value:,.0f} in 30d. "
                              f"Test on 50% of retailers with holdout; judge on volume and wallet share.",
                })

    # ---------------- slow / near-expiry batches
    inv = ws.inventory
    if not inv.empty:
        sup = ws.supplier_index
        for b in inv.itertuples():
            if b.on_hold or b.qty <= 0:
                continue
            key = (b.sku_id, b.warehouse_id)
            sku = ws.sku_index.get(b.sku_id)
            if sku is None:
                continue
            cls = classes.get(key, SkuClass.SLOW.value)
            dte = (b.expiry - ws.today).days if b.expiry is not None and not pd.isna(b.expiry) else 9999
            age = (ws.today - b.inward_date).days if b.inward_date is not None and not pd.isna(b.inward_date) else 0
            rate = float(forecasts.get(key, {}).get(1, {}).get("daily_rate", 0.0))
            at_risk = rate * max(dte - min_sale_life, 0) < b.qty  # won't sell through before it becomes unsellable
            low = cls in LOW_MOVEMENT
            sig = signals.setdefault(key, {})
            base = float(sku.get("ptr") or sku["mrp"] * 0.8)
            value = float(b.qty) * float(b.cost or sku.get("cost") or 0)
            if dte < min_sale_life:
                rights = sup.get(b.supplier_id, {}).get("return_rights") if b.supplier_id else False
                sig.setdefault("tags", set()).add("RETURN_TO_SUPPLIER" if rights else "WRITE_OFF_REVIEW")
                sig["unsellable_value"] = sig.get("unsellable_value", 0.0) + value
                continue
            if not (low or at_risk):
                continue
            rights = bool(b.supplier_id and sup.get(b.supplier_id, {}).get("return_rights"))
            if rights and dte <= return_window:
                sig.setdefault("tags", set()).add("RETURN_TO_SUPPLIER")
                sig["return_value"] = sig.get("return_value", 0.0) + value
                offers.append({
                    "offer_id": f"OFR-{ws.today:%Y%m%d}-{b.warehouse_id}-{b.sku_id}-{b.batch}", "date": ws.today,
                    "sku_id": b.sku_id, "warehouse_id": b.warehouse_id, "batch": b.batch, "tier": "SUPPLIER_RETURN",
                    "kind": "RETURN_TO_SUPPLIER", "normal_discount_pct": float(sku.get("normal_discount_pct") or 0),
                    "discount_pct": float(sku.get("normal_discount_pct") or 0), "term_flag": TermFlag.NORMAL.value,
                    "qty": float(b.qty), "net_price": 0.0, "valid_from": ws.today, "valid_to": ws.today + timedelta(days=valid_days),
                    "cohort": None, "status": "PROPOSED",
                    "reason": f"Batch {b.batch} expires in {dte}d; supplier {b.supplier_id} accepts expiry returns. Return before discounting.",
                })
                continue
            tier = pick_tier(tiers, dte, age, low, at_risk)
            if tier is None:
                continue
            liquidation = tier.get("action") == "LIQUIDATE"
            normal = float(sku.get("normal_discount_pct") or 0)
            cap = max_discount_pct(sku, liquidation, cfg)
            disc = min(normal + float(tier["extra_discount_pct"]), cap)
            disc = max(disc, min_discount_pct(sku, cfg))
            if disc <= normal:
                sig.setdefault("tags", set()).add("MARGIN_FLOOR_BLOCKS_DISCOUNT")
                continue
            action = "LIQUIDATE" if liquidation else "DISCOUNT"
            if sig.get("action") != "LIQUIDATE":
                sig["action"] = action
            sig["eligible_value"] = sig.get("eligible_value", 0.0) + value
            sig["recommended_discount"] = max(sig.get("recommended_discount", 0.0), round(disc, 2))
            sig.setdefault("current_discount", normal)
            offers.append({
                "offer_id": f"OFR-{ws.today:%Y%m%d}-{b.warehouse_id}-{b.sku_id}-{b.batch}", "date": ws.today,
                "sku_id": b.sku_id, "warehouse_id": b.warehouse_id, "batch": b.batch, "tier": tier["name"], "kind": "SPECIAL_LOT",
                "normal_discount_pct": normal, "discount_pct": round(disc, 2), "term_flag": TermFlag.SPECIAL_NON_RETURNABLE.value,
                "qty": float(b.qty), "net_price": round(base * (1 - disc / 100), 2), "valid_from": ws.today,
                "valid_to": min(ws.today + timedelta(days=valid_days), (b.expiry - timedelta(days=min_sale_life)) if b.expiry else ws.today + timedelta(days=valid_days)),
                "cohort": None, "status": "PROPOSED",
                "reason": f"{cls} batch {b.batch}: age {age}d, expires in {dte}d, {b.qty:.0f} units (Rs{value:,.0f}). "
                          f"Tier {tier['name']}: {normal:.1f}% -> {disc:.1f}% on Special / Non-Returnable terms.",
            })
    for sig in signals.values():
        if "tags" in sig:
            sig["tags"] = sorted(sig["tags"])
    return offers, signals


# ------------------------------------------------------------- checkout
def special_exposure(db: Session, retailer_id: str, today: date, window_days: int) -> float:
    since = today - timedelta(days=window_days)
    v = db.scalar(select(func.sum(OrderLine.qty * OrderLine.price * (1 - OrderLine.discount_pct / 100))).where(
        OrderLine.retailer_id == retailer_id, OrderLine.term_flag == TermFlag.SPECIAL_NON_RETURNABLE.value,
        OrderLine.ts >= since,
    ))
    return float(v or 0.0)


def checkout_options(db: Session, policy: Policy, retailer_id: str, sku_id: str, warehouse_id: str, today: date) -> dict[str, Any]:
    sku = db.get(Sku, sku_id)
    if sku is None:
        raise KeyError(sku_id)
    base = sku.selling_base
    options = [{
        "option": "NORMAL", "term_flag": TermFlag.NORMAL.value, "discount_pct": sku.normal_discount_pct,
        "net_price": round(base * (1 - sku.normal_discount_pct / 100), 2), "returnable": True,
        "terms": "Normal discount with normal return / GRB terms",
    }]
    test = db.scalar(select(PriceOffer).where(
        PriceOffer.sku_id == sku_id, PriceOffer.warehouse_id == warehouse_id, PriceOffer.kind == "PRICE_TEST",
        PriceOffer.status.in_(["APPROVED", "PUBLISHED"]), PriceOffer.valid_from <= today, PriceOffer.valid_to >= today,
    ))
    if test is not None:
        arm = experiment_arm(retailer_id, test.offer_id)
        options[0]["experiment"] = {"offer_id": test.offer_id, "arm": arm}
        if arm == "TEST":
            options[0]["discount_pct"] = test.discount_pct
            options[0]["net_price"] = round(base * (1 - test.discount_pct / 100), 2)
    cfg = policy.section("pricing")
    exposure = special_exposure(db, retailer_id, today, int(cfg.get("special_exposure_window_days", 90)))
    cap = float(cfg.get("max_retailer_special_exposure_inr", 50000))
    offer = db.scalar(select(PriceOffer).where(
        PriceOffer.sku_id == sku_id, PriceOffer.warehouse_id == warehouse_id, PriceOffer.kind == "SPECIAL_LOT",
        PriceOffer.status.in_(["APPROVED", "PUBLISHED"]), PriceOffer.valid_from <= today, PriceOffer.valid_to >= today,
    ).order_by(PriceOffer.discount_pct.desc()))
    blocked = None
    if offer is not None:
        headroom = cap - exposure
        if headroom <= 0:
            blocked = "RETAILER_SPECIAL_EXPOSURE_CAP"
        else:
            options.append({
                "option": "SPECIAL", "offer_id": offer.offer_id, "term_flag": TermFlag.SPECIAL_NON_RETURNABLE.value,
                "discount_pct": offer.discount_pct, "net_price": round(base * (1 - offer.discount_pct / 100), 2), "returnable": False,
                "max_qty": min(offer.qty, int(headroom // max(base * (1 - offer.discount_pct / 100), 0.01))), "batch": offer.batch,
                "valid_to": offer.valid_to.isoformat(),
                "terms": "Higher discount on Special / Non-Returnable terms: no GRB or expiry return on this line",
            })
    return {"sku_id": sku_id, "retailer_id": retailer_id, "warehouse_id": warehouse_id, "options": options,
            "special_exposure_inr": round(exposure, 2), "special_exposure_cap_inr": cap, "special_blocked_reason": blocked}


def experiment_arm(retailer_id: str, offer_id: str, test_share: int = 50) -> str:
    """Deterministic, sticky assignment of a retailer to a price-test arm (no state to store)."""
    h = int(hashlib.sha256(f"{offer_id}:{retailer_id}".encode()).hexdigest(), 16) % 100
    return "TEST" if h < test_share else "CONTROL"


def experiment_readout(db: Session, offer_id: str) -> dict[str, Any]:
    """Per-arm volume, revenue and margin for a price test. Order lines must carry the experiment offer_id."""
    offer = db.get(PriceOffer, offer_id)
    if offer is None or offer.kind != "PRICE_TEST":
        raise KeyError(offer_id)
    sku = db.get(Sku, offer.sku_id)
    lines = db.scalars(select(OrderLine).where(OrderLine.offer_id == offer_id)).all()
    arms: dict[str, dict[str, float]] = {}
    for ln in lines:
        arm = experiment_arm(ln.retailer_id, offer_id)
        a = arms.setdefault(arm, {"retailers": set(), "lines": 0, "qty": 0.0, "revenue": 0.0, "margin": 0.0})
        rev = ln.qty * ln.price * (1 - ln.discount_pct / 100)
        a["retailers"].add(ln.retailer_id)
        a["lines"] += 1
        a["qty"] += ln.qty
        a["revenue"] += rev
        a["margin"] += rev - ln.qty * (sku.cost if sku else 0.0)
    out = {arm: {**{k: round(v, 2) for k, v in a.items() if k != "retailers"}, "retailers": len(a["retailers"]),
                 "qty_per_retailer": round(a["qty"] / max(len(a["retailers"]), 1), 2)} for arm, a in arms.items()}
    return {"offer_id": offer_id, "sku_id": offer.sku_id, "warehouse_id": offer.warehouse_id, "test_discount_pct": offer.discount_pct,
            "baseline_discount_pct": offer.normal_discount_pct, "valid_to": offer.valid_to.isoformat(), "arms": out,
            "decision_rule": "Adopt only if TEST keeps qty per retailer and wallet share within tolerance of CONTROL while margin improves."}


def grb_allowed(db: Session, order_id: str, line_id: str) -> dict[str, Any]:
    line = db.scalar(select(OrderLine).where(OrderLine.order_id == order_id, OrderLine.line_id == line_id))
    if line is None:
        return {"allowed": None, "reason": "LINE_NOT_FOUND"}
    if line.term_flag == TermFlag.SPECIAL_NON_RETURNABLE.value:
        return {"allowed": False, "reason": "SPECIAL_NON_RETURNABLE_TERMS", "offer_id": line.offer_id}
    return {"allowed": True, "reason": "NORMAL_TERMS"}
