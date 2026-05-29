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
        "This service currently returns **placeholder/demo data** to support the React dashboards, "
        "and will evolve to use PostgreSQL persistence and real analytics."
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

    content = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.DictReader(StringIO(content))

    if reader.fieldnames is None:
        return JSONResponse(
            status_code=400,
            content=ProblemDetail(
                title="Invalid CSV",
                status=400,
                detail="CSV must include a header row with 'timestamp' and 'kWh' columns.",
            ).model_dump(),
        )

    normalized = {h.strip().lower(): h for h in reader.fieldnames if h}
    if "timestamp" not in normalized or "kwh" not in normalized:
        return JSONResponse(
            status_code=400,
            content=ProblemDetail(
                title="Missing required columns",
                status=400,
                detail=f"Expected columns: timestamp, kWh. Got: {', '.join(reader.fieldnames)}",
            ).model_dump(),
        )

    rows_received = 0
    rows_accepted = 0
    errors: List[str] = []

    ts_key = normalized["timestamp"]
    kwh_key = normalized["kwh"]

    for row in reader:
        rows_received += 1
        raw_ts = (row.get(ts_key) or "").strip()
        raw_kwh = (row.get(kwh_key) or "").strip()

        try:
            # Accept ISO-8601; for placeholder we just validate parseability.
            datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except Exception:
            if len(errors) < 10:
                errors.append(f"Row {rows_received}: invalid timestamp '{raw_ts}'")
            continue

        try:
            kwh_val = float(raw_kwh)
            if kwh_val < 0:
                raise ValueError("kWh must be >= 0")
        except Exception:
            if len(errors) < 10:
                errors.append(f"Row {rows_received}: invalid kWh '{raw_kwh}'")
            continue

        rows_accepted += 1

    rows_rejected = rows_received - rows_accepted
    upload_id = f"upl_{int(datetime.now(timezone.utc).timestamp())}"

    return IngestSummary(
        upload_id=upload_id,
        site_id=site_id,
        rows_received=rows_received,
        rows_accepted=rows_accepted,
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
    site_id, site_name = _make_demo_site(site_id)
    baseline_series = _demo_baseline_vs_actual(threshold_pct=threshold_pct, days=28)

    # KPI values derived from placeholder anomalies/baseline, formatted as strings for UI tiles.
    anomalies = [p for p in baseline_series if p.is_anomaly]
    latest_dev = anomalies[-1].deviation_pct if anomalies else baseline_series[-1].deviation_pct

    kpis = [
        KpiItem(label="Selected site", value=site_name),
        KpiItem(label="Baseline deviation", value=f"+{latest_dev:.1f}%"),
        KpiItem(label="Anomalies (30d)", value=str(len(anomalies)), tone="danger" if len(anomalies) > 0 else None),
        KpiItem(label="Benchmark vs peers", value="-6.1%"),
    ]

    return AnalyticsResponse(
        site_id=site_id,
        site_name=site_name,
        period=period,
        kpis=kpis,
        trends=_demo_trend_series(period=period, days=28),
        baseline_vs_actual=baseline_series,
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
    return AnomaliesResponse(site_id=site_id, threshold_pct=threshold_pct, items=_demo_anomalies(site_id, threshold_pct))


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
    return BenchmarkResponse(
        site_id=site_id,
        category="Logistics (demo)",
        period=period,
        series=_demo_benchmark(period=period, days=28),
    )


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
    return PortfolioResponse(items=_demo_portfolio())


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
    items = _demo_alerts()
    if status:
        items = [a for a in items if a.status == status]
    return AlertsListResponse(items=items)


def _generate_export_csv(site_id: str, start_date: date, end_date: date) -> str:
    """Generate a simple CSV export using demo baseline_vs_actual series."""
    threshold_pct = 20.0
    series = _demo_baseline_vs_actual(threshold_pct=threshold_pct, days=60)
    rows = [p for p in series if start_date <= p.ts <= end_date]

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "actual_kwh", "baseline_kwh", "deviation_pct", "is_anomaly"])
    for p in rows:
        writer.writerow([p.ts.isoformat(), p.actual_kwh, p.baseline_kwh, p.deviation_pct, p.is_anomaly])
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
