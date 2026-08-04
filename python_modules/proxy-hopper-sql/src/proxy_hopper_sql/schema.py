"""SQLAlchemy Core schema — single source of truth for both the runtime
ConfigStore queries and the Alembic migration that creates the table.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, MetaData, String, Table

metadata = MetaData()

config_entities = Table(
    "config_entities",
    metadata,
    Column("entity_type", String, primary_key=True),
    Column("name", String, primary_key=True),
    Column("data", JSON, nullable=False),
    Column("static", Boolean, nullable=False),
    Column("mutable", Boolean, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)
