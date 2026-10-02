"""Operations agent: supervises the daily process end to end and briefs procurement and management.

Each morning (or on demand) it runs the deterministic cycle, reads data-quality
and job results, sweeps the sourcing queue, checks what is waiting for human
approval, and writes a short briefing that is sent to procurement on WhatsApp.

It can run jobs and sweep sourcing; it cannot approve POs, change prices or
quantities. Without Claude it executes the same runbook deterministically and
sends a templated briefing, so the process never depends on the LLM.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_policy, get_settings
from ..engines import sourcing
from ..integrations import notifications
from ..integrations.base import ERPConnector
from ..pipeline.daily_cycle import run_daily_cycle
from . import llm
from .tools import ops_tools, read_tools

SYSTEM = """You are the operations supervisor for Acintyo's predictive distribution platform (B2B pharmaceutical distribution). \
A deterministic engine makes all stock, purchase and price decisions; your job is to make sure the daily process ran correctly, \
fix what you safely can with your tools, and brief people.

Runbook:
1. If the task says to run the cycle, call run_daily_cycle once (sync=true). If it halts on data quality, do not retry; report \
   the failing checks and who must fix them (ERP team for missing batch/expiry or stale feeds).
2. Sweep sourcing: retry_open_sourcing, then expire_stale_sourcing with max_age_hours=2.
3. Read pending_approvals, get_kpis and purchase_intelligence / bounce_intelligence as needed.
4. Write the briefing for the procurement head and management.

Briefing format (plain text for WhatsApp, max 180 words): status line (OK / ATTENTION / BLOCKED); PO drafts waiting (count, value, \
high priority); top 3 bounce problems with the recommended action; liquidation / special-lot value proposed; any data-quality or job \
failure and the owner. Use Rs and lakh. Never claim an action was taken unless a tool result shows it. You cannot approve POs or \
change prices; say what needs human approval."""


def deterministic_briefing(db: Session, day: date) -> str:
    k = analytics.kpis(db, day)
    p = analytics.purchase_view(db, day, limit=5)
    b = analytics.bounce_view(db, day, limit=3)
    m = analytics.margin_view(db, day, limit=3)
    dq_fail = k["platform"]["data_quality_failures"]
    status = "BLOCKED" if any(f["severity"] == "ERROR" for f in dq_fail) else ("ATTENTION" if dq_fail else "OK")
    lines = [
        f"Acintyo daily brief {day.isoformat()} - {status}",
        f"PO drafts: {p['po_lines']} lines, Rs{(p['po_value'] or 0) / 1e5:.2f} lakh ({p['po_by_status']}).",
        f"Forecast: tomorrow {p['tomorrow_demand_units']:.0f} units (Rs{(p['tomorrow_demand_value'] or 0) / 1e5:.2f} lakh), 2-day {p['two_day_demand_units']:.0f}.",
        f"Fill rate 30d: {k['availability']['fill_rate_line_30d']}, bounce rate {k['availability']['bounce_rate_30d']}, "
        f"revenue lost 30d Rs{(k['availability']['revenue_lost_30d'] or 0) / 1e5:.2f} lakh.",
    ]
    for t in b["top_bounced"][:3]:
        lines.append(f"Bounce: {t['name']} ({t['warehouse_id']}) {t['bounces_90']}x/90d, {t['retailers']} retailers, {t['pattern']}/{t['external_availability']}.")
    lines.append(f"Liquidation proposed Rs{(m['liquidation_opportunity_value'] or 0) / 1e5:.2f} lakh; slow-stock offers Rs{(m['slow_moving_eligible_value'] or 0) / 1e5:.2f} lakh; "
                 f"supplier returns Rs{(m['return_to_supplier_value'] or 0) / 1e5:.2f} lakh.")
    for f in dq_fail:
        lines.append(f"DQ {f['severity']}: {f['check']}")
    return "\n".join(lines)


def run(db: Session, day: date, erp: ERPConnector, run_cycle: bool = True, notify: bool = True, task: str | None = None) -> dict[str, Any]:
    task = task or ("Run today's cycle, sweep sourcing, and write the morning briefing." if run_cycle
                    else "Do not run the cycle. Sweep sourcing and write the briefing from current results.")
    tools = ops_tools(db, day, erp) + read_tools(db, day)
    if not run_cycle:
        tools = [t for t in tools if t.name != "run_daily_cycle"]
    source = "claude"
    try:
        result = llm.run_tool_loop(SYSTEM, f"Run date: {day.isoformat()}. Autonomy mode: {get_policy().get('autonomy.mode')}.\n\nTask: {task}",
                                   tools, effort="medium", max_turns=16)
        briefing = result.text
        llm.record(db, "operations", task, result)
    except llm.LLMUnavailable as exc:
        source = "runbook"
        cycle = run_daily_cycle(db, day, erp=erp) if run_cycle else None
        expired = sourcing.expire_open_requests(db, get_policy(), day)
        briefing = deterministic_briefing(db, day)
        result = llm.AgentResult(text=briefing, tool_calls=[{"tool": "runbook", "input": {"cycle": bool(cycle), "expired_sourcing": expired}}])
        llm.record(db, "operations", task, result, status="RUNBOOK", response=f"{briefing}\n\n(LLM unavailable: {exc})")
    sent = None
    if notify:
        n = notifications.send(db, "whatsapp", get_settings().procurement_whatsapp, "procurement_briefing", ref=f"brief-{day}", body=briefing)
        sent = n.status
    db.commit()
    return {"run_date": day.isoformat(), "source": source, "briefing": briefing, "tool_calls": result.tool_calls, "notification": sent}
