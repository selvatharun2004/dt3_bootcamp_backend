"""init schema (aligned to 001_init_schema.sql)

Revision ID: 0001_init_schema
Revises:
Create Date: 2026-05-29

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_init_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extension used by schema for gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # Enums
    anomaly_severity = postgresql.ENUM("medium", "high", name="anomaly_severity")
    alert_status = postgresql.ENUM("open", "acknowledged", "resolved", name="alert_status")
    export_format = postgresql.ENUM("csv", "pdf", name="export_format")
    export_status = postgresql.ENUM("queued", "running", "complete", "failed", name="export_status")

    anomaly_severity.create(op.get_bind(), checkfirst=True)
    alert_status.create(op.get_bind(), checkfirst=True)
    export_format.create(op.get_bind(), checkfirst=True)
    export_status.create(op.get_bind(), checkfirst=True)

    # schema_migrations (legacy)
    op.create_table(
        "schema_migrations",
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # customers
    op.create_table(
        "customers",
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("business_category", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", name="customers_name_uniq"),
    )

    # sites
    op.create_table(
        "sites",
        sa.Column("site_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.customer_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("business_category", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("customer_id", "name", name="sites_customer_name_uniq"),
    )

    # meters
    op.create_table(
        "meters",
        sa.Column("meter_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_meter_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("site_id", "external_meter_ref", name="meters_site_external_ref_uniq"),
    )

    # ingestion_uploads
    op.create_table(
        "ingestion_uploads",
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("file_sha256", sa.Text(), nullable=True),
        sa.Column("rows_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_sample", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # meter_readings
    op.create_table(
        "meter_readings",
        sa.Column("reading_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("meter_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("meters.meter_id", ondelete="SET NULL"), nullable=True),
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ingestion_uploads.upload_id", ondelete="SET NULL"), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kwh", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kwh >= 0", name="meter_readings_kwh_chk"),
    )
    op.create_index("meter_readings_site_ts_idx", "meter_readings", ["site_id", "ts"], unique=False)
    # Unique index from reference schema:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS meter_readings_site_meter_ts_uniq
          ON meter_readings(site_id, COALESCE(meter_id, '00000000-0000-0000-0000-000000000000'::uuid), ts);
        """.strip()
    )

    # daily_site_baselines
    op.create_table(
        "daily_site_baselines",
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("actual_kwh", sa.Float(), nullable=False),
        sa.Column("baseline_kwh", sa.Float(), nullable=False),
        sa.Column("deviation_pct", sa.Float(), nullable=False),
        sa.Column("threshold_pct", sa.Float(), nullable=False, server_default="20.0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("actual_kwh >= 0", name="daily_site_baselines_actual_kwh_chk"),
        sa.CheckConstraint("baseline_kwh >= 0", name="daily_site_baselines_baseline_kwh_chk"),
    )
    op.create_index("daily_site_baselines_day_idx", "daily_site_baselines", ["day"], unique=False)

    # anomalies
    op.create_table(
        "anomalies",
        sa.Column("anomaly_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("deviation_pct", sa.Float(), nullable=False),
        sa.Column("threshold_pct", sa.Float(), nullable=False, server_default="20.0"),
        sa.Column("severity", anomaly_severity, nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("baseline_kwh", sa.Float(), nullable=True),
        sa.Column("actual_kwh", sa.Float(), nullable=True),
        sa.UniqueConstraint("site_id", "day", name="anomalies_site_day_uniq"),
    )
    op.create_index("anomalies_site_day_idx", "anomalies", ["site_id", "day"], unique=False)
    op.create_index("anomalies_detected_at_idx", "anomalies", ["detected_at"], unique=False)

    # alerts
    op.create_table(
        "alerts",
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("anomaly_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("anomalies.anomaly_id", ondelete="SET NULL"), nullable=True),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("deviation_pct", sa.Float(), nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=False),
        sa.Column("severity", anomaly_severity, nullable=False),
        sa.Column("status", alert_status, nullable=False, server_default=sa.text("'open'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("site_id", "day", name="alerts_site_day_uniq"),
    )
    op.create_index("alerts_status_created_at_idx", "alerts", ["status", sa.text("created_at DESC")], unique=False)
    op.create_index("alerts_site_day_idx", "alerts", ["site_id", "day"], unique=False)

    # category_benchmarks_daily
    op.create_table(
        "category_benchmarks_daily",
        sa.Column("business_category", sa.Text(), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("peer_avg_kwh", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("peer_avg_kwh >= 0", name="category_benchmarks_daily_peer_avg_kwh_chk"),
    )
    op.create_index("category_benchmarks_daily_day_idx", "category_benchmarks_daily", ["day"], unique=False)

    # export_jobs
    op.create_table(
        "export_jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.site_id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("format", export_format, nullable=False),
        sa.Column("status", export_status, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("end_date >= start_date", name="export_jobs_date_range_chk"),
    )
    op.create_index("export_jobs_site_created_at_idx", "export_jobs", ["site_id", sa.text("created_at DESC")], unique=False)
    op.create_index("export_jobs_status_created_at_idx", "export_jobs", ["status", sa.text("created_at DESC")], unique=False)

    # export_artifacts
    op.create_table(
        "export_artifacts",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("export_jobs.job_id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_backend", sa.Text(), nullable=False, server_default=sa.text("'local'")),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="export_artifacts_byte_size_chk"),
        sa.UniqueConstraint("job_id", "storage_key", name="export_artifacts_job_key_uniq"),
    )


def downgrade() -> None:
    # Drop tables (reverse-ish order)
    op.drop_table("export_artifacts")
    op.drop_table("export_jobs")
    op.drop_index("category_benchmarks_daily_day_idx", table_name="category_benchmarks_daily")
    op.drop_table("category_benchmarks_daily")
    op.drop_index("alerts_site_day_idx", table_name="alerts")
    op.drop_index("alerts_status_created_at_idx", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("anomalies_detected_at_idx", table_name="anomalies")
    op.drop_index("anomalies_site_day_idx", table_name="anomalies")
    op.drop_table("anomalies")
    op.drop_index("daily_site_baselines_day_idx", table_name="daily_site_baselines")
    op.drop_table("daily_site_baselines")
    op.execute("DROP INDEX IF EXISTS meter_readings_site_meter_ts_uniq;")
    op.drop_index("meter_readings_site_ts_idx", table_name="meter_readings")
    op.drop_table("meter_readings")
    op.drop_table("ingestion_uploads")
    op.drop_table("meters")
    op.drop_table("sites")
    op.drop_table("customers")
    op.drop_table("schema_migrations")

    # Drop enums (checkfirst to avoid errors if already removed)
    bind = op.get_bind()
    postgresql.ENUM(name="export_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="export_format").drop(bind, checkfirst=True)
    postgresql.ENUM(name="alert_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="anomaly_severity").drop(bind, checkfirst=True)
