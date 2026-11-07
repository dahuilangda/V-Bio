"""Engine capability matrix loader.

The canonical matrix lives in configs/engine_capabilities.json; the embedded
copy below mirrors it for containers that cannot read the repo path. A
contract test asserts the embedded dict matches the JSON file, so the two
cannot drift. Extending an engine or adding one means editing the JSON and
refreshing EMBEDDED_ENGINES (tests enforce the schema).
"""
from __future__ import annotations

import json
from pathlib import Path

MATRIX_PATH = Path(__file__).resolve().parents[2] / "configs" / "engine_capabilities.json"

EMBEDDED_ENGINES: dict = {
    "boltz": {
        "label": "Boltz-2", "status": "active",
        "workflows": ["prediction", "virtual_screening", "lead_optimization"],
        "chirality": ["l", "d"], "design_modes": ["linear"],
        "features": {"pocket": False, "custom_ccd": True, "msa": True, "ncaa": False},
    },
    "alphafold3": {
        "label": "AlphaFold3", "status": "active",
        "workflows": ["prediction"],
        "chirality": ["l", "d"], "design_modes": ["linear"],
        "features": {"pocket": False, "custom_ccd": False, "msa": True, "ncaa": False},
    },
    "protenix": {
        "label": "Protenix", "status": "active",
        "workflows": ["prediction", "peptide_design"],
        "chirality": ["l"], "design_modes": ["linear", "cyclic", "bicyclic"],
        "features": {"pocket": True, "custom_ccd": True, "msa": True, "ncaa": True},
    },
    "protenix2dock": {
        "label": "Protenix2Dock", "status": "active",
        "workflows": ["peptide_design", "lead_optimization"],
        "chirality": ["l", "d"], "design_modes": ["linear", "cyclic", "bicyclic"],
        "features": {"pocket": True, "custom_ccd": True, "msa": True, "ncaa": True},
    },
    "mol1": {
        "label": "Mol-1", "status": "active",
        "workflows": ["prediction", "peptide_design"],
        "chirality": ["l"], "design_modes": ["linear"],
        "features": {"pocket": False, "custom_ccd": False, "msa": True, "ncaa": True},
    },
    "rfd4": {
        "label": "RFD4-Proteina", "status": "active",
        "workflows": ["prediction"],
        "chirality": ["l"], "design_modes": ["linear"],
        "features": {"pocket": False, "custom_ccd": False, "msa": False, "ncaa": True},
    },
    "nesso": {
        "label": "Nesso", "status": "active",
        "workflows": ["virtual_screening"],
        "chirality": [], "design_modes": [],
        "features": {"pocket": False, "custom_ccd": False, "msa": False, "ncaa": False},
    },
    "boltz2dock": {
        "label": "Boltz2Dock", "status": "deprecated",
        "workflows": [],
        "chirality": ["l", "d"], "design_modes": ["linear"],
        "features": {"pocket": False, "custom_ccd": True, "msa": True, "ncaa": False},
    },
    "mol1dock": {
        "label": "Mol1Dock", "status": "deprecated",
        "workflows": [],
        "chirality": ["l", "d"], "design_modes": ["bicyclic"],
        "features": {"pocket": False, "custom_ccd": False, "msa": True, "ncaa": False},
    },
}


def _load() -> dict:
    try:
        data = json.loads(MATRIX_PATH.read_text())
        return data.get("engines", EMBEDDED_ENGINES)
    except (OSError, ValueError):
        return EMBEDDED_ENGINES


ENGINES = _load()


def get_engine(backend: str) -> dict | None:
    return ENGINES.get(str(backend or "").strip().lower())


def is_active(backend: str) -> bool:
    engine = get_engine(backend)
    return bool(engine) and engine.get("status") == "active"


def active_engine_ids() -> list[str]:
    return sorted(k for k, v in ENGINES.items() if v.get("status") == "active")


def supports(backend: str, workflow: str | None = None,
             feature: str | None = None) -> bool:
    engine = get_engine(backend)
    if not engine or engine.get("status") != "active":
        return False
    if workflow and workflow not in engine.get("workflows", []):
        return False
    if feature and not engine.get("features", {}).get(feature, False):
        return False
    return True
