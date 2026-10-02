"""Command line: python -m smart_inventory.cli <command>

    init-db                         create tables
    sync [--full]                   pull reference + fact data from the ERP
    run-cycle [--date] [--publish]  run the daily decision cycle
    backfill --days N               run the cycle for the last N days (builds class/forecast history)
    ops-agent [--run-cycle]         operations agent: (cycle +) sweep + briefing
    scheduler                       run the IST batch scheduler in the foreground
    serve [--port]                  start the API + console
    check-erp                       verify the ERP connector can read every resource
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, timedelta

from .config import get_settings
from .db import create_all, session_scope
from .integrations import get_connector
from .integrations.base import READ_RESOURCES


def _date(s: str | None) -> date:
    if s:
        return date.fromisoformat(s)
    from .api.deps import plan_date
    return plan_date()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="smart_inventory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    s = sub.add_parser("sync")
    s.add_argument("--full", action="store_true")
    s.add_argument("--date")
    r = sub.add_parser("run-cycle")
    r.add_argument("--date")
    r.add_argument("--publish", action="store_true")
    r.add_argument("--no-sync", action="store_true")
    r.add_argument("--full-sync", action="store_true")
    r.add_argument("--ignore-dq", action="store_true", help="run even if blocking data-quality checks fail")
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=14)
    b.add_argument("--end")
    o = sub.add_parser("ops-agent")
    o.add_argument("--date")
    o.add_argument("--run-cycle", action="store_true")
    o.add_argument("--notify", action="store_true")
    sub.add_parser("scheduler")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("check-erp")
    a = p.parse_args(argv)

    create_all()
    if a.cmd == "init-db":
        print("tables created at", get_settings().database_url.split("@")[-1])
    elif a.cmd == "sync":
        from .pipeline.ingest import sync_all
        with session_scope() as db:
            print(json.dumps(sync_all(db, get_connector(), _date(a.date), full=a.full), indent=1))
    elif a.cmd == "run-cycle":
        from .pipeline.daily_cycle import run_daily_cycle
        with session_scope() as db:
            out = run_daily_cycle(db, _date(a.date), sync=not a.no_sync, full_sync=a.full_sync, publish=a.publish, enforce_dq=not a.ignore_dq)
        print(json.dumps(out, indent=1, default=str))
    elif a.cmd == "backfill":
        from .pipeline.daily_cycle import run_daily_cycle
        end = _date(a.end)
        with session_scope() as db:
            run_daily_cycle(db, end - timedelta(days=a.days), full_sync=True, enforce_dq=False)
        for i in range(a.days - 1, -1, -1):
            with session_scope() as db:
                out = run_daily_cycle(db, end - timedelta(days=i), sync=False, enforce_dq=False)
            print(out["run_date"], out.get("orchestrator", {}).get("by_action"))
    elif a.cmd == "ops-agent":
        from .agents import operations
        with session_scope() as db:
            out = operations.run(db, _date(a.date), get_connector(), run_cycle=a.run_cycle, notify=a.notify)
        print(out["briefing"])
    elif a.cmd == "scheduler":
        from .pipeline.scheduler import build_scheduler
        sched = build_scheduler()
        sched.start()
        print("scheduler running:", [f"{j.id} next={j.next_run_time}" for j in sched.get_jobs()])
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sched.shutdown()
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("smart_inventory.main:app", host=a.host, port=a.port)
    elif a.cmd == "check-erp":
        erp = get_connector()
        print("connector:", erp.name)
        for res in READ_RESOURCES:
            try:
                rows = erp.fetch(res)
                sample = {k: v for k, v in (rows[0] if rows else {}).items()}
                missing = [k for k, v in sample.items() if v is None]
                print(f"  {res:16} OK  {len(rows):>7} rows  null fields in first row: {missing or '-'}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {res:16} FAIL {exc}")


if __name__ == "__main__":
    main()
