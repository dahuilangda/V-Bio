"""Contract tests for the Protenix common-cache overlay built by
``_merge_custom_ccd_with_existing_cache``.

The overlay must contain exactly two unique files per task (the merged
``components.cif`` and ``components.cif.rdkit_mol.pkl``); every other entry of
the shared cache — notably the multi-GB ``hf_hub`` tree whose snapshot entries
are *relative symlinks* into ``blobs/`` — must be shared via hardlinks and
preserved symlinks. A regression here silently cost ~7GB per custom-CCD task
(the default ``copytree`` dereferences those symlinks into full blob copies).

Ground truth for the hardlink fallback: ``shutil.copyfile`` raises
``SameFileError`` for entries already hardlinked by a partial first pass, so a
fallback ``copytree`` over the same target must start from a clean tree.
"""
from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.runtime.run_single_prediction import _merge_custom_ccd_with_existing_cache

LINKER_SEZ_CIF = REPO_ROOT / "backend" / "runtime" / "linker_ccd" / "SEZ.cif"


def _make_source_common_cache(root: Path) -> Path:
    """Build a miniature stand-in for /data/protenix/common_cache."""
    src = root / "common_cache"
    src.mkdir()
    (src / "components.cif").write_text("data_SRC\n#\n", encoding="utf-8")
    with (src / "components.cif.rdkit_mol.pkl").open("wb") as handle:
        pickle.dump({"SRC": "placeholder-mol"}, handle)
    (src / "clusters-by-entity-40.txt").write_text("cluster\n", encoding="utf-8")

    # HF-hub style tree: a blob plus a snapshot entry that is a *relative*
    # symlink into blobs/ (this is exactly what hf_hub creates).
    blob = src / "hf_hub" / "hub" / "models--m" / "blobs" / "deadbeef"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"w" * 4096)
    weights_dir = src / "hf_hub" / "hub" / "models--m" / "snapshots" / "rev1" / "weights"
    weights_dir.mkdir(parents=True)
    os.symlink("../../../blobs/deadbeef", weights_dir / "model.pth")
    return src


@pytest.fixture(autouse=True)
def _require_hardlink_support(tmp_path: Path):
    probe_a = tmp_path / "probe_a"
    probe_b = tmp_path / "probe_b"
    probe_a.write_text("x")
    try:
        os.link(probe_a, probe_b)
    except OSError as exc:
        pytest.skip(f"filesystem does not support hardlinks: {exc}")


def test_merge_shares_static_cache_and_merges_ccd(tmp_path: Path) -> None:
    src = _make_source_common_cache(tmp_path)
    overlay_root = tmp_path / "task_overlay"

    result = _merge_custom_ccd_with_existing_cache(
        src,
        overlay_root,
        custom_molecules=[],
        extra_cif_text=LINKER_SEZ_CIF.read_text(),
        extra_mols=None,
    )
    assert result is not None
    common = overlay_root / "common"

    merged_cif = (common / "components.cif").read_text(encoding="utf-8")
    assert "data_SRC" in merged_cif, "source CCD content lost in merge"
    assert "data_SEZ" in merged_cif, "custom CCD block not merged"

    with (common / "components.cif.rdkit_mol.pkl").open("rb") as handle:
        merged = pickle.load(handle)
    assert isinstance(merged, dict)

    blob_src = src / "hf_hub" / "hub" / "models--m" / "blobs" / "deadbeef"
    blob_overlay = common / "hf_hub" / "hub" / "models--m" / "blobs" / "deadbeef"
    assert blob_src.stat().st_ino == blob_overlay.stat().st_ino, (
        "static blob must be hardlinked (shared inode), not copied"
    )
    assert blob_overlay.stat().st_nlink >= 2

    snapshot = common / "hf_hub" / "hub" / "models--m" / "snapshots" / "rev1" / "weights" / "model.pth"
    assert snapshot.is_symlink(), (
        "HF snapshot entry must stay a symlink; dereferencing it duplicates "
        "every blob into the task overlay (the original 7GB/task leak)"
    )
    assert snapshot.resolve() == blob_overlay.resolve()

    # The merged CCD files are the ONLY unique regular files: nothing else may
    # be materialized as a fresh copy with link count 1.
    unique = [
        p for p in common.rglob("*")
        if p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1
    ]
    assert sorted(p.name for p in unique) == [
        "components.cif",
        "components.cif.rdkit_mol.pkl",
    ]


def test_fallback_full_copy_when_hardlinks_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_source_common_cache(tmp_path)
    overlay_root = tmp_path / "task_overlay"

    def _refuse_link(src_path, dst_path, *args, **kwargs):
        raise OSError("hardlinks unavailable")

    monkeypatch.setattr(os, "link", _refuse_link)

    result = _merge_custom_ccd_with_existing_cache(
        src,
        overlay_root,
        custom_molecules=[],
        extra_cif_text=LINKER_SEZ_CIF.read_text(),
        extra_mols=None,
    )
    assert result is not None
    common = overlay_root / "common"

    merged_cif = (common / "components.cif").read_text(encoding="utf-8")
    assert "data_SEZ" in merged_cif

    blob_src = src / "hf_hub" / "hub" / "models--m" / "blobs" / "deadbeef"
    blob_overlay = common / "hf_hub" / "hub" / "models--m" / "blobs" / "deadbeef"
    assert blob_overlay.read_bytes() == blob_src.read_bytes(), "full copy lost content"
    assert blob_overlay.stat().st_nlink == 1, "fallback must not silently hardlink"

    snapshot = common / "hf_hub" / "hub" / "models--m" / "snapshots" / "rev1" / "weights" / "model.pth"
    assert snapshot.is_symlink(), "fallback copy must still preserve HF symlinks"

    overlay_root_two = tmp_path / "task_overlay_unused"
    assert (
        _merge_custom_ccd_with_existing_cache(src, overlay_root_two, custom_molecules=[])
        is None
    ), "no custom CCD content should skip the overlay entirely"
    assert not overlay_root_two.exists()
