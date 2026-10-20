"""
FastAPI Application Entry Point.

This module sets up the FastAPI application with proper database integration,
CORS middleware, and API routing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from src.api.routers import ops
from src.shared.config import get_settings
from src.shared.db.dependencies import AppSettings, DbSession


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.

    Handles startup and shutdown events for the FastAPI application.
    On startup, it initializes the database tables if they don't exist.
    """
    # El esquema lo crea la migracion, no el API: `init_db()` aqui lo
    # construiria saltandose Alembic, y con el rol de solo lectura al que
    # este servicio debe correr ni siquiera tendria permiso.
    settings = get_settings()
    print(f"API arriba contra {settings.POSTGRES_DB}")

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

    app.include_router(ops.router, prefix=settings.API_PREFIX)

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
            "engine": "postgresql",
            "database_url": settings.DATABASE_URL.split("@")[-1]
            if "@" in settings.DATABASE_URL
            else settings.DATABASE_URL,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "engine": "postgresql",
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
        "engine": "postgresql",
        "environment": "development",  # Add environment variable for this
    }


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.LOG_LEVEL.lower(),
    )
