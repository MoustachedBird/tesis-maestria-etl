"""
FastAPI Dependencies for Database Operations.

This module provides dependency injection functions for FastAPI routes.
"""

from typing import Annotated
from fastapi import Depends
from sqlalchemy.orm import Session

from src.db.database import get_db
from src.core.config import Settings, get_settings


# Type alias for database dependency
# Use this in your FastAPI route handlers:
# @app.get("/items/")
# def read_items(db: DbSession):
#     return db.query(Item).all()
DbSession = Annotated[Session, Depends(get_db)]

# Type alias for settings dependency
# Use this in your FastAPI route handlers:
# @app.get("/config")
# def get_config(settings: AppSettings):
#     return {"backend": settings.DATABASE_BACKEND}
AppSettings = Annotated[Settings, Depends(get_settings)]
