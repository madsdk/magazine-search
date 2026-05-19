"""Desktop entrypoint.

Configures per-OS data dirs, disables auth (so /import is registered and
the admin pages render without a login), applies migrations, starts uvicorn
on a free localhost port in a background thread, and opens a pywebview
window pointed at it.
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from magsearch.desktop_paths import get_app_data_dir


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urlopen(url, timeout=0.5).read()
            return
        except (URLError, ConnectionError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"server at {url} did not come up within {timeout}s")


def _prepare_data_dir() -> Path:
    data_dir = get_app_data_dir()
    bundles_dir = data_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    # Configure the rest of the package via env vars, BEFORE anything imports
    # magsearch.settings. DB url and bundles dir use setdefault so a developer
    # who exports them for testing can still override.
    os.environ.setdefault(
        "MAGSEARCH_DATABASE_URL",
        f"sqlite:///{data_dir / 'magsearch.db'}",
    )
    os.environ.setdefault("MAGSEARCH_BUNDLES_DIR", str(bundles_dir))
    # Auth is forced off, not setdefault: the desktop launcher creates a
    # password-less `local` admin and routes through it. If an inherited
    # MAGSEARCH_AUTH_ENABLED=true leaked in, the login gate would activate
    # and lock the user out of their own app, since the synthesized local
    # user has no usable password.
    os.environ["MAGSEARCH_AUTH_ENABLED"] = "false"
    return data_dir


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from magsearch.settings import get_settings

    settings = get_settings()
    here = Path(__file__).resolve().parent
    # _MEIPASS is PyInstaller's runtime extraction dir; only set when frozen.
    frozen_root = Path(getattr(sys, "_MEIPASS", here))
    candidates = [
        here.parent.parent / "alembic.ini",  # editable install from source
        frozen_root / "alembic.ini",          # PyInstaller bundle
    ]
    cfg_path = next((p for p in candidates if p.is_file()), None)
    if cfg_path is None:
        raise FileNotFoundError(
            f"alembic.ini not found in: {[str(p) for p in candidates]}"
        )
    cfg = Config(str(cfg_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    # Override script_location to an absolute path so it resolves correctly
    # inside a PyInstaller bundle (where cwd != bundle root).
    cfg.set_main_option("script_location", str(cfg_path.parent / "alembic"))
    command.upgrade(cfg, "head")


def _ensure_local_admin() -> None:
    """Pre-create the local admin row so the first request doesn't have to."""
    from magsearch.db import make_engine, make_session_factory, session_scope
    from magsearch.settings import get_settings
    from magsearch.web.auth import ensure_local_admin

    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory) as db:
        ensure_local_admin(db)


def _start_server(port: int) -> threading.Thread:
    def _run():
        import uvicorn
        uvicorn.run(
            "magsearch.web.app:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    thread = threading.Thread(target=_run, name="uvicorn", daemon=True)
    thread.start()
    return thread


def _open_window_or_fallback(url: str) -> None:
    """Open a pywebview window; if no GUI backend is available
    (no Qt on Linux, no Cocoa on a headless Mac, etc.), open the user's
    default browser instead and block so uvicorn keeps serving."""
    try:
        import webview  # pywebview
        from webview.errors import WebViewException
    except ImportError as e:
        print(
            f"[desktop] pywebview not importable ({e}); "
            f"opening {url} in default browser instead.",
            file=sys.stderr,
        )
        _serve_in_browser(url)
        return

    try:
        webview.create_window("Magazine Search", url, width=1200, height=900)
        webview.start()
    except WebViewException as e:
        print(
            f"[desktop] pywebview could not start ({e}); "
            f"opening {url} in default browser instead.",
            file=sys.stderr,
        )
        _serve_in_browser(url)


def _serve_in_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)
    # Block forever so the daemon uvicorn thread stays alive while the
    # user has the browser tab open. Ctrl+C terminates the process.
    threading.Event().wait()


def main() -> None:
    _prepare_data_dir()
    _migrate()
    _ensure_local_admin()
    port = _pick_free_port()
    _start_server(port)
    url = f"http://127.0.0.1:{port}/"
    _wait_for_server(url)
    _open_window_or_fallback(url)


if __name__ == "__main__":
    main()
