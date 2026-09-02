"""Parse compound-library files for Nesso virtual screening submissions.

The API accepts the library either inline (``virtual_screening.compounds`` inside
``yaml_file``) or as a separate ``compounds_file`` upload.  This module parses the
separate upload into the same compound records the inline YAML carries, so the
rest of the pipeline (SMILES validation, deduplication, batching) stays
single-sourced in ``normalize_nesso_screening_input_yaml``.

The accepted formats mirror the frontend parser (``parseVirtualScreeningInput``)
exactly — FASTA-style ``>name`` records, CSV/TSV with an optional header row, and
whitespace-delimited ``SMILES name`` lines — so a file the UI accepts in its
compound textarea is also valid as a ``compounds_file`` upload.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import yaml

_MAX_SMILES_LENGTH = 4096

_SMILES_COLUMN_ALIASES = ("smiles", "canonical_smiles", "isomeric_smiles", "molecule_smiles")
_NAME_COLUMN_ALIASES = ("name", "compound", "compound_name", "id", "identifier", "title")


def _slug_token(value: str, fallback: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    # Strip only the combining diacritics the TS slug strips (U+0300-U+036F); other marks
    # fall through to the regex as replacement characters, matching the frontend ids.
    without_accents = "".join(
        char for char in decomposed if not (0x0300 <= ord(char) <= 0x036F)
    )
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", without_accents).strip("-").lower()[:56]
    return token or fallback


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        keep = max(1, 60 - len(str(suffix)) - 1)
        candidate = f"{base[:keep]}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _add_compound(
    compounds: List[Dict[str, str]],
    used_ids: set[str],
    smiles: str,
    name: str,
    source_index: int,
    errors: List[str],
) -> None:
    normalized_smiles = smiles.strip()
    if not normalized_smiles:
        return
    if len(normalized_smiles) > _MAX_SMILES_LENGTH:
        errors.append(f"Compound {source_index}: SMILES is longer than {_MAX_SMILES_LENGTH} characters.")
        return
    display_name = name.strip()[:160] or f"Compound {source_index}"
    compound_id = _unique_id(
        _slug_token(display_name, f"compound-{source_index:03d}"),
        used_ids,
    )
    compounds.append({"id": compound_id, "name": display_name, "smiles": normalized_smiles})


def _split_delimited_line(line: str, delimiter: str) -> Optional[List[str]]:
    fields: List[str] = []
    current: List[str] = []
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == '"':
            if quoted and index + 1 < len(line) and line[index + 1] == '"':
                current.append('"')
                index += 1
            else:
                quoted = not quoted
            index += 1
            continue
        if char == delimiter and not quoted:
            fields.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quoted:
        return None
    fields.append("".join(current).strip())
    return fields


def _normalized_column_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized


def _find_column_index(columns: List[str], aliases: Tuple[str, ...]) -> int:
    for position, column in enumerate(columns):
        if _normalized_column_name(column) in aliases:
            return position
    return -1


def _parse_delimited(source_lines: List[Tuple[str, int]], delimiter: str) -> Optional[List[Dict[str, str]]]:
    """Parse CSV/TSV rows; return None when the content is not delimited records."""
    rows: List[Tuple[List[str], int]] = []
    errors: List[str] = []
    for raw_line, line_number in source_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = _split_delimited_line(raw_line, delimiter)
        if fields is None:
            errors.append(f"Line {line_number}: unterminated quoted field.")
            continue
        rows.append((fields, line_number))
    if errors or not rows:
        if errors:
            raise ValueError(" ".join(errors))
        return None

    first_row = rows[0][0]
    smiles_column = _find_column_index(first_row, _SMILES_COLUMN_ALIASES)
    has_recognized_header = smiles_column >= 0
    compound_rows = rows[1:] if has_recognized_header else rows
    if not has_recognized_header and all(len(fields) < 2 for fields, _ in rows):
        return None

    if has_recognized_header:
        name_column = _find_column_index(first_row, _NAME_COLUMN_ALIASES)
    else:
        name_column = 1
    effective_smiles_column = smiles_column if has_recognized_header else 0

    compounds: List[Dict[str, str]] = []
    used_ids: set[str] = set()
    for fields, line_number in compound_rows:
        smiles = fields[effective_smiles_column] if effective_smiles_column < len(fields) else ""
        name = fields[name_column] if 0 <= name_column < len(fields) else ""
        if not smiles.strip() and not name.strip():
            continue
        if not smiles.strip():
            errors.append(f"Line {line_number}: SMILES is missing.")
            continue
        _add_compound(compounds, used_ids, smiles, name or f"Compound {len(compounds) + 1}", line_number, errors)
    if errors:
        raise ValueError(" ".join(errors))
    return compounds


def parse_screening_compounds_file(text: str) -> List[Dict[str, str]]:
    """Parse a compound library into ``[{id, name, smiles}]`` records.

    Raises ``ValueError`` with every parse problem joined into one message, mirroring
    the frontend parser's error contract.
    """
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Keep raw lines for delimited parsing (the TS parser splits the untrimmed line, so a
    # leading tab yields an empty first field there); stripped lines drive the other modes.
    raw_numbered: List[Tuple[str, int]] = [
        (line, number) for number, line in enumerate(source.split("\n"), start=1)
    ]
    content_lines = [
        (stripped, number)
        for raw_line, number in raw_numbered
        if (stripped := raw_line.strip()) and not stripped.startswith("#")
    ]
    if not content_lines:
        raise ValueError("compounds_file contains no compound records.")

    errors: List[str] = []

    if any(line.startswith(">") for line, _ in content_lines):
        compounds: List[Dict[str, str]] = []
        used_ids: set[str] = set()
        current_name = ""
        current_line = 0
        current_smiles: List[str] = []

        def flush() -> None:
            nonlocal current_smiles
            if not current_name and not current_smiles:
                return
            if not current_name:
                errors.append(f"Line {current_line}: FASTA/NCBI record is missing a name.")
            elif not current_smiles:
                errors.append(f"Compound {current_name}: SMILES is missing.")
            else:
                _add_compound(compounds, used_ids, "".join(current_smiles).strip(), current_name, current_line, errors)
            current_smiles = []

        for line, number in content_lines:
            if line.startswith(">"):
                flush()
                current_name = line[1:].strip()
                current_line = number
            elif current_name:
                current_smiles.append(line.split()[0] if line.split() else "")
            else:
                errors.append(f"Line {number}: expected a record header beginning with \">\".")
        flush()
        if errors:
            raise ValueError(" ".join(errors))
        if not compounds:
            raise ValueError("compounds_file contains no compound records.")
        return compounds

    first_data_line = content_lines[0][0]
    if "\t" in first_data_line:
        delimited = _parse_delimited(raw_numbered, "\t")
        if delimited is not None:
            return delimited
    elif "," in first_data_line:
        delimited = _parse_delimited(raw_numbered, ",")
        if delimited is not None:
            return delimited

    compounds = []
    used_ids = set()
    for line, number in content_lines:
        fields = line.split()
        smiles = fields[0] if fields else ""
        name = " ".join(fields[1:]) or f"Compound {len(compounds) + 1}"
        _add_compound(compounds, used_ids, smiles, name, number, errors)
    if errors:
        raise ValueError(" ".join(errors))
    if not compounds:
        raise ValueError("compounds_file contains no compound records.")
    return compounds


def merge_screening_compounds_file_into_yaml(yaml_content: str, file_text: str) -> str:
    """Return ``yaml_content`` with the library from ``file_text`` as its compounds.

    The library is provided either inline or as a file — never both.  Chemistry
    validation, deduplication, and the compound cap stay with
    ``normalize_nesso_screening_input_yaml``; this only moves parsed records into
    the YAML the rest of the pipeline already consumes.
    """
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except Exception as exc:
        raise ValueError(f"yaml_file is not valid YAML: {exc}") from exc
    if not isinstance(yaml_data, dict):
        raise ValueError("yaml_file must be a YAML mapping.")

    existing = yaml_data.get("virtual_screening")
    if isinstance(existing, dict) and isinstance(existing.get("compounds"), list):
        inline = [item for item in existing["compounds"] if isinstance(item, dict) and str(item.get("smiles") or "").strip()]
        if inline:
            raise ValueError(
                "Provide the compound library either as virtual_screening.compounds inside yaml_file "
                "or as a separate compounds_file upload, not both."
            )

    compounds = parse_screening_compounds_file(file_text)
    screening = dict(existing) if isinstance(existing, dict) else {}
    screening["compounds"] = compounds
    yaml_data["virtual_screening"] = screening
    return yaml.safe_dump(yaml_data, sort_keys=False, allow_unicode=True)
