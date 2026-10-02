"""Claude client wrapper shared by all agents.

The LLM sits at the edges, never in the decision loop: it parses unstructured
supplier messages, explains decisions, answers questions and supervises runs.
Stock quantities, prices, discounts and PO values stay deterministic.

The platform runs fully without an API key; agent features then degrade to
deterministic fallbacks (template explanations, runbook-only operations).
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AgentRun

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    pass


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    settings = get_settings()
    if not settings.llm_enabled:
        raise LLMUnavailable("LLM features are disabled (LLM_ENABLED=false)")
    if _client is None:
        # A server process must be configured explicitly: never fall through to ambient credentials.
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not settings.anthropic_api_key and not token:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        try:
            kwargs = {"api_key": settings.anthropic_api_key} if settings.anthropic_api_key else {"auth_token": token}
            _client = anthropic.Anthropic(**kwargs, timeout=settings.llm_timeout_seconds, max_retries=2)
        except anthropic.AnthropicError as exc:
            raise LLMUnavailable(f"Claude client could not be initialised: {exc}") from exc
    return _client


def set_client(client: Any) -> None:
    """Inject a client (tests use a fake)."""
    global _client
    _client = client


def available() -> bool:
    try:
        get_client()
        return True
    except LLMUnavailable:
        return False


def create(*, effort: str | None = None, max_tokens: int = 16000, **kwargs: Any):
    """messages.create with the platform defaults: model, effort, refusal fallbacks."""
    settings = get_settings()
    client = get_client()
    output_config = dict(kwargs.pop("output_config", {}) or {})
    output_config.setdefault("effort", effort or settings.llm_effort)
    extra: dict[str, Any] = {}
    if settings.llm_fallbacks:
        extra = {"extra_headers": {"anthropic-beta": FALLBACK_BETA}, "extra_body": {"fallbacks": "default"}}
    try:
        return client.messages.create(model=settings.llm_model, max_tokens=max_tokens, output_config=output_config, **kwargs, **extra)
    except anthropic.AuthenticationError as exc:
        raise LLMUnavailable("Claude API key rejected") from exc
    except anthropic.PermissionDeniedError as exc:
        raise LLMUnavailable(f"Claude API permission denied: {exc.message}") from exc
    except anthropic.NotFoundError as exc:
        raise LLMUnavailable(f"Model or endpoint not found: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise LLMUnavailable("Claude API rate limited; retry shortly") from exc
    except anthropic.APIStatusError as exc:
        raise LLMUnavailable(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailable("Cannot reach the Claude API") from exc


def text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False},
        }


@dataclass
class AgentResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


def run_tool_loop(system: str, user: str, tools: list[Tool], effort: str | None = None, max_turns: int = 12) -> AgentResult:
    """Manual agentic loop: keeps the full assistant content (incl. thinking blocks) unchanged in history."""
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    result = AgentResult(text="")
    for _ in range(max_turns):
        resp = create(effort=effort, system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      tools=specs, messages=messages)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            result.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            result.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        result.stop_reason = resp.stop_reason
        if resp.stop_reason == "refusal":
            result.text = "The request was declined by the model's safety policy."
            return result
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            result.text = text_of(resp)
            if resp.stop_reason == "max_tokens":
                result.text += "\n\n[truncated]"
            return result
        messages.append({"role": "assistant", "content": resp.content})
        outputs = []
        for tu in tool_uses:
            tool = by_name.get(tu.name)
            try:
                if tool is None:
                    raise ValueError(f"unknown tool {tu.name}")
                args = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
                out = tool.fn(**args)
                content, is_error = json.dumps(out, default=str)[:60000], False
            except Exception as exc:  # noqa: BLE001
                content, is_error = f"Error: {exc}", True
            result.tool_calls.append({"tool": tu.name, "input": tu.input, "is_error": is_error})
            outputs.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": outputs})
    result.text = result.text or "Stopped after the maximum number of tool turns."
    return result


def record(db: Session, agent: str, request: str, result: AgentResult | None, status: str = "SUCCESS", response: str | None = None) -> AgentRun:
    run = AgentRun(
        agent=agent, request=request[:20000], response=(response if response is not None else (result.text if result else None)),
        tool_calls=result.tool_calls if result else [], model=get_settings().llm_model if result else None,
        input_tokens=result.input_tokens if result else 0, output_tokens=result.output_tokens if result else 0, status=status,
    )
    db.add(run)
    db.flush()
    return run
