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
