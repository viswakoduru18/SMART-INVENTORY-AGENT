"""FastAPI application: REST API + procurement/management console."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import routes_console, routes_ops
from .config import get_settings
from .db import create_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    scheduler = None
    if get_settings().scheduler_enabled:
        from .pipeline.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        logging.getLogger(__name__).info("scheduler started: %s", [j.id for j in scheduler.get_jobs()])
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Acintyo Predictive Distribution Intelligence Platform",
        version="1.0.0",
        description="Smart inventory, bounce intelligence, sourcing and dynamic discounting, layered beside the Acintyo ERP. "
                    "The ERP stays the system of record; this layer writes only drafts (PO, price rules, term flags).",
        lifespan=lifespan,
    )
    app.include_router(routes_ops.router)
    app.include_router(routes_console.router)

    @app.get("/health", tags=["admin"])
    def health():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    return app


app = create_app()
