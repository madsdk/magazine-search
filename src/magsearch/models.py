from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    is_admin: Mapped[bool] = mapped_column(default=False)
    is_researcher: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]
    last_login_at: Mapped[datetime | None]


class ConfigEntry(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]
    value_type: Mapped[str]
    updated_at: Mapped[datetime]


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


class ResearchTopic(Base):
    __tablename__ = "research_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str]
    description: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    pages: Mapped[list["ResearchTopicPage"]] = relationship(
        back_populates="topic",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ResearchTopicPage(Base):
    __tablename__ = "research_topic_pages"
    __table_args__ = (UniqueConstraint("topic_id", "page_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("research_topics.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    note: Mapped[str | None]
    saved_at: Mapped[datetime]

    topic: Mapped[ResearchTopic] = relationship(back_populates="pages")
    page: Mapped[Page] = relationship()
