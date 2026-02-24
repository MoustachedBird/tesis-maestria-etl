"""
Database engine and session management.

This module provides database connectivity abstraction for both PostgreSQL and DuckDB.
It follows FastAPI best practices for database session management.
"""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src_etl.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """
    Get or create the SQLAlchemy engine based on configuration.

    This function is cached to ensure only one engine instance exists.
    The engine is configured differently based on the database backend:

    - PostgreSQL: Uses connection pooling with configurable pool size
    - DuckDB: Uses NullPool (no pooling) as DuckDB is file-based

    Returns:
        Engine: SQLAlchemy engine instance

    Example:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
    """
    settings = get_settings()

    # Common engine arguments
    engine_kwargs = {
        "echo": settings.SQLALCHEMY_ECHO,
        "future": True,  # Use SQLAlchemy 2.0 style
    }

    # Backend-specific configuration
    if settings.DATABASE_BACKEND == "postgresql":
        engine_kwargs.update(
            {
                "pool_size": settings.SQLALCHEMY_POOL_SIZE,
                "max_overflow": settings.SQLALCHEMY_MAX_OVERFLOW,
                "pool_pre_ping": settings.SQLALCHEMY_POOL_PRE_PING,
            }
        )

    elif settings.DATABASE_BACKEND == "duckdb":
        # DuckDB is file-based, so we disable pooling
        engine_kwargs.update(
            {
                "poolclass": NullPool,
                "connect_args": {
                    "read_only": False,
                },
            }
        )

        # For DuckDB, we need to set some config options
        # We'll do this via event listeners after engine creation

    # Create engine
    engine = create_engine(settings.DATABASE_URL, **engine_kwargs)

    # Configure DuckDB-specific settings
    if settings.DATABASE_BACKEND == "duckdb":

        @event.listens_for(engine, "connect")
        def configure_duckdb(dbapi_conn, connection_record):
            """Configure DuckDB connection with performance settings."""
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET threads TO {settings.DUCKDB_THREADS}")
            cursor.execute(f"SET memory_limit = '{settings.DUCKDB_MEMORY_LIMIT}'")
            cursor.close()

    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """
    Get or create the SQLAlchemy session factory.

    This function is cached to ensure only one session factory exists.
    The session factory is used to create new database sessions.

    Returns:
        sessionmaker: SQLAlchemy session factory

    Example:
        SessionLocal = get_session_factory()
        session = SessionLocal()
        try:
            # Do database operations
            session.commit()
        finally:
            session.close()
    """
    engine = get_engine()

    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,  # Don't expire objects after commit
        future=True,  # Use SQLAlchemy 2.0 style
    )


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency for database sessions.

    This is a generator function that yields a database session and ensures
    it's properly closed after use. Use this as a FastAPI dependency.

    Yields:
        Session: SQLAlchemy database session

    Example:
        @app.get("/items/")
        def read_items(db: Session = Depends(get_db)):
            items = db.query(Item).all()
            return items
    """
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Initialize the database.

    This creates all tables defined in the Base metadata if they don't exist.
    For production, you should use Alembic migrations instead.

    Example:
        # In your application startup
        init_db()
    """
    from src_etl.db.base import Base

    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def drop_db() -> None:
    """
    Drop all database tables.

    WARNING: This will delete all data! Only use in development/testing.

    Example:
        # In tests
        drop_db()
        init_db()
    """
    from src_etl.db.base import Base

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)


# For backward compatibility with existing code
BaseWarehouse = get_db
