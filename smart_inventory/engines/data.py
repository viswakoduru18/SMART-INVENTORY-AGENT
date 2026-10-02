"""Loads the working set each engine needs into pandas frames (silver -> engine)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    BounceEvent,
    ComplianceHold,
    InventoryBatch,
    OpenPO,
    OrderLine,
    Retailer,
    Sku,
    SourcingRequest,
    Supplier,
    SupplierOffer,
    Warehouse,
)


def _frame(db: Session, stmt) -> pd.DataFrame:
    result = db.execute(stmt)
    return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


@dataclass
class WorkingSet:
    today: date
    skus: pd.DataFrame
    warehouses: pd.DataFrame
    retailers: pd.DataFrame
    suppliers: pd.DataFrame
    orders: pd.DataFrame
    bounces: pd.DataFrame
    inventory: pd.DataFrame
    open_pos: pd.DataFrame
    offers: pd.DataFrame
    holds: pd.DataFrame
    sourcing: pd.DataFrame
    cache: dict = field(default_factory=dict)

    @property
    def sku_index(self) -> dict[str, dict]:
        if "sku_index" not in self.cache:
            self.cache["sku_index"] = self.skus.set_index("sku_id").to_dict("index") if len(self.skus) else {}
        return self.cache["sku_index"]

    @property
    def supplier_index(self) -> dict[str, dict]:
        if "supplier_index" not in self.cache:
            self.cache["supplier_index"] = (
                self.suppliers.set_index("supplier_id").to_dict("index") if len(self.suppliers) else {}
            )
        return self.cache["supplier_index"]

    def held(self, sku_id: str, warehouse_id: str | None = None) -> bool:
        if self.holds.empty:
            return False
        h = self.holds[(self.holds.sku_id == sku_id) & (self.holds.batch.isna())]
        if warehouse_id is not None:
            h = h[h.warehouse_id.isna() | (h.warehouse_id == warehouse_id)]
        return not h.empty

    def daily_demand(self, history_days: int) -> pd.DataFrame:
        """True demand per sku/warehouse/day = fulfilled order qty + finally-lost bounce qty.

        Recovered bounces already appear as order lines, so only `final` bounces are added.
        """
        key = ("daily_demand", history_days)
        if key in self.cache:
            return self.cache[key]
        start = pd.Timestamp(self.today - timedelta(days=history_days))
        end = pd.Timestamp(self.today)
        parts = []
        if not self.orders.empty:
            o = self.orders[(self.orders.ts >= start) & (self.orders.ts < end)]
            parts.append(o.assign(day=o.ts.dt.normalize())[["sku_id", "warehouse_id", "retailer_id", "day", "qty"]])
        if not self.bounces.empty:
            b = self.bounces[(self.bounces.ts >= start) & (self.bounces.ts < end) & (self.bounces.outcome == "final")]
            parts.append(b.assign(day=b.ts.dt.normalize())[["sku_id", "warehouse_id", "retailer_id", "day", "qty"]])
        if parts:
            df = pd.concat(parts, ignore_index=True)
        else:
            df = pd.DataFrame(columns=["sku_id", "warehouse_id", "retailer_id", "day", "qty"])
        self.cache[key] = df
        return df


def load_working_set(db: Session, today: date, history_days: int = 200) -> WorkingSet:
    since = datetime.combine(today - timedelta(days=history_days), datetime.min.time())
    latest_snapshot = db.scalar(select(func.max(InventoryBatch.snapshot_date)).where(InventoryBatch.snapshot_date <= today))
    orders = _frame(db, select(
        OrderLine.order_id, OrderLine.retailer_id, OrderLine.sku_id, OrderLine.warehouse_id, OrderLine.qty,
        OrderLine.price, OrderLine.discount_pct, OrderLine.term_flag, OrderLine.ts,
    ).where(OrderLine.ts >= since))
    bounces = _frame(db, select(
        BounceEvent.bounce_id, BounceEvent.sku_id, BounceEvent.retailer_id, BounceEvent.warehouse_id, BounceEvent.qty,
        BounceEvent.reason_code, BounceEvent.outcome, BounceEvent.sourcing_attempted, BounceEvent.value_lost, BounceEvent.ts,
    ).where(BounceEvent.ts >= since))
    for df in (orders, bounces):
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
    def table(model, *where) -> pd.DataFrame:
        return _frame(db, select(*model.__table__.columns).where(*where))

    inventory = table(InventoryBatch, InventoryBatch.snapshot_date == latest_snapshot)

    sourcing_since = datetime.combine(today - timedelta(days=90), datetime.min.time())
    sourcing = _frame(db, select(
        SourcingRequest.id, SourcingRequest.sku_id, SourcingRequest.warehouse_id, SourcingRequest.status,
        SourcingRequest.created_at, SourcingRequest.qty, SourcingRequest.retailer_id,
    ).where(SourcingRequest.created_at >= sourcing_since))
    holds = table(ComplianceHold)
    if not holds.empty:
        holds = holds[holds.active]
    return WorkingSet(
        today=today,
        skus=table(Sku),
        warehouses=table(Warehouse),
        retailers=table(Retailer),
        suppliers=table(Supplier),
        orders=orders,
        bounces=bounces,
        inventory=inventory,
        open_pos=table(OpenPO),
        offers=table(SupplierOffer),
        holds=holds,
        sourcing=sourcing,
    )
