"""admin users and app config

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-16

"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    config_table = op.create_table(
        "app_config",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("value_type", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.bulk_insert(
        config_table,
        [
            {
                "key": "require_login",
                "value": "false",
                "value_type": "bool",
                "updated_at": datetime.utcnow(),
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("app_config")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
