import json
from pathlib import Path

import pytest

from magsearch.bundle_edit import apply_drop, plan_drop
from magsearch.checksums import verify
from magsearch.manifest import Manifest
from tests.fixtures.bundles import make_bundle


def _snapshot(bundle: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(bundle)): p.read_bytes()
        for p in sorted(bundle.rglob("*"))
        if p.is_file()
    }


def test_apply_renumbers_pages_and_drops_the_first(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.page_count == 2
    assert [p.page_number for p in manifest.pages] == [1, 2]
    assert [p.image_path for p in manifest.pages] == ["pages/0001.webp", "pages/0002.webp"]
    assert [p.thumb_path for p in manifest.pages] == ["thumbs/0001.webp", "thumbs/0002.webp"]
    assert [p.ocr_path for p in manifest.pages] == ["ocr/0001.json", "ocr/0002.json"]
    # New page 1 is the old page 2, byte for byte.
    assert (bundle / "pages/0001.webp").read_bytes() == before["pages/0002.webp"]
    assert (bundle / "ocr/0001.json").read_bytes() == before["ocr/0002.json"]
    assert not (bundle / "pages/0003.webp").exists()


def test_apply_drops_the_old_page_one_files(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    remaining = set(_snapshot(bundle).values())
    assert before["pages/0001.webp"] not in remaining
    assert before["ocr/0001.json"] not in remaining


def test_apply_rebuilds_the_cover_from_the_new_first_thumb(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.cover_path == "cover.webp"
    assert (bundle / "cover.webp").read_bytes() == before["thumbs/0002.webp"]
    assert (bundle / "cover.webp").read_bytes() != before["cover.webp"]


def test_apply_writes_a_manifest_that_verifies(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    on_disk = Manifest.model_validate_json((bundle / "manifest.json").read_text())
    verify(bundle, on_disk)  # must not raise
    declared = {c.path for c in on_disk.checksums}
    actual = {
        str(p.relative_to(bundle)) for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert declared == actual


def test_apply_preserves_provenance_fields(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = Manifest.model_validate_json((bundle / "manifest.json").read_text())

    after = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert after.id == before.id
    assert after.content_hash == before.content_hash
    assert after.original_filename == before.original_filename
    assert after.original_format == before.original_format
    assert after.title == before.title
    assert after.publication_date == before.publication_date
    assert after.ocr_engine == before.ocr_engine
    assert (bundle / f"original.{before.original_format}").exists()


def test_apply_with_count_two(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=4)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=2))

    assert manifest.page_count == 2
    assert [p.page_number for p in manifest.pages] == [1, 2]
    assert (bundle / "pages/0001.webp").read_bytes() == before["pages/0003.webp"]
    assert (bundle / "pages/0002.webp").read_bytes() == before["pages/0004.webp"]


def test_apply_preserves_non_canonical_paths(tmp_path):
    """A third-party producer may use its own naming; only the NNNN stem moves."""
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "img").mkdir()
    for old, new in (("pages/0001.webp", "img/0001.png"), ("pages/0002.webp", "img/0002.png")):
        (bundle / old).rename(bundle / new)
    data = json.loads((bundle / "manifest.json").read_text())
    for entry in data["pages"]:
        entry["image_path"] = f"img/{entry['page_number']:04d}.png"
    data["checksums"] = [
        {**c, "path": c["path"].replace("pages/", "img/").replace(".webp", ".png")}
        if c["path"].startswith("pages/") else c
        for c in data["checksums"]
    ]
    (bundle / "manifest.json").write_text(json.dumps(data))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert [p.image_path for p in manifest.pages] == ["img/0001.png"]
    assert (bundle / "img/0001.png").exists()
    assert not (bundle / "img/0002.png").exists()


def test_apply_leaves_bundle_untouched_when_the_swap_fails(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)
    plan = plan_drop(bundle.parent, bundle.name, count=1)

    real_rename = Path.rename

    def exploding_rename(self, target):
        # Fail on the first half of the swap: <id> → <id>.old.
        if str(target).endswith(".old"):
            raise OSError("simulated failure during swap")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", exploding_rename)

    with pytest.raises(OSError, match="simulated failure"):
        apply_drop(plan)

    monkeypatch.undo()
    assert _snapshot(bundle) == before
    assert not (bundle.parent / f"{bundle.name}.new").exists()
    assert not (bundle.parent / f"{bundle.name}.old").exists()


def test_apply_restores_the_bundle_when_the_second_rename_fails(tmp_path, monkeypatch):
    """The window between <id> → <id>.old and <id>.new → <id> is the only moment
    the live bundle does not exist. Failing there must put it back."""
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)
    plan = plan_drop(bundle.parent, bundle.name, count=1)

    real_rename = Path.rename

    def exploding_rename(self, target):
        if str(self).endswith(".new"):
            raise OSError("simulated failure restoring")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", exploding_rename)

    with pytest.raises(OSError, match="simulated failure"):
        apply_drop(plan)

    monkeypatch.undo()
    assert _snapshot(bundle) == before
    assert not (bundle.parent / f"{bundle.name}.new").exists()
    assert not (bundle.parent / f"{bundle.name}.old").exists()


def test_apply_is_not_re_runnable_on_an_already_repaired_bundle(tmp_path):
    """Second run drops what is now a real page — it must be an explicit choice,
    never an accident of re-running. It succeeds, so the operator's protection is
    the dry-run preview, not a refusal."""
    bundle = make_bundle(tmp_path, num_pages=3)
    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.page_count == 1
