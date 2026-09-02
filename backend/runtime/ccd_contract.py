"""mmCIF chem_comp contract validation for custom CCD blocks appended to Protenix's
components.cif.

The contract implemented here is not invented: it is exactly what biotite's
``structure.io.pdbx.convert.get_component`` (the reader Protenix uses for every ligand
CCD) accepts, cross-checked against the official RCSB components.cif conventions:

- A category may be absent entirely (e.g. single-ion entries like NA/ZN carry no
  ``_chem_comp_bond`` at all).
- A category present as a ``loop_`` must deserialize: column headers with zero data
  rows are invalid and raise ``biotite.DeserializationError``
  ("Failed to deserialize category 'chem_comp_bond'" ← "Array must contain at least
  one element").
- Bond rows must reference atom names defined by the block's ``_chem_comp_atom`` rows
  and their own comp_id.

Historical failure this guards against: a regenerated single-atom linker CCD (BS3 =
Bi3+) was emitted with a header-only bond loop; Protenix then failed every sample of
every affected task during featurization. Validation runs before the overlay cache is
written so failures name the offending CCD and never reach the GPU.
"""
from __future__ import annotations

from typing import Dict, List

__all__ = ["validate_ccd_additions", "CCDContractError"]


class CCDContractError(RuntimeError):
    """Raised when appended custom-CCD cif text violates the chem_comp contract."""


def _strip_token_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


class _BlockModel:
    """A parsed data block: key-value items plus loop sections, kept separately so a
    header-only loop (the invalid form) stays distinguishable from an absent one."""

    def __init__(self, code: str):
        self.code = code
        self.kv_categories: Dict[str, List[str]] = {}          # category -> item names
        self.loop_headers: Dict[str, List[List[str]]] = {}     # category -> [header rows]
        self.loop_rows: Dict[str, List[List[str]]] = {}        # category -> [data rows]


def _parse_block(block_lines: List[str]) -> _BlockModel:
    if not block_lines or not block_lines[0].startswith("data_"):
        raise CCDContractError("Block does not start with 'data_<code>'.")
    model = _BlockModel(code=_strip_token_quotes(block_lines[0][len("data_"):].strip()))
    i = 1
    n = len(block_lines)
    while i < n:
        line = block_lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue
        if line.startswith("loop_"):
            i += 1
            headers: List[str] = []
            while i < n and block_lines[i].strip().startswith("_"):
                headers.append(block_lines[i].strip())
                i += 1
            if not headers:
                raise CCDContractError(
                    f"Custom CCD {model.code}: loop_ without any category column."
                )
            category = headers[0].split(".", 1)[0]
            model.loop_headers.setdefault(category, []).append(headers)
            rows = model.loop_rows.setdefault(category, [])
            while i < n:
                stripped = block_lines[i].strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("data_") \
                        or stripped.startswith("loop_"):
                    break
                rows.append([_strip_token_quotes(t) for t in stripped.split()])
                i += 1
            continue
        if line.startswith("_"):
            parts = line.split()
            if len(parts) < 2:
                # Multi-line semicolon values are not produced by our builders; reject
                # loudly instead of silently misparsing them as row data.
                raise CCDContractError(
                    f"Custom CCD {model.code}: key-value item {parts[0]!r} has no inline "
                    f"value (multi-line values are unsupported)."
                )
            category, item = parts[0].split(".", 1)
            model.kv_categories.setdefault(category, []).append(item)
            i += 1
            continue
        raise CCDContractError(
            f"Custom CCD {model.code}: unexpected token outside category/loop: "
            f"{line[:60]!r}"
        )
    return model


def _iter_blocks(text: str) -> List[_BlockModel]:
    cleaned = text.replace("\r\n", "\n")
    lines = cleaned.split("\n")
    starts = [i for i, l in enumerate(lines) if l.strip().startswith("data_")]
    if not starts:
        return []
    if starts[0] != 0:
        prefix = "".join(lines[:starts[0]]).strip()
        if prefix:
            raise CCDContractError("Non-empty content before the first data_ block.")
    models: List[_BlockModel] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        models.append(_parse_block([lines[start].strip()] + lines[start + 1:end]))
    return models


def _validate_block(model: _BlockModel) -> None:
    code = model.code
    if not code:
        raise CCDContractError("Block has an empty component code.")

    # Every category present as a loop must have >=1 data row (header-only loops are the
    # exact shape that crashes biotite deserialization).
    for category in sorted(model.loop_headers):
        if not model.loop_rows.get(category):
            raise CCDContractError(
                f"Custom CCD {code} defines category {category} with zero data rows "
                f"(header-only loop). Either emit >=1 row or omit the category "
                f"entirely (cf. official single-ion entries like NA/ZN)."
            )
        widths = {len(headers) for headers in model.loop_headers[category]}
        row_widths = {len(row) for row in model.loop_rows[category]}
        if len(widths) != 1 or row_widths != widths:
            raise CCDContractError(
                f"Custom CCD {code} category {category}: ragged loop "
                f"(headers={sorted(widths)}, rows={sorted(row_widths)})."
            )

    atom_rows = model.loop_rows.get("_chem_comp_atom") or []
    if not atom_rows:
        raise CCDContractError(f"Custom CCD {code} has no _chem_comp_atom data rows.")
    atom_ids = {row[1] for row in atom_rows}

    bond_rows = model.loop_rows.get("_chem_comp_bond") or []
    for row in bond_rows:
        if row[0] != code:
            raise CCDContractError(
                f"Custom CCD {code}: bond row references comp_id {row[0]!r}."
            )
        missing = [name for name in row[1:3] if name not in atom_ids]
        if missing:
            raise CCDContractError(
                f"Custom CCD {code}: bonds reference undefined atoms {missing}."
            )

    # A component is one connected chemical species. Emitted atom lists carry heavy
    # atoms only, so graph connectivity over the listed atoms/bonds is exact.
    if len(atom_ids) > 1:
        adjacency: Dict[str, set[str]] = {name: set() for name in atom_ids}
        for row in bond_rows:
            atom_a, atom_b = row[1], row[2]
            adjacency[atom_a].add(atom_b)
            adjacency[atom_b].add(atom_a)
        visited: set[str] = set()
        stack = [next(iter(atom_ids))]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(adjacency[current] - visited)
        if len(visited) != len(atom_ids):
            disconnected = sorted(set(atom_ids) - visited)
            raise CCDContractError(
                f"Custom CCD {code} describes a disconnected species; atoms "
                f"{disconnected[:8]} have no bond path to the rest of the component."
            )


def validate_ccd_additions(*cif_texts: str) -> None:
    """Validate every data block across the appended cif texts against the chem_comp
    contract described in the module docstring. Raises :class:`CCDContractError`
    naming the offending component."""
    seen_codes: Dict[str, int] = {}
    total = 0
    for text_index, text in enumerate(cif_texts):
        if not str(text or "").strip():
            continue
        for model in _iter_blocks(str(text)):
            _validate_block(model)
            if model.code in seen_codes:
                raise CCDContractError(
                    f"Custom CCD {model.code} is defined twice (occurrences "
                    f"{seen_codes[model.code]} and {total}); duplicate components.cif "
                    f"entries are ambiguous."
                )
            seen_codes[model.code] = total
            total += 1
