"""
Database wiring for the FastAPI Backend API.

Uses SQLAlchemy 2.0 style engine + sessionmaker and reads configuration from
environment variables. This module is intentionally small and framework-agnostic
so it can be imported by both the FastAPI app and Alembic.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class DbSettings:
    """Database settings loaded from environment variables."""

    url: str
    echo: bool = False
    pool_pre_ping: bool = True


def _env_bool(name: str, default: str = "false") -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def load_db_settings() -> Optional[DbSettings]:
    """
    Load DB settings from env.

    Returns None if DATABASE_URL is not set, allowing the app to run in
    "no-db / placeholder" mode (useful during early scaffold).
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return None

    return DbSettings(
        url=url,
        echo=_env_bool("SQLALCHEMY_ECHO", "false"),
        pool_pre_ping=_env_bool("SQLALCHEMY_POOL_PRE_PING", "true"),
    )


def create_db_engine(settings: DbSettings) -> Engine:
    """Create a SQLAlchemy Engine for PostgreSQL."""
    return create_engine(
        settings.url,
        echo=settings.echo,
        pool_pre_ping=settings.pool_pre_ping,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a sessionmaker factory bound to the provided engine."""
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


# Lazily initialized singletons (FastAPI app imports should not crash if DB is not configured).
_DB_SETTINGS = load_db_settings()
ENGINE: Optional[Engine] = create_db_engine(_DB_SETTINGS) if _DB_SETTINGS else None
SessionLocal: Optional[sessionmaker[Session]] = create_session_factory(ENGINE) if ENGINE else None


def get_db_session() -> Session:
    """
    Get a new SQLAlchemy session.

    Raises:
        RuntimeError: if DATABASE_URL was not configured.
    """
    if SessionLocal is None:
        raise RuntimeError(
            "Database is not configured. Set DATABASE_URL (e.g., postgresql+psycopg://user:pass@host:5432/dbname)."
        )
    return SessionLocal()
