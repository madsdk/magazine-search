"""Tests for the auth_enabled=False (desktop) code path."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory
from magsearch.models import User
from magsearch.web.app import create_app
from magsearch.web.auth import LOCAL_ADMIN_USERNAME


@pytest.fixture
def disabled_auth_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    monkeypatch.setenv("MAGSEARCH_AUTH_ENABLED", "false")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    from magsearch.web import deps
    deps._session_factory_for.cache_clear()

    factory = make_session_factory(make_engine(f"sqlite:///{db_path}"))
    return TestClient(create_app()), factory


def test_admin_pages_reachable_without_login(disabled_auth_client):
    client, _ = disabled_auth_client
    r = client.get("/admin")
    assert r.status_code == 200, r.text
    r2 = client.get("/admin/issues")
    assert r2.status_code == 200, r2.text


def test_local_admin_user_is_auto_created(disabled_auth_client):
    client, factory = disabled_auth_client
    # First request triggers AuthMiddleware → ensure_local_admin.
    client.get("/")
    with factory() as db:
        u = db.scalar(select(User).where(User.username == LOCAL_ADMIN_USERNAME))
        assert u is not None
        assert u.is_admin is True


def test_repeated_requests_reuse_local_admin(disabled_auth_client):
    client, factory = disabled_auth_client
    for _ in range(3):
        client.get("/admin")
    with factory() as db:
        users = db.scalars(
            select(User).where(User.username == LOCAL_ADMIN_USERNAME)
        ).all()
        assert len(users) == 1


def test_landing_renders_with_local_user_pinned(disabled_auth_client):
    client, _ = disabled_auth_client
    r = client.get("/")
    assert r.status_code == 200
    # Header nav still rendered; no login redirect.
    assert b"The Archive" in r.content
