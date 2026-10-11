"""Pure helpers for peptide-design result archive layout.

The archive layout knowledge (rank_NN structure files, results_summary.json /
design_results.json members, per-chain mean pLDDT from B-factors) previously
lived as private Flask closures in routes/task.py and as inline regexes in the
frontend parser. This module is the single backend source of truth: pure
functions over a zip member-name list, no Flask and no I/O, so both the status
routes and maintenance scripts can share one tested implementation.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# structures/rank_01.pdb — written by the peptide design export loop. The
# frontend parser's DESIGN_RANK_RE matches a BROADER family (rank_NN followed
# by any separator, any extension) because it also resolves in-progress
# samples; the backend gate here deliberately accepts only the shipped
# rank_NN.pdb|cif|mmcif archive members.
RANK_STRUCTURE_RE = re.compile(r'(?:^|/)rank_(\d+)\.(?:pdb|cif|mmcif)$', re.IGNORECASE)

SUMMARY_MEMBER = 'results_summary.json'
DESIGN_RESULTS_MEMBER = 'design_results.json'


def rank_structure_ranks(names: Iterable[str]) -> Dict[str, int]:
    """Map each rank structure base name to its rank number (lowest wins)."""
    out: Dict[str, int] = {}
    for name in names:
        match = RANK_STRUCTURE_RE.search(name.lower())
        if not match:
            continue
        rank = int(match.group(1))
        base = os.path.basename(name)
        if base not in out or rank < out[base]:
            out[base] = rank
    return out


def contains_structure(names: Iterable[str], structure_name: str) -> bool:
    """Whether a structure file with this base name exists in the archive."""
    token = (structure_name or '').strip().lower()
    if not token:
        return False
    for name in names:
        lowered = name.lower()
        if os.path.basename(lowered) == token or lowered.endswith('/' + token):
            return True
    return False


def lowest_rank_structure(names: Iterable[str]) -> str:
    """Base name of the lowest-numbered rank structure ('' when none)."""
    best_name, best_rank = '', None
    for name in names:
        match = RANK_STRUCTURE_RE.search(name.lower())
        if not match:
            continue
        rank = int(match.group(1))
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_name = os.path.basename(name)
    return best_name


def _row_structure_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ''
    return str(row.get('structure_name') or '').strip()


def pick_display_structure(top_results: List[Any], names: Iterable[str]) -> str:
    """Choose the structure base name the UI should open for a design archive.

    Resolution order:
      1. The best-ranked row's structure_name, when that file is in the archive.
      2. The first ranked row whose structure_name exists — rank 1 legitimately
         ships structureless after a checkpoint resume loses early-gen tmp
         files, and the viewer must never be pointed at a missing file.
      3. The lowest-numbered rank file — archives written before the row→
         structure mirror carry no structure_name fields at all.
    """
    names = list(names)
    structure_name = _row_structure_name(top_results[0]) if top_results else ''
    if not structure_name or not contains_structure(names, structure_name):
        structure_name = ''
        for row in top_results:
            candidate = _row_structure_name(row)
            if candidate and contains_structure(names, candidate):
                structure_name = candidate
                break
    if not structure_name:
        structure_name = lowest_rank_structure(names)
    return structure_name


def summary_member_names(names: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    """Locate the (results_summary.json, design_results.json) members."""
    summary_name: Optional[str] = None
    results_name: Optional[str] = None
    for name in names:
        base = os.path.basename(name).lower()
        if summary_name is None and base == SUMMARY_MEMBER:
            summary_name = name
        elif results_name is None and base == DESIGN_RESULTS_MEMBER:
            results_name = name
    return summary_name, results_name


def chain_mean_plddt_from_structure_text(structure_text: str) -> Dict[str, float]:
    """Per-chain mean B-factor (engine per-atom pLDDT) of a PDB structure text."""
    means: Dict[str, Dict[str, float]] = {}
    for line in structure_text.splitlines():
        if not line.startswith('ATOM'):
            continue
        chain_id = line[21] if len(line) > 21 else ' '
        try:
            b_iso = float(line[60:66])
        except ValueError:
            continue
        if b_iso <= 0:
            continue
        agg = means.setdefault(chain_id, {'sum': 0.0, 'count': 0.0})
        agg['sum'] += b_iso
        agg['count'] += 1
    return {
        chain_id: agg['sum'] / agg['count']
        for chain_id, agg in means.items()
        if agg['count'] >= 3
    }
