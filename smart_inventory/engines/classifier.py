"""E2 SKU Classifier.

Layer 1, statistical base class (Syntetos-Boylan) from 180 days of true demand:
    ADI  = history_days / days_with_demand
    CV^2 = (std / mean)^2 of non-zero daily demand sizes
    SMOOTH (ADI<1.32, CV2<0.49) | ERRATIC (ADI<1.32, CV2>=0.49)
    INTERMITTENT (ADI>=1.32, CV2<0.49) | LUMPY (ADI>=1.32, CV2>=0.49)
Layer 2, business overlays: Sporadic (1-2 events or single retailer), Critical
hard-to-source (repeat multi-retailer bounces that sourcing could NOT
recover + difficult external supply),
Non-moving (no demand in window).

Stability: a class change needs the new class to persist for `hysteresis_days`
(two weekly confirmations) so SKUs do not flip daily and churn price/stock.
Sparse SKUs (new launches) borrow the majority class of their composition group.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..config import Policy
from ..models import SkuClass
from .data import WorkingSet


def base_class(adi: float | None, cv2: float | None, adi_t: float = 1.32, cv2_t: float = 0.49) -> str:
    if adi is None:
        return "NONE"
    cv2 = cv2 or 0.0
    if adi < adi_t:
        return "SMOOTH" if cv2 < cv2_t else "ERRATIC"
    return "INTERMITTENT" if cv2 < cv2_t else "LUMPY"


def business_class_from_stats(
    base: str,
    adi: float | None,
    demand_days: int,
    demand_days_30: int,
    distinct_retailers: int,
    bounce_profile: dict[str, Any] | None,
    cfg: dict[str, Any],
) -> tuple[str, list[str]]:
    flags: list[str] = []
    bp = bounce_profile
    if (
        bp
        and bp["bounces_90"] - bp["recovered_90"] >= int(cfg.get("critical_min_bounces_90d", 3))
        and bp["distinct_retailers_90"] >= int(cfg.get("critical_min_distinct_retailers", 2))
        and bp["external_availability"] in ("DIFFICULT", "SUPPLY_SHORTAGE")
    ):
        flags.append("REPEAT_BOUNCE_HARD_SOURCE")
        return SkuClass.CRITICAL_HARD_TO_SOURCE.value, flags
    if demand_days == 0:
        return SkuClass.NON_MOVING.value, flags
    if demand_days <= int(cfg.get("sporadic_max_events", 2)) or (
        distinct_retailers == 1 and demand_days <= int(cfg.get("sporadic_single_retailer_max_events", 4))
    ):
        if distinct_retailers == 1:
            flags.append("SINGLE_RETAILER")
        return SkuClass.SPORADIC.value, flags
    if base == "SMOOTH":
        return SkuClass.FAST.value, flags
    if base == "ERRATIC":
        if demand_days_30 >= int(cfg.get("erratic_fast_min_events_30d", 15)):
            flags.append("VOLATILE")
            return SkuClass.FAST.value, flags
        return SkuClass.MEDIUM.value, flags
    if base == "INTERMITTENT":
        if adi is not None and adi < float(cfg.get("slow_adi_threshold", 4.0)):
            return SkuClass.MEDIUM.value, flags
        return SkuClass.SLOW.value, flags
    return SkuClass.SLOW.value, flags  # LUMPY


def apply_hysteresis(
    computed: str, prev: dict[str, Any] | None, today: date, cfg: dict[str, Any]
) -> tuple[str, str | None, date | None, list[str]]:
    """Returns (business_class, candidate_class, candidate_since, flags)."""
    if prev is None:
        return computed, None, None, ["INITIAL"]
    current = prev["business_class"]
    if computed == current:
        return current, None, None, []
    if computed in cfg.get("immediate_classes", []):
        return computed, None, None, ["IMMEDIATE_SWITCH"]
    days = int(cfg.get("hysteresis_days", 14))
    if prev.get("candidate_class") == computed and prev.get("candidate_since"):
        since = prev["candidate_since"]
        if (today - since).days >= days:
            return computed, None, None, ["CONFIRMED_SWITCH"]
        return current, computed, since, ["PENDING_SWITCH"]
    return current, computed, today, ["PENDING_SWITCH"]


def classify(
    ws: WorkingSet,
    policy: Policy,
    bounce_profiles: dict[tuple[str, str], dict[str, Any]],
    previous: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    cfg = policy.section("classification")
    hist = int(cfg.get("history_days", 180))
    adi_t, cv2_t = float(cfg.get("adi_threshold", 1.32)), float(cfg.get("cv2_threshold", 0.49))
    dd = ws.daily_demand(hist)

    stats = pd.DataFrame(columns=["sku_id", "warehouse_id", "demand_days", "mean", "std", "retailers", "days30", "first_day"])
    if not dd.empty:
        per_day = dd.groupby(["sku_id", "warehouse_id", "day"], as_index=False).qty.sum()
        per_day = per_day[per_day.qty > 0]
        cutoff30 = pd.Timestamp(ws.today - timedelta(days=30))
        stats = per_day.groupby(["sku_id", "warehouse_id"]).agg(
            demand_days=("qty", "size"), mean=("qty", "mean"), std=("qty", lambda s: float(np.std(s, ddof=0))),
            first_day=("day", "min"),
        ).reset_index()
        d30 = per_day[per_day.day >= cutoff30].groupby(["sku_id", "warehouse_id"]).size().rename("days30")
        retailers = dd.groupby(["sku_id", "warehouse_id"]).retailer_id.nunique().rename("retailers")
        stats = stats.join(d30, on=["sku_id", "warehouse_id"]).join(retailers, on=["sku_id", "warehouse_id"])
        stats["days30"] = stats["days30"].fillna(0).astype(int)
    stats_idx = {(r.sku_id, r.warehouse_id): r for r in stats.itertuples(index=False)}

    # universe: every sku x warehouse with demand, bounces, stock or a prior class
    universe = set(stats_idx) | set(bounce_profiles) | set(previous)
    if not ws.inventory.empty:
        universe |= set(zip(ws.inventory.sku_id, ws.inventory.warehouse_id))
    composition = ws.skus.set_index("sku_id").composition.to_dict() if not ws.skus.empty else {}

    rows: list[dict[str, Any]] = []
    for key in sorted(universe):
        sku_id, wh = key
        s = stats_idx.get(key)
        demand_days = int(s.demand_days) if s is not None else 0
        adi = hist / demand_days if demand_days else None
        cv2 = (s.std / s.mean) ** 2 if s is not None and s.mean else None
        b = base_class(adi, cv2, adi_t, cv2_t)
        computed, flags = business_class_from_stats(
            b, adi, demand_days, int(s.days30) if s is not None else 0,
            int(s.retailers) if s is not None and not pd.isna(s.retailers) else 0,
            bounce_profiles.get(key), cfg,
        )
        new_sku = s is not None and (pd.Timestamp(ws.today) - s.first_day).days < int(cfg.get("pooling_min_history_days", 30))
        rows.append({
            "sku_id": sku_id, "warehouse_id": wh, "date": ws.today, "base_class": b, "computed_class": computed,
            "adi": None if adi is None else round(adi, 3), "cv2": None if cv2 is None else round(float(cv2), 3),
            "demand_days": demand_days, "distinct_retailers": int(s.retailers) if s is not None and not pd.isna(s.retailers) else 0,
            "flags": flags, "_new": bool(new_sku), "_composition": composition.get(sku_id),
        })

    # pooling: new SKUs take their composition group's majority class (per warehouse)
    group_classes: dict[tuple[str, str], Counter] = {}
    for r in rows:
        if not r["_new"] and r["_composition"]:
            group_classes.setdefault((r["_composition"], r["warehouse_id"]), Counter())[r["computed_class"]] += 1
    for r in rows:
        if r["_new"] and r["_composition"] and r["computed_class"] != SkuClass.CRITICAL_HARD_TO_SOURCE.value:
            c = group_classes.get((r["_composition"], r["warehouse_id"]))
            if c:
                r["computed_class"] = c.most_common(1)[0][0]
                r["flags"] = r["flags"] + ["POOLED_BY_COMPOSITION"]

    for r in rows:
        r.pop("_new"), r.pop("_composition")
        business, cand, since, hflags = apply_hysteresis(r["computed_class"], previous.get((r["sku_id"], r["warehouse_id"])), ws.today, cfg)
        r.update(business_class=business, candidate_class=cand, candidate_since=since, flags=r["flags"] + hflags)
    return rows
