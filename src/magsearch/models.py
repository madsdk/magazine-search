from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Magazine(Base):
    __tablename__ = "magazines"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    issue: Mapped[str | None]
    publication_date: Mapped[date | None]
    publisher: Mapped[str | None]
    original_filename: Mapped[str]
    original_format: Mapped[str]
    page_count: Mapped[int]
    content_hash: Mapped[str]
    cover_path: Mapped[str]
    ocr_engine: Mapped[str]
    ocr_engine_version: Mapped[str]
    ingested_at: Mapped[datetime]

    pages: Mapped[list["Page"]] = relationship(
        back_populates="magazine",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("magazine_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    magazine_id: Mapped[str] = mapped_column(
        ForeignKey("magazines.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int]
    image_path: Mapped[str]
    thumb_path: Mapped[str]
    text: Mapped[str]

    magazine: Mapped[Magazine] = relationship(back_populates="pages")
