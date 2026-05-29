"""
SQLAlchemy ORM models for the Energy Analytics database.

These are aligned to the reference schema in:
dt3_bootcamp_database/PostgreSQLDatabase/scripts/001_init_schema.sql

Note: This file models the current v1 schema; it can evolve as the database
schema evolves via Alembic migrations.
"""

from __future__ import annotations

from datetime import date, datetime
import uuid

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Enums in the schema
AnomalySeverity = Enum("medium", "high", name="anomaly_severity")
AlertStatus = Enum("open", "acknowledged", "resolved", name="alert_status")
ExportFormat = Enum("csv", "pdf", name="export_format")
ExportStatus = Enum("queued", "running", "complete", "failed", name="export_status")


class SchemaMigration(Base):
    """Lightweight schema migration tracking table (legacy; Alembic also tracks migrations)."""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    business_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sites: Mapped[list["Site"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("customer_id", "name", name="sites_customer_name_uniq"),)

    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.customer_id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    business_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="sites")
    meters: Mapped[list["Meter"]] = relationship(back_populates="site", cascade="all, delete-orphan")
    uploads: Mapped[list["IngestionUpload"]] = relationship(back_populates="site", cascade="all, delete-orphan")
    readings: Mapped[list["MeterReading"]] = relationship(back_populates="site", cascade="all, delete-orphan")


class Meter(Base):
    __tablename__ = "meters"
    __table_args__ = (UniqueConstraint("site_id", "external_meter_ref", name="meters_site_external_ref_uniq"),)

    meter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)
    external_meter_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship(back_populates="meters")
    readings: Mapped[list["MeterReading"]] = relationship(back_populates="meter")


class IngestionUpload(Base):
    __tablename__ = "ingestion_uploads"

    upload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)

    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)

    rows_received: Mapped[int] = mapped_column(nullable=False, server_default="0")
    rows_accepted: Mapped[int] = mapped_column(nullable=False, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(nullable=False, server_default="0")
    error_sample: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship(back_populates="uploads")
    readings: Mapped[list["MeterReading"]] = relationship(back_populates="upload")


class MeterReading(Base):
    __tablename__ = "meter_readings"
    __table_args__ = (
        CheckConstraint("kwh >= 0", name="meter_readings_kwh_chk"),
        # Note: original schema has a unique index on COALESCE(meter_id, '000..') + ts.
        # We model a close equivalent uniqueness; Alembic migration preserves the exact index.
        Index("meter_readings_site_ts_idx", "site_id", "ts"),
    )

    reading_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)
    meter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("meters.meter_id", ondelete="SET NULL"), nullable=True)
    upload_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_uploads.upload_id", ondelete="SET NULL"), nullable=True)

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kwh: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship(back_populates="readings")
    meter: Mapped["Meter"] = relationship(back_populates="readings")
    upload: Mapped["IngestionUpload"] = relationship(back_populates="readings")


class DailySiteBaseline(Base):
    __tablename__ = "daily_site_baselines"
    __table_args__ = (
        CheckConstraint("actual_kwh >= 0", name="daily_site_baselines_actual_kwh_chk"),
        CheckConstraint("baseline_kwh >= 0", name="daily_site_baselines_baseline_kwh_chk"),
        Index("daily_site_baselines_day_idx", "day"),
    )

    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    actual_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    deviation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False, server_default="20.0")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        UniqueConstraint("site_id", "day", name="anomalies_site_day_uniq"),
        Index("anomalies_site_day_idx", "site_id", "day"),
        Index("anomalies_detected_at_idx", "detected_at"),
    )

    anomaly_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)

    day: Mapped[date] = mapped_column(Date, nullable=False)
    deviation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False, server_default="20.0")
    severity: Mapped[str] = mapped_column(AnomalySeverity, nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    baseline_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("site_id", "day", name="alerts_site_day_uniq"),
        Index("alerts_status_created_at_idx", "status", "created_at"),
        Index("alerts_site_day_idx", "site_id", "day"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    anomaly_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("anomalies.anomaly_id", ondelete="SET NULL"), nullable=True)
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)

    day: Mapped[date] = mapped_column(Date, nullable=False)
    deviation_pct: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(AnomalySeverity, nullable=False)

    status: Mapped[str] = mapped_column(AlertStatus, nullable=False, server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CategoryBenchmarkDaily(Base):
    __tablename__ = "category_benchmarks_daily"
    __table_args__ = (Index("category_benchmarks_daily_day_idx", "day"),)

    business_category: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    peer_avg_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="export_jobs_date_range_chk"),
        Index("export_jobs_site_created_at_idx", "site_id", "created_at"),
        Index("export_jobs_status_created_at_idx", "status", "created_at"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False)

    requested_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    format: Mapped[str] = mapped_column(ExportFormat, nullable=False)
    status: Mapped[str] = mapped_column(ExportStatus, nullable=False, server_default="queued")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    artifacts: Mapped[list["ExportArtifact"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "storage_key", name="export_artifacts_job_key_uniq"),)

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("export_jobs.job_id", ondelete="CASCADE"), nullable=False)

    storage_backend: Mapped[str] = mapped_column(Text, nullable=False, server_default="local")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int | None] = mapped_column(nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job: Mapped["ExportJob"] = relationship(back_populates="artifacts")
