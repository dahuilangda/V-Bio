"""protenix2dock mode configurations.

Mirrors the semantics of Boltz2Score's MODE_CONFIGS
(capabilities/boltz2score/core/flexible_optimization.py) so both engines run
the same mode workflow with equivalent noise schedules:

  score     — no diffusion; confidence heads evaluate the input pose directly
  pose      — input-pose anchored refinement, sigma_max 0.02, 8 steps
  refine    — general flexible refinement,       sigma_max 0.03, 10 steps
  interface — interface-focused refinement,      sigma_max 0.04, 12 steps
  dock      — native blind inpainting: receptor pinned, SMILES ligand
              denoises from pure noise on the full schedule (160, 200)
  peptide   — receptor-fixed peptide inpainting; --blind_peptide starts the
              peptide from pure noise on the full schedule (default for the
              V-Bio linear D-route; the staged local refine stays for the
              bicyclic/linker route)

Protenix's InferenceNoiseScheduler uses the same EDM parameterisation as
Boltz2 (sigma_data = 16), so ``s_max == sigma_max`` reproduces Boltz2Score's
schedule range exactly (first-step noise = 16 * s_max Å).
"""

from __future__ import annotations

SCORE_MODE = "score"
POSE_MODE = "pose"
REFINE_MODE = "refine"
INTERFACE_MODE = "interface"
DOCK_MODE = "dock"
PEPTIDE_MODE = "peptide"

SUPPORTED_MODES = (SCORE_MODE, POSE_MODE, REFINE_MODE, INTERFACE_MODE, DOCK_MODE, PEPTIDE_MODE)

MODE_DESCRIPTIONS = {
    SCORE_MODE: "confidence scoring of an input pose (no diffusion)",
    POSE_MODE: "refinement keeping the input pose close",
    REFINE_MODE: "general flexible refinement",
    INTERFACE_MODE: "interface-focused flexible refinement",
    DOCK_MODE: "flexible docking from SMILES (no external docking software needed)",
    PEPTIDE_MODE: "receptor-fixed peptide inpainting (peptide as proteinChain, "
                  "receptor pinned to the input pose, covalent bond TFG for bicyclic)",
}

MODE_CONFIGS: dict[str, dict[str, object]] = {
    SCORE_MODE: {
        "name": "score_default",
        "sigma_max": 0.0,
        "sampling_steps": 0,
        "diffusion_samples": 1,
        "anchor_contact_cutoff": 0.0,
        "anchor_max_distance": 0.0,
    },
    POSE_MODE: {
        "name": "pose_default",
        "sigma_max": 0.02,
        "sampling_steps": 8,
        "diffusion_samples": 5,
        "anchor_contact_cutoff": 5.0,
        "anchor_max_distance": 5.5,
    },
    REFINE_MODE: {
        "name": "refine_default",
        "sigma_max": 0.03,
        "sampling_steps": 10,
        "diffusion_samples": 5,
        "anchor_contact_cutoff": 5.0,
        "anchor_max_distance": 6.0,
    },
    INTERFACE_MODE: {
        "name": "interface_default",
        "sigma_max": 0.04,
        "sampling_steps": 12,
        "diffusion_samples": 5,
        "anchor_contact_cutoff": 5.0,
        "anchor_max_distance": 6.5,
    },
    DOCK_MODE: {
        "name": "dock_default",
        # sigma stays on the calibrated small-sigma ladder: the TFG contact
        # projection is only well-conditioned in the local refinement regime
        # (full generation schedules, s_max=160, make its projection matrix
        # singular). Reference case (CDK8): ligand pLDDT 87.1 /
        # iptm 0.986 / ipsae 0.838.
        "sigma_max": 0.05,
        "sampling_steps": 12,
        "diffusion_samples": 5,
        # Same pocket anchoring semantics as Boltz2Score dock: contacts within
        # 9 A of the placed conformer, upper bound 10 A.
        "anchor_contact_cutoff": 9.0,
        "anchor_max_distance": 10.0,
    },
    PEPTIDE_MODE: {
        "name": "peptide_default",
        # Same calibrated small-sigma ladder as dock: the full generation
        # schedule (s_max=160) makes the TFG constraint projection singular.
        # The receptor is pinned to the input pose for every step (true
        # inpainting); the peptide starts from its placed pose and denoises
        # locally under pocket-anchoring + covalent bond TFG contacts.
        "sigma_max": 0.05,
        "sampling_steps": 12,
        "diffusion_samples": 8,
        "anchor_contact_cutoff": 9.0,
        "anchor_max_distance": 8.0,
    },
}


def built_in_config(mode_name: str) -> dict[str, object]:
    try:
        return MODE_CONFIGS[mode_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported protenix2dock mode {mode_name!r}.") from exc
