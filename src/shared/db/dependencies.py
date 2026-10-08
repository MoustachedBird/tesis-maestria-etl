"""
FastAPI Dependencies for Database Operations.

This module provides dependency injection functions for FastAPI routes.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from src.shared.config import Settings, get_settings
from src.shared.db.session import get_db

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
#     return {"db": settings.POSTGRES_DB}
AppSettings = Annotated[Settings, Depends(get_settings)]
