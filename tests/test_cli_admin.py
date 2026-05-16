from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from typer.testing import CliRunner

from magsearch.cli import app
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import User
from magsearch.web.auth import verify_password

runner = CliRunner()


def _setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(tmp_path / "bundles"))
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return db_path


def _factory(db_path):
    return make_session_factory(make_engine(f"sqlite:///{db_path}"))


def test_admin_create_makes_admin_user(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)

    result = runner.invoke(
        app, ["admin", "create", "alice"], input="strong-pw\nstrong-pw\n"
    )
    assert result.exit_code == 0, result.output

    with session_scope(_factory(db_path)) as s:
        u = s.scalar(select(User).where(User.username == "alice"))
        assert u is not None
        assert u.is_admin is True
        assert verify_password("strong-pw", u.password_hash)


def test_admin_create_rejects_duplicate(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)

    runner.invoke(app, ["admin", "create", "alice"], input="pw1\npw1\n")
    result = runner.invoke(app, ["admin", "create", "alice"], input="pw2\npw2\n")
    assert result.exit_code == 1


def test_admin_list_shows_users(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    runner.invoke(app, ["admin", "create", "alice"], input="alice-pw\nalice-pw\n")

    result = runner.invoke(app, ["admin", "list"])
    assert result.exit_code == 0
    assert "alice" in result.output
    assert "admin" in result.output


def test_admin_reset_password(tmp_path, monkeypatch):
    db_path = _setup_db(tmp_path, monkeypatch)
    runner.invoke(app, ["admin", "create", "alice"], input="old-pw\nold-pw\n")

    result = runner.invoke(
        app, ["admin", "reset-password", "alice"], input="new-pw\nnew-pw\n"
    )
    assert result.exit_code == 0

    with session_scope(_factory(db_path)) as s:
        u = s.scalar(select(User).where(User.username == "alice"))
        assert verify_password("new-pw", u.password_hash)
        assert not verify_password("old-pw", u.password_hash)
