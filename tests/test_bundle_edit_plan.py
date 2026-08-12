import json
from pathlib import Path

import pytest

from magsearch.bundle_edit import BundleEditError, page_text_preview, plan_drop
from magsearch.manifest import PageEntry
from tests.fixtures.bundles import make_bundle


def _rewrite_manifest(bundle: Path, **updates) -> None:
    """Edit manifest.json in place. Checksums are NOT recomputed — callers that
    need the bundle to still verify must pass consistent data."""
    data = json.loads((bundle / "manifest.json").read_text())
    data.update(updates)
    (bundle / "manifest.json").write_text(json.dumps(data))


def test_plan_splits_dropped_from_surviving(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    plan = plan_drop(bundle.parent, bundle.name, count=1)

    assert plan.magazine_id == bundle.name
    assert plan.count == 1
    assert [p.page_number for p in plan.dropped] == [1]
    assert [p.page_number for p in plan.surviving] == [2, 3]
    assert plan.new_page_count == 2


def test_plan_handles_count_above_one(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=4)

    plan = plan_drop(bundle.parent, bundle.name, count=2)

    assert [p.page_number for p in plan.dropped] == [1, 2]
    assert [p.page_number for p in plan.surviving] == [3, 4]
    assert plan.new_page_count == 2


def test_plan_orders_pages_by_number_regardless_of_manifest_order(tmp_path):
    """page_number is the ordering key; the manifest may list pages in any order."""
    bundle = make_bundle(tmp_path, num_pages=3)
    data = json.loads((bundle / "manifest.json").read_text())
    data["pages"] = list(reversed(data["pages"]))
    (bundle / "manifest.json").write_text(json.dumps(data))

    plan = plan_drop(bundle.parent, bundle.name, count=1)

    assert [p.page_number for p in plan.dropped] == [1]
    assert [p.page_number for p in plan.surviving] == [2, 3]


def test_plan_rejects_count_below_one(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    with pytest.raises(BundleEditError, match="count must be at least 1"):
        plan_drop(bundle.parent, bundle.name, count=0)


def test_plan_rejects_emptying_the_bundle(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="would leave 0 pages"):
        plan_drop(bundle.parent, bundle.name, count=2)


def test_plan_rejects_missing_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "manifest.json").unlink()

    with pytest.raises(BundleEditError, match="manifest.json missing"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_missing_directory(tmp_path):
    with pytest.raises(BundleEditError, match="manifest.json missing"):
        plan_drop(tmp_path / "bundles", "no-such-bundle", count=1)


def test_plan_rejects_a_traversing_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent, f"../{bundle.parent.name}/{bundle.name}", count=1)


def test_plan_rejects_a_nested_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent.parent, f"bundles/{bundle.name}", count=1)


def test_plan_rejects_an_absolute_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent, str(bundle), count=1)


def test_plan_rejects_unparseable_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "manifest.json").write_text("{not json")

    with pytest.raises(BundleEditError, match="manifest.json unparseable"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_non_contiguous_page_numbers(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    data = json.loads((bundle / "manifest.json").read_text())
    data["pages"][2]["page_number"] = 9
    (bundle / "manifest.json").write_text(json.dumps(data))

    with pytest.raises(BundleEditError, match="page numbers are not 1..3"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_a_bundle_that_already_fails_checksums(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle / "pages" / "0002.webp").write_bytes(b"corrupted")

    with pytest.raises(BundleEditError, match="checksum mismatch on pages/0002.webp"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_leftover_staging_directory(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle.parent / f"{bundle.name}.new").mkdir()

    with pytest.raises(BundleEditError, match="left over from an interrupted run"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_leftover_old_directory(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle.parent / f"{bundle.name}.old").mkdir()

    with pytest.raises(BundleEditError, match="left over from an interrupted run"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_preview_truncates_on_a_word_boundary():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="Scanned by RETRO-SCANS in 2019 for the preservation community everywhere online",
    )

    preview = page_text_preview(entry, width=40)

    assert len(preview) <= 41  # 40 chars plus the ellipsis
    assert preview.endswith("…")
    assert not preview.rstrip("…").endswith(" ")
    assert preview.startswith("Scanned by RETRO-SCANS")


def test_preview_marks_empty_text():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="   ",
    )

    assert page_text_preview(entry) == "(no text)"


def test_preview_strips_control_characters():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="line one\nline\ttwo",
    )

    assert page_text_preview(entry) == "line one line two"
