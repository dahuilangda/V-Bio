"""Product flip for the D-peptide route.

The production D-route (backend/runtime/run_single_prediction.py) stages and
refines in mirror space; `flip_product` maps the refined complex back to the
display frame (L-target + D-peptide).
"""

from __future__ import annotations

from pathlib import Path

import gemmi

from . import mirror as mirror_mod


def flip_product(mirror_space_structure: Path, out_path: Path) -> Path:
    """(D-target + L-peptide) -> mirror -> (L-target + D-peptide) product."""
    st = gemmi.read_structure(str(mirror_space_structure))
    st.setup_entities()
    mirror_mod.mirror_structure(st)
    st.setup_entities()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(out_path))
    return out_path
