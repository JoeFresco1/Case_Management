"""Shared declarative base re-export and timestamp mixin.

The SQLAlchemy 2 declarative ``Base`` (portable naming convention) lives in
:mod:`app.db`; every model class below subclasses it. This module is imported
first by ``models/__init__.py`` so the shared surface (``Base``,
``TimestampMixin``) resolves before any table class.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

__all__ = ["Base", "TimestampMixin"]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
