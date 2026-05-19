import os

from magsearch.desktop import _prepare_data_dir
from magsearch.desktop_paths import get_app_data_dir


def test_macos_app_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert get_app_data_dir() == tmp_path / "Library" / "Application Support" / "magsearch"


def test_windows_app_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert get_app_data_dir() == tmp_path / "magsearch"


def test_linux_app_data_dir_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert get_app_data_dir() == tmp_path / "magsearch"


def test_linux_app_data_dir_default(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert get_app_data_dir() == tmp_path / ".local" / "share" / "magsearch"


def test_prepare_data_dir_forces_auth_off(monkeypatch, tmp_path):
    """An inherited MAGSEARCH_AUTH_ENABLED=true must NOT leak through —
    the desktop launcher would otherwise activate the login gate and
    lock the user out of their own app."""
    # Use setenv (not delenv) on every var the function touches so monkeypatch
    # tracks them all for restore. delenv(raising=False) on an unset var
    # records nothing, letting `_prepare_data_dir`'s direct assignment leak
    # past teardown and poison sibling tests.
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("MAGSEARCH_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", "")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", "")
    # setdefault treats empty string as "set"; force a re-default by deleting,
    # but only after monkeypatch has captured the empty-string baseline so
    # teardown will still wipe the var.
    os.environ.pop("MAGSEARCH_DATABASE_URL")
    os.environ.pop("MAGSEARCH_BUNDLES_DIR")

    _prepare_data_dir()

    assert os.environ["MAGSEARCH_AUTH_ENABLED"] == "false"


def test_prepare_data_dir_respects_existing_db_url(monkeypatch, tmp_path):
    """DB url and bundles dir remain overridable for developers."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", "sqlite:///custom.db")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(tmp_path / "custom"))
    # setenv (not delenv) so monkeypatch tracks the var for proper teardown —
    # `_prepare_data_dir` overwrites it directly via os.environ assignment,
    # and delenv(raising=False) on an unset var records nothing.
    monkeypatch.setenv("MAGSEARCH_AUTH_ENABLED", "true")

    _prepare_data_dir()

    assert os.environ["MAGSEARCH_DATABASE_URL"] == "sqlite:///custom.db"
    assert os.environ["MAGSEARCH_BUNDLES_DIR"] == str(tmp_path / "custom")
    assert os.environ["MAGSEARCH_AUTH_ENABLED"] == "false"
