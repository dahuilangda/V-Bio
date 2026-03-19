from __future__ import annotations

import json
import shutil
from pathlib import Path

from boltz.data.types import StructureV2

from metrics.ligand_ipsae import compute_ligand_ipsae_from_files
from utils.result_utils import BEST_CONFIDENCE_NAME, BEST_IPSAE_NAME, BEST_STRUCTURE_NAMES, confidence_model_stem


def write_chain_map(processed_dir: Path, output_dir: Path, record_id: str) -> None:
    try:
        structure = StructureV2.load(
            processed_dir / "structures" / f"{record_id}.npz"
        )
        structure = structure.remove_invalid_chains()
        chain_map = {
            str(idx): str(chain["name"])
            for idx, chain in enumerate(structure.chains)
        }
        chain_map_path = output_dir / record_id / "chain_map.json"
        chain_map_path.parent.mkdir(parents=True, exist_ok=True)
        chain_map_path.write_text(json.dumps(chain_map, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"[Warning] Failed to write chain map: {exc}")


def compute_and_write_ipsae(
    output_dir: Path,
    record_id: str,
    pae_cutoff: float,
    dist_cutoff: float,
) -> None:
    result_dir = output_dir / record_id
    conf_files = sorted(result_dir.glob(f"confidence_{record_id}_model_*.json"))
    if not conf_files:
        raise FileNotFoundError(f"No confidence JSON files found for IPSAE in {result_dir}")

    for conf_path in conf_files:
        model_stem = confidence_model_stem(conf_path)
        cif_path = result_dir / f"{model_stem}.cif"
        if not cif_path.exists():
            mmcif_path = result_dir / f"{model_stem}.mmcif"
            if mmcif_path.exists():
                cif_path = mmcif_path
            else:
                raise FileNotFoundError(f"Missing mmCIF output required by IPSAE: {model_stem}.cif")
        pae_path = result_dir / f"pae_{model_stem}.npz"
        if not pae_path.exists():
            raise FileNotFoundError(f"Missing full PAE output required by IPSAE: {pae_path.name}")

        result = compute_ligand_ipsae_from_files(
            confidence_path=conf_path,
            cif_path=cif_path,
            pae_path=pae_path,
            pae_cutoff=pae_cutoff,
            dist_cutoff=dist_cutoff,
            chain_map_path=result_dir / "chain_map.json",
            result_dir=result_dir,
        )

        ipsae_path = result_dir / f"ipsae_{model_stem}.json"
        ipsae_path.write_text(json.dumps(result, indent=2) + "\n")

        payload = json.loads(conf_path.read_text())
        payload.update(result)
        conf_path.write_text(json.dumps(payload, indent=2))


def rerank_diffusion_samples(
    output_dir: Path,
    record_id: str,
) -> dict[str, object] | None:
    result_dir = output_dir / record_id
    conf_files = sorted(result_dir.glob(f"confidence_{record_id}_model_*.json"))
    if not conf_files:
        return None

    def _write_best_aliases(
        structure_path: Path,
        confidence_path: Path,
        ipsae_path: Path | None,
    ) -> None:
        alias_targets = {
            BEST_STRUCTURE_NAMES[0]: structure_path,
            BEST_CONFIDENCE_NAME: confidence_path,
        }
        if ipsae_path is not None:
            alias_targets[BEST_IPSAE_NAME] = ipsae_path

        for alias_name, source_path in alias_targets.items():
            if not source_path.exists():
                continue
            alias_path = result_dir / alias_name
            shutil.copy2(source_path, alias_path)

    if len(conf_files) == 1:
        conf_path = conf_files[0]
        model_stem = confidence_model_stem(conf_path)
        structure_path = result_dir / f"{model_stem}.cif"
        if not structure_path.exists():
            mmcif_path = result_dir / f"{model_stem}.mmcif"
            if mmcif_path.exists():
                structure_path = mmcif_path
        ipsae_path = result_dir / f"ipsae_{model_stem}.json"
        _write_best_aliases(
            structure_path=structure_path,
            confidence_path=conf_path,
            ipsae_path=ipsae_path if ipsae_path.exists() else None,
        )
        return None

    def _metric(payload: dict[str, object], key: str, default: float = 0.0) -> float:
        value = payload.get(key, default)
        try:
            return float(value)
        except Exception:
            return float(default)

    scored_rows: list[dict[str, object]] = []
    for conf_path in conf_files:
        payload = json.loads(conf_path.read_text())
        model_stem = confidence_model_stem(conf_path)
        cif_path = result_dir / f"{model_stem}.cif"
        mmcif_path = result_dir / f"{model_stem}.mmcif"
        ipsae_path = result_dir / f"ipsae_{model_stem}.json"
        row = {
            "model_stem": model_stem,
            "model_index": int(model_stem.rsplit("_model_", 1)[-1]),
            "confidence_path": str(conf_path),
            "structure_path": str(cif_path if cif_path.exists() else mmcif_path),
            "ipsae_path": str(ipsae_path) if ipsae_path.exists() else "",
            "confidence_score": _metric(payload, "confidence_score"),
            "iptm": _metric(payload, "iptm"),
            "ligand_iptm": _metric(payload, "ligand_iptm"),
            "ligand_plddt_mean": _metric(payload, "ligand_plddt_mean") / 100.0,
            "ligand_atom_plddt_p10": _metric(payload, "ligand_atom_plddt_p10") / 100.0,
            "ligand_atom_plddt_min": _metric(payload, "ligand_atom_plddt_min") / 100.0,
            "ligand_atom_plddt_fraction_ge_50": _metric(payload, "ligand_atom_plddt_fraction_ge_50"),
            "ligand_atom_plddt_fraction_ge_70": _metric(payload, "ligand_atom_plddt_fraction_ge_70"),
            "ipsae_dom": _metric(payload, "ipsae_dom"),
            "ligand_ipsae_max": _metric(payload, "ligand_ipsae_max"),
            "mean_interface_pae": _metric(payload, "mean_interface_pae", default=32.0),
            "mean_interface_distance": _metric(payload, "mean_interface_distance", default=8.0),
        }
        row["interface_rank_score"] = (
            0.30 * row["ligand_ipsae_max"]
            + 0.25 * row["ipsae_dom"]
            + 0.17 * row["iptm"]
            + 0.10 * row["ligand_iptm"]
            + 0.06 * row["ligand_plddt_mean"]
            + 0.05 * row["ligand_atom_plddt_p10"]
            + 0.04 * row["ligand_atom_plddt_min"]
            + 0.02 * row["ligand_atom_plddt_fraction_ge_50"]
            + 0.01 * row["ligand_atom_plddt_fraction_ge_70"]
            + 0.05 * row["confidence_score"]
            - 0.05 * min(row["mean_interface_pae"] / 8.0, 1.0)
            - 0.03 * min(row["mean_interface_distance"] / 8.0, 1.0)
        )
        scored_rows.append(row)

    scored_rows.sort(
        key=lambda row: (
            row["interface_rank_score"],
            row["ligand_ipsae_max"],
            row["ipsae_dom"],
            row["iptm"],
            row["ligand_plddt_mean"],
            row["confidence_score"],
        ),
        reverse=True,
    )
    best_row = scored_rows[0]
    default_row = next((row for row in scored_rows if row["model_index"] == 0), best_row)

    summary = {
        "record_id": record_id,
        "rank_metric": "interface_rank_score",
        "weights": {
            "ligand_ipsae_max": 0.30,
            "ipsae_dom": 0.25,
            "iptm": 0.17,
            "ligand_iptm": 0.10,
            "ligand_plddt_mean_normalized": 0.06,
            "ligand_atom_plddt_p10_normalized": 0.05,
            "ligand_atom_plddt_min_normalized": 0.04,
            "ligand_atom_plddt_fraction_ge_50": 0.02,
            "ligand_atom_plddt_fraction_ge_70": 0.01,
            "confidence_score": 0.05,
            "mean_interface_pae_penalty": -0.05,
            "mean_interface_distance_penalty": -0.03,
        },
        "default_writer_model": default_row["model_stem"],
        "selected_model": best_row["model_stem"],
        "selected_is_writer_default": bool(best_row["model_index"] == 0),
        "models": scored_rows,
    }
    summary_path = result_dir / f"best_sample_{record_id}.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    _write_best_aliases(
        structure_path=Path(best_row["structure_path"]),
        confidence_path=Path(best_row["confidence_path"]),
        ipsae_path=Path(best_row["ipsae_path"]) if best_row["ipsae_path"] else None,
    )

    return summary
