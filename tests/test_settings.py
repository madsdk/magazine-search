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
