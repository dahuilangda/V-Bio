"""Central configuration for HALO runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class GeneratorConfig:
    # prior / agent architecture (small SMILES transformer)
    d_model: int = 256
    n_layers: int = 4
    n_heads: int = 8
    dropout: float = 0.1
    # pretraining
    max_epochs: int = 20
    batch_size: int = 256
    lr: float = 3e-4
    val_fraction: float = 0.05
    # sampling
    max_smiles_len: int = 128
    # RL (REINVENT-style augmented likelihood)
    rl_batch_size: int = 512
    rl_epochs_per_round: int = 4
    rl_lr: float = 3e-5
    rl_algorithm: str = "grpo"  # "grpo" (default) or "reinvent"
    rl_clip_eps: float = 0.2    # PPO clip (real trust region needs frozen old log-probs)
    rl_kl_beta: float = 0.01    # k5 (quadratic) KL-to-prior coefficient
    rl_ent_coef: float = 0.003  # token entropy bonus (anti-collapse)
    rl_use_replay: bool = True
    sigma: float = 8.0  # reward scaling
    alpha_init: float = 0.98  # prior weight in augmented likelihood
    alpha_decay: float = 0.998  # per round
    alpha_min: float = 0.60
    kl_beta: float = 0.15  # soft KL penalty toward prior
    replay_size: int = 4096


@dataclass
class SurrogateConfig:
    # ECFP -> multi-task ensemble MLP
    fp_bits: int = 2048
    fp_radius: int = 2
    hidden: tuple = (1024, 512)
    ensemble_size: int = 5
    dropout: float = 0.1
    epochs: int = 60
    batch_size: int = 128
    lr: float = 1e-3
    weight_decay: float = 1e-5
    # targets predicted: affinity_pic50, ipsae, ligand_plddt_mean


@dataclass
class OracleConfig:
    gpus: tuple = (1, 2, 3)
    score_batch_size: int = 24  # ligands per CLI invocation per GPU
    recycling_steps: int = 1
    affinity_recycling_steps: int = 1
    precision: str = "bf16-mixed"
    conformers_per_ligand: int = 4
    score_timeout_s: int = 3600


@dataclass
class LoopConfig:
    n_rounds: int = 12
    n_agent_samples: int = 384
    n_mmp_samples: int = 256
    n_random_mutants: int = 64
    oracle_budget_per_round: int = 48  # molecules sent to Boltz2Score each round
    acquisition_kappa: float = 1.0  # uncertainty weight in UCB-style acquisition
    exploit_fraction: float = 0.5  # fraction of oracle budget spent on predicted-best
    human_every_rounds: int = 4
    human_topk: int = 12
    # ablations
    use_surrogate: bool = True
    use_agent: bool = True
    use_dpo: bool = True   # align the agent on human preferences (RLHF/DPO)
    use_rnd_diversity: bool = True  # TanhIMS + RND multi-objective shaping
    ims_bucket: int = 25
    unified_prior: bool = False  # one <cont>-prompt operator for all edit modes + de novo
    use_human: bool = True
    # reward
    pref_lambda_max: float = 0.30  # max weight of preference model in final reward
    diversity_scaffold_limit: int = 3  # max molecules per scaffold Murcko in final pool
    seed_similarity_band: tuple = (0.2, 0.9)  # Tanimoto band to seed set (scaffold-preserve)
    keep_fragment_smiles: str = ""  # scenario c: fragment that must be conserved
    edit_atom_indices: tuple = ()   # scenario d: user-directed edit position (canonical atom order)
    reference_smiles: str = ""      # scenario a/d: the user's lead
    scaffold_hop_ratio: float = 0.0  # fraction of edit proposals that hop scaffolds
    conservative_kappa: float = 0.3  # risk-averse surrogate penalty (sigma multiple)
    use_pose_gate: bool = True  # pose-evidence gating of the affinity reward
    seed_ligands: tuple = ()    # benchmark protocol: visible seed subset (weaker half); holdout stays hidden


@dataclass
class HaloConfig:
    target_name: str = "cdk8"
    run_dir: str = "runs/default"
    seed: int = 0
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    surrogate: SurrogateConfig = field(default_factory=SurrogateConfig)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, default=list))

    @classmethod
    def load(cls, path: Path) -> "HaloConfig":
        data = json.loads(Path(path).read_text())

        def build(dc, payload):
            flds = {f.name for f in dc.__dataclass_fields__.values()}
            return dc(**{k: v for k, v in payload.items() if k in flds})

        cfg = build(cls, data)
        for name in ("generator", "surrogate", "oracle", "loop"):
            sub = getattr(cfg, name)
            if name in data:
                setattr(cfg, name, build(type(sub), data[name]))
        return cfg
