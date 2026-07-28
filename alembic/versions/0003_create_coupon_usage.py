# Миграция: счётчики и журнал купонов
"""create coupon usage tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-01-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "coupon_usages",
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column(
            "used_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("coupon_code", sa.String(32), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
        sa.ForeignKeyConstraint(["purchase_id"], ["purchases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_redemptions_code_wallet",
        "coupon_redemptions",
        ["coupon_code", "wallet_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_redemptions_code_wallet",
        table_name="coupon_redemptions",
    )
    op.drop_table("coupon_redemptions")
    op.drop_table("coupon_usages")
