from __future__ import annotations

import os
from typing import Iterable, List


def _normalize_property_names(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen: set[str] = set()
    for raw in values:
        token = str(raw or "").strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        output.append(token)
    return output


def write_compound_batch_template(output_file: str, *, rows: int = 3) -> str:
    path = str(output_file or "").strip()
    if not path:
        raise ValueError("output_file is required")
    count = max(1, int(rows or 1))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("SMILES\tID\n")
        for idx in range(1, count + 1):
            handle.write(f"<SMILES_{idx}>\tCMPD_{idx:04d}\n")
    return path


def write_property_batch_template(
    output_file: str,
    *,
    property_names: Iterable[str],
    rows: int = 3,
) -> str:
    path = str(output_file or "").strip()
    if not path:
        raise ValueError("output_file is required")
    properties = _normalize_property_names(property_names)
    if not properties:
        properties = ["PROPERTY_A", "PROPERTY_B"]
    count = max(1, int(rows or 1))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    header = ["smiles", *properties]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\t".join(header) + "\n")
        for idx in range(1, count + 1):
            values = [f"<SMILES_{idx}>"] + ["" for _ in properties]
            handle.write("\t".join(values) + "\n")
    return path
