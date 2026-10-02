"""API dependencies: DB session, role-based API-key auth, IST business date."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, Query, status

from ..config import get_settings
from ..db import get_db  # noqa: F401  (re-export)

ROLES = {"admin", "procurement", "management", "portal", "erp"}


def _keys() -> dict[str, str]:
    raw = get_settings().api_keys.strip()
    out: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        key, _, role = pair.partition(":")
        out[key.strip()] = (role.strip() or "admin")
    return out


class Principal:
    def __init__(self, role: str, key_hint: str):
        self.role, self.key_hint = role, key_hint

    @property
    def name(self) -> str:
        return f"{self.role}:{self.key_hint}"


def principal(x_api_key: str | None = Header(default=None), api_key: str | None = Query(default=None, include_in_schema=False)) -> Principal:
    """X-API-Key header; `?api_key=` is accepted as a fallback for webhook providers that cannot set headers."""
    keys = _keys()
    if not keys:
        return Principal("admin", "dev")  # development mode: open
    x_api_key = x_api_key or api_key
    if not x_api_key or x_api_key not in keys:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")
    return Principal(keys[x_api_key], x_api_key[-4:])


def require(*roles: str) -> Callable[[Principal], Principal]:
    allowed = set(roles) | {"admin"}

    def dep(p: Principal = Depends(principal)) -> Principal:
        if p.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{p.role}' cannot access this endpoint")
        return p

    return dep


def business_today() -> date:
    """Current business date in IST."""
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def plan_date() -> date:
    """Date the nightly cycle plans for: after 18:00 IST it is tomorrow (23:30 sync -> next day's plan)."""
    return (datetime.now(ZoneInfo(get_settings().timezone)) + timedelta(hours=6)).date()
