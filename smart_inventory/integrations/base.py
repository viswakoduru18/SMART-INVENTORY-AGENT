"""ERP connector contract.

The platform never edits ERP stock, ledger or invoice history. Connectors READ
reference/fact data and WRITE only drafts (purchase orders, price/offer rules).
All records cross this boundary as dicts with canonical field names (see
config/erp.yaml for the canonical names per resource).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

READ_RESOURCES = (
    "warehouses",
    "skus",
    "retailers",
    "suppliers",
    "order_lines",
    "bounces",
    "inventory",
    "open_pos",
    "supplier_prices",
)


class ERPError(RuntimeError):
    pass


class ERPConnector(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, resource: str, since: datetime | None = None) -> list[dict[str, Any]]:
        """Return canonical records for a read resource. `since` enables incremental pulls."""

    @abstractmethod
    def push_po_draft(self, po: dict[str, Any]) -> str | None:
        """Create a DRAFT purchase order in the ERP. Returns the ERP reference."""

    @abstractmethod
    def push_price_rule(self, rule: dict[str, Any]) -> str | None:
        """Publish an offer/price rule (with return-term flag). Returns the ERP reference."""

    def live_stock(self, sku_id: str, warehouse_id: str) -> float | None:
        """Real-time available qty, if the ERP exposes it. None = not supported."""
        return None

    def health(self) -> dict[str, Any]:
        return {"connector": self.name, "ok": True}
