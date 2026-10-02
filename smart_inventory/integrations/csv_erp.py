"""Scheduled-extract connector: the ERP drops CSVs nightly; drafts go to an outbox.

Acceptable at current volume (architecture doc section 7) when CDC/API is not
available. CSV headers must use canonical field names.
"""
from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import ERPConnector


class CsvERPConnector(ERPConnector):
    name = "csv"

    def __init__(self, cfg: dict[str, Any]):
        self.directory = Path(cfg.get("directory", "./data/erp_drop"))
        self.outbox = Path(cfg.get("outbox", "./data/erp_outbox"))
        self.outbox.mkdir(parents=True, exist_ok=True)

    def fetch(self, resource: str, since: datetime | None = None) -> list[dict[str, Any]]:
        path = self.directory / f"{resource}.csv"
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(fh)]
        if since and resource in ("order_lines", "bounces"):
            rows = [r for r in rows if r.get("ts") and datetime.fromisoformat(str(r["ts"])) >= since]
        return rows

    def _append(self, name: str, record: dict[str, Any]) -> str:
        ref = f"{name.upper()}-{uuid.uuid4().hex[:10]}"
        with open(self.outbox / f"{name}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ref": ref, **record}, default=str) + "\n")
        return ref

    def push_po_draft(self, po: dict[str, Any]) -> str | None:
        return self._append("po_draft", {**po, "status": "DRAFT"})

    def push_price_rule(self, rule: dict[str, Any]) -> str | None:
        return self._append("price_rule", rule)
