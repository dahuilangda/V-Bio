from __future__ import annotations

"""Serve the CCD mmcif definitions a prediction task actually used.

When an input has non-natural amino acids (custom residues) or small-molecule ligands, the
runtime generates CCD mmcif for them. For Protenix these are merged into one big
`components.cif` alongside the entire standard CCD database (~hundreds of MB), so we slice out
only the `data_<code>` blocks the task referenced (custom residues + ligand CCD codes, read
from the task's own `input.json`) and return them as a zip — the ground-truth definitions the
predictor consumed, not a re-derivation.

The merged file is huge, so block offsets are located with a single `grep -ab` pass (C-speed)
and then sliced by byte seek, instead of a Python line scan.
"""

import io
import os
import re
import subprocess
import zipfile
from typing import Dict, List, Optional, Tuple

from flask import Response, jsonify, make_response

RESULTS_ROOT = os.environ.get("VBIO_RESULTS_ROOT", "/data/boltz_central_results")
KNOWN_BACKENDS = ("protenix", "alphafold3", "boltz2", "boltz")


def _find_task_dir(task_id: str) -> Tuple[Optional[str], Optional[str]]:
    tid = str(task_id or "").strip()
    if not tid:
        return None, None
    for backend in KNOWN_BACKENDS:
        candidate = os.path.join(RESULTS_ROOT, backend, tid)
        if os.path.isdir(candidate):
            return backend, candidate
    return None, None


def _collect_protenix_ccd_codes(task_dir: str) -> List[str]:
    """User-referenced CCD codes from the task's Protenix input.json — modification
    `ptmType: CCD_<code>` and ligand `ccd` fields. These are the only CCD entries the user
    actually defined (custom residues + small molecules); everything else in components.cif is
    the stock CCD database and must NOT be returned."""
    input_path = os.path.join(task_dir, "runtime", "input", "input.json")
    if not os.path.isfile(input_path):
        return []
    try:
        with open(input_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    codes: List[str] = list(re.findall(r"CCD_([A-Za-z0-9_\-]+)", text))
    codes.extend(re.findall(r'"ccd"\s*:\s*"([^"]+)"', text))
    seen = set()
    unique: List[str] = []
    for code in codes:
        normalized = str(code or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _data_block_offsets(cif_path: str) -> Optional[List[Tuple[int, str]]]:
    """One fast C-level grep pass: byte offset + code of every `data_<code>` line.
    Returns None if grep is unavailable so the caller can fall back."""
    try:
        proc = subprocess.run(
            ["grep", "-a", "-b", "^data_", cif_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode not in (0, 1):  # 0 = matches, 1 = no matches
        return None
    offsets: List[Tuple[int, str]] = []
    for line in proc.stdout.split("\n"):
        sep = line.find(":")
        if sep <= 0:
            continue
        try:
            offset = int(line[:sep])
        except ValueError:
            continue
        rest = line[sep + 1:]
        if rest.startswith("data_"):
            offsets.append((offset, rest[5:].strip()))
    return offsets


def _extract_ccd_blocks_streaming(cif_path: str, codes: List[str]) -> Dict[str, str]:
    """Fallback when grep is unavailable: stream once and capture wanted blocks."""
    wanted = {c.upper(): c for c in codes}
    if not wanted:
        return {}
    found: Dict[str, str] = {}
    capturing: Optional[str] = None
    buf: List[str] = []
    data_re = re.compile(r"^data_(\S+)\s*$")
    try:
        with open(cif_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = data_re.match(line)
                if m:
                    if capturing is not None and capturing not in found:
                        found[capturing] = "".join(buf).rstrip() + "\n"
                    key = m.group(1).upper()
                    if key in wanted and key not in found:
                        capturing = key
                        buf = [line]
                    else:
                        capturing = None
                        buf = []
                    if len(found) == len(wanted):
                        break
                elif capturing is not None:
                    buf.append(line)
            if capturing is not None and capturing not in found:
                found[capturing] = "".join(buf).rstrip() + "\n"
    except OSError:
        pass
    return {wanted[k]: found[k] for k in found}


def _extract_ccd_blocks(cif_path: str, codes: List[str]) -> Dict[str, str]:
    wanted = {c.upper(): c for c in codes}
    if not wanted or not os.path.isfile(cif_path):
        return {}
    offsets = _data_block_offsets(cif_path)
    if offsets is None:
        return _extract_ccd_blocks_streaming(cif_path, list(wanted.values()))
    if not offsets:
        return {}
    size = os.path.getsize(cif_path)
    spans: Dict[str, Tuple[int, int]] = {}
    for index, (offset, code) in enumerate(offsets):
        key = code.upper()
        if key in wanted and key not in spans:
            end = offsets[index + 1][0] if index + 1 < len(offsets) else size
            spans[key] = (offset, end)
    found: Dict[str, str] = {}
    try:
        with open(cif_path, "rb") as fh:
            for key, (start, end) in spans.items():
                fh.seek(start)
                chunk = fh.read(end - start)
                found[wanted[key]] = chunk.decode("utf-8", errors="replace").rstrip() + "\n"
    except OSError:
        pass
    return found


def _zip_bytes(task_id: str, blocks: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for code, block in blocks.items():
            zf.writestr(f"{code}.cif", block)
    return buf.getvalue()


def build_task_ccd_response(task_id: str) -> Tuple[Response, int]:
    backend, task_dir = _find_task_dir(task_id)
    if not task_dir:
        return jsonify({"error": "Task results directory not found."}), 404
    if backend != "protenix":
        # Protenix merges every user CCD into one components.cif we can slice by code.
        # AF3 / Boltz store CCDs differently (separate userCCD files / mol cache); support
        # lands when those layouts are needed. Honest 501, never a wrong or empty file.
        return jsonify({"error": f"CCD download is not yet supported for backend '{backend}'."}), 501
    codes = _collect_protenix_ccd_codes(task_dir)
    cif_path = os.path.join(task_dir, "runtime", "protenix_common_overlay", "common", "components.cif")
    blocks = _extract_ccd_blocks(cif_path, codes) if codes else {}
    if not blocks:
        return jsonify({"error": "No custom CCD residues or ligand CCD entries found for this task."}), 404
    resp = make_response(_zip_bytes(task_id, blocks))
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="ccd_{task_id}.zip"'
    return resp, 200
