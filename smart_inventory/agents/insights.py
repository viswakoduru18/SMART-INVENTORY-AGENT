"""Insights agent: natural-language questions over the dashboards (read-only tools)."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from .. import analytics
from . import llm
from .tools import read_tools

SYSTEM = """You are the inventory and distribution analyst for Acintyo, a B2B pharmaceutical distributor serving independent \
pharmacies from warehouses in Hyderabad (HYD01) and Navi Mumbai (NMB01). Management and procurement ask you questions about \
inventory, bounces (orders we could not fulfil), purchasing, discounts and margin.

Answer only from tool results; call tools to get numbers rather than guessing. Amounts are in Indian rupees (write Rs, use lakh/crore \
when large). Lead with the answer, then the 2-4 numbers that support it, then one recommended action if the data supports one. \
Recommendations must stay within what the decision engine already proposes (PO drafts, offers, sourcing); you cannot change \
quantities, prices or approvals yourself. If the user writes in Telugu, answer in Telugu. Keep answers under 250 words unless a list is requested."""


def ask(db: Session, question: str, day: date | None = None) -> dict[str, Any]:
    day = analytics.resolve_date(db, day)
    tools = read_tools(db, day)
    try:
        result = llm.run_tool_loop(SYSTEM, f"Run date: {day.isoformat()}\n\n{question}", tools, effort="medium")
    except llm.LLMUnavailable as exc:
        llm.record(db, "insights", question, None, status="UNAVAILABLE", response=str(exc))
        raise
    run = llm.record(db, "insights", question, result)
    return {"answer": result.text, "tool_calls": result.tool_calls, "run_id": run.id,
            "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens}}
