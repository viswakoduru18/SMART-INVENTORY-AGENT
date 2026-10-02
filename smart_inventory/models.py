"""Data model (architecture doc section 6.2).

Reference + fact tables are synced from the ERP (the system of record).
Engine output tables are owned by this platform. Every table is keyed on
warehouse_id where it applies, so Hyderabad and Navi Mumbai share one platform.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


# --------------------------------------------------------------------------- enums
class SkuClass(str, enum.Enum):
    FAST = "FAST"
    MEDIUM = "MEDIUM"
    SLOW = "SLOW"
    SPORADIC = "SPORADIC"
    CRITICAL_HARD_TO_SOURCE = "CRITICAL_HARD_TO_SOURCE"
    NON_MOVING = "NON_MOVING"


class Action(str, enum.Enum):
    COMPLIANCE_HOLD = "COMPLIANCE_HOLD"  # P0
    LIQUIDATE = "LIQUIDATE"  # P1
    SOURCE = "SOURCE"  # P2
    STOCK = "STOCK"  # P3
    DISCOUNT = "DISCOUNT"  # P4
    SELL = "SELL"  # P5
    DONT_STOCK = "DONT_STOCK"  # P6


ACTION_PRIORITY = {a: i for i, a in enumerate(Action)}


class TermFlag(str, enum.Enum):
    NORMAL = "NORMAL"  # normal discount, normal return/GRB terms
    SPECIAL_NON_RETURNABLE = "SPECIAL_NON_RETURNABLE"


class BounceReason(str, enum.Enum):
    NOT_IN_STOCK = "NOT_IN_STOCK"
    EXTERNAL_SOURCING_FAILED = "EXTERNAL_SOURCING_FAILED"
    SUPPLY_SHORTAGE = "SUPPLY_SHORTAGE"
    COMPLIANCE_HOLD = "COMPLIANCE_HOLD"


# ------------------------------------------------------------------ reference data
class Warehouse(Base):
    __tablename__ = "warehouse"
    warehouse_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    working_capital_cap_inr: Mapped[float | None] = mapped_column(Float, nullable=True)


class Sku(Base):
    __tablename__ = "sku_master"
    sku_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    composition: Mapped[str | None] = mapped_column(String(300), index=True, nullable=True)
    pack: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pack_size: Mapped[int] = mapped_column(Integer, default=1)
    mrp: Mapped[float] = mapped_column(Float, default=0.0)
    ptr: Mapped[float | None] = mapped_column(Float, nullable=True)  # price to retailer (pre-discount)
    cost: Mapped[float] = mapped_column(Float, default=0.0)  # landed purchase cost per unit
    schedule: Mapped[str | None] = mapped_column(String(20), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shelf_life_days: Mapped[int] = mapped_column(Integer, default=730)
    moq: Mapped[int] = mapped_column(Integer, default=1)
    normal_discount_pct: Mapped[float] = mapped_column(Float, default=8.0)
    margin_floor_pct: Mapped[float] = mapped_column(Float, default=2.0)
    price_ceiling: Mapped[float | None] = mapped_column(Float, nullable=True)  # DPCO/NPPA ceiling
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def selling_base(self) -> float:
        return self.ptr if self.ptr else self.mrp * 0.8


class Retailer(Base):
    __tablename__ = "retailer"
    retailer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_top_account: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    drug_licence_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(30), nullable=True)


class Supplier(Base):
    __tablename__ = "supplier"
    supplier_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    licence_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    gst_compliant: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_time_days: Mapped[float] = mapped_column(Float, default=1.0)
    lead_time_std_days: Mapped[float] = mapped_column(Float, default=0.3)
    return_rights: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[str] = mapped_column(String(20), default="manual")  # api|whatsapp|email|manual
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)


# --------------------------------------------------------------------- fact tables
class OrderLine(Base):
    __tablename__ = "order_line_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    line_id: Mapped[str] = mapped_column(String(64))
    retailer_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float, default=0.0)  # unit price before discount
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    term_flag: Mapped[str] = mapped_column(String(32), default=TermFlag.NORMAL.value)
    offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    __table_args__ = (UniqueConstraint("order_id", "line_id", name="uq_order_line"),)


class BounceEvent(Base):
    __tablename__ = "bounce_event"
    bounce_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_line_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    retailer_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    reason_code: Mapped[str] = mapped_column(String(40), default=BounceReason.NOT_IN_STOCK.value)
    sourcing_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(20), default="pending")  # pending|recovered|final
    value_lost: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)


class RetailerSkuInteraction(Base):
    __tablename__ = "retailer_sku_interaction"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    retailer_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(20))  # search|cart_add|ask
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)


class InventoryBatch(Base):
    __tablename__ = "inventory_batch_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    batch: Mapped[str] = mapped_column(String(64))
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    inward_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)

    def age_days(self, today: date) -> int:
        return (today - self.inward_date).days if self.inward_date else 0


class OpenPO(Base):
    __tablename__ = "open_po"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[str] = mapped_column(String(64))
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class SupplierOffer(Base):
    __tablename__ = "supplier_offer"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    price: Mapped[float] = mapped_column(Float)
    scheme: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effective_price: Mapped[float] = mapped_column(Float)
    available_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    eta_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="price_list")  # api|price_list|llm_parsed|manual
    gate_status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PASS|FAIL|PENDING
    gate_failures: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ComplianceHold(Base):
    __tablename__ = "compliance_hold"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # None = all warehouses
    batch: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None = all batches
    reason: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(50), default="manual")  # CDSCO|state_fda|manual|qc
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ----------------------------------------------------------------- engine outputs
class SkuClassDaily(Base):
    __tablename__ = "sku_class_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    base_class: Mapped[str] = mapped_column(String(20))  # SMOOTH|ERRATIC|INTERMITTENT|LUMPY|NONE
    computed_class: Mapped[str] = mapped_column(String(32))  # today's raw class
    business_class: Mapped[str] = mapped_column(String(32))  # effective class after hysteresis
    candidate_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    candidate_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    adi: Mapped[float | None] = mapped_column(Float, nullable=True)
    cv2: Mapped[float | None] = mapped_column(Float, nullable=True)
    demand_days: Mapped[int] = mapped_column(Integer, default=0)
    distinct_retailers: Mapped[int] = mapped_column(Integer, default=0)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("sku_id", "warehouse_id", "date", name="uq_class_day"),)


class BounceProfile(Base):
    __tablename__ = "bounce_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    bounces_30: Mapped[int] = mapped_column(Integer, default=0)
    bounces_60: Mapped[int] = mapped_column(Integer, default=0)
    bounces_90: Mapped[int] = mapped_column(Integer, default=0)
    bounced_qty_90: Mapped[float] = mapped_column(Float, default=0.0)
    distinct_retailers_90: Mapped[int] = mapped_column(Integer, default=0)
    distinct_weeks_90: Mapped[int] = mapped_column(Integer, default=0)
    value_lost_90: Mapped[float] = mapped_column(Float, default=0.0)
    recovered_90: Mapped[int] = mapped_column(Integer, default=0)
    repeat_ask_rate: Mapped[float] = mapped_column(Float, default=0.0)
    top_retailer_share: Mapped[float] = mapped_column(Float, default=0.0)
    order_frequency_90: Mapped[int] = mapped_column(Integer, default=0)
    demand_pattern: Mapped[str] = mapped_column(String(20))  # REGULAR|RARE|ONE_TIME
    retailer_spread: Mapped[str] = mapped_column(String(20))  # SINGLE|MULTI
    external_availability: Mapped[str] = mapped_column(String(20))  # EASY|DIFFICULT|SUPPLY_SHORTAGE|UNKNOWN
    sourcing_success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    suppressed_demand_est: Mapped[float] = mapped_column(Float, default=0.0)
    top_account_asks: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("sku_id", "warehouse_id", "date", name="uq_bounce_profile_day"),)


class Forecast(Base):
    __tablename__ = "forecast"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)  # forecast origin (run date)
    horizon: Mapped[int] = mapped_column(Integer)  # 1 = tomorrow, 2 = next 2 days (cumulative)
    p50: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    daily_rate: Mapped[float] = mapped_column(Float, default=0.0)
    daily_std: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(40))
    model_version: Mapped[str] = mapped_column(String(60))
    sku_class: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("sku_id", "warehouse_id", "date", "horizon", name="uq_forecast"),)


class ForecastAccuracy(Base):
    __tablename__ = "forecast_accuracy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32))
    sku_class: Mapped[str] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(20))  # champion|challenger|live
    wape: Mapped[float | None] = mapped_column(Float, nullable=True)
    bias: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_skus: Mapped[int] = mapped_column(Integer, default=0)


class BufferPolicy(Base):
    __tablename__ = "buffer_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    service_level: Mapped[float] = mapped_column(Float)
    z: Mapped[float] = mapped_column(Float)
    cover_days: Mapped[float] = mapped_column(Float)
    lead_time_days: Mapped[float] = mapped_column(Float)
    safety_stock: Mapped[float] = mapped_column(Float)
    bounce_uplift: Mapped[float] = mapped_column(Float, default=0.0)
    min_stock: Mapped[float] = mapped_column(Float, default=0.0)
    buffer_qty: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("sku_id", "warehouse_id", "date", name="uq_buffer_day"),)


class StockDecision(Base):
    __tablename__ = "stock_decision"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    decision: Mapped[str] = mapped_column(String(30))  # STOCK|LIMITED_SAFETY_STOCK|SOURCE_ON_DEMAND|DONT_STOCK
    target_stock_qty: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_demand_units: Mapped[float] = mapped_column(Float, default=0.0)
    margin_captured: Mapped[float] = mapped_column(Float, default=0.0)
    carrying_cost: Mapped[float] = mapped_column(Float, default=0.0)
    expiry_risk_cost: Mapped[float] = mapped_column(Float, default=0.0)
    sourcing_delay_cost: Mapped[float] = mapped_column(Float, default=0.0)
    economic_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(400))
    __table_args__ = (UniqueConstraint("sku_id", "warehouse_id", "date", name="uq_stock_decision_day"),)


class PriceOffer(Base):
    __tablename__ = "price_offer"
    offer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tier: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(20))  # SPECIAL_LOT|PRICE_TEST|RETURN_TO_SUPPLIER
    normal_discount_pct: Mapped[float] = mapped_column(Float)
    discount_pct: Mapped[float] = mapped_column(Float)
    term_flag: Mapped[str] = mapped_column(String(32))
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    net_price: Mapped[float] = mapped_column(Float, default=0.0)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    cohort: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED")  # PROPOSED|APPROVED|PUBLISHED|REJECTED|EXPIRED
    reason: Mapped[str] = mapped_column(String(400))
    erp_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SuggestedPO(Base):
    __tablename__ = "suggested_po"
    po_draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_class: Mapped[str] = mapped_column(String(32))
    current_stock: Mapped[float] = mapped_column(Float, default=0.0)
    open_po_qty: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_1d: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_2d: Mapped[float] = mapped_column(Float, default=0.0)
    cover_demand: Mapped[float] = mapped_column(Float, default=0.0)
    buffer_qty: Mapped[float] = mapped_column(Float, default=0.0)
    raw_qty: Mapped[float] = mapped_column(Float, default=0.0)
    qty: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    line_value: Mapped[float] = mapped_column(Float, default=0.0)
    margin_per_rupee: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[str] = mapped_column(String(10))  # HIGH|MEDIUM|LOW
    reason_code: Mapped[str] = mapped_column(String(60))
    constraints_applied: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT|APPROVED|REJECTED|PUSHED|DEFERRED
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    erp_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(400), nullable=True)


class SourcingRequest(Base):
    __tablename__ = "sourcing_request"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bounce_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    order_line_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    retailer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN|HELD|PURCHASE_TASK|FULFILLED|FAILED|CANCELLED
    best_offer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranked_offers: Mapped[list] = mapped_column(JSON, default=list)
    eta_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    retailer_importance: Mapped[float] = mapped_column(Float, default=1.0)
    failure_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    retailer_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DecisionLog(Base):
    __tablename__ = "decision_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    sku_id: Mapped[str] = mapped_column(String(64), index=True)
    warehouse_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(30), index=True)
    priority: Mapped[str] = mapped_column(String(4))
    secondary_tags: Mapped[list] = mapped_column(JSON, default=list)
    sku_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(60))
    reason_text: Mapped[str] = mapped_column(String(600))
    inputs_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(80))
    autonomy_mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED")  # PROPOSED|APPROVED|OVERRIDDEN|AUTO_EXECUTED|SHADOW
    overridden_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    override_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("sku_id", "warehouse_id", "date", name="uq_decision_day"),
        Index("ix_decision_date_wh", "date", "warehouse_id"),
    )


# ------------------------------------------------------------------- operations
class IngestedEvent(Base):
    """Idempotency ledger for the event API."""

    __tablename__ = "ingested_event"
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(30))
    schema_version: Mapped[str] = mapped_column(String(10), default="1")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobRun(Base):
    __tablename__ = "job_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    run_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING|SUCCESS|FAILED|WARN
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class DataQualityResult(Base):
    __tablename__ = "data_quality_result"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    check: Mapped[str] = mapped_column(String(80))
    passed: Mapped[bool] = mapped_column(Boolean)
    severity: Mapped[str] = mapped_column(String(10))  # ERROR|WARN
    detail: Mapped[str] = mapped_column(String(600))


class AgentRun(Base):
    __tablename__ = "agent_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(40), index=True)
    request: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(20))  # whatsapp|portal|email
    recipient: Mapped[str] = mapped_column(String(100))
    template: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")  # QUEUED|SENT|FAILED|LOGGED
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
