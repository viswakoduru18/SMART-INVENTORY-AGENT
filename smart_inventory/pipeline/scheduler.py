"""IST batch window (architecture doc 4.3), driven by policy.yaml `schedule`.

    23:30  ERP extract cut-off + full decision cycle for the next business day
           (classification -> forecast/buffers -> bounce-to-stock/pricing -> orchestrator)
    05:30  alert if today's cycle has not succeeded
    06:30  publish approved/auto-approved drafts + operations agent briefing
    every 15 min  intraday ERP sync (orders, bounces, stock) + sourcing sweep

The engines run back-to-back in one job because each stage consumes the
previous one's output; the per-stage times in the architecture doc are the
latest acceptable completion times, tracked in job_run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from ..config import get_policy, get_settings
from ..db import session_scope
from ..integrations import get_connector, notifications
from ..models import JobRun

log = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_policy().get("schedule.timezone", get_settings().timezone))


def _plan_date():
    return (datetime.now(_tz()) + timedelta(hours=6)).date()


def nightly_cycle() -> None:
    from .daily_cycle import run_daily_cycle

    day = _plan_date()
    log.info("nightly cycle for %s", day)
    with session_scope() as db:
        run_daily_cycle(db, day, erp=get_connector())


def morning_publish() -> None:
    from ..agents import operations
    from .daily_cycle import publish_approved

    day = datetime.now(_tz()).date()
    with session_scope() as db:
        publish_approved(db, day, get_connector(), get_policy())
        operations.run(db, day, get_connector(), run_cycle=False, notify=True)


def failure_alert() -> None:
    day = datetime.now(_tz()).date()
    with session_scope() as db:
        ok = db.scalar(select(JobRun.id).where(JobRun.job == "e8_orchestrator", JobRun.run_date == day, JobRun.status == "SUCCESS"))
        if not ok:
            notifications.send(db, "whatsapp", get_settings().procurement_whatsapp, "procurement_briefing", ref=f"alert-{day}",
                               body=f"ALERT: the decision cycle for {day} has not completed. Suggested PO will be late. Check /api/v1/admin/jobs.")


def intraday_sync() -> None:
    from ..engines import sourcing
    from .ingest import sync_facts

    day = datetime.now(_tz()).date()
    with session_scope() as db:
        sync_facts(db, get_connector(), day)
        sourcing.expire_open_requests(db, get_policy(), day)


def _cron(hhmm: str, tz: ZoneInfo) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def build_scheduler() -> BackgroundScheduler:
    sched = get_policy().section("schedule")
    tz = _tz()
    s = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
    s.add_job(nightly_cycle, _cron(sched.get("erp_sync", "23:30"), tz), id="nightly_cycle")
    s.add_job(failure_alert, _cron(sched.get("po_failure_alert", "05:30"), tz), id="po_failure_alert")
    s.add_job(morning_publish, _cron(sched.get("publish_po", "06:30"), tz), id="publish_po")
    s.add_job(intraday_sync, IntervalTrigger(minutes=int(sched.get("intraday_sync_minutes", 15)), timezone=tz), id="intraday_sync")
    return s
