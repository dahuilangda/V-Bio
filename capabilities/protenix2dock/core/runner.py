"""Protenix inference orchestration for protenix2dock (runs in-container)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def build_configs(
    *,
    input_json_path: Path,
    output_dir: Path,
    model_name: str,
    checkpoint_dir: Path,
    seeds: list[int],
    n_step: int,
    n_sample: int,
    sigma_max: float,
    guidance_enable: bool,
) -> Any:
    """Mirror runner/inference.py::main's two-pass config merge."""
    from configs.configs_base import configs as configs_base
    from configs.configs_data import data_configs
    from configs.configs_inference import inference_configs
    from configs.configs_model_type import model_configs
    from protenix.config.config import parse_configs

    overrides = {
        "input_json_path": str(input_json_path),
        "dump_dir": str(output_dir),
        "model_name": model_name,
        "load_checkpoint_dir": str(checkpoint_dir),
        "seeds": ",".join(str(s) for s in seeds),
        "use_msa": "true",
        "need_atom_confidence": "true",
        "sample_diffusion.N_step": str(n_step),
        "sample_diffusion.N_sample": str(n_sample),
        "inference_noise_scheduler.s_max": str(sigma_max),
        "sample_diffusion.guidance.enable": str(guidance_enable).lower(),
    }
    arg_str = " ".join(f"--{k} {v}" for k, v in overrides.items())

    configs = parse_configs(
        configs={**configs_base, **{"data": data_configs}, **inference_configs},
        arg_str=arg_str,
        fill_required_with_null=True,
    )
    base_configs = {**configs_base, **{"data": data_configs}, **inference_configs}

    def deep_update(d, u):
        for k, v in u.items():
            if isinstance(v, Mapping) and k in d and isinstance(d[k], Mapping):
                deep_update(d[k], v)
            else:
                d[k] = v
        return d

    deep_update(base_configs, model_configs[configs.model_name])
    return parse_configs(configs=base_configs, arg_str=arg_str, fill_required_with_null=True)


def run_protenix(
    *,
    input_json_path: Path,
    output_dir: Path,
    model_name: str,
    checkpoint_dir: Path,
    seeds: list[int],
    n_step: int,
    n_sample: int,
    sigma_max: float,
    guidance_enable: bool,
    low_vram: bool,
) -> Path:
    """Run one Protenix inference job via the stock InferenceRunner.

    Mode behaviour (score-only bypass, input-coord init, contact injection) is
    driven entirely by the PROTENIX_* side-channel env vars consumed by the
    vendored protenix fork; see protenix/model/protenix.py::_load_p2d_side_channels.
    """
    import os

    if low_vram:
        os.environ.setdefault("PROTENIX_LOW_VRAM", "1")

    from runner.inference import InferenceRunner, infer_predict

    configs = build_configs(
        input_json_path=input_json_path,
        output_dir=output_dir,
        model_name=model_name,
        checkpoint_dir=checkpoint_dir,
        seeds=seeds,
        n_step=n_step,
        n_sample=n_sample,
        sigma_max=sigma_max,
        guidance_enable=guidance_enable,
    )
    runner = InferenceRunner(configs)
    infer_predict(runner, configs)
    return output_dir


def collect_results(output_dir: Path) -> dict[str, Any]:
    """Summarise dumper outputs (per-seed confidence jsons + structures).

    Layout: <dump_dir>/<sample_name>/seed_<seed>/predictions/*.cif and
    *_summary_confidence_sample_<i>.json.
    """
    all_confidences: list[dict[str, Any]] = []
    structures: list[dict[str, str]] = []
    for conf_file in sorted(output_dir.glob("**/*_summary_confidence_sample_*.json")):
        payload = json.loads(conf_file.read_text(encoding="utf-8"))
        all_confidences.append(
            {
                "sample": int(conf_file.stem.rsplit("_sample_", 1)[1]),
                "seed": conf_file.parent.name,
                "file": str(conf_file),
                "ranking_score": payload.get("ranking_score"),
                "iptm": payload.get("iptm"),
                "ptm": payload.get("ptm"),
                "confidence_score": payload.get("confidence_score"),
                "plddt": payload.get("plddt"),
                "chain_iptm": payload.get("chain_iptm"),
            }
        )
    for cif in sorted(output_dir.glob("**/seed_*/predictions/*.cif")):
        structures.append({"seed": cif.parent.parent.name, "path": str(cif)})
    best = None
    if all_confidences:
        best = max(
            all_confidences,
            key=lambda c: (
                c["ranking_score"] is not None,
                float(c["ranking_score"] or 0.0),
            ),
        )
    return {
        "n_confidences": len(all_confidences),
        "confidences": all_confidences,
        "structures": structures,
        "best": best,
    }
