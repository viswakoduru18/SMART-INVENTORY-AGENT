from __future__ import annotations

import os
import tempfile
from datetime import date
from types import SimpleNamespace

import pytest

_tmp = tempfile.mkdtemp(prefix="si-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ["API_KEYS"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SCHEDULER_ENABLED"] = "false"

from smart_inventory import db as dbm  # noqa: E402
from smart_inventory.integrations import set_connector  # noqa: E402
from smart_inventory.integrations.mock_erp import MockERPConnector  # noqa: E402

TODAY = date(2026, 10, 2)


@pytest.fixture(scope="session")
def erp():
    conn = MockERPConnector({"seed": 7, "skus": 120, "retailers": 60, "suppliers": 10, "history_days": 200}, today=TODAY,
                            outbox=f"{_tmp}/outbox")
    set_connector(conn)
    return conn


@pytest.fixture(scope="session")
def seeded(erp):
    dbm.init_engine(os.environ["DATABASE_URL"])
    dbm.Base.metadata.drop_all(dbm.get_engine())
    dbm.create_all()
    from smart_inventory.pipeline.daily_cycle import run_daily_cycle

    with dbm.session_scope() as s:
        summary = run_daily_cycle(s, TODAY, erp=erp, enforce_dq=False)
    return summary


@pytest.fixture()
def db(seeded):
    s = dbm.SessionLocal()
    yield s
    s.close()


@pytest.fixture(scope="session")
def client(seeded):
    from fastapi.testclient import TestClient

    import smart_inventory.api.deps as deps
    from smart_inventory.main import app

    deps.business_today = lambda: TODAY  # freeze business date
    import smart_inventory.api.routes_console as rc
    import smart_inventory.api.routes_ops as ro

    ro.business_today = lambda: TODAY
    rc.business_today = lambda: TODAY
    rc.plan_date = lambda: TODAY
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- fake Claude
def block_text(text):
    return SimpleNamespace(type="text", text=text)


def block_tool(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def message(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=100, output_tokens=50))


class FakeClaude:
    """Scripted stand-in for anthropic.Anthropic: returns queued responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)
