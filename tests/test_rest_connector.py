"""The YAML-mapped REST connector against the reference ERP API (contract test)."""
from __future__ import annotations

import os
import sys
from datetime import date

from fastapi.testclient import TestClient

from smart_inventory.config import load_yaml
from smart_inventory.integrations.mock_erp import MockERPConnector
from smart_inventory.integrations.rest_erp import RestERPConnector, get_path, map_inbound, map_outbound

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from reference_erp_server import build_app  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _connector(page_size=50):
    cfg = load_yaml(os.path.join(ROOT, "config", "erp.yaml"))
    cfg["rest"]["base_url"] = "http://erp.test"
    cfg["rest"]["pagination"]["page_size"] = page_size
    mock = MockERPConnector({"seed": 3, "skus": 30, "retailers": 20, "history_days": 60}, today=date(2026, 10, 2))
    app = build_app(mock, cfg)
    client = TestClient(app, base_url="http://erp.test")
    return RestERPConnector(cfg["rest"], client=client), mock, app


def test_mapping_helpers():
    assert get_path({"a": {"b": [1, {"c": 5}]}}, "a.b.1.c") == 5
    assert map_inbound({"item_code": "X", "stock": {"qty": 3}}, {"sku_id": "item_code", "qty": "stock.qty"}) == {"sku_id": "X", "qty": 3}
    assert map_outbound({"sku_id": "X", "valid_from": date(2026, 1, 1), "unused": 1}, {"sku_id": "item_code", "valid_from": "vf"}) == {
        "item_code": "X", "vf": "2026-01-01"}


def test_rest_connector_reads_every_resource_with_pagination():
    conn, mock, _ = _connector(page_size=50)
    for res in ("warehouses", "skus", "retailers", "suppliers", "order_lines", "bounces", "inventory", "open_pos", "supplier_prices"):
        got = conn.fetch(res)
        assert len(got) == len(mock.fetch(res)), res
    sku = conn.fetch("skus")[0]
    assert set(sku) >= {"sku_id", "name", "mrp", "cost", "composition"}


def test_rest_connector_writes_drafts_and_live_stock():
    conn, mock, app = _connector()
    ref = conn.push_po_draft({"warehouse_id": "HYD01", "supplier_id": "SUP001", "sku_id": "SKU00001", "qty": 10, "unit_cost": 12.5,
                              "reason_code": "BELOW_COVER_PLUS_BUFFER"})
    assert ref.startswith("ERP-PO-")
    sent = app.state.received[-1]
    assert sent["item_code"] == "SKU00001" and sent["branch_code"] == "HYD01" and sent["status"] == "DRAFT"
    assert conn.push_price_rule({"sku_id": "SKU00001", "warehouse_id": "HYD01", "discount_pct": 10, "term_flag": "SPECIAL_NON_RETURNABLE",
                                 "valid_from": date(2026, 10, 2), "valid_to": date(2026, 10, 16)}).startswith("ERP-RULE-")
    inv = mock.fetch("inventory")[0]
    assert conn.live_stock(inv["sku_id"], inv["warehouse_id"]) is not None


def test_full_cycle_over_rest(tmp_path):
    """Sync through the REST connector into a fresh DB and run the whole decision cycle."""
    from smart_inventory import db as dbm
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    conn, _, _ = _connector(page_size=200)
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/rest.db")
    dbm.Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        out = run_daily_cycle(s, date(2026, 10, 2), erp=conn, enforce_dq=False)
        s.commit()
    assert out["sync"]["order_lines"] > 0 and out["orchestrator"]["decisions"] > 0
