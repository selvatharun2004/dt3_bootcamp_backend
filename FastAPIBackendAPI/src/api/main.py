from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from io import StringIO
from typing import Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
import csv
import os

from sqlalchemy import text

from .db import ENGINE
from .db import get_db_session
from . import models
from .analytics import (
    aggregate_daily_to_period,
    ensure_category_benchmarks,
    parse_readings_csv,
    recompute_site_daily_baselines,
    resolve_or_seed_site,
    sha256_hex,
    utc_today,
)


def _parse_cors_origins(raw: Optional[str]) -> List[str]:
    """
    Parse CORS origins from an env var.

    Accepts:
      - Comma-separated origins
      - "*" to allow all (not recommended for production)

    Returns:
      list[str] suitable for CORSMiddleware allow_origins.
    """
    if not raw:
        return ["http://localhost:5173"]

    raw = raw.strip()
    if raw == "*":
        return ["*"]

    return [o.strip() for o in raw.split(",") if o.strip()]


def _utc_today() -> date:
    """Return today's date in UTC."""
    return datetime.now(timezone.utc).date()


class Period(str, Enum):
    """Aggregation period used by dashboards."""

    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class ProblemDetail(BaseModel):
    """RFC7807-ish problem detail response model."""

    type: str = Field(default="about:blank", description="A URI reference that identifies the problem type.")
    title: str = Field(..., description="Short, human-readable summary of the problem.")
    status: int = Field(..., description="HTTP status code.")
    detail: str = Field(..., description="Human-readable explanation specific to this occurrence.")
    instance: Optional[str] = Field(default=None, description="URI reference that identifies the specific occurrence.")


class IngestSummary(BaseModel):
    """CSV ingestion response."""

    upload_id: str = Field(..., description="Server-generated identifier for this upload.")
    site_id: str = Field(..., description="Site identifier the upload was associated with.")
    rows_received: int = Field(..., description="Total data rows received (excluding header).")
    rows_accepted: int = Field(..., description="Rows accepted after validation.")
    rows_rejected: int = Field(..., description="Rows rejected after validation.")
    errors: List[str] = Field(default_factory=list, description="Human readable validation errors (sample).")


class KpiItem(BaseModel):
    """Simple KPI name/value pair used by dashboards."""

    label: str = Field(..., description="KPI label.")
    value: str = Field(..., description="Human readable KPI value.")
    tone: Optional[Literal["default", "danger"]] = Field(default=None, description="Optional semantic tone.")


class TimeseriesPoint(BaseModel):
    """A single timeseries point."""

    ts: date = Field(..., description="Date bucket in UTC.")
    kwh: float = Field(..., description="Energy usage for the bucket in kWh.")


class BaselinePoint(BaseModel):
    """Baseline vs actual point for a bucket."""

    ts: date = Field(..., description="Date bucket in UTC.")
    actual_kwh: float = Field(..., description="Actual consumption.")
    baseline_kwh: float = Field(..., description="Rolling baseline value.")
    deviation_pct: float = Field(..., description="(actual-baseline)/baseline * 100")
    is_anomaly: bool = Field(..., description="True when deviation exceeds configured threshold.")


class AnalyticsResponse(BaseModel):
    """Consumer analytics payload backing charts and KPI strip."""

    site_id: str = Field(..., description="Site identifier.")
    site_name: str = Field(..., description="Human readable site name.")
    period: Period = Field(..., description="Aggregation period used.")
    kpis: List[KpiItem] = Field(..., description="KPI tiles.")
    trends: List[TimeseriesPoint] = Field(..., description="Aggregated trend series.")
    baseline_vs_actual: List[BaselinePoint] = Field(..., description="Baseline vs actual series.")
    threshold_pct: float = Field(..., description="Anomaly threshold percentage.")


class AnomalyItem(BaseModel):
    """Anomaly item for listing."""

    # NOTE: Pydantic v2 raises a PydanticUserError when a field name (`date`) clashes with
    # a type annotation symbol in the same scope (`from datetime import date`).
    # Keep the API field name as "date" via alias while using a safe Python attribute name.
    date_: date = Field(..., alias="date", description="Date of anomaly bucket.")
    deviation_pct: float = Field(..., description="Deviation percent above baseline.")
    suggested_action: str = Field(..., description="Suggested operational follow-up.")
    severity: Literal["medium", "high"] = Field(..., description="Severity bucket derived from deviation.")

    model_config = {"populate_by_name": True}


class AnomaliesResponse(BaseModel):
    """List anomalies for a site/date range."""

    site_id: str = Field(..., description="Site identifier.")
    threshold_pct: float = Field(..., description="Threshold used for detection.")
    items: List[AnomalyItem] = Field(..., description="Anomalies detected.")


class BenchmarkSeriesPoint(BaseModel):
    """Peer benchmark time series point."""

    ts: date = Field(..., description="Date bucket in UTC.")
    site_kwh: float = Field(..., description="Site consumption.")
    peer_avg_kwh: float = Field(..., description="Peer average consumption (placeholder).")


class BenchmarkResponse(BaseModel):
    """Peer benchmarking response."""

    site_id: str = Field(..., description="Site identifier.")
    category: str = Field(..., description="Business category used for benchmarking (placeholder).")
    period: Period = Field(..., description="Aggregation period used.")
    series: List[BenchmarkSeriesPoint] = Field(..., description="Site vs peer average series.")


class AlertStatus(str, Enum):
    """Alert lifecycle status (placeholder)."""

    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class AlertItem(BaseModel):
    """Alert item for account manager dashboard."""

    alert_id: str = Field(..., description="Alert identifier.")
    created_at: date = Field(..., description="Alert created date.")
    customer: str = Field(..., description="Customer name.")
    site: str = Field(..., description="Site name.")
    deviation_pct: float = Field(..., description="Deviation percent.")
    suggested_action: str = Field(..., description="Suggested action.")
    severity: Literal["medium", "high"] = Field(..., description="Severity bucket.")
    status: AlertStatus = Field(..., description="Current alert status.")


class AlertsListResponse(BaseModel):
    """Alerts list response."""

    items: List[AlertItem] = Field(..., description="Alerts in descending date order.")


class PortfolioRankItem(BaseModel):
    """Ranking item for account manager portfolio panel."""

    customer: str = Field(..., description="Customer name.")
    site: str = Field(..., description="Site name.")
    anomalies_30d: int = Field(..., description="Number of anomalies in last 30 days.")
    max_deviation_pct: float = Field(..., description="Max deviation percent in last 30 days.")


class PortfolioResponse(BaseModel):
    """Portfolio ranking response."""

    items: List[PortfolioRankItem] = Field(..., description="Ranked portfolio list.")


class ExportFormat(str, Enum):
    """Export output format."""

    csv = "csv"
    pdf = "pdf"


class ExportRequest(BaseModel):
    """Export job request."""

    site_id: str = Field(..., description="Site identifier.")
    start_date: date = Field(..., description="Start date inclusive.")
    end_date: date = Field(..., description="End date inclusive.")
    format: ExportFormat = Field(..., description="Desired export format.")


class ExportJobResponse(BaseModel):
    """Export job response (placeholder immediate completion)."""

    job_id: str = Field(..., description="Export job identifier.")
    status: Literal["complete"] = Field(..., description="Job status (placeholder always complete).")
    download_url: str = Field(..., description="URL to download the export result.")


OPENAPI_TAGS = [
    {"name": "health", "description": "Service health and diagnostics endpoints."},
    {"name": "ingestion", "description": "CSV upload/ingestion for meter readings."},
    {"name": "consumer", "description": "Consumer dashboard analytics, anomalies, and benchmarking."},
    {"name": "account_manager", "description": "Account manager portfolio ranking and alerts review."},
    {"name": "exports", "description": "Export job creation and download endpoints."},
]

API_PREFIX = os.getenv("API_PREFIX", "/api/v1").rstrip("/")

app = FastAPI(
    title="Energy Analytics Backend API",
    description=(
        "Backend APIs for the Commercial Energy Consumption Analytics & Anomaly Alerts system.\n\n"
        "This service persists readings to PostgreSQL and computes rolling 4-week baselines and anomalies "
        "to power the React dashboards."
    ),
    version="0.1.0",
    openapi_tags=OPENAPI_TAGS,
)

cors_origins = _parse_cors_origins(os.getenv("CORS_ORIGINS"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _make_demo_site(site_id: str) -> Tuple[str, str]:
    """Return (site_id, site_name) for demo."""
    # In future: resolve site from DB.
    mapping = {
        "site_main": "Main Warehouse",
        "site_wa": "Warehouse A",
        "site_store12": "Store 12",
        "site_plant3": "Plant 3",
    }
    return site_id, mapping.get(site_id, "Main Warehouse")


def _demo_trend_series(period: Period, days: int = 28) -> List[TimeseriesPoint]:
    """
    Create a deterministic demo series.

    Note: This is deliberately simple; future implementation will aggregate real readings.
    """
    today = _utc_today()
    start = today - timedelta(days=days - 1)
    points: List[TimeseriesPoint] = []
    for i in range(days):
        d = start + timedelta(days=i)
        # Baseline-ish wave + mild drift
        kwh = 820.0 + (i % 7) * 18.0 + (i // 7) * 6.0
        if period == Period.daily:
            points.append(TimeseriesPoint(ts=d, kwh=kwh))
        elif period == Period.weekly and d.weekday() == 0:
            # weekly bucket on Mondays
            points.append(TimeseriesPoint(ts=d, kwh=kwh * 7.0))
        elif period == Period.monthly and d.day == 1:
            points.append(TimeseriesPoint(ts=d, kwh=kwh * 30.0))
    if period != Period.daily and not points:
        # ensure at least one point for short ranges
        points.append(TimeseriesPoint(ts=today, kwh=820.0))
    return points


def _demo_baseline_vs_actual(threshold_pct: float, days: int = 28) -> List[BaselinePoint]:
    """Generate demo baseline vs actual series with a few anomalies."""
    today = _utc_today()
    start = today - timedelta(days=days - 1)
    series: List[BaselinePoint] = []
    anomaly_days = {today - timedelta(days=11), today - timedelta(days=7), today - timedelta(days=2)}
    for i in range(days):
        d = start + timedelta(days=i)
        baseline = 800.0 + (i % 7) * 10.0
        actual = baseline * (1.05 + ((i % 5) * 0.01))
        if d in anomaly_days:
            actual = baseline * (1.0 + (threshold_pct / 100.0) + 0.08)  # exceed threshold
        deviation_pct = ((actual - baseline) / baseline) * 100.0
        is_anomaly = deviation_pct > threshold_pct
        series.append(
            BaselinePoint(
                ts=d,
                actual_kwh=round(actual, 2),
                baseline_kwh=round(baseline, 2),
                deviation_pct=round(deviation_pct, 1),
                is_anomaly=is_anomaly,
            )
        )
    return series


def _demo_anomalies(site_id: str, threshold_pct: float) -> List[AnomalyItem]:
    """Return demo anomalies matching the UI examples."""
    today = _utc_today()
    items = [
        (today - timedelta(days=11), 28.0, "Check HVAC scheduling"),
        (today - timedelta(days=7), 24.0, "Inspect overnight load"),
        (today - timedelta(days=2), 21.0, "Verify operational changes"),
    ]
    out: List[AnomalyItem] = []
    for d, dev, action in items:
        if dev <= threshold_pct:
            continue
        out.append(
            AnomalyItem(
                date=d,
                deviation_pct=dev,
                suggested_action=action,
                severity="high" if dev >= threshold_pct + 10 else "medium",
            )
        )
    return out


def _demo_alerts() -> List[AlertItem]:
    """Return demo account manager alerts list."""
    today = _utc_today()
    return [
        AlertItem(
            alert_id="alrt_001",
            created_at=today - timedelta(days=2),
            customer="Acme Logistics",
            site="Warehouse A",
            deviation_pct=41.0,
            suggested_action="Schedule customer check-in",
            severity="high",
            status=AlertStatus.open,
        ),
        AlertItem(
            alert_id="alrt_002",
            created_at=today - timedelta(days=4),
            customer="Bright Retail",
            site="Store 12",
            deviation_pct=34.0,
            suggested_action="Investigate refrigeration load",
            severity="high",
            status=AlertStatus.open,
        ),
        AlertItem(
            alert_id="alrt_003",
            created_at=today - timedelta(days=7),
            customer="Northside Manufacturing",
            site="Plant 3",
            deviation_pct=23.0,
            suggested_action="Confirm operational changes",
            severity="medium",
            status=AlertStatus.acknowledged,
        ),
    ]


def _demo_portfolio() -> List[PortfolioRankItem]:
    """Return demo portfolio ranking list."""
    return [
        PortfolioRankItem(customer="Acme Logistics", site="Warehouse A", anomalies_30d=7, max_deviation_pct=41.0),
        PortfolioRankItem(customer="Bright Retail", site="Store 12", anomalies_30d=5, max_deviation_pct=34.0),
        PortfolioRankItem(customer="Northside Manufacturing", site="Plant 3", anomalies_30d=4, max_deviation_pct=23.0),
    ]


def _demo_benchmark(period: Period, days: int = 28) -> List[BenchmarkSeriesPoint]:
    """Generate demo benchmark series."""
    today = _utc_today()
    start = today - timedelta(days=days - 1)
    out: List[BenchmarkSeriesPoint] = []
    for i in range(days):
        d = start + timedelta(days=i)
        site = 820.0 + (i % 7) * 12.0
        peer = 870.0 + (i % 7) * 8.0
        if period == Period.daily:
            out.append(BenchmarkSeriesPoint(ts=d, site_kwh=round(site, 2), peer_avg_kwh=round(peer, 2)))
        elif period == Period.weekly and d.weekday() == 0:
            out.append(BenchmarkSeriesPoint(ts=d, site_kwh=round(site * 7.0, 2), peer_avg_kwh=round(peer * 7.0, 2)))
        elif period == Period.monthly and d.day == 1:
            out.append(
                BenchmarkSeriesPoint(ts=d, site_kwh=round(site * 30.0, 2), peer_avg_kwh=round(peer * 30.0, 2))
            )
    if not out:
        out.append(BenchmarkSeriesPoint(ts=today, site_kwh=820.0, peer_avg_kwh=870.0))
    return out


@app.get("/docs/ws", tags=["health"], include_in_schema=False)
def websocket_usage_help() -> PlainTextResponse:
    """
    WebSocket usage note.

    This project currently does not expose WebSocket endpoints.
    When real-time alert streaming is introduced, this page can be expanded.
    """
    return PlainTextResponse(
        "No WebSocket endpoints are implemented yet. "
        "The React dashboards consume REST endpoints under /api/v1."
    )


@app.get("/health", tags=["health"], summary="Health check", description="Simple health check endpoint for uptime monitoring.")
def health() -> Dict[str, str]:
    """Return service health status."""
    return {"status": "ok"}


# PUBLIC_INTERFACE
@app.get(
    "/health/db",
    tags=["health"],
    summary="Database health check",
    description=(
        "Checks PostgreSQL connectivity using SQLAlchemy.\\n\\n"
        "If DATABASE_URL is not configured, returns 503 to indicate DB is unavailable."
    ),
)
def health_db() -> Dict[str, str]:
    """
    Check database connectivity.

    Returns:
    - {"status":"ok","db":"ok"} when DB connectivity is healthy
    - 503 when DATABASE_URL is not configured or the DB cannot be reached
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")
    try:
        with ENGINE.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database connectivity failed: {e}") from e


# PUBLIC_INTERFACE
@app.post(
    f"{API_PREFIX}/ingest/csv",
    tags=["ingestion"],
    summary="Upload meter readings CSV",
    description=(
        "Accepts a CSV file containing `timestamp` and `kWh` columns.\n\n"
        "This implementation performs lightweight validation and returns a summary. "
        "Persistence and real ingestion will be implemented next."
    ),
    responses={
        200: {"model": IngestSummary, "description": "Ingestion summary"},
        400: {"model": ProblemDetail, "description": "Validation error"},
    },
)
async def ingest_csv(
    site_id: str = Query(default="site_main", description="Target site identifier for the readings."),
    file: UploadFile = File(..., description="CSV file with `timestamp` and `kWh` columns."),
) -> IngestSummary:
    """
    Upload and validate a meter readings CSV.

    Parameters:
    - site_id: Site identifier the readings belong to.
    - file: CSV file with header including `timestamp` and `kWh`.

    Returns:
    - IngestSummary containing accepted/rejected row counts and sample errors.
    """
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="Missing filename.",
        )

    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    raw_bytes = await file.read()
    content = raw_bytes.decode("utf-8", errors="replace")
    accepted, rows_received, rows_rejected, errors = parse_readings_csv(content)

    if rows_received == 0 and accepted == [] and errors:
        return JSONResponse(
            status_code=400,
            content=ProblemDetail(
                title="Invalid CSV",
                status=400,
                detail=errors[0],
            ).model_dump(),
        )

    # Persist upload + readings
    with get_db_session() as session:
        site = resolve_or_seed_site(session, site_id)

        upload = models.IngestionUpload(
            site_id=site.site_id,
            original_filename=file.filename,
            content_type=file.content_type,
            file_sha256=sha256_hex(raw_bytes),
            rows_received=rows_received,
            rows_accepted=len(accepted),
            rows_rejected=rows_rejected,
            error_sample=errors,
        )
        session.add(upload)
        session.flush()  # get upload_id

        # Insert readings (dedupe handled by DB unique index; we keep behavior "accept and ignore duplicates").
        inserted = 0
        for r in accepted:
            mr = models.MeterReading(site_id=site.site_id, meter_id=None, upload_id=upload.upload_id, ts=r.ts, kwh=r.kwh)
            session.add(mr)
            inserted += 1

        # Compute baselines/anomalies over the affected window (ingested range, plus 28d lookback handled internally).
        if accepted:
            min_day = min(r.ts.date() for r in accepted)
            max_day = max(r.ts.date() for r in accepted)
            recompute_site_daily_baselines(session, site=site, threshold_pct=20.0, start_day=min_day, end_day=max_day)

        # Commit all changes. If a unique constraint fails due to duplicates, rollback and return a helpful error.
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=400, detail=f"Ingestion failed: {e}") from e

        return IngestSummary(
            upload_id=str(upload.upload_id),
            site_id=site_id,
            rows_received=rows_received,
            rows_accepted=len(accepted),
            rows_rejected=rows_rejected,
            errors=errors,
        )


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/consumer/analytics",
    tags=["consumer"],
    summary="Consumer analytics for dashboards",
    description="Returns KPIs and time-series data (trends, baseline vs actual) for a site and period.",
    responses={200: {"model": AnalyticsResponse}},
)
def consumer_analytics(
    site_id: str = Query(default="site_main", description="Site identifier."),
    period: Period = Query(default=Period.weekly, description="Aggregation period."),
    threshold_pct: float = Query(default=20.0, ge=0.0, le=200.0, description="Anomaly threshold percent."),
) -> AnalyticsResponse:
    """
    Get consumer dashboard analytics.

    Parameters:
    - site_id: Site identifier.
    - period: daily|weekly|monthly aggregation.
    - threshold_pct: anomaly threshold in percent (default 20%).

    Returns:
    - AnalyticsResponse with KPI tiles and chart series.
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        site = resolve_or_seed_site(session, site_id)

        end_day = utc_today()
        start_day = end_day - timedelta(days=27)

        # Ensure baselines are present for the last 28 days if we have readings.
        # (If no readings, the response will be empty series but still contract-compatible.)
        recompute_site_daily_baselines(session, site=site, threshold_pct=threshold_pct, start_day=start_day, end_day=end_day)
        session.commit()

        baseline_rows = (
            session.execute(
                select(models.DailySiteBaseline)
                .where(models.DailySiteBaseline.site_id == site.site_id)
                .where(models.DailySiteBaseline.day >= start_day)
                .where(models.DailySiteBaseline.day <= end_day)
                .order_by(models.DailySiteBaseline.day)
            )
            .scalars()
            .all()
        )

        baseline_vs_actual = [
            BaselinePoint(
                ts=r.day,
                actual_kwh=float(r.actual_kwh),
                baseline_kwh=float(r.baseline_kwh),
                deviation_pct=float(r.deviation_pct),
                is_anomaly=float(r.deviation_pct) > float(threshold_pct),
            )
            for r in baseline_rows
        ]

        # Trends: aggregate daily actuals into requested period buckets.
        daily_actuals = [(r.day, float(r.actual_kwh)) for r in baseline_rows]
        trend_points = aggregate_daily_to_period(daily_actuals, period=period.value)
        trends = [TimeseriesPoint(ts=d, kwh=round(kwh, 2)) for d, kwh in trend_points]

        # KPI: anomaly count from anomalies table (last 30 days)
        kpi_end = end_day
        kpi_start = kpi_end - timedelta(days=29)
        anomaly_count_30d = (
            session.execute(
                select(func.count())
                .select_from(models.Anomaly)
                .where(models.Anomaly.site_id == site.site_id)
                .where(models.Anomaly.day >= kpi_start)
                .where(models.Anomaly.day <= kpi_end)
            )
            .scalar_one()
        )

        latest_dev = baseline_vs_actual[-1].deviation_pct if baseline_vs_actual else 0.0
        latest_kwh = baseline_vs_actual[-1].actual_kwh if baseline_vs_actual else 0.0

        # Benchmark KPI: compare latest day to category peer avg if available (else "…")
        bench_value = "…"
        if site.business_category and baseline_vs_actual:
            # Ensure benchmark row exists; populate a deterministic placeholder peer avg from latest actual.
            ensure_category_benchmarks(session, business_category=site.business_category, days=[baseline_vs_actual[-1].ts])
            bench = session.get(
                models.CategoryBenchmarkDaily,
                {"business_category": site.business_category, "day": baseline_vs_actual[-1].ts},
            )
            if bench and (bench.peer_avg_kwh or 0.0) <= 0.0:
                bench.peer_avg_kwh = float(latest_kwh) * 1.06
                session.commit()
            if bench and bench.peer_avg_kwh > 0:
                bench_value = f"{((latest_kwh - bench.peer_avg_kwh) / bench.peer_avg_kwh) * 100.0:+.1f}%"

        kpis = [
            KpiItem(label="Selected site", value=site.name),
            KpiItem(label="Baseline deviation", value=f"{latest_dev:+.1f}%"),
            KpiItem(
                label="Anomalies (30d)",
                value=str(int(anomaly_count_30d)),
                tone="danger" if int(anomaly_count_30d) > 0 else None,
            ),
            KpiItem(label="Benchmark vs peers", value=bench_value),
        ]

        return AnalyticsResponse(
            site_id=site_id,
            site_name=site.name,
            period=period,
            kpis=kpis,
            trends=trends,
            baseline_vs_actual=baseline_vs_actual,
            threshold_pct=threshold_pct,
        )


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/consumer/anomalies",
    tags=["consumer"],
    summary="List anomalies for a site",
    description="Returns detected anomalies for a site (placeholder demo list).",
    responses={200: {"model": AnomaliesResponse}},
)
def consumer_anomalies(
    site_id: str = Query(default="site_main", description="Site identifier."),
    threshold_pct: float = Query(default=20.0, ge=0.0, le=200.0, description="Anomaly threshold percent."),
) -> AnomaliesResponse:
    """
    List anomalies for consumer dashboard.

    Parameters:
    - site_id: Site identifier.
    - threshold_pct: anomaly threshold in percent.

    Returns:
    - AnomaliesResponse containing anomaly items.
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        site = resolve_or_seed_site(session, site_id)
        end_day = utc_today()
        start_day = end_day - timedelta(days=29)

        # Ensure anomalies are up to date for the visible range.
        recompute_site_daily_baselines(session, site=site, threshold_pct=threshold_pct, start_day=start_day, end_day=end_day)
        session.commit()

        rows = (
            session.execute(
                select(models.Anomaly)
                .where(models.Anomaly.site_id == site.site_id)
                .where(models.Anomaly.day >= start_day)
                .where(models.Anomaly.day <= end_day)
                .where(models.Anomaly.deviation_pct > threshold_pct)
                .order_by(models.Anomaly.day.desc())
            )
            .scalars()
            .all()
        )

        items = [
            AnomalyItem(
                date=r.day,
                deviation_pct=float(r.deviation_pct),
                suggested_action=r.suggested_action,
                severity=r.severity,
            )
            for r in rows
        ]
        return AnomaliesResponse(site_id=site_id, threshold_pct=threshold_pct, items=items)


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/consumer/benchmark",
    tags=["consumer"],
    summary="Peer benchmark series",
    description="Returns a site vs peer average series (placeholder).",
    responses={200: {"model": BenchmarkResponse}},
)
def consumer_benchmark(
    site_id: str = Query(default="site_main", description="Site identifier."),
    period: Period = Query(default=Period.weekly, description="Aggregation period."),
) -> BenchmarkResponse:
    """
    Get peer benchmarking series.

    Parameters:
    - site_id: Site identifier.
    - period: daily|weekly|monthly aggregation.

    Returns:
    - BenchmarkResponse with site series vs peer average series.
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        site = resolve_or_seed_site(session, site_id)
        end_day = utc_today()
        start_day = end_day - timedelta(days=27)

        # Ensure we have daily baselines for site (used as daily actuals quickly).
        recompute_site_daily_baselines(session, site=site, threshold_pct=20.0, start_day=start_day, end_day=end_day)
        session.commit()

        baseline_rows = (
            session.execute(
                select(models.DailySiteBaseline)
                .where(models.DailySiteBaseline.site_id == site.site_id)
                .where(models.DailySiteBaseline.day >= start_day)
                .where(models.DailySiteBaseline.day <= end_day)
                .order_by(models.DailySiteBaseline.day)
            )
            .scalars()
            .all()
        )

        daily_actuals = [(r.day, float(r.actual_kwh)) for r in baseline_rows]
        site_series = aggregate_daily_to_period(daily_actuals, period=period.value)

        category = site.business_category or "Uncategorised"
        # Ensure benchmark rows exist for each day bucket start.
        days_needed = [d for d, _ in site_series]
        if site.business_category:
            ensure_category_benchmarks(session, business_category=site.business_category, days=days_needed)
            # Deterministic placeholder: peer avg = site_kwh * 1.06
            for d, kwh in site_series:
                bench = session.get(models.CategoryBenchmarkDaily, {"business_category": site.business_category, "day": d})
                if bench and (bench.peer_avg_kwh or 0.0) <= 0.0:
                    bench.peer_avg_kwh = float(kwh) * 1.06
            session.commit()

        series: List[BenchmarkSeriesPoint] = []
        for d, kwh in site_series:
            peer = kwh * 1.06
            if site.business_category:
                bench = session.get(models.CategoryBenchmarkDaily, {"business_category": site.business_category, "day": d})
                if bench and bench.peer_avg_kwh > 0:
                    peer = float(bench.peer_avg_kwh)
            series.append(BenchmarkSeriesPoint(ts=d, site_kwh=round(kwh, 2), peer_avg_kwh=round(peer, 2)))

        return BenchmarkResponse(site_id=site_id, category=category, period=period, series=series)


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/account-manager/portfolio",
    tags=["account_manager"],
    summary="Account manager portfolio ranking",
    description="Rank customers by anomaly counts and max deviation (placeholder).",
    responses={200: {"model": PortfolioResponse}},
)
def account_manager_portfolio() -> PortfolioResponse:
    """
    Get portfolio ranking used by the account manager dashboard.

    Returns:
    - PortfolioResponse with ranked customers and summary statistics.
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        end_day = utc_today()
        start_day = end_day - timedelta(days=29)

        # Rank by anomaly count last 30d, max deviation last 30d.
        q = (
            select(
                models.Customer.name.label("customer"),
                models.Site.name.label("site"),
                func.count(models.Anomaly.anomaly_id).label("anomalies_30d"),
                func.coalesce(func.max(models.Anomaly.deviation_pct), 0.0).label("max_deviation_pct"),
            )
            .select_from(models.Site)
            .join(models.Customer, models.Customer.customer_id == models.Site.customer_id)
            .outerjoin(
                models.Anomaly,
                (models.Anomaly.site_id == models.Site.site_id)
                & (models.Anomaly.day >= start_day)
                & (models.Anomaly.day <= end_day),
            )
            .group_by(models.Customer.name, models.Site.name)
            .order_by(func.count(models.Anomaly.anomaly_id).desc(), func.max(models.Anomaly.deviation_pct).desc())
            .limit(50)
        )

        rows = session.execute(q).all()
        items = [
            PortfolioRankItem(
                customer=r.customer,
                site=r.site,
                anomalies_30d=int(r.anomalies_30d or 0),
                max_deviation_pct=float(r.max_deviation_pct or 0.0),
            )
            for r in rows
        ]
        return PortfolioResponse(items=items)


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/account-manager/alerts",
    tags=["account_manager"],
    summary="List alerts for account manager",
    description="Returns alert list (placeholder) for review and follow-up.",
    responses={200: {"model": AlertsListResponse}},
)
def account_manager_alerts(
    status: Optional[AlertStatus] = Query(default=None, description="Optional filter by alert status."),
) -> AlertsListResponse:
    """
    List alerts for account manager.

    Parameters:
    - status: optional lifecycle status filter.

    Returns:
    - AlertsListResponse list.
    """
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        q = (
            select(
                models.Alert,
                models.Site.name.label("site_name"),
                models.Customer.name.label("customer_name"),
            )
            .select_from(models.Alert)
            .join(models.Site, models.Site.site_id == models.Alert.site_id)
            .join(models.Customer, models.Customer.customer_id == models.Site.customer_id)
            .order_by(models.Alert.created_at.desc())
            .limit(200)
        )
        if status is not None:
            q = q.where(models.Alert.status == status.value)

        rows = session.execute(q).all()
        items = [
            AlertItem(
                alert_id=str(alert.alert_id),
                created_at=alert.created_at.date(),
                customer=customer_name,
                site=site_name,
                deviation_pct=float(alert.deviation_pct),
                suggested_action=alert.suggested_action,
                severity=alert.severity,
                status=AlertStatus(alert.status),
            )
            for alert, site_name, customer_name in rows
        ]
        return AlertsListResponse(items=items)


def _generate_export_csv(site_id: str, start_date: date, end_date: date) -> str:
    """Generate a CSV export from persisted daily baselines (and compute missing baselines if needed)."""
    if ENGINE is None:
        raise HTTPException(status_code=503, detail="Database is not configured (DATABASE_URL missing).")

    with get_db_session() as session:
        site = resolve_or_seed_site(session, site_id)
        recompute_site_daily_baselines(session, site=site, threshold_pct=20.0, start_day=start_date, end_day=end_date)
        session.commit()

        rows = (
            session.execute(
                select(models.DailySiteBaseline)
                .where(models.DailySiteBaseline.site_id == site.site_id)
                .where(models.DailySiteBaseline.day >= start_date)
                .where(models.DailySiteBaseline.day <= end_date)
                .order_by(models.DailySiteBaseline.day)
            )
            .scalars()
            .all()
        )

        out = StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "actual_kwh", "baseline_kwh", "deviation_pct", "is_anomaly"])
        for r in rows:
            writer.writerow(
                [
                    r.day.isoformat(),
                    float(r.actual_kwh),
                    float(r.baseline_kwh),
                    float(r.deviation_pct),
                    float(r.deviation_pct) > float(r.threshold_pct),
                ]
            )
        return out.getvalue()


# PUBLIC_INTERFACE
@app.post(
    f"{API_PREFIX}/exports",
    tags=["exports"],
    summary="Create export job",
    description=(
        "Creates an export job for a date range and returns a download URL.\n\n"
        "Placeholder implementation: returns a job that is immediately complete."
    ),
    responses={200: {"model": ExportJobResponse}},
)
def create_export_job(req: ExportRequest) -> ExportJobResponse:
    """
    Create an export job (placeholder immediate-complete).

    Parameters:
    - req: ExportRequest including site_id, start_date, end_date, and format.

    Returns:
    - ExportJobResponse with a download URL.
    """
    if req.end_date < req.start_date:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    job_id = f"exp_{int(datetime.now(timezone.utc).timestamp())}"
    download_url = f"{API_PREFIX}/exports/{job_id}/download?format={req.format.value}&site_id={req.site_id}&start_date={req.start_date.isoformat()}&end_date={req.end_date.isoformat()}"
    return ExportJobResponse(job_id=job_id, status="complete", download_url=download_url)


# PUBLIC_INTERFACE
@app.get(
    f"{API_PREFIX}/exports/{{job_id}}/download",
    tags=["exports"],
    summary="Download export result",
    description="Downloads the export file (CSV or placeholder PDF text).",
)
def download_export(
    job_id: str,
    format: ExportFormat = Query(default=ExportFormat.csv, description="Export format."),
    site_id: str = Query(default="site_main", description="Site identifier."),
    start_date: date = Query(..., description="Start date inclusive."),
    end_date: date = Query(..., description="End date inclusive."),
):
    """
    Download an export.

    Parameters:
    - job_id: Export job identifier.
    - format: csv|pdf
    - site_id: Site identifier.
    - start_date/end_date: date range inclusive.

    Returns:
    - StreamingResponse with the file content.
    """
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    if format == ExportFormat.csv:
        csv_content = _generate_export_csv(site_id=site_id, start_date=start_date, end_date=end_date)
        filename = f"{job_id}_{site_id}_{start_date.isoformat()}_{end_date.isoformat()}.csv"
        return StreamingResponse(
            iter([csv_content.encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Placeholder PDF: return plain text with application/pdf content-type to keep frontend wiring simple.
    pdf_text = (
        f"PDF export placeholder\njob_id={job_id}\nsite_id={site_id}\n"
        f"range={start_date.isoformat()}..{end_date.isoformat()}\n"
        "Next step: generate a real PDF with a reporting library and templates.\n"
    )
    filename = f"{job_id}_{site_id}_{start_date.isoformat()}_{end_date.isoformat()}.pdf"
    return StreamingResponse(
        iter([pdf_text.encode("utf-8")]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
