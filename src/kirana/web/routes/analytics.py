"""Sales Analytics & Business Intelligence routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...config import PROJECT_ROOT
from ...db.connection import IST
from ..auth import TenantContext, get_current_tenant_context

router = APIRouter(prefix="/analytics", tags=["Analytics"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "kirana" / "web" / "templates"))


@router.get("", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    start: str | None = Query(None),
    end: str | None = Query(None),
    ctx: TenantContext = Depends(get_current_tenant_context),
) -> Any:
    """Render Sales Analytics view with revenue trend chart and top items."""
    repo = ctx.repo
    today = datetime.now(IST).date()

    end_date = end.strip() if end and end.strip() else today.isoformat()
    start_date = start.strip() if start and start.strip() else (today - timedelta(days=14)).isoformat()

    report = repo.sales_report(start_date, end_date)

    # Prepare chart labels and values from daily revenue buckets
    chart_labels: list[str] = []
    chart_values: list[float] = []

    # by_day is list of (date_str, revenue_paise)
    for date_str, revenue_paise in report.by_day:
        chart_labels.append(date_str[5:])  # MM-DD
        chart_values.append(round(revenue_paise / 100, 2))

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "ctx": ctx,
            "active_page": "analytics",
            "start_date": start_date,
            "end_date": end_date,
            "sales_report": report,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
        },
    )
