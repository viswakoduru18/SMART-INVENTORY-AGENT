"""Unit tests for the deterministic engines."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from smart_inventory.engines import bounce_to_stock, classifier, compliance, forecast, orchestrator, pricing, replenishment, signal

POLICY_SOURCING = {"min_residual_shelf_life_days": 90, "max_purchase_price_pct_of_mrp": 80}
TODAY = date(2026, 10, 2)


# ------------------------------------------------------------ compliance
def test_scheme_factor():
    assert compliance.scheme_factor("10+1") == pytest.approx(10 / 11)
    assert compliance.scheme_factor("5%") == pytest.approx(0.95)
    assert compliance.scheme_factor(None) == 1.0
    assert compliance.scheme_factor("buy more") == 1.0
    assert compliance.effective_price(110, "10+1") == pytest.approx(100.0)


def _supplier(**kw):
    base = {"approved": True, "licence_valid_until": TODAY + timedelta(days=100), "gst_compliant": True}
    return {**base, **kw}


def test_gates_pass_and_fail():
    sku = {"mrp": 100.0}
    ok = {"batch": "B1", "expiry": TODAY + timedelta(days=365), "effective_price": 60.0}
    assert compliance.gate_supplier_offer(ok, _supplier(), sku, TODAY, POLICY_SOURCING).passed
    bad = {"batch": None, "expiry": TODAY + timedelta(days=30), "effective_price": 90.0}
    res = compliance.gate_supplier_offer(bad, _supplier(approved=False, licence_valid_until=TODAY - timedelta(days=1), gst_compliant=False),
                                         sku, TODAY, POLICY_SOURCING, held=True)
    assert not res.passed
    assert set(res.failures) >= {"SUPPLIER_NOT_APPROVED", "LICENCE_EXPIRED", "GST_NOT_COMPLIANT", "BATCH_MISSING",
                                 "SHELF_LIFE_BELOW_MIN", "PRICE_ABOVE_CEILING", "COMPLIANCE_HOLD"}
    assert "SUPPLIER_UNKNOWN" in compliance.gate_supplier_offer(ok, None, sku, TODAY, POLICY_SOURCING).failures


def test_sale_price_guardrails():
    sku = {"mrp": 100.0, "ptr": 80.0, "cost": 68.0, "margin_floor_pct": 2.0, "price_ceiling": None}
    cfg = {"liquidation_min_recovery_pct": 85}
    # normal: net >= 68 * 1.02 = 69.36 -> max discount = 1 - 69.36/80 = 13.3%
    assert compliance.max_discount_pct(sku, False, cfg) == pytest.approx(13.3, abs=0.01)
    # liquidation: net >= 68 * 0.85 = 57.8 -> 27.75%
    assert compliance.max_discount_pct(sku, True, cfg) == pytest.approx(27.75, abs=0.01)
    # statutory ceiling below PTR forces a minimum discount
    assert compliance.min_discount_pct({**sku, "price_ceiling": 76.0}, cfg) == pytest.approx(5.0)


# ------------------------------------------------------------ classifier
@pytest.mark.parametrize("adi,cv2,expected", [
    (1.0, 0.2, "SMOOTH"), (1.1, 0.8, "ERRATIC"), (2.0, 0.3, "INTERMITTENT"), (5.0, 1.0, "LUMPY"), (None, None, "NONE"),
])
def test_syntetos_boylan(adi, cv2, expected):
    assert classifier.base_class(adi, cv2) == expected


CFG = {"slow_adi_threshold": 4.0, "erratic_fast_min_events_30d": 15, "sporadic_max_events": 2, "sporadic_single_retailer_max_events": 4,
       "critical_min_bounces_90d": 3, "critical_min_distinct_retailers": 2, "hysteresis_days": 14, "immediate_classes": ["CRITICAL_HARD_TO_SOURCE"]}


def test_business_overlays():
    assert classifier.business_class_from_stats("SMOOTH", 1.0, 150, 28, 30, None, CFG)[0] == "FAST"
    assert classifier.business_class_from_stats("ERRATIC", 1.2, 120, 10, 30, None, CFG)[0] == "MEDIUM"
    assert classifier.business_class_from_stats("ERRATIC", 1.2, 120, 20, 30, None, CFG) == ("FAST", ["VOLATILE"])
    assert classifier.business_class_from_stats("INTERMITTENT", 2.5, 70, 5, 10, None, CFG)[0] == "MEDIUM"
    assert classifier.business_class_from_stats("INTERMITTENT", 9.0, 20, 1, 10, None, CFG)[0] == "SLOW"
    assert classifier.business_class_from_stats("LUMPY", 9.0, 20, 1, 10, None, CFG)[0] == "SLOW"
    assert classifier.business_class_from_stats("LUMPY", 90.0, 2, 0, 2, None, CFG)[0] == "SPORADIC"
    assert classifier.business_class_from_stats("LUMPY", 45.0, 4, 0, 1, None, CFG)[0] == "SPORADIC"
    assert classifier.business_class_from_stats("NONE", None, 0, 0, 0, None, CFG)[0] == "NON_MOVING"
    bp = {"bounces_90": 6, "recovered_90": 1, "distinct_retailers_90": 3, "external_availability": "DIFFICULT"}
    assert classifier.business_class_from_stats("SMOOTH", 1.0, 150, 28, 30, bp, CFG)[0] == "CRITICAL_HARD_TO_SOURCE"
    # bounces that sourcing recovered do not make a SKU hard to source
    assert classifier.business_class_from_stats("SMOOTH", 1.0, 150, 28, 30, {**bp, "recovered_90": 5}, CFG)[0] == "FAST"


def test_hysteresis_requires_two_weeks():
    prev = {"business_class": "FAST", "candidate_class": None, "candidate_since": None}
    cls, cand, since, _ = classifier.apply_hysteresis("MEDIUM", prev, TODAY, CFG)
    assert (cls, cand, since) == ("FAST", "MEDIUM", TODAY)
    prev2 = {"business_class": "FAST", "candidate_class": "MEDIUM", "candidate_since": TODAY - timedelta(days=7)}
    assert classifier.apply_hysteresis("MEDIUM", prev2, TODAY, CFG)[0] == "FAST"
    prev3 = {**prev2, "candidate_since": TODAY - timedelta(days=14)}
    assert classifier.apply_hysteresis("MEDIUM", prev3, TODAY, CFG)[0] == "MEDIUM"
    # a different candidate restarts the clock
    assert classifier.apply_hysteresis("SLOW", prev3, TODAY, CFG)[1:3] == ("SLOW", TODAY)
    # critical switches immediately; first sighting is immediate
    assert classifier.apply_hysteresis("CRITICAL_HARD_TO_SOURCE", prev, TODAY, CFG)[0] == "CRITICAL_HARD_TO_SOURCE"
    assert classifier.apply_hysteresis("SLOW", None, TODAY, CFG)[0] == "SLOW"


# --------------------------------------------------------------- signal
def test_demand_pattern():
    cfg = {"regular_min_bounces_90d": 4, "regular_min_distinct_weeks": 3}
    assert signal.classify_demand_pattern(1, 1, 1, 0, cfg) == "ONE_TIME"
    assert signal.classify_demand_pattern(2, 1, 1, 3, cfg) == "ONE_TIME"
    assert signal.classify_demand_pattern(6, 4, 5, 60, cfg) == "REGULAR"
    assert signal.classify_demand_pattern(3, 2, 2, 40, cfg) == "RARE"
    assert signal.classify_demand_pattern(5, 3, 1, 4, cfg) == "RARE"  # burst in one week is not regular


# -------------------------------------------------------------- forecast
def test_croston_and_tsb_recover_rate():
    rng = np.random.default_rng(1)
    Y = ((rng.random((1, 400)) < 0.25) * 4.0)  # true rate = 1.0/day
    sba_preds, _ = forecast.croston_sba(Y, 0.1)
    tsb_preds, _ = forecast.tsb(Y, 0.1, 0.1)
    # one-step forecasts should be close to unbiased over the series (end-points are noisy by design)
    assert sba_preds[0, 100:].mean() == pytest.approx(Y[0, 100:].mean(), rel=0.2)
    assert tsb_preds[0, 100:].mean() == pytest.approx(Y[0, 100:].mean(), rel=0.2)


def test_seasonal_ewma_learns_weekly_pattern():
    days = pd.date_range("2026-01-01", periods=140, freq="D")
    pattern = np.where(days.dayofweek == 6, 2.0, 10.0)
    _, level = forecast.seasonal_ewma(pattern[None, :], days)
    f = forecast.dow_factors(pattern[None, :], days)
    assert level[0] * f[0, 6] < level[0] * f[0, 0]
    assert level[0] * f[0, 0] == pytest.approx(10.0, rel=0.15)


def test_wape():
    assert forecast.wape(np.array([10, 10]), np.array([8, 12])) == (pytest.approx(0.2), pytest.approx(0.0))
    assert forecast.wape(np.array([0, 0]), np.array([1, 1])) == (None, None)


# --------------------------------------------------------- replenishment
@pytest.mark.parametrize("stock,f2,buffer,expected", [
    (40, 55, 10, 25),  # Product A in the business spec
    (15, 9, 3, 0),     # Product B
    (0, 20, 5, 25),    # Product C
])
def test_spec_example_table(stock, f2, buffer, expected):
    assert replenishment.suggested_qty(f2, buffer, stock, 0) == expected


def test_safety_stock_formula():
    z = replenishment.z_for(0.95)
    assert z == pytest.approx(1.645, abs=0.001)
    ss = replenishment.safety_stock(z, lead_time=2, demand_mean=10, demand_std=3, lead_time_std=0.5)
    assert ss == pytest.approx(z * np.sqrt(2 * 9 + 100 * 0.25))
    assert replenishment.z_for(0.0) == 0.0


def test_lot_rules():
    assert replenishment.apply_lot_rules(23, 10, 1) == (30.0, ["PACK_ROUNDING"])
    assert replenishment.apply_lot_rules(3, 1, 5) == (5.0, ["MOQ"])
    assert replenishment.apply_lot_rules(0, 10, 5) == (0.0, [])


# -------------------------------------------------------- bounce-to-stock
@pytest.mark.parametrize("pattern,external,econ,expected", [
    ("REGULAR", "EASY", False, "STOCK"),
    ("REGULAR", "SUPPLY_SHORTAGE", False, "STOCK"),
    ("RARE", "EASY", True, "SOURCE_ON_DEMAND"),
    ("RARE", "DIFFICULT", True, "LIMITED_SAFETY_STOCK"),
    ("RARE", "DIFFICULT", False, "SOURCE_ON_DEMAND"),
    ("ONE_TIME", "DIFFICULT", True, "DONT_STOCK"),
])
def test_bounce_to_stock_rules(pattern, external, econ, expected):
    assert bounce_to_stock.decide(pattern, external, econ)[0] == expected


def test_bounce_economics():
    cfg = {"carrying_cost_annual_pct": 18, "expected_retention_factor": 0.6, "sourcing_overhead_pct": 1.5}
    hi = bounce_to_stock.economics(60, 100, 85, 0.1, 1.5, 7, 730, False, cfg)
    assert hi["economic_pass"] and hi["target_stock_qty"] == 14
    lo = bounce_to_stock.economics(0.5, 100, 99, 0.9, 1.0, 7, 365, False, cfg)
    assert not lo["economic_pass"]


# ----------------------------------------------------------- orchestrator
def test_priority_resolution():
    action, code, _, secondary = orchestrator.resolve([
        ("SELL", "P5", ""), ("STOCK", "P3", ""), ("LIQUIDATE", "P1", ""), ("DONT_STOCK", "P6", ""),
    ])
    assert action == "LIQUIDATE" and code == "P1"
    assert secondary == ["DONT_STOCK", "SELL", "STOCK"]
    assert orchestrator.resolve([("SELL", "x", ""), ("COMPLIANCE_HOLD", "P0", "")])[0] == "COMPLIANCE_HOLD"


# ---------------------------------------------------------------- pricing
TIERS = [
    {"name": "LIQUIDATE_T3", "max_days_to_expiry": 90, "extra_discount_pct": 7, "action": "LIQUIDATE"},
    {"name": "LIQUIDATE_T2", "max_days_to_expiry": 150, "extra_discount_pct": 4, "action": "LIQUIDATE"},
    {"name": "SLOW_T1", "min_age_days": 60, "extra_discount_pct": 2, "action": "DISCOUNT"},
    {"name": "SLOW_T0", "min_age_days": 0, "extra_discount_pct": 1, "action": "DISCOUNT"},
]


def test_pricing_tiers():
    assert pricing.pick_tier(TIERS, 80, 10, True, False)["name"] == "LIQUIDATE_T3"
    assert pricing.pick_tier(TIERS, 120, 10, True, False)["name"] == "LIQUIDATE_T2"
    assert pricing.pick_tier(TIERS, 400, 90, True, False)["name"] == "SLOW_T1"
    assert pricing.pick_tier(TIERS, 400, 10, True, False)["name"] == "SLOW_T0"
    # fast mover near expiry that will not sell through still liquidates; healthy fast mover gets nothing
    assert pricing.pick_tier(TIERS, 80, 10, False, True)["name"] == "LIQUIDATE_T3"
    assert pricing.pick_tier(TIERS, 400, 90, False, False) is None


# ------------------------------------------------------- supplier matching
def test_sku_matcher_never_crosses_manufacturer_or_pack():
    from types import SimpleNamespace as NS

    from smart_inventory.agents.supplier_parser import match_sku

    skus = [
        NS(sku_id="A", name="Pantoprazole Alkem 15 Tab", composition="Pantoprazole 40mg", pack="15 Tab", manufacturer="Alkem"),
        NS(sku_id="B", name="Pantoprazole Ajanta 30 Tab", composition="Pantoprazole 40mg", pack="30 Tab", manufacturer="Ajanta"),
        NS(sku_id="C", name="Telmisartan Mankind 10 Tab", composition="Telmisartan 40mg", pack="10 Tab", manufacturer="Mankind"),
    ]
    assert match_sku(skus, "PANTOPRAZOLE ALKEM 15TAB")[0].sku_id == "A"
    assert match_sku(skus, "Pantoprazole 30 tab")[0].sku_id == "B"
    assert match_sku(skus, "Pantoprazole 40 Cipla 15tab", "Pantoprazole 40mg")[2] == "SKU_NOT_MATCHED"  # wrong manufacturer
    assert match_sku(skus, "Telmisartan Lupin 30 Tab")[2] == "SKU_NOT_MATCHED"  # pack + manufacturer conflict
    assert match_sku(skus, "Pantoprazole")[2] == "AMBIGUOUS"  # two candidates tie
    assert match_sku(skus, "Totally Unknown Syrup")[0] is None
