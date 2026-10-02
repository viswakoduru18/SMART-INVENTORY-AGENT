from __future__ import annotations

from datetime import date

from ..config import get_settings, load_yaml
from .base import ERPConnector, ERPError

_connector: ERPConnector | None = None


def build_connector(kind: str | None = None, today: date | None = None) -> ERPConnector:
    cfg = load_yaml(get_settings().erp_config_path)
    kind = kind or get_settings().erp_connector or cfg.get("connector", "mock")
    if kind == "rest":
        from .rest_erp import RestERPConnector

        return RestERPConnector(cfg.get("rest", {}))
    if kind == "csv":
        from .csv_erp import CsvERPConnector

        return CsvERPConnector(cfg.get("csv", {}))
    if kind == "mock":
        from .mock_erp import MockERPConnector

        return MockERPConnector(cfg.get("mock", {}), today=today, outbox=cfg.get("csv", {}).get("outbox"))
    raise ERPError(f"unknown ERP connector '{kind}'")


def get_connector() -> ERPConnector:
    global _connector
    if _connector is None:
        _connector = build_connector()
    return _connector


def set_connector(connector: ERPConnector | None) -> None:
    global _connector
    _connector = connector


__all__ = ["ERPConnector", "ERPError", "build_connector", "get_connector", "set_connector"]
