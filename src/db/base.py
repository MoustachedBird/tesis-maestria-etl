"""
SQLAlchemy Base and Database Configuration.

This module provides the declarative base for all ORM models
and database engine configuration following FastAPI best practices.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase
from typing import Any


# Consistent naming convention for constraints
# This ensures that constraint names are predictable and follow best practices
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.

    This uses SQLAlchemy 2.0 declarative style with proper metadata configuration.
    All models should inherit from this class.

    Example:
        class MyModel(Base):
            __tablename__ = "my_table"
            id = Column(Integer, primary_key=True)
    """

    metadata = MetaData(naming_convention=naming_convention)

    # Type annotation for proper type checking
    __tablename__: str
