# Миграция: таблица покупок
"""create purchases table

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purchases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("promo_code", sa.String(32), nullable=True),
        sa.Column("subtotal", sa.Numeric(20, 2), nullable=False),
        sa.Column("discount", sa.Numeric(20, 2), nullable=False),
        sa.Column("total", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "refunded_total",
            sa.Numeric(20, 2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_purchases_wallet_id",
        "purchases",
        ["wallet_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_purchases_wallet_id", table_name="purchases")
    op.drop_table("purchases")
