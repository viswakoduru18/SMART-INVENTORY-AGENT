"""Reference ERP API: what the platform expects Acintyo's ERP to expose.

Serves the mock dataset using the ERP-side field names from config/erp.yaml
(e.g. item_code, branch_code, batch_no), paginated, so the REST connector can
be verified end to end before the real ERP endpoints are ready. Hand this file
and config/erp.yaml to the ERP team as the integration contract.

    PYTHONPATH=. python scripts/reference_erp_server.py --port 9000
    ERP_CONNECTOR=rest ERP_BASE_URL=http://localhost:9000 python -m smart_inventory.cli check-erp
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Any

from fastapi import FastAPI, Query, Request

from smart_inventory.config import get_settings, load_yaml
from smart_inventory.integrations.mock_erp import MockERPConnector


def _to_erp(record: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for canonical, erp_path in fields.items():
        v = record.get(canonical)
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        node = out
        parts = erp_path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    return out


def build_app(mock: MockERPConnector | None = None, erp_cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = (erp_cfg or load_yaml(get_settings().erp_config_path))["rest"]
    mock = mock or MockERPConnector()
    app = FastAPI(title="Reference Acintyo ERP API")
    app.state.received = []
    res = cfg["resources"]
    psize = cfg.get("pagination", {}).get("size_param", "page_size")
    pparam = cfg.get("pagination", {}).get("page_param", "page")
    since_param = cfg.get("since_param", "updated_since")

    def make_reader(name: str):
        spec = res[name]

        def reader(request: Request):
            q = request.query_params
            page, size = int(q.get(pparam, 1)), int(q.get(psize, 500))
            since = q.get(since_param)
            rows = mock.fetch(name, since=datetime.fromisoformat(since) if since else None)
            chunk = rows[(page - 1) * size: page * size]
            return {"data": [_to_erp(r, spec["fields"]) for r in chunk], "page": page, "total": len(rows)}

        return reader

    for name in ("warehouses", "skus", "retailers", "suppliers", "order_lines", "bounces", "inventory", "open_pos", "supplier_prices"):
        app.get(res[name]["path"], name=name)(make_reader(name))

    def make_writer(name: str, prefix: str):
        def writer(payload: dict[str, Any]):
            app.state.received.append({"resource": name, **payload})
            ref = f"{prefix}{len(app.state.received):06d}"
            return {"data": {"po_no" if name == "po_draft" else "rule_id": ref}}

        return writer

    app.post(res["po_draft"]["path"])(make_writer("po_draft", "ERP-PO-"))
    app.post(res["price_rule"]["path"])(make_writer("price_rule", "ERP-RULE-"))

    @app.get(res["live_stock"]["path"])
    def live_stock(item_code: str = Query(...), branch_code: str = Query(...)):
        inv = [r for r in mock.fetch("inventory") if r["sku_id"] == item_code and r["warehouse_id"] == branch_code and not r["on_hold"]]
        return {"data": {"available_qty": sum(r["qty"] for r in inv)}}

    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    uvicorn.run(build_app(), host="0.0.0.0", port=ap.parse_args().port)
