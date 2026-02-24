"""
FastAPI Application Entry Point.

This module sets up the FastAPI application with proper database integration,
CORS middleware, and API routing.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from src_etl.core.config import get_settings, Settings
from src_etl.db import get_db, init_db
from src_etl.db.dependencies import DbSession, AppSettings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.

    Handles startup and shutdown events for the FastAPI application.
    On startup, it initializes the database tables if they don't exist.
    """
    # Startup: Initialize database
    settings = get_settings()
    print(f"🚀 Starting API with {settings.DATABASE_BACKEND} backend")
    print(f"📊 Database URL: {settings.DATABASE_URL}")

    # Create tables if they don't exist (for development)
    # In production, use Alembic migrations instead
    try:
        init_db()
        print("✅ Database initialized")
    except Exception as e:
        print(f"⚠️  Database initialization warning: {e}")

    yield

    # Shutdown: Cleanup if needed
    print("👋 Shutting down API")


# Create FastAPI application
def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        FastAPI: Configured FastAPI application instance
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        description="ETL API for DENUE Data Processing",
        docs_url=f"{settings.API_PREFIX}/docs",
        redoc_url=f"{settings.API_PREFIX}/redoc",
        openapi_url=f"{settings.API_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    # CORS middleware
    cors_origins = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    # app.include_router(establecimientos_router, prefix=f"{settings.API_PREFIX}/establecimientos")

    return app


# Create app instance
app = create_app()


# ------------------------------------------------------------------------------
# Health Check Endpoints
# ------------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns basic health status of the API.
    """
    return {
        "status": "healthy",
        "service": "DENUE ETL API",
    }


@app.get(f"{get_settings().API_PREFIX}/health/db")
async def database_health_check(db: DbSession):
    """
    Database health check endpoint.

    Verifies database connectivity and returns backend information.
    """
    settings = get_settings()

    try:
        # Simple query to check database connection
        db.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "backend": settings.DATABASE_BACKEND,
            "database_url": settings.DATABASE_URL.split("@")[-1]
            if "@" in settings.DATABASE_URL
            else settings.DATABASE_URL,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "backend": settings.DATABASE_BACKEND,
        }


@app.get(f"{get_settings().API_PREFIX}/info")
async def api_info(settings: AppSettings):
    """
    API information endpoint.

    Returns configuration information about the API.
    """
    return {
        "title": settings.API_TITLE,
        "version": settings.API_VERSION,
        "database_backend": settings.DATABASE_BACKEND,
        "environment": "development",  # Add environment variable for this
    }


# ------------------------------------------------------------------------------
# Example CRUD Endpoints (commented out - implement as needed)
# ------------------------------------------------------------------------------
# from src_etl.db.models import Establecimiento
# from src_api.schemas import EstablecimientoResponse
# from typing import List

# @app.get(f"{get_settings().API_PREFIX}/establecimientos", response_model=List[EstablecimientoResponse])
# async def list_establecimientos(
#     db: DbSession,
#     skip: int = 0,
#     limit: int = 100,
# ):
#     """List establecimientos with pagination."""
#     establecimientos = db.query(Establecimiento).offset(skip).limit(limit).all()
#     return establecimientos


# @app.get(f"{get_settings().API_PREFIX}/establecimientos/{{est_id}}", response_model=EstablecimientoResponse)
# async def get_establecimiento(est_id: str, db: DbSession):
#     """Get a single establecimiento by id."""
#     establecimiento = db.query(Establecimiento).filter(Establecimiento.id == est_id).first()
#     if not establecimiento:
#         raise HTTPException(status_code=404, detail="Establecimiento not found")
#     return establecimiento


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src_api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower(),
    )
