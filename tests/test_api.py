"""End-to-end tests: mock ERP -> daily cycle -> API flows used by portal, procurement, ERP and agents."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from smart_inventory.agents import llm
from smart_inventory.config import get_settings
from smart_inventory.models import (
    BounceEvent,
    DecisionLog,
    PriceOffer,
    Sku,
    SkuClassDaily,
    SourcingRequest,
    SuggestedPO,
    Supplier,
    SupplierOffer,
)

from .conftest import TODAY, FakeClaude, block_text, block_tool, message


# ------------------------------------------------------------------ cycle
def test_cycle_ran_every_stage(seeded):
    for stage in ("sync", "data_quality", "offer_gates", "signal", "classification", "forecast", "bounce_to_stock",
                  "replenishment", "pricing", "orchestrator"):
        assert stage in seeded, stage
    assert seeded["orchestrator"]["decisions"] > 0
    assert seeded["replenishment"]["po_lines"] > 0


def test_one_primary_action_per_sku_warehouse(db):
    dupes = db.execute(select(DecisionLog.sku_id, DecisionLog.warehouse_id, func.count()).where(DecisionLog.date == TODAY)
                       .group_by(DecisionLog.sku_id, DecisionLog.warehouse_id).having(func.count() > 1)).all()
    assert dupes == []
    d = db.scalar(select(DecisionLog).where(DecisionLog.date == TODAY))
    assert d.inputs_hash and d.version and d.reason_code.startswith(d.priority)


def test_classes_match_archetypes(db, erp):
    """Sanity check the classifier against the archetypes the mock generated."""
    rows = db.scalars(select(SkuClassDaily).where(SkuClassDaily.date == TODAY, SkuClassDaily.warehouse_id == "HYD01")).all()
    agree = total = 0
    expected = {"fast": {"FAST"}, "nonmoving": {"NON_MOVING"}, "sporadic": {"SPORADIC", "NON_MOVING"},
                "hard": {"CRITICAL_HARD_TO_SOURCE", "SLOW", "MEDIUM"}}
    for r in rows:
        arch = erp.archetype_of(r.sku_id)
        if arch in expected:
            total += 1
            agree += r.business_class in expected[arch]
    assert total > 0 and agree / total >= 0.85


def test_po_math_is_consistent(db):
    for po in db.scalars(select(SuggestedPO).where(SuggestedPO.date == TODAY)).all():
        raw = max(0.0, po.cover_demand + po.buffer_qty - po.current_stock - po.open_po_qty)
        assert po.raw_qty >= raw - 0.03  # components are stored rounded to 2dp
        assert po.qty > 0
        if "SUPPLIER_AVAILABILITY" not in po.constraints_applied and "SHELF_LIFE_SELL_THROUGH" not in po.constraints_applied:
            assert po.qty >= po.raw_qty - 0.01


# ------------------------------------------------------------- dashboards
@pytest.mark.parametrize("path", ["kpis", "inventory", "bounce", "purchase", "margin"])
def test_dashboards(client, path):
    r = client.get(f"/api/v1/dashboard/{path}", params={"date": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["run_date"] == TODAY.isoformat()


def test_dashboard_warehouse_filter_and_sku_360(client, db):
    assert client.get("/api/v1/dashboard/purchase", params={"warehouse_id": "NMB01"}).status_code == 200
    sku = db.scalar(select(DecisionLog.sku_id).where(DecisionLog.date == TODAY, DecisionLog.action == "STOCK"))
    r = client.get(f"/api/v1/skus/{sku}")
    assert r.status_code == 200 and r.json()["decision"]
    assert client.get("/api/v1/skus/search", params={"q": "Paracetamol"}).status_code == 200
    assert client.get("/api/v1/skus/NOPE").status_code == 404


def test_decisions_endpoint(client):
    r = client.get("/api/v1/decisions", params={"action": "STOCK", "limit": 5})
    body = r.json()
    assert r.status_code == 200 and body["total"] > 0 and all(i["action"] == "STOCK" for i in body["items"])


# ------------------------------------------------------- bounce -> source
def _sku_with_gated_stock(db):
    o = db.scalar(select(SupplierOffer).where(SupplierOffer.gate_status == "PASS", SupplierOffer.available_qty > 10))
    return o.sku_id


def _sku_without_gated_stock(db):
    passing = select(SupplierOffer.sku_id).where(SupplierOffer.gate_status == "PASS", SupplierOffer.available_qty > 0)
    return db.scalar(select(Sku.sku_id).where(Sku.sku_id.not_in(passing)))


def test_bounce_is_sourced_and_retailer_told_eta(client, db):
    sku = _sku_with_gated_stock(db)
    ev = {"event_id": "evt-src-1", "type": "bounce_candidate", "retailer_id": "RET0001", "sku_id": sku, "warehouse_id": "HYD01",
          "qty": 2, "order_id": "ORD-1", "line_id": "1"}
    r = client.post("/api/v1/events", json=ev).json()["results"][0]
    assert r["sourcing_status"] == "HELD" and r["retailer_message"] == "AVAILABLE_ON_REQUEST" and r["eta_hours"] > 0
    db.expire_all()
    assert db.get(BounceEvent, "BNC-evt-src-1").outcome == "recovered"
    # idempotent
    assert client.post("/api/v1/events", json=ev).json()["results"][0]["status"] == "duplicate"
    # procurement confirms
    req_id = r["sourcing_request_id"]
    assert client.post(f"/api/v1/sourcing/requests/{req_id}/confirm_purchase").json()["status"] == "PURCHASE_TASK"
    assert client.post(f"/api/v1/sourcing/requests/{req_id}/confirm_purchase").status_code == 409


def test_unsourceable_bounce_becomes_final_with_reason(client, db):
    sku = _sku_without_gated_stock(db)
    ev = {"event_id": "evt-src-2", "type": "bounce_candidate", "retailer_id": "RET0002", "sku_id": sku, "warehouse_id": "HYD01", "qty": 1}
    r = client.post("/api/v1/events", json=ev).json()["results"][0]
    assert r["sourcing_status"] in ("OPEN", "FAILED")
    from smart_inventory.config import get_policy
    from smart_inventory.engines import sourcing
    sourcing.expire_open_requests(db, get_policy(), TODAY, max_age_hours=0)
    db.commit()
    req = db.get(SourcingRequest, r["sourcing_request_id"])
    assert req.status == "FAILED" and req.failure_reason in ("EXTERNAL_SOURCING_FAILED", "SUPPLY_SHORTAGE")
    b = db.get(BounceEvent, "BNC-evt-src-2")
    assert b.outcome == "final" and b.reason_code == req.failure_reason and b.value_lost > 0


def test_other_events_and_availability(client, db):
    r = client.post("/api/v1/events", json=[
        {"event_id": "evt-s-1", "type": "search", "retailer_id": "RET0003", "sku_id": "SKU00001", "warehouse_id": "HYD01"},
        {"event_id": "evt-o-1", "type": "order_line", "retailer_id": "RET0003", "sku_id": "SKU00001", "warehouse_id": "HYD01",
         "qty": 3, "order_id": "ORD-9", "line_id": "1"},
    ]).json()["results"]
    assert [x["status"] for x in r] == ["accepted", "accepted"]
    a = client.get("/api/v1/availability", params={"sku_id": _sku_with_gated_stock(db), "warehouse_id": "HYD01", "qty": 100000}).json()
    assert a["state"] in ("AVAILABLE_ON_REQUEST", "IN_STOCK", "NOT_AVAILABLE")
    assert "same_composition_alternatives" in a


# ------------------------------------------------------------- PO approval
def test_po_approve_edit_reject_and_rerun_preserves(client, db, erp):
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    pos = db.scalars(select(SuggestedPO).where(SuggestedPO.date == TODAY, SuggestedPO.status == "DRAFT",
                                               SuggestedPO.supplier_id.is_not(None))).all()
    a, b = pos[0], pos[1]
    r = client.post(f"/api/v1/po-drafts/{a.po_draft_id}/approve", json={"qty": a.qty + 5}).json()
    assert r["status"] == "PUSHED" and r["erp_ref"].startswith("MOCK-PO") and "qty edited" in r["note"]
    assert any(p["ref"] == r["erp_ref"] and p["qty"] == a.qty + 5 for p in erp.pushed)
    assert client.post(f"/api/v1/po-drafts/{b.po_draft_id}/reject", json={"reason": "supplier on credit hold"}).json()["status"] == "REJECTED"
    assert client.post(f"/api/v1/po-drafts/{a.po_draft_id}/approve", json={}).status_code == 409
    run_daily_cycle(db, TODAY, erp=erp, sync=False)
    db.expire_all()
    assert db.get(SuggestedPO, a.po_draft_id).status == "PUSHED"
    assert db.get(SuggestedPO, b.po_draft_id).status == "REJECTED"


def test_override_rejects_po_and_survives_rerun(client, db, erp):
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    d = db.scalar(select(DecisionLog).where(DecisionLog.date == TODAY, DecisionLog.action == "STOCK",
                                            DecisionLog.overridden_by.is_(None)).offset(3))
    r = client.post(f"/api/v1/decisions/{d.id}/override", json={"action": "DONT_STOCK", "reason": "discontinued by manufacturer"})
    assert r.json()["status"] == "OVERRIDDEN"
    db.expire_all()
    po = db.scalar(select(SuggestedPO).where(SuggestedPO.date == TODAY, SuggestedPO.sku_id == d.sku_id, SuggestedPO.warehouse_id == d.warehouse_id))
    assert po is None or po.status in ("REJECTED", "PUSHED", "APPROVED")
    run_daily_cycle(db, TODAY, erp=erp, sync=False)
    db.expire_all()
    assert db.get(DecisionLog, d.id).override_action == "DONT_STOCK"


# --------------------------------------------- special terms and GRB block
def test_special_lot_checkout_and_grb_block(client, db):
    offer = db.scalar(select(PriceOffer).where(PriceOffer.date == TODAY, PriceOffer.kind == "SPECIAL_LOT", PriceOffer.status == "PROPOSED"))
    r = client.post(f"/api/v1/price-offers/{offer.offer_id}/approve").json()
    assert r["status"] == "PUBLISHED" and r["erp_ref"].startswith("MOCK-RULE")
    opts = client.get("/api/v1/offers", params={"retailer_id": "RET0010", "sku_id": offer.sku_id, "warehouse_id": offer.warehouse_id}).json()
    kinds = {o["option"]: o for o in opts["options"]}
    assert set(kinds) == {"NORMAL", "SPECIAL"}
    assert kinds["SPECIAL"]["discount_pct"] > kinds["NORMAL"]["discount_pct"] and kinds["SPECIAL"]["returnable"] is False
    sku = db.get(Sku, offer.sku_id)
    assert kinds["SPECIAL"]["net_price"] <= sku.mrp
    bad = {"event_id": "evt-sp-0", "type": "order_line", "retailer_id": "RET0010", "sku_id": offer.sku_id, "warehouse_id": offer.warehouse_id,
           "qty": 1, "order_id": "ORD-SP", "line_id": "1", "term_flag": "SPECIAL_NON_RETURNABLE"}
    assert client.post("/api/v1/events", json=bad).status_code == 422
    ok = {**bad, "event_id": "evt-sp-1", "offer_id": offer.offer_id, "discount_pct": kinds["SPECIAL"]["discount_pct"]}
    assert client.post("/api/v1/events", json=ok).json()["results"][0]["status"] == "accepted"
    assert client.get("/api/v1/returns/check", params={"order_id": "ORD-SP", "line_id": "1"}).json() == {
        "allowed": False, "reason": "SPECIAL_NON_RETURNABLE_TERMS", "offer_id": offer.offer_id}
    assert client.get("/api/v1/returns/check", params={"order_id": "ORD-9", "line_id": "1"}).json()["allowed"] is True


def test_special_exposure_cap(client, db, monkeypatch):
    from smart_inventory.config import Policy, get_policy, set_policy_override

    data = json.loads(json.dumps(get_policy().data))
    data["pricing"]["max_retailer_special_exposure_inr"] = 1
    set_policy_override(Policy(data))
    try:
        offer = db.scalar(select(PriceOffer).where(PriceOffer.status == "PUBLISHED"))
        opts = client.get("/api/v1/offers", params={"retailer_id": "RET0010", "sku_id": offer.sku_id, "warehouse_id": offer.warehouse_id}).json()
        assert [o["option"] for o in opts["options"]] == ["NORMAL"] and opts["special_blocked_reason"] == "RETAILER_SPECIAL_EXPOSURE_CAP"
    finally:
        set_policy_override(None)


# ------------------------------------------------------------- compliance
def test_compliance_hold_is_p0(client, db, erp):
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    sku = db.scalar(select(DecisionLog.sku_id).where(DecisionLog.date == TODAY, DecisionLog.action == "SELL"))
    hold = client.post("/api/v1/compliance/holds", json={"sku_id": sku, "reason": "CDSCO NSQ alert", "source": "CDSCO"}).json()
    run_daily_cycle(db, TODAY, erp=erp, sync=False)
    db.expire_all()
    acts = db.scalars(select(DecisionLog.action).where(DecisionLog.date == TODAY, DecisionLog.sku_id == sku)).all()
    assert acts and set(acts) == {"COMPLIANCE_HOLD"}
    assert client.get("/api/v1/availability", params={"sku_id": sku, "warehouse_id": "HYD01"}).json()["state"] == "COMPLIANCE_HOLD"
    assert db.scalar(select(SuggestedPO).where(SuggestedPO.date == TODAY, SuggestedPO.sku_id == sku, SuggestedPO.status == "DRAFT")) is None
    client.delete(f"/api/v1/compliance/holds/{hold['id']}")


def test_manual_offer_is_gated(client, db):
    sup = db.scalar(select(Supplier).where(Supplier.approved.is_(False)))
    r = client.post("/api/v1/supplier-offers", json={"supplier_id": sup.supplier_id, "sku_id": "SKU00002", "price": 10.0,
                                                     "batch": "X1", "expiry": "2028-01-31", "available_qty": 50}).json()
    assert r["gate_status"] == "FAIL" and "SUPPLIER_NOT_APPROVED" in r["gate_failures"]


# ------------------------------------------------------------------ agents
@pytest.fixture()
def fake_llm(monkeypatch):
    holder = {}

    def install(responses):
        fake = FakeClaude(responses)
        llm.set_client(fake)
        holder["fake"] = fake
        return fake

    yield install
    llm.set_client(None)


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_enabled", False)
    llm.set_client(None)
    yield


def test_insights_agent_tool_loop(client, fake_llm):
    fake = fake_llm([
        message([block_tool("tu_1", "get_kpis", {"warehouse_id": None})], stop_reason="tool_use"),
        message([block_text("Fill rate is healthy; approve the HIGH priority POs.")]),
    ])
    r = client.post("/api/v1/agents/ask", json={"question": "How are we doing?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"].startswith("Fill rate") and body["tool_calls"][0]["tool"] == "get_kpis"
    second = fake.requests[1]
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu_1" and not tool_result["is_error"]
    assert second["model"] == "claude-opus-5-5" and second["extra_body"] == {"fallbacks": "default"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in second["tools"])


def test_agents_degrade_without_claude(client, db, no_llm):
    d = db.scalar(select(DecisionLog).where(DecisionLog.date == TODAY))
    r = client.get(f"/api/v1/agents/explain/{d.id}", params={"lang": "te"}).json()
    assert r["source"] == "template" and r["explanation"]
    assert client.post("/api/v1/agents/ask", json={"question": "hi"}).status_code == 503
    ops = client.post("/api/v1/agents/ops/run", json={"run_cycle": False, "notify": True}).json()
    assert ops["source"] == "runbook" and "Acintyo daily brief" in ops["briefing"] and ops["notification"] == "LOGGED"
    assert client.post("/api/v1/supplier-offers/parse", json={"text": "Dolo 650 avl"}).status_code == 503


def test_supplier_message_parser(client, db, fake_llm):
    sku = db.get(Sku, _sku_without_gated_stock(db))
    sup = db.scalar(select(Supplier).where(Supplier.approved.is_(True), Supplier.gst_compliant.is_(True),
                                           Supplier.licence_valid_until > TODAY))
    payload = {"supplier_name": sup.name, "offers": [{
        "product_name": sku.name, "composition": sku.composition, "price": round(sku.cost * 0.98, 2), "mrp": sku.mrp,
        "scheme": "10+1", "available_qty": 100, "batch": "WA123", "expiry": "2028-06-30"}, {
        "product_name": "Totally Unknown Syrup 100ml", "composition": None, "price": 50, "mrp": None, "scheme": None,
        "available_qty": None, "batch": None, "expiry": None}]}
    fake_llm([message([block_text(json.dumps(payload))])])
    r = client.post("/api/v1/supplier-offers/parse", json={"text": f"{sku.name} avl 100 @ {sku.cost} 10+1 B.WA123 exp 06/28",
                                                           "supplier_id": sup.supplier_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["offers_stored"], body
    assert body["offers_stored"][0]["sku_id"] == sku.sku_id and body["offers_stored"][0]["gate_status"] == "PASS"
    assert body["unmatched"][0]["reason"] == "SKU_NOT_MATCHED"


# -------------------------------------------------------------------- auth
def test_role_based_auth(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "portal-key:portal,boss-key:management,admin-key:admin")
    assert client.get("/api/v1/dashboard/kpis").status_code == 401
    assert client.get("/api/v1/dashboard/kpis", headers={"X-API-Key": "portal-key"}).status_code == 403
    assert client.get("/api/v1/dashboard/kpis", headers={"X-API-Key": "boss-key"}).status_code == 200
    assert client.post("/api/v1/admin/run-cycle", json={}, headers={"X-API-Key": "boss-key"}).status_code == 403
    assert client.get("/api/v1/returns/check", params={"order_id": "x"}, headers={"X-API-Key": "portal-key"}).status_code == 200
    assert client.get("/api/v1/meta").status_code == 200


def test_price_test_arms_and_readout(client, db):
    from smart_inventory.engines.pricing import experiment_arm

    t = db.scalar(select(PriceOffer).where(PriceOffer.date == TODAY, PriceOffer.kind == "PRICE_TEST", PriceOffer.status == "PROPOSED"))
    assert client.post(f"/api/v1/price-offers/{t.offer_id}/approve").json()["status"] == "APPROVED"
    arms = {}
    for i in range(1, 40):
        rid = f"RET{i:04d}"
        opt = client.get("/api/v1/offers", params={"retailer_id": rid, "sku_id": t.sku_id, "warehouse_id": t.warehouse_id}).json()["options"][0]
        assert opt["experiment"]["arm"] == experiment_arm(rid, t.offer_id)  # sticky + deterministic
        arms.setdefault(opt["experiment"]["arm"], opt["discount_pct"])
        client.post("/api/v1/events", json={"event_id": f"exp-{i}", "type": "order_line", "retailer_id": rid, "sku_id": t.sku_id,
                                             "warehouse_id": t.warehouse_id, "qty": 2, "order_id": f"EXP{i}", "line_id": "1",
                                             "discount_pct": opt["discount_pct"], "offer_id": t.offer_id})
    assert set(arms) == {"TEST", "CONTROL"} and arms["TEST"] == t.discount_pct
    r = client.get(f"/api/v1/experiments/{t.offer_id}").json()
    assert set(r["arms"]) == {"TEST", "CONTROL"} and r["arms"]["TEST"]["lines"] + r["arms"]["CONTROL"]["lines"] == 39


def test_regulatory_alert_holds_batch_in_stock(client, db, fake_llm):
    from smart_inventory.models import ComplianceHold, InventoryBatch

    b = db.scalar(select(InventoryBatch).where(InventoryBatch.on_hold.is_(False), InventoryBatch.qty > 0).offset(7))
    sku = db.get(Sku, b.sku_id)
    other = db.scalar(select(Sku).where(Sku.sku_id != sku.sku_id, Sku.composition != sku.composition))
    payload = {"issuer": "CDSCO", "alerts": [
        {"product_name": sku.name, "composition": sku.composition, "manufacturer": sku.manufacturer, "batch": b.batch.lower(), "reason": "NSQ - assay"},
        {"product_name": other.name, "composition": other.composition, "manufacturer": other.manufacturer, "batch": "ZZ9999", "reason": "NSQ"},
    ]}
    fake_llm([message([block_text(json.dumps(payload))])])
    r = client.post("/api/v1/compliance/alerts/parse", json={"text": "CDSCO NSQ list ...", "source": "CDSCO"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["holds_applied"][0]["batch"] == b.batch and body["proposed_holds"][0]["sku_id"] == other.sku_id
    db.expire_all()
    assert db.get(InventoryBatch, b.id).on_hold is True
    assert db.scalar(select(ComplianceHold).where(ComplianceHold.batch == b.batch, ComplianceHold.active.is_(True))) is not None
