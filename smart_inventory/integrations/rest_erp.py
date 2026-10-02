"""Generic REST connector driven entirely by config/erp.yaml.

To connect Acintyo's ERP: set `connector: rest`, ERP_BASE_URL and the auth env
vars, then map endpoint paths and field names in erp.yaml. No code changes.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx

from .base import ERPConnector, ERPError

log = logging.getLogger(__name__)


def get_path(obj: Any, dotted: str | None) -> Any:
    if not dotted:
        return obj
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            idx = int(part)
            obj = obj[idx] if idx < len(obj) else None
        else:
            return None
    return obj


def map_inbound(record: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    """ERP record -> canonical record using {canonical: erp_path}."""
    return {canonical: get_path(record, erp_path) for canonical, erp_path in fields.items()}


def map_outbound(record: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    """Canonical record -> ERP payload using {canonical: erp_field}."""
    out: dict[str, Any] = {}
    for canonical, erp_field in fields.items():
        if canonical in record:
            value = record[canonical]
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            out[erp_field] = value
    return out


class RestERPConnector(ERPConnector):
    name = "rest"

    def __init__(self, cfg: dict[str, Any], client: httpx.Client | None = None):
        self.cfg = cfg
        base_url = cfg.get("base_url") or ""
        if not base_url:
            raise ERPError("ERP_BASE_URL is not set; configure rest.base_url in config/erp.yaml")
        self.resources: dict[str, dict[str, Any]] = cfg.get("resources", {})
        self.pagination = cfg.get("pagination", {"style": "none"})
        self.client = client or httpx.Client(
            base_url=base_url,
            timeout=float(cfg.get("timeout_seconds", 30)),
            headers=self._auth_headers(),
            auth=self._basic_auth(),
        )

    # ------------------------------------------------------------------ auth
    def _auth_headers(self) -> dict[str, str]:
        auth = self.cfg.get("auth", {})
        kind = auth.get("type", "none")
        headers = {"Accept": "application/json"}
        if kind == "bearer":
            token = os.environ.get(auth.get("token_env", "ERP_API_TOKEN"), "")
            headers["Authorization"] = f"Bearer {token}"
        elif kind == "api_key":
            headers[auth.get("api_key_header", "X-API-Key")] = os.environ.get(
                auth.get("api_key_env", "ERP_API_KEY"), ""
            )
        return headers

    def _basic_auth(self) -> tuple[str, str] | None:
        auth = self.cfg.get("auth", {})
        if auth.get("type") == "basic":
            return (
                os.environ.get(auth.get("username_env", "ERP_USERNAME"), ""),
                os.environ.get(auth.get("password_env", "ERP_PASSWORD"), ""),
            )
        return None

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.client.request(method, path, **kwargs)
                if resp.status_code in (429, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 502, 503, 504)
                )
                last_exc = exc
                if not retryable or attempt == 3:
                    break
                time.sleep(2**attempt)
        raise ERPError(f"ERP {method} {path} failed: {last_exc}")

    def _resource(self, resource: str) -> dict[str, Any]:
        spec = self.resources.get(resource)
        if not spec:
            raise ERPError(f"resource '{resource}' is not configured in erp.yaml")
        return spec

    # ------------------------------------------------------------------ read
    def fetch(self, resource: str, since: datetime | None = None) -> list[dict[str, Any]]:
        spec = self._resource(resource)
        params: dict[str, Any] = dict(spec.get("params", {}))
        if since and spec.get("incremental"):
            params[self.cfg.get("since_param", "updated_since")] = since.isoformat()
        style = self.pagination.get("style", "none")
        size = int(self.pagination.get("page_size", 500))
        out: list[dict[str, Any]] = []
        page, offset, cursor = 1, 0, None
        for _ in range(10_000):  # hard stop against runaway pagination
            p = dict(params)
            if style == "page":
                p[self.pagination.get("page_param", "page")] = page
                p[self.pagination.get("size_param", "page_size")] = size
            elif style == "offset":
                p["offset"] = offset
                p["limit"] = size
            elif style == "cursor" and cursor:
                p[self.pagination.get("cursor_param", "cursor")] = cursor
            body = self._request(spec.get("method", "GET"), spec["path"], params=p)
            records = get_path(body, spec.get("records_path")) or []
            if isinstance(records, dict):
                records = [records]
            out.extend(map_inbound(r, spec["fields"]) for r in records)
            if style == "none" or not records:
                break
            if style == "page":
                if len(records) < size:
                    break
                page += 1
            elif style == "offset":
                if len(records) < size:
                    break
                offset += size
            elif style == "cursor":
                cursor = get_path(body, self.pagination.get("next_cursor_path", "next_cursor"))
                if not cursor:
                    break
        return out

    def live_stock(self, sku_id: str, warehouse_id: str) -> float | None:
        spec = self.resources.get("live_stock")
        if not spec:
            return None
        params = {
            k: str(v).format(sku_id=sku_id, warehouse_id=warehouse_id) for k, v in spec.get("params", {}).items()
        }
        body = self._request("GET", spec["path"], params=params)
        rec = get_path(body, spec.get("records_path"))
        if isinstance(rec, list):
            rec = rec[0] if rec else {}
        mapped = map_inbound(rec or {}, spec["fields"])
        qty = mapped.get("qty")
        return float(qty) if qty is not None else None

    # ----------------------------------------------------------------- write
    def _write(self, resource: str, record: dict[str, Any]) -> str | None:
        spec = self._resource(resource)
        payload = map_outbound(record, spec["fields"])
        body = self._request(spec.get("method", "POST"), spec["path"], json=payload)
        ref = get_path(body, spec.get("response_ref_path"))
        return str(ref) if ref is not None else None

    def push_po_draft(self, po: dict[str, Any]) -> str | None:
        return self._write("po_draft", {**po, "status": "DRAFT"})

    def push_price_rule(self, rule: dict[str, Any]) -> str | None:
        return self._write("price_rule", rule)

    def health(self) -> dict[str, Any]:
        try:
            self.client.get("/", timeout=5)
            return {"connector": self.name, "ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"connector": self.name, "ok": False, "error": str(exc)}
