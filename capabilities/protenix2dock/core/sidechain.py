"""CCD component template loader (components.cif ideal coordinates).

The aromatic-ring and covalent-bond guidance bands in core/constraints read
ideal interatomic distances from the PDB Chemical Component Dictionary.
Loaded once per residue type and cached.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_CCD_CACHE: dict[str, dict[str, np.ndarray]] = {}


def _load_ccd_template(res_name: str) -> dict[str, np.ndarray] | None:
    """Load ideal coordinates for a residue from the CCD components.cif."""
    if res_name in _CCD_CACHE:
        return _CCD_CACHE[res_name]

    # CCD path: works both in-container (PROTENIX_ROOT_DIR=/cache) and host
    import os
    ccd_path = Path(os.environ.get("PROTENIX_ROOT_DIR", "/cache")
                    + "/common/components.cif")
    if not ccd_path.exists():
        ccd_path = Path("/data/protenix/common_cache/components.cif")
    if not ccd_path.exists():
        return None

    import gemmi

    doc = gemmi.cif.read(str(ccd_path))
    block = doc.find_block(res_name.upper())
    if block is None:
        _CCD_CACHE[res_name] = {}
        return {}

    coords: dict[str, np.ndarray] = {}
    table = block.find("_chem_comp_atom.",
                       ["atom_id", "model_Cartn_x", "model_Cartn_y",
                        "model_Cartn_z"])
    for row in table:
        name = str(row[0]).strip()
        try:
            coords[name] = np.array([float(row[1]), float(row[2]),
                                     float(row[3])])
        except (ValueError, IndexError):
            continue

    _CCD_CACHE[res_name] = coords
    return coords


_BACKBONE = ("N", "CA", "C")


