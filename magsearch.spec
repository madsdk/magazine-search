# PyInstaller spec for magsearch desktop. Build with:
#   pyinstaller magsearch.spec
# Produces a bundled app under dist/magsearch-desktop/.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
repo = Path(SPECPATH)

datas = [
    (str(repo / "alembic.ini"), "."),
    (str(repo / "alembic"), "alembic"),
    (str(repo / "src" / "magsearch" / "web" / "templates"),
     "magsearch/web/templates"),
    (str(repo / "src" / "magsearch" / "web" / "static"),
     "magsearch/web/static"),
]
datas += collect_data_files("magsearch", includes=["**/*.html"])
datas += collect_data_files(
    "magsearch", includes=["**/static/*", "**/static/**/*"]
)

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("alembic")
hiddenimports += [
    "magsearch.web.app",
    "magsearch.web.routes",
    "magsearch.web.routes_admin",
    "magsearch.web.routes_auth",
    "magsearch.web.routes_import",
]

a = Analysis(
    [str(repo / "src" / "magsearch" / "desktop.py")],
    pathex=[str(repo / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["paddleocr", "paddlepaddle", "pymupdf", "rarfile"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="magsearch-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="magsearch-desktop",
)
