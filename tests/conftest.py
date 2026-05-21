from datetime import date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.models import User
from magsearch.web.app import create_app
from magsearch.web.auth import hash_password
from tests.fixtures.pdfs import make_pdf


def _ingest_two_magazines(bundles_dir: Path, src_dir: Path):
    """Create two distinct test magazines with predictable text."""
    return [
        IngestPipeline(
            bundles_dir,
            FakeOCREngine(responses=[
                [OCRRegion(text="vintage synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
                [OCRRegion(text="modem speeds compared", bbox=(0,0,50,10), confidence=1.0)],
            ]),
            IngestOptions(title="Byte", publisher="McGraw-Hill",
                          publication_date=date(1985, 12, 1)),
        ).run(make_pdf(src_dir / "byte.pdf", num_pages=2)),
        IngestPipeline(
            bundles_dir,
            FakeOCREngine(responses=[
                [OCRRegion(text="apple ii basic", bbox=(0,0,50,10), confidence=1.0)],
            ]),
            IngestOptions(title="Compute", publisher="ABC Publishing",
                          publication_date=date(1984, 6, 1)),
        ).run(make_pdf(src_dir / "compute.pdf", num_pages=1)),
    ]


def _clear_deps_cache() -> None:
    from magsearch.web import deps
    deps._session_factory_for.cache_clear()


def _build_app_client(tmp_path, monkeypatch, *, auth_enabled: bool):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    monkeypatch.setenv("MAGSEARCH_SESSION_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MAGSEARCH_AUTH_ENABLED", "true" if auth_enabled else "false")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)

    results = _ingest_two_magazines(bundles, src)
    with session_scope(factory) as s:
        for r in results:
            import_bundle(r.bundle_dir, s)

    _clear_deps_cache()

    return TestClient(create_app()), bundles


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    return _build_app_client(tmp_path, monkeypatch, auth_enabled=True)


@pytest.fixture
def desktop_app_client(tmp_path, monkeypatch):
    """app_client variant for the desktop/auth-off code path, pre-populated
    with the same two test magazines so view tests can render them."""
    return _build_app_client(tmp_path, monkeypatch, auth_enabled=False)


def create_user(
    db_factory, username: str, password: str = "secret-pw", is_admin: bool = False
) -> int:
    """Create a user directly in the DB. Returns user id."""
    with session_scope(db_factory) as s:
        u = User(
            username=username.lower(),
            password_hash=hash_password(password),
            is_admin=is_admin,
            created_at=datetime.utcnow(),
            last_login_at=None,
        )
        s.add(u)
        s.flush()
        uid = u.id
    return uid


def login(client: TestClient, username: str, password: str = "secret-pw") -> None:
    """Prime CSRF, then submit the login form. Asserts a successful redirect."""
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    # The csrf_token is in the form as a hidden input. Extract it.
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "csrf token not found on login form"
    token = m.group(1)
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login failed: {resp.status_code} {resp.text}"


def get_csrf(client: TestClient) -> str:
    """Fetch any page that renders a form to surface the current session's CSRF token.

    /login is reachable in every state (allow-listed) and always renders csrf_token.
    """
    import re
    resp = client.get("/login")
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "csrf token not found"
    return m.group(1)


@pytest.fixture
def admin_client(app_client):
    """A TestClient already signed in as an admin."""
    client, bundles = app_client
    from magsearch.settings import get_settings
    from magsearch.db import make_engine, make_session_factory

    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    create_user(factory, "alice", "alice-pw", is_admin=True)
    login(client, "alice", "alice-pw")
    return client, bundles


@pytest.fixture
def user_client(app_client):
    """A TestClient signed in as a non-admin user."""
    client, bundles = app_client
    from magsearch.settings import get_settings
    from magsearch.db import make_engine, make_session_factory

    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    create_user(factory, "bob", "bob-pw", is_admin=False)
    login(client, "bob", "bob-pw")
    return client, bundles
