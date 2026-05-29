"""
Analytics and ingestion helpers for the Energy Analytics backend.

This module contains:
- CSV parsing/validation helpers for ingestion
- Daily aggregation + 4-week rolling baseline computation
- Anomaly detection + suggested action heuristics
- Convenience helpers to resolve "frontend site_id" strings to real DB Site rows

The FastAPI routes in main.py use these functions to:
- persist MeterReading rows on ingest
- compute DailySiteBaseline rows
- create Anomaly + Alert rows
- serve dashboard responses from real persisted data

Design notes:
- We keep logic in a separate module so main.py stays focused on HTTP and schemas.
- We intentionally avoid heavy dependencies (e.g., pandas) to keep the stack small.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from io import StringIO
from typing import Iterable, List, Optional, Sequence, Tuple

import csv
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models


@dataclass(frozen=True)
class ParsedReading:
    """Validated reading parsed from CSV."""
    ts: datetime
    kwh: float


def _ensure_utc(ts: datetime) -> datetime:
    """
    Normalize a datetime to timezone-aware UTC.

    If the timestamp is naive, assume it is UTC.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _day_bucket(ts: datetime) -> date:
    """Convert a timestamp to a UTC day bucket."""
    return _ensure_utc(ts).date()


def utc_today() -> date:
    """Return today's date in UTC."""
    return datetime.now(timezone.utc).date()


def parse_readings_csv(content: str) -> Tuple[List[ParsedReading], int, int, List[str]]:
    """
    Parse a meter readings CSV content string.

    Expected header columns (case-insensitive):
    - timestamp
    - kWh

    Returns:
        (accepted_readings, rows_received, rows_rejected, error_sample)
    """
    reader = csv.DictReader(StringIO(content))
    if reader.fieldnames is None:
        return ([], 0, 0, ["CSV must include a header row with 'timestamp' and 'kWh' columns."])

    normalized = {h.strip().lower(): h for h in reader.fieldnames if h}
    if "timestamp" not in normalized or "kwh" not in normalized:
        return (
            [],
            0,
            0,
            [f"Expected columns: timestamp, kWh. Got: {', '.join(reader.fieldnames)}"],
        )

    ts_key = normalized["timestamp"]
    kwh_key = normalized["kwh"]

    rows_received = 0
    rows_rejected = 0
    errors: List[str] = []
    accepted: List[ParsedReading] = []

    for row in reader:
        rows_received += 1
        raw_ts = (row.get(ts_key) or "").strip()
        raw_kwh = (row.get(kwh_key) or "").strip()

        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            ts = _ensure_utc(ts)
        except Exception:
            rows_rejected += 1
            if len(errors) < 10:
                errors.append(f"Row {rows_received}: invalid timestamp '{raw_ts}'")
            continue

        try:
            kwh_val = float(raw_kwh)
            if kwh_val < 0:
                raise ValueError("kWh must be >= 0")
        except Exception:
            rows_rejected += 1
            if len(errors) < 10:
                errors.append(f"Row {rows_received}: invalid kWh '{raw_kwh}'")
            continue

        accepted.append(ParsedReading(ts=ts, kwh=kwh_val))

    return accepted, rows_received, rows_rejected, errors


def sha256_hex(data: bytes) -> str:
    """Compute SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def resolve_or_seed_site(session: Session, external_site_id: str) -> models.Site:
    """
    Resolve a frontend-facing site_id string to a real Site row.

    The React app currently passes values like 'site_main' and expects a name mapping.
    This function:
    - tries to find a Site whose name matches the mapping for that external id
    - if none exists, seeds a Customer+Site record to keep the system usable

    This keeps endpoint contracts stable while enabling real persistence/analytics.
    """
    mapping = {
        "site_main": ("Demo Customer", "Main Warehouse", "Logistics"),
        "site_wa": ("Demo Customer", "Warehouse A", "Logistics"),
        "site_store12": ("Bright Retail", "Store 12", "Retail"),
        "site_plant3": ("Northside Manufacturing", "Plant 3", "Manufacturing"),
    }
    customer_name, site_name, category = mapping.get(external_site_id, ("Demo Customer", "Main Warehouse", "Logistics"))

    # Prefer exact site name match.
    site = session.execute(select(models.Site).where(models.Site.name == site_name)).scalar_one_or_none()
    if site:
        return site

    customer = session.execute(select(models.Customer).where(models.Customer.name == customer_name)).scalar_one_or_none()
    if not customer:
        customer = models.Customer(name=customer_name, business_category=category)
        session.add(customer)
        session.flush()

    site = models.Site(
        customer_id=customer.customer_id,
        name=site_name,
        timezone="UTC",
        business_category=category,
    )
    session.add(site)
    session.flush()
    return site


def get_site_display_name(session: Session, site: models.Site) -> str:
    """Return a stable site display name."""
    return site.name


def daily_totals_for_site(
    session: Session,
    site_id: models.Site,
    start_day: date,
    end_day: date,
) -> List[Tuple[date, float]]:
    """
    Compute daily kWh totals for a site between start_day and end_day inclusive.

    We bucket by UTC day (date_trunc('day', ts)).
    """
    if end_day < start_day:
        return []

    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    q = (
        select(
            func.date_trunc("day", models.MeterReading.ts).label("day_ts"),
            func.sum(models.MeterReading.kwh).label("kwh_sum"),
        )
        .where(models.MeterReading.site_id == site_id.site_id)
        .where(models.MeterReading.ts >= start_dt)
        .where(models.MeterReading.ts < end_dt)
        .group_by("day_ts")
        .order_by("day_ts")
    )

    rows = session.execute(q).all()
    out: List[Tuple[date, float]] = []
    for day_ts, kwh_sum in rows:
        # day_ts comes back as a datetime in UTC
        out.append((_ensure_utc(day_ts).date(), float(kwh_sum or 0.0)))
    return out


def compute_rolling_4w_baseline(
    day: date,
    actual_kwh: float,
    history: Sequence[Tuple[date, float]],
    window_days: int = 28,
) -> float:
    """
    Compute a rolling baseline for `day` from prior history.

    Baseline definition (pragmatic):
    - average of actual totals over the prior `window_days` days (exclusive of `day`)
    - if there is no prior data, baseline defaults to actual_kwh (deviation 0)
    """
    start = day - timedelta(days=window_days)
    vals = [kwh for d, kwh in history if start <= d < day]
    if not vals:
        return float(actual_kwh)
    return float(sum(vals) / len(vals))


def deviation_pct(actual_kwh: float, baseline_kwh: float) -> float:
    """Compute deviation percent (actual-baseline)/baseline*100, safe for baseline=0."""
    if baseline_kwh <= 0:
        return 0.0 if actual_kwh <= 0 else 100.0
    return ((actual_kwh - baseline_kwh) / baseline_kwh) * 100.0


def anomaly_severity_for(deviation: float, threshold: float) -> str:
    """Map deviation to severity buckets compatible with DB enum."""
    return "high" if deviation >= (threshold + 10.0) else "medium"


def suggested_action_for(deviation: float) -> str:
    """
    Provide a deterministic suggested action string.

    Kept simple but stable for UI display.
    """
    if deviation >= 40:
        return "Schedule customer check-in"
    if deviation >= 30:
        return "Investigate refrigeration/HVAC load"
    if deviation >= 25:
        return "Inspect overnight load"
    return "Verify operational changes"


def upsert_daily_baseline_and_anomaly(
    session: Session,
    site: models.Site,
    day: date,
    actual_kwh: float,
    baseline_kwh: float,
    threshold_pct: float,
) -> None:
    """
    Upsert daily baseline row, and (re)create anomaly+alert if threshold exceeded.

    Notes:
    - We store the baseline for transparency and for quick dashboard reads.
    - We keep anomalies unique per (site, day) via DB constraint.
    - We also create an Alert row unique per (site, day).
    """
    dev = deviation_pct(actual_kwh=actual_kwh, baseline_kwh=baseline_kwh)

    baseline_row = session.get(models.DailySiteBaseline, {"site_id": site.site_id, "day": day})
    if baseline_row is None:
        baseline_row = models.DailySiteBaseline(
            site_id=site.site_id,
            day=day,
            actual_kwh=actual_kwh,
            baseline_kwh=baseline_kwh,
            deviation_pct=dev,
            threshold_pct=threshold_pct,
        )
        session.add(baseline_row)
    else:
        baseline_row.actual_kwh = actual_kwh
        baseline_row.baseline_kwh = baseline_kwh
        baseline_row.deviation_pct = dev
        baseline_row.threshold_pct = threshold_pct

    # If not an anomaly, ensure any existing anomaly/alert is removed (keeps results consistent after re-ingest).
    if dev <= threshold_pct:
        existing_anom = session.execute(
            select(models.Anomaly).where(models.Anomaly.site_id == site.site_id, models.Anomaly.day == day)
        ).scalar_one_or_none()
        if existing_anom is not None:
            session.delete(existing_anom)

        existing_alert = session.execute(
            select(models.Alert).where(models.Alert.site_id == site.site_id, models.Alert.day == day)
        ).scalar_one_or_none()
        if existing_alert is not None:
            session.delete(existing_alert)
        return

    severity = anomaly_severity_for(dev, threshold_pct)
    action = suggested_action_for(dev)

    anomaly = session.execute(
        select(models.Anomaly).where(models.Anomaly.site_id == site.site_id, models.Anomaly.day == day)
    ).scalar_one_or_none()
    if anomaly is None:
        anomaly = models.Anomaly(
            site_id=site.site_id,
            day=day,
            deviation_pct=dev,
            threshold_pct=threshold_pct,
            severity=severity,
            suggested_action=action,
            baseline_kwh=baseline_kwh,
            actual_kwh=actual_kwh,
        )
        session.add(anomaly)
        session.flush()
    else:
        anomaly.deviation_pct = dev
        anomaly.threshold_pct = threshold_pct
        anomaly.severity = severity
        anomaly.suggested_action = action
        anomaly.baseline_kwh = baseline_kwh
        anomaly.actual_kwh = actual_kwh

    alert = session.execute(
        select(models.Alert).where(models.Alert.site_id == site.site_id, models.Alert.day == day)
    ).scalar_one_or_none()
    if alert is None:
        alert = models.Alert(
            anomaly_id=anomaly.anomaly_id,
            site_id=site.site_id,
            day=day,
            deviation_pct=dev,
            suggested_action=action,
            severity=severity,
            status="open",
        )
        session.add(alert)
    else:
        alert.anomaly_id = anomaly.anomaly_id
        alert.deviation_pct = dev
        alert.suggested_action = action
        alert.severity = severity
        # keep existing status as-is (if acknowledged/resolved)


def recompute_site_daily_baselines(
    session: Session,
    site: models.Site,
    threshold_pct: float,
    start_day: date,
    end_day: date,
) -> None:
    """
    Recompute daily baselines/anomalies for a site over a day range (inclusive).

    This is run after ingestion to keep dashboards consistent without needing a separate job runner.
    """
    totals = daily_totals_for_site(session, site, start_day=start_day, end_day=end_day)

    # Build a bigger history window so each day can look back 28 days.
    history_start = start_day - timedelta(days=28)
    history = daily_totals_for_site(session, site, start_day=history_start, end_day=end_day)

    history_map = {d: kwh for d, kwh in history}

    # For each day in [start_day, end_day] where we have actual totals, compute baseline from prior days.
    for day, actual in totals:
        # Baseline computed from prior days present in history.
        prior = sorted([(d, kwh) for d, kwh in history_map.items() if d < day], key=lambda x: x[0])
        baseline = compute_rolling_4w_baseline(day=day, actual_kwh=actual, history=prior, window_days=28)
        upsert_daily_baseline_and_anomaly(
            session=session,
            site=site,
            day=day,
            actual_kwh=actual,
            baseline_kwh=baseline,
            threshold_pct=threshold_pct,
        )


def period_bucket_start(day: date, period: str) -> date:
    """Return the bucket start date for a day given daily/weekly/monthly."""
    if period == "daily":
        return day
    if period == "weekly":
        # Monday as week start, matches previous demo series behavior.
        return day - timedelta(days=day.weekday())
    if period == "monthly":
        return date(day.year, day.month, 1)
    raise ValueError(f"Unknown period: {period}")


def aggregate_daily_to_period(
    daily_points: Sequence[Tuple[date, float]],
    period: str,
) -> List[Tuple[date, float]]:
    """
    Aggregate daily (day,kwh) points into daily/weekly/monthly buckets.
    """
    buckets: dict[date, float] = {}
    for d, kwh in daily_points:
        b = period_bucket_start(d, period)
        buckets[b] = buckets.get(b, 0.0) + float(kwh)
    return sorted(buckets.items(), key=lambda x: x[0])


def ensure_category_benchmarks(
    session: Session,
    business_category: str,
    days: Sequence[date],
    fallback_peer_multiplier: float = 1.06,
) -> None:
    """
    Ensure CategoryBenchmarkDaily rows exist for a category and set of days.

    Until real peer aggregation is implemented, we create deterministic placeholder peer averages
    derived from actual site usage (multiplied by a constant).
    """
    if not business_category:
        return

    existing = session.execute(
        select(models.CategoryBenchmarkDaily.day).where(models.CategoryBenchmarkDaily.business_category == business_category)
    ).scalars().all()
    existing_set = set(existing)

    for d in days:
        if d in existing_set:
            continue
        # Placeholder peer average; real compute will use aggregate across peers.
        session.add(
            models.CategoryBenchmarkDaily(
                business_category=business_category,
                day=d,
                peer_avg_kwh=0.0,  # will be updated by caller if desired
            )
        )

    # Caller may update peer_avg_kwh; we keep the rows present to support joins.
    session.flush()
