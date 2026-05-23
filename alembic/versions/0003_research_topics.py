"""research topics

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-23

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_topics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_research_topics_user_id", "research_topics", ["user_id"]
    )

    op.create_table(
        "research_topic_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "topic_id",
            sa.Integer(),
            sa.ForeignKey("research_topics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "page_id",
            sa.Integer(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "topic_id", "page_id", name="uq_research_topic_pages_topic_page"
        ),
    )
    op.create_index(
        "ix_research_topic_pages_topic_id",
        "research_topic_pages",
        ["topic_id"],
    )
    op.create_index(
        "ix_research_topic_pages_page_id",
        "research_topic_pages",
        ["page_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_topic_pages_page_id", table_name="research_topic_pages"
    )
    op.drop_index(
        "ix_research_topic_pages_topic_id", table_name="research_topic_pages"
    )
    op.drop_table("research_topic_pages")
    op.drop_index("ix_research_topics_user_id", table_name="research_topics")
    op.drop_table("research_topics")
