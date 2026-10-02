"""E1 Signal and Bounce Intelligence.

Turns "Product Not Available" into demand intelligence per SKU x warehouse:
how often it is asked for, by how many retailers, 30/60/90-day bounce counts,
value lost, regular vs rare vs one-time demand, single vs multi-retailer, and
whether the outside market can supply it (Easy / Difficult / Supply shortage).
Also estimates suppressed demand: retailers stop asking for what they know is
not stocked, so bounces understate true demand.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..config import Policy
from .data import WorkingSet

SUCCESS_STATUSES = {"HELD", "PURCHASE_TASK", "FULFILLED"}


def _external_availability(attempts: int, successes: int, has_live_offer: bool, cfg: dict[str, Any]) -> tuple[str, float | None]:
    rate = successes / attempts if attempts else None
    if rate is None:
        return ("EASY" if has_live_offer else "UNKNOWN"), None
    if rate >= float(cfg.get("easy_sourcing_success_rate", 0.7)):
        return "EASY", rate
    if (
        attempts >= int(cfg.get("shortage_min_attempts", 3))
        and rate <= float(cfg.get("shortage_sourcing_success_rate", 0.2))
        and not has_live_offer
    ):
        return "SUPPLY_SHORTAGE", rate
    return "DIFFICULT", rate


def classify_demand_pattern(n_events: int, distinct_retailers: int, distinct_weeks: int, span_days: int, cfg: dict[str, Any]) -> str:
    if n_events <= 1 or (distinct_retailers == 1 and n_events <= 2 and span_days <= 7):
        return "ONE_TIME"
    if n_events >= int(cfg.get("regular_min_bounces_90d", 4)) and distinct_weeks >= int(cfg.get("regular_min_distinct_weeks", 3)):
        return "REGULAR"
    return "RARE"


def compute_bounce_profiles(ws: WorkingSet, policy: Policy) -> list[dict[str, Any]]:
    cfg = policy.section("bounce")
    today = pd.Timestamp(ws.today)
    b = ws.bounces
    if b.empty:
        return []
    b90 = b[b.ts >= today - pd.Timedelta(days=90)]
    if b90.empty:
        return []

    top_accounts = set(ws.retailers.loc[ws.retailers.is_top_account.astype(bool), "retailer_id"]) if not ws.retailers.empty else set()

    # live approved offers with stock -> outside market can supply today
    live = set()
    if not ws.offers.empty:
        sup = ws.supplier_index
        o = ws.offers[(ws.offers.available_qty.fillna(0) > 0)]
        for row in o.itertuples():
            s = sup.get(row.supplier_id)
            if s and s.get("approved"):
                live.add(row.sku_id)

    # sourcing attempts: bounce outcomes + platform sourcing requests
    att = b90[b90.sourcing_attempted.astype(bool)]
    attempts = att.groupby(["sku_id", "warehouse_id"]).size()
    successes = att[att.outcome == "recovered"].groupby(["sku_id", "warehouse_id"]).size()
    if not ws.sourcing.empty:
        sr = ws.sourcing[ws.sourcing.status.isin(SUCCESS_STATUSES | {"FAILED"})]
        attempts = attempts.add(sr.groupby(["sku_id", "warehouse_id"]).size(), fill_value=0)
        successes = successes.add(sr[sr.status.isin(SUCCESS_STATUSES)].groupby(["sku_id", "warehouse_id"]).size(), fill_value=0)

    asks90 = ws.orders[ws.orders.ts >= today - pd.Timedelta(days=90)] if not ws.orders.empty else ws.orders
    order_freq = asks90.groupby(["sku_id", "warehouse_id"]).size() if not asks90.empty else pd.Series(dtype=int)

    on_hand = (
        ws.inventory[~ws.inventory.on_hold.astype(bool)].groupby(["sku_id", "warehouse_id"]).qty.sum()
        if not ws.inventory.empty else pd.Series(dtype=float)
    )
    zero_stock_bounced = {k for k in set(zip(b90.sku_id, b90.warehouse_id)) if float(on_hand.get(k, 0.0)) <= 0}
    suppressed_by_key = suppressed_demand(ws.orders, b, zero_stock_bounced, today)

    profiles = []
    for (sku_id, wh), g in b90.groupby(["sku_id", "warehouse_id"]):
        age = (today - g.ts).dt.days
        n90 = len(g)
        n60 = int((age < 60).sum())
        n30 = int((age < 30).sum())
        distinct_r = g.retailer_id.nunique()
        weeks = g.ts.dt.isocalendar()
        distinct_weeks = int((weeks.year.astype(str) + "-" + weeks.week.astype(str)).nunique())
        span = int((g.ts.max() - g.ts.min()).days)
        share = float(g.retailer_id.value_counts(normalize=True).iloc[0])
        repeat = float((g.retailer_id.value_counts() >= 2).sum() / distinct_r) if distinct_r else 0.0
        key = (sku_id, wh)
        n_att = int(attempts.get(key, 0))
        n_ok = int(successes.get(key, 0))
        ext, rate = _external_availability(n_att, n_ok, sku_id in live, cfg)
        pattern = classify_demand_pattern(n90, distinct_r, distinct_weeks, span, cfg)
        suppressed = float(suppressed_by_key.get(key, 0.0))
        profiles.append({
            "sku_id": sku_id,
            "warehouse_id": wh,
            "date": ws.today,
            "bounces_30": n30,
            "bounces_60": n60,
            "bounces_90": n90,
            "bounced_qty_90": float(g.qty.sum()),
            "distinct_retailers_90": int(distinct_r),
            "distinct_weeks_90": distinct_weeks,
            "value_lost_90": float(g.value_lost.sum()),
            "recovered_90": int((g.outcome == "recovered").sum()),
            "repeat_ask_rate": round(repeat, 3),
            "top_retailer_share": round(share, 3),
            "order_frequency_90": int(order_freq.get(key, 0)) + n90 - int((g.outcome == "recovered").sum()),
            "demand_pattern": pattern,
            "retailer_spread": "SINGLE" if distinct_r == 1 else "MULTI",
            "external_availability": ext,
            "sourcing_success_rate": None if rate is None else round(rate, 3),
            "suppressed_demand_est": round(suppressed, 2),
            "top_account_asks": int(g.retailer_id.isin(top_accounts).sum()),
        })
    return profiles


def suppressed_demand(orders: pd.DataFrame, bounces: pd.DataFrame, keys: set[tuple[str, str]], today: pd.Timestamp) -> pd.Series:
    """Expected qty from retailers whose regular cadence says they should have asked, but went silent.

    Vectorised over all (sku, warehouse, retailer): cadence = median gap between asks (needs >= 3 asks);
    a retailer silent for > 1.5x cadence is assumed to have stopped asking because we never have it.
    """
    if not keys:
        return pd.Series(dtype=float)
    cols = ["sku_id", "warehouse_id", "retailer_id", "ts", "qty"]
    h = pd.concat([orders[cols] if not orders.empty else pd.DataFrame(columns=cols), bounces[cols]], ignore_index=True)
    idx = pd.MultiIndex.from_frame(h[["sku_id", "warehouse_id"]])
    h = h[idx.isin(list(keys))]
    if h.empty:
        return pd.Series(dtype=float)
    h = h.sort_values(["sku_id", "warehouse_id", "retailer_id", "ts"])
    g = ["sku_id", "warehouse_id", "retailer_id"]
    h["gap"] = h.groupby(g).ts.diff().dt.days
    r = h.groupby(g).agg(n=("ts", "size"), last=("ts", "max"), cadence=("gap", "median"), q=("qty", "mean"))
    r = r[(r.n >= 3) & (r.cadence > 0)]
    silent = (today - r["last"]).dt.days
    r = r[silent > 1.5 * r.cadence]
    if r.empty:
        return pd.Series(dtype=float)
    silent = (today - r["last"]).dt.days
    missed = np.minimum(np.floor(silent / r.cadence) - 1, np.floor(90 / r.cadence)).clip(lower=0)
    return (missed * r.q).groupby(level=["sku_id", "warehouse_id"]).sum()


def bounce_profile_index(profiles: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(p["sku_id"], p["warehouse_id"]): p for p in profiles}


