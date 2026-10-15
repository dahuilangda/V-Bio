"""Single source of truth for peptide-design physical/geometry constants.

Every physical quantity gets defined ONCE here with its provenance. When the
same threshold appears a second time in code, it must be imported from this
module instead of re-inlined (engineering-standards.md §7).

Consumers: backend/runtime/run_single_prediction.py (export gates, refine
collect, design loop). Capabilities modules (peplm, protenix2dock) run in
separate processes and keep their own numbers until they are wired here too.
"""
from __future__ import annotations

import os

# --- Export gates ---------------------------------------------------------
# A shipped product must be a BOUND complex. Real docks pass at 1.5-8 A min
# heavy-atom distance; the staged pre-docking frame sits 50-100 A off-target.
# 12 A guarantees the peptide is docked against the receptor, not floating
# or buried (dissociated-product guard).
PRODUCT_BOUND_MAX_DISTANCE_A = 12.0

# --- Refine collect gates (D-space samples) -------------------------------
# Physically impossible interpenetration: crystal contacts reach ~2.5 A;
# anything below this is a failed pose, not a tight one.
REFINE_INTERPENETRATION_MIN_A = 2.3
# Absolute single-pair floor used by the same gate (any pair below fails).
REFINE_ABSOLUTE_PAIR_MIN_A = 2.2
# Packing-band pairs (< this) are counted but not fatal: post-polish samples
# legitimately sit at 2.55-2.6 A (3LNJ crystal contacts down to 2.52 A).
REFINE_PACKING_BAND_A = 2.8
# A sample counts as "engaged" with at least this many receptor-peptide
# contacts (< 4.5 A). Defined alongside ENGAGE_MIN_CONTACTS in
# run_single_prediction (kept there: it is referenced by selection tuples).

# --- Backbone / geometry integrity ---------------------------------------
# Consecutive CA-CA virtual bonds below this mean a post-processing step
# crushed the backbone (the omega-band polish incident).
BACKBONE_CA_CA_MIN_A = 2.80
# SEZ tris(bromomethyl)benzene linker: anchor-to-Cys SG bond is considered
# formed below this distance (export ring-closure check).
SEZ_RING_BOND_CUTOFF_A = 2.5

# --- Design loop ----------------------------------------------------------
# Hard gate: only candidates whose binder mean pLDDT reaches this enter the
# elite pool / final ranking. Contact-maximizing junk (H/G-rich no-fold)
# otherwise dominates GRPO (measured H 19%, G 16% after 7 auto gens).
DPEPTIDE_PLDDT_GATE_DEFAULT = 55.0
# D-peptide pLDDT comes from the refined complex's peptide chain, whose
# distribution sits below L-side co-folding binder pLDDT (v11: D elites rode
# the 55 gate with 36-55% pass rates → elite pools collapsed to 4-7 rows).
# A per-chirality floor keeps both pools populated.
DPEPTIDE_PLDDT_GATE_D_DEFAULT = 50.0


def plddt_gate_from_env() -> float:
    """Operator override hook; default from DPEPTIDE_PLDDT_GATE_DEFAULT."""
    raw = os.environ.get("DPEPTIDE_PLDDT_GATE", "").strip()
    try:
        return float(raw) if raw else DPEPTIDE_PLDDT_GATE_DEFAULT
    except ValueError:
        return DPEPTIDE_PLDDT_GATE_DEFAULT


def plddt_gate_for_chirality(chirality: str) -> float:
    """Gate for a chirality ('l'/'d'), with operator overrides per side."""
    key = "DPEPTIDE_PLDDT_GATE_D" if str(chirality or "").strip().lower() == "d" else "DPEPTIDE_PLDDT_GATE"
    raw = os.environ.get(key, "").strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        raw_default = os.environ.get("DPEPTIDE_PLDDT_GATE", "").strip()
        return float(raw_default) if raw_default else (
            DPEPTIDE_PLDDT_GATE_D_DEFAULT
            if key.endswith("_D")
            else DPEPTIDE_PLDDT_GATE_DEFAULT
        )
    except ValueError:
        return DPEPTIDE_PLDDT_GATE_DEFAULT
