#!/usr/bin/env python3
"""Shared helpers for locating and reading Boltz2Score result artifacts."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence


BEST_CONFIDENCE_NAME = "best_confidence.json"
BEST_STRUCTURE_NAMES = ("best_model.cif", "best_model.mmcif")
BEST_IPSAE_NAME = "best_ipsae.json"


def coerce_float(value) -> float:
    if value is None:
        return math.nan
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        return math.nan


def confidence_model_stem(conf_path: Path) -> str:
    stem = conf_path.stem
    return stem[len("confidence_") :] if stem.startswith("confidence_") else stem


def select_confidence_file(
    conf_files: Sequence[Path],
    *,
    include_best_alias: bool = True,
    model_index: int | None = None,
) -> Path | None:
    ordered = sorted(Path(path) for path in conf_files)
    if not ordered:
        return None
    if include_best_alias:
        for path in ordered:
            if path.name == BEST_CONFIDENCE_NAME:
                return path
    if model_index is not None:
        wanted = f"_model_{model_index}"
        for path in ordered:
            if wanted in path.name:
                return path
        return None
    for path in ordered:
        if "_model_0" in path.name or "_model_1" in path.name:
            return path
    return ordered[0]


def select_confidence_file_from_dir(
    record_dir: Path,
    *,
    include_best_alias: bool = True,
    model_index: int | None = None,
    required: bool = True,
) -> Path | None:
    candidates: list[Path] = []
    if include_best_alias:
        best_path = record_dir / BEST_CONFIDENCE_NAME
        if best_path.exists():
            candidates.append(best_path)
    candidates.extend(sorted(record_dir.glob("confidence_*.json")))
    selected = select_confidence_file(
        candidates,
        include_best_alias=include_best_alias,
        model_index=model_index,
    )
    if selected is None and required:
        raise FileNotFoundError(f"No confidence JSON files found in {record_dir}")
    return selected


def resolve_structure_file(record_dir: Path, conf_path: Path) -> Path:
    if conf_path.name == BEST_CONFIDENCE_NAME:
        for candidate_name in BEST_STRUCTURE_NAMES:
            candidate = record_dir / candidate_name
            if candidate.exists():
                return candidate
    model_stem = confidence_model_stem(conf_path)
    for ext in (".cif", ".mmcif"):
        path = record_dir / f"{model_stem}{ext}"
        if path.exists():
            return path
    raise FileNotFoundError(f"Cannot find structure output for {conf_path.name} in {record_dir}")


def resolve_ipsae_file(record_dir: Path, conf_path: Path) -> Path | None:
    if conf_path.name == BEST_CONFIDENCE_NAME:
        alias = record_dir / BEST_IPSAE_NAME
        if alias.exists():
            return alias
    path = record_dir / f"ipsae_{confidence_model_stem(conf_path)}.json"
    return path if path.exists() else None


def discover_record_dirs(root_dir: Path) -> dict[str, Path]:
    records: dict[str, Path] = {}
    for conf_path in sorted(root_dir.rglob("confidence_*.json")):
        record_dir = conf_path.parent
        record_id = record_dir.name
        expected_prefix = f"confidence_{record_id}_model_"
        if conf_path.name.startswith(expected_prefix):
            records[record_id] = record_dir
    for conf_path in sorted(root_dir.rglob(BEST_CONFIDENCE_NAME)):
        record_dir = conf_path.parent
        records.setdefault(record_dir.name, record_dir)
    return records
