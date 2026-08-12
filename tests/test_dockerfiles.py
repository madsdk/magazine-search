"""Regression tests for runtime deps that only bite once an image is running.

Both are build-clean / run-broken failures:

* magsearch.ingest.formats imports fitz (pymupdf) and rarfile at module level,
  and those live only in the optional [ingest] extra. An image that runs a
  plain `pip install .` builds fine and then dies with ModuleNotFoundError the
  first time someone runs `magsearch ingest`.
* rarfile is a pure-Python *wrapper* — it shells out to an external tool to do
  the actual decompression. An image without one of its supported backends
  builds fine and then dies with RarCannotExec on the first CBR.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DOCKERFILES = ["Dockerfile", "Dockerfile.ingest"]

# rarfile probes these, in order, and uses the first whose --version-ish check
# exits 0: unrar, unar, 7z, 7zz, bsdtar (see rarfile.tool_setup).
#
# Only unrar and unar are listed here as acceptable: bsdtar and 7z pass the
# probe but fail on *solid* archives, which are common in the wild —
# `rarfile.RarFile.open()` on a solid RAR5 raises BadRarFile under both. unar
# is the free one (Ubuntu universe / Debian main); unrar is non-free.
SUPPORTED_BACKENDS = {"unrar", "unar"}

# unrar-free provides /usr/bin/unrar via update-alternatives, so it *looks*
# like rarfile's preferred backend — but whether it actually works depends on
# the version the base image happens to ship:
#
#   0.1.3 (ubuntu:24.04, i.e. the CUDA ingest base) — no `p` pipe-to-stdout
#     command, and rarfile's probe `unrar -inul -?` exits 64. rarfile finds no
#     working tool: every CBR dies with RarCannotExec.
#   0.3.1 (current python:3.11-slim) — probe passes and extraction works.
#
# Depending on that coin flip is what broke CBR ingestion, so pin the images to
# a backend rarfile actually targets rather than tracking unrar-free versions.
UNSUPPORTED_BACKENDS = {"unrar-free"}


def apt_packages(text: str) -> set[str]:
    """Package names from `apt-get install` lines, ignoring comments/flags."""
    packages: set[str] = set()
    in_install = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "apt-get install" in stripped:
            in_install = True
            stripped = stripped.split("apt-get install", 1)[1]
        elif not in_install:
            continue
        for token in re.split(r"[\s\\&|]+", stripped):
            # Skip flags, shell operators and paths (`rm -rf /var/lib/apt/...`
            # rides along on the same continued RUN).
            if token and not token.startswith("-") and "/" not in token:
                packages.add(token)
        if not stripped.endswith("\\"):
            in_install = False
    return packages


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_dockerfile_installs_ingest_extra(dockerfile: str):
    text = (ROOT / dockerfile).read_text()
    # A local-package spec is the bare `.` or `.[extra,...]`, optionally quoted.
    local_installs = [
        m.group(1)
        for line in text.splitlines()
        if "pip install" in line and not line.lstrip().startswith("#")
        for m in [re.search(r"[\"']?(\.(?:\[[\w,-]+\])?)[\"']?(?:\s|$)", line)]
        if m
    ]
    assert local_installs, f"{dockerfile} never pip-installs the local package"
    assert any("[ingest]" in spec for spec in local_installs), (
        f"{dockerfile} installs {local_installs} — none include the [ingest] "
        "extra, so fitz/rarfile will be missing at runtime"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_dockerfile_installs_supported_cbr_backend(dockerfile: str):
    packages = apt_packages((ROOT / dockerfile).read_text())
    assert packages & SUPPORTED_BACKENDS, (
        f"{dockerfile} installs no rarfile-supported CBR backend "
        f"(need one of {sorted(SUPPORTED_BACKENDS)}); every CBR ingest will "
        "fail with RarCannotExec: Cannot find working tool"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_dockerfile_does_not_rely_on_unrar_free(dockerfile: str):
    packages = apt_packages((ROOT / dockerfile).read_text())
    assert not packages & UNSUPPORTED_BACKENDS, (
        f"{dockerfile} installs {sorted(packages & UNSUPPORTED_BACKENDS)}, "
        "which rarfile cannot drive — its CLI rejects rarfile's probe and it "
        "has no pipe-to-stdout command"
    )
