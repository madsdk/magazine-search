"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-11

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "magazines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("issue", sa.String(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("publisher", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("original_format", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("cover_path", sa.String(), nullable=False),
        sa.Column("ocr_engine", sa.String(), nullable=False),
        sa.Column("ocr_engine_version", sa.String(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "magazine_id",
            sa.String(),
            sa.ForeignKey("magazines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(), nullable=False),
        sa.Column("thumb_path", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("magazine_id", "page_number", name="uq_pages_mag_page"),
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            text,
            content='pages',
            content_rowid='id',
            tokenize = 'porter unicode61 remove_diacritics 2'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, text) VALUES('delete', old.id, old.text);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, text) VALUES('delete', old.id, old.text);
            INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pages_au")
    op.execute("DROP TRIGGER IF EXISTS pages_ad")
    op.execute("DROP TRIGGER IF EXISTS pages_ai")
    op.execute("DROP TABLE IF EXISTS pages_fts")
    op.drop_table("pages")
    op.drop_table("magazines")
