from pathlib import Path

from magsearch.settings import Settings


def test_settings_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.database_url == "sqlite:///./data/magsearch.db"
    assert s.bundles_dir == Path("./data/bundles")


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", "sqlite:////tmp/x.db")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", "/var/bundles")
    s = Settings()
    assert s.database_url == "sqlite:////tmp/x.db"
    assert s.bundles_dir == Path("/var/bundles")


def test_settings_max_upload_bytes_default(tmp_path, monkeypatch):
    # Settings reads ".env" from CWD as well as the process environment.
    # Mirror test_settings_defaults and chdir into an empty tmp dir so a
    # repo-local or CI .env can't leak in and override the default.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAGSEARCH_MAX_UPLOAD_BYTES", raising=False)
    from magsearch.settings import Settings
    s = Settings()
    assert s.max_upload_bytes == 2 * 1024 * 1024 * 1024  # 2 GB


def test_settings_max_upload_bytes_override(monkeypatch):
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "1048576")  # 1 MB
    from magsearch.settings import Settings
    s = Settings()
    assert s.max_upload_bytes == 1048576
