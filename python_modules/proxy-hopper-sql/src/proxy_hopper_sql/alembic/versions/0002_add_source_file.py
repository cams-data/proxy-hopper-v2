"""add source_file to config_entities

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "config_entities",
        sa.Column("source_file", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("config_entities", "source_file")
