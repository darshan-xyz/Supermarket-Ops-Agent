"""FastAPI application factory for the Kirana Admin Web Dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import PROJECT_ROOT
from .routes import (
    analytics,
    api,
    auth_routes,
    billing,
    dashboard,
    employees,
    health,
    inventory,
    khata,
    settings,
)

log = logging.getLogger("kirana.web.app")


def create_web_app(ptb_application: Any = None) -> FastAPI:
    """Instantiate and configure the FastAPI web application."""
    app = FastAPI(
        title="Kirana Store Admin Dashboard",
        description="Autonomous Supermarket Operations Co-Pilot Web Interface",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # Store Telegram Application reference in app state if provided
    app.state.ptb_application = ptb_application

    # Mount static assets directory if present
    static_dir = PROJECT_ROOT / "src" / "kirana" / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register all domain routers
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(inventory.router)
    app.include_router(billing.router)
    app.include_router(khata.router)
    app.include_router(analytics.router)
    app.include_router(employees.router)
    app.include_router(settings.router)
    app.include_router(api.router)

    log.info("Initialized Kirana Admin Dashboard FastAPI app")
    return app
