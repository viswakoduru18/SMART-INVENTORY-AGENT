"""Runtime settings (environment) and business policy (YAML)."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'data' / 'smart_inventory.db'}"
    policy_path: str = str(ROOT / "config" / "policy.yaml")
    erp_config_path: str = str(ROOT / "config" / "erp.yaml")
    erp_connector: str | None = None  # overrides erp.yaml "connector"

    # API security: comma separated "key:role" pairs. Roles: admin, procurement, management, portal, erp.
    # Empty = open (development only).
    api_keys: str = ""

    # Claude agent layer. The platform runs fully without it (deterministic core).
    anthropic_api_key: str | None = None
    llm_enabled: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_fallbacks: bool = True
    llm_timeout_seconds: float = 120.0

    # Notifications (WhatsApp Business API via Gupshup / AiSensy or any webhook).
    whatsapp_webhook_url: str | None = None
    whatsapp_api_key: str | None = None
    procurement_whatsapp: str | None = None

    scheduler_enabled: bool = False
    timezone: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return _expand_env(yaml.safe_load(fh) or {})


class Policy:
    """Typed-ish accessor over policy.yaml with dotted lookups."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    @property
    def version(self) -> str:
        return self.data.get("version", "policy-unversioned")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})


_policy_override: Policy | None = None


def get_policy() -> Policy:
    if _policy_override is not None:
        return _policy_override
    return Policy(load_yaml(get_settings().policy_path))


def set_policy_override(policy: Policy | None) -> None:
    """Used by tests and the admin API to swap policy at runtime."""
    global _policy_override
    _policy_override = policy
