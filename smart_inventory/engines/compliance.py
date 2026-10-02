"""Compliance hard gates. These BLOCK an action; they are never scores.

Covers: approved supplier list, drug licence validity, GST compliance, batch and
expiry capture, minimum residual shelf life, purchase price ceiling, sale price
never above MRP / statutory ceiling, margin floor, and active compliance holds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

_FREE_GOODS = re.compile(r"^\s*(\d+)\s*\+\s*(\d+)\s*$")
_PCT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


def scheme_factor(scheme: str | None) -> float:
    """Price multiplier implied by a trade scheme: '10+1' -> 10/11, '5%' -> 0.95."""
    if not scheme:
        return 1.0
    m = _FREE_GOODS.match(scheme)
    if m:
        paid, free = int(m.group(1)), int(m.group(2))
        return paid / (paid + free) if paid + free else 1.0
    m = _PCT.match(scheme)
    if m:
        return max(0.0, 1 - float(m.group(1)) / 100)
    return 1.0


def effective_price(price: float, scheme: str | None) -> float:
    return round(float(price) * scheme_factor(scheme), 4)


@dataclass
class GateResult:
    passed: bool
    failures: list[str]

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def gate_supplier_offer(
    offer: dict[str, Any],
    supplier: dict[str, Any] | None,
    sku: dict[str, Any],
    today: date,
    policy_sourcing: dict[str, Any],
    held: bool = False,
) -> GateResult:
    f: list[str] = []
    if supplier is None:
        f.append("SUPPLIER_UNKNOWN")
    else:
        if not supplier.get("approved"):
            f.append("SUPPLIER_NOT_APPROVED")
        lic = supplier.get("licence_valid_until")
        if lic is None:
            f.append("LICENCE_MISSING")
        elif lic < today:
            f.append("LICENCE_EXPIRED")
        if not supplier.get("gst_compliant"):
            f.append("GST_NOT_COMPLIANT")
    if not offer.get("batch"):
        f.append("BATCH_MISSING")
    expiry = offer.get("expiry")
    if expiry is None:
        f.append("EXPIRY_MISSING")
    elif (expiry - today).days < int(policy_sourcing.get("min_residual_shelf_life_days", 90)):
        f.append("SHELF_LIFE_BELOW_MIN")
    mrp = float(sku.get("mrp") or 0)
    eff = float(offer.get("effective_price") or offer.get("price") or 0)
    ceiling = mrp * float(policy_sourcing.get("max_purchase_price_pct_of_mrp", 80)) / 100 if mrp else None
    if ceiling and eff > ceiling:
        f.append("PRICE_ABOVE_CEILING")
    if eff <= 0:
        f.append("PRICE_INVALID")
    if held:
        f.append("COMPLIANCE_HOLD")
    return GateResult(not f, f)


def sale_price_bounds(sku: dict[str, Any], liquidation: bool, policy_pricing: dict[str, Any]) -> tuple[float, float]:
    """(min_net_price, max_net_price) a retailer may be charged for one unit."""
    mrp = float(sku.get("mrp") or 0)
    ceiling = sku.get("price_ceiling")
    max_price = min(mrp, float(ceiling)) if ceiling else mrp
    cost = float(sku.get("cost") or 0)
    if liquidation:
        min_price = cost * float(policy_pricing.get("liquidation_min_recovery_pct", 85)) / 100
    else:
        min_price = cost * (1 + float(sku.get("margin_floor_pct") or 0) / 100)
    return min_price, max_price


def max_discount_pct(sku: dict[str, Any], liquidation: bool, policy_pricing: dict[str, Any]) -> float:
    """Largest discount off the selling base that keeps the net price inside the bounds."""
    base = float(sku.get("ptr") or (float(sku.get("mrp") or 0) * 0.8))
    if base <= 0:
        return 0.0
    min_price, _ = sale_price_bounds(sku, liquidation, policy_pricing)
    return max(0.0, round((1 - min_price / base) * 100, 2))


def min_discount_pct(sku: dict[str, Any], policy_pricing: dict[str, Any]) -> float:
    """Smallest discount that keeps the net price at or below MRP / statutory ceiling."""
    base = float(sku.get("ptr") or (float(sku.get("mrp") or 0) * 0.8))
    if base <= 0:
        return 0.0
    _, max_price = sale_price_bounds(sku, False, policy_pricing)
    return max(0.0, round((1 - max_price / base) * 100, 2))
