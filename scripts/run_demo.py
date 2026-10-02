"""One-command demo on synthetic data (no ERP, no API key needed).

    PYTHONPATH=. python scripts/run_demo.py            # builds data/demo.db with 7 days of decision history
    DATABASE_URL=sqlite:///data/demo.db PYTHONPATH=. uvicorn smart_inventory.main:app --port 8000

Then open http://localhost:8000
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    ap.add_argument("--days", type=int, default=7, help="days of decision history to build (class hysteresis, accuracy)")
    ap.add_argument("--skus", type=int, default=400)
    a = ap.parse_args()

    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(a.db + suffix):
            os.remove(a.db + suffix)
    os.environ["DATABASE_URL"] = f"sqlite:///{a.db}"

    from smart_inventory import db as dbm
    from smart_inventory.agents import operations
    from smart_inventory.config import get_policy
    from smart_inventory.engines import sourcing
    from smart_inventory.integrations import set_connector
    from smart_inventory.integrations.mock_erp import MockERPConnector
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    dbm.init_engine(os.environ["DATABASE_URL"])
    dbm.create_all()
    today = date.today()
    erp = MockERPConnector({"skus": a.skus, "retailers": 150, "history_days": 200}, today=today)
    set_connector(erp)

    print(f"Building {a.days} days of decision history on synthetic data ({a.skus} SKUs x 2 warehouses)...")
    for i in range(a.days - 1, -1, -1):
        day = today - timedelta(days=i)
        with dbm.session_scope() as s:
            # full sync on the first day; re-sync on the last day so the stock snapshot is fresh
            out = run_daily_cycle(s, day, erp=erp, sync=i in (0, a.days - 1), full_sync=(i == a.days - 1), enforce_dq=False)
        print(f"  {day}: {out['orchestrator']['by_action']}")

    print("Simulating live retailer bounces through the sourcing engine...")
    with dbm.session_scope() as s:
        from sqlalchemy import select

        from smart_inventory.models import BounceEvent, Sku
        skus = list(s.scalars(select(Sku).limit(40)))
        for i, sku in enumerate(skus[::4]):
            bid = f"DEMO-BNC-{i}"
            s.add(BounceEvent(bounce_id=bid, sku_id=sku.sku_id, retailer_id=f"RET{i + 1:04d}", warehouse_id="HYD01", qty=2,
                              reason_code="NOT_IN_STOCK", sourcing_attempted=True, outcome="pending",
                              ts=datetime.utcnow()))
            s.flush()
            req = sourcing.open_request(s, get_policy(), sku.sku_id, "HYD01", f"RET{i + 1:04d}", 2, today, bounce_id=bid)
            note = f"ETA {req.eta_hours:.0f}h, retailer told 'Available on request'" if req.status == "HELD" else (
                f"inquiry sent to WhatsApp suppliers ({req.failure_reason})" if req.status == "OPEN" else req.failure_reason)
            print(f"  {sku.name:35} -> {req.status:7} {note}")

    print("\nOperations agent briefing (Claude if ANTHROPIC_API_KEY is set, deterministic runbook otherwise):\n")
    with dbm.session_scope() as s:
        print(operations.run(s, today, erp, run_cycle=False, notify=False)["briefing"])

    print(f"\nDone. Start the console:\n  DATABASE_URL=sqlite:///{a.db} PYTHONPATH=. uvicorn smart_inventory.main:app --port 8000\n  open http://localhost:8000")


if __name__ == "__main__":
    main()
