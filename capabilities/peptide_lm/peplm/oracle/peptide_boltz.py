"""Peptide-protein Boltz-2 oracle (multi-GPU, local CLI).

Score a batch of peptide candidates against a target sequence:
  candidate tokens -> Boltz YAML (base sequence + NCAA modifications + cyclic
  flag, the production protocol) -> `boltz predict` per GPU chunk -> parse
  ipTM / pair ipTM / per-residue pLDDT (mmCIF B-factor) -> ipSAE from the
  PAE npz + structure (metrics.ligand_ipsae, same module the production
  pipeline uses).

Cost control: one CLI process per GPU chunk amortizes model loading; failed
chunks are bisected to isolate offending candidates (HALO's bisect pattern —
a single degenerate peptide can otherwise kill a whole batch).
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from peplm.candidate import Candidate
from peplm.oracle.ccdcache import prepare_run_cache
from peplm.vocab import to_modifications

BOLTZ_PY = "/data/Boltz2Score/.venv/bin/python"

# V-Bio production bicyclic protocol (run_single_prediction.py)
BICYCLIC_LINKER_ATOM_MAP = {
    "SEZ": ["CD", "C1", "C2"],
    "29N": ["C16", "C19", "C25"],
}


def enforce_bicyclic_cys(residues: list[str], cys_positions: list[int],
                         rng: random.Random | None = None,
                         layout: str = "first_last",
                         protected: set[int] | None = None) -> list[str]:
    """Bicyclic Cys layout enforcement.

    layout="first_last": Cys at position 1, one interior anchor (the first
    user-pinned Cys if any, else cys_positions if valid, else the midpoint),
    and the terminal position — the user-spec layout for macrocycle stapling
    at both termini.
    layout="interior_terminal": production default — two interior anchors +
    terminal.
    protected: 0-based positions pinned by the user — never modified; anchor
    correctness on protected positions is validated by PeptideLoop.__init__
    (fixed non-Cys at pos 1 / terminal is rejected there), not here.
    Every other C is replaced (deterministic-repair semantics)."""
    import random as _r

    rng = rng or _r.Random(0)
    no_c = "ARNDQEGHILKMFPSTWYV"
    protected = set(protected or [])
    seq = list(residues)
    L = len(seq)
    terminal = L - 1
    anchors: set[int] = set()
    if layout == "first_last":
        anchors.update({0, terminal})
        interior = None
        # free-optimization default: prefer the midpoint when the user did
        # not specify an interior anchor (a user-pinned Cys wins over both)
        for pos in cys_positions:
            if isinstance(pos, int) and 0 < pos < terminal and pos not in protected:
                interior = pos
                break
        if interior is None:
            mid = terminal // 2
            for d in range(terminal):
                cand_pos = mid + d if (d % 2 == 0) else mid - d
                if 0 < cand_pos < terminal and cand_pos not in protected:
                    interior = cand_pos
                    break
        if interior is None:
            raise ValueError("no free interior position for the bicyclic Cys")
        anchors.add(interior)
    else:
        chosen: list[int] = []
        for pos in cys_positions:
            if isinstance(pos, int) and 0 <= pos < terminal and pos not in chosen \
                    and pos not in protected:
                chosen.append(pos)
            if len(chosen) == 2:
                break
        if len(chosen) < 2:
            pool = [i for i in range(terminal) if i not in chosen and i not in protected]
            chosen.extend(rng.sample(pool, k=2 - len(chosen)))
        anchors.update(chosen)
        anchors.add(terminal)
    out = []
    for i, t in enumerate(seq):
        if i in protected:
            out.append(t)          # user-pinned residue wins
        elif i in anchors:
            out.append("C")
        elif t == "C":
            out.append(rng.choice(no_c))
        else:
            out.append(t)
    return out


def build_complex_yaml(target_sequence: str, cand: Candidate,
                       target_id: str = "A", binder_id: str = "B",
                       bicyclic: dict | None = None) -> str:
    """bicyclic: {"cys_positions": [i, j] (0-based interior), "linker_ccd":
    "SEZ", "anchor_positions": [i, j, k] (explicit 0-based anchors; when
    absent the exactly-3 Cys in the sequence are the anchors)} -> peptide +
    linker ligand chain + 3 SG-bond constraints (the production protocol)."""
    res = cand.residues
    base, mods = to_modifications(res)
    binder: dict = {"id": binder_id, "sequence": base, "msa": "empty"}
    if mods:
        binder["modifications"] = [
            {"position": m["position"], "ccd": m["ccd"]} for m in mods]
    if cand.cyclic:
        binder["cyclic"] = True
    data = {
        "sequences": [
            {"protein": {"id": target_id, "sequence": target_sequence,
                         "msa": "empty"}},
            {"protein": binder},
        ],
    }
    if bicyclic:
        linker_ccd = str(bicyclic.get("linker_ccd") or "SEZ").upper()
        atoms = BICYCLIC_LINKER_ATOM_MAP[linker_ccd]
        data["sequences"].append({"ligand": {"id": "C", "ccd": linker_ccd}})
        explicit = sorted({int(p) for p in (bicyclic.get("anchor_positions") or [])
                           if 0 <= int(p) < len(res)})
        cys_idx = explicit or [i for i, t in enumerate(res) if t == "C"]
        if len(cys_idx) != 3:
            raise ValueError(
                f"bicyclic candidate needs exactly 3 anchor Cys, got "
                f"{len(cys_idx)}: {''.join(res)}")
        constraints = []
        for ci, atom in zip(cys_idx, atoms):
            constraints.append({"bond": {
                "atom1": [binder_id, ci + 1, "SG"],
                "atom2": ["C", 1, atom],
            }})
        data["constraints"] = constraints
    return yaml.safe_dump(data, sort_keys=False)


# ----------------------------------------------------------------- parsing
def parse_cif_plddts(cif_path: Path, chain_id: str) -> list[float]:
    """CA-atom B-factors of one chain from an mmCIF atom_site loop."""
    lines = cif_path.read_text().splitlines()
    cols: dict[str, int] = {}
    rows: list[float] = []
    in_loop = False
    for ln in lines:
        s = ln.strip()
        if s == "loop_":
            in_loop = True
            cols = {}
            continue
        if in_loop and s.startswith("_atom_site."):
            name = s.split(".", 1)[1].split()[0]
            cols[name] = len(cols)
            continue
        if in_loop and cols and s.startswith(("A", "H")) and len(s.split()) >= len(cols):
            parts = s.split()
            try:
                if parts[cols["label_atom_id"]] != "CA":
                    continue
                if parts[cols.get("auth_asym_id", cols.get("label_asym_id", 0))] != chain_id:
                    continue
                rows.append(float(parts[cols["B_iso_or_I_equiv"]]))
            except (KeyError, ValueError, IndexError):
                continue
        elif in_loop and cols and not s.startswith("_"):
            if s == "#":
                in_loop = False
    return rows


def _pick_best_model(record_dir: Path) -> int | None:
    """Best diffusion sample by ipTM (production best_confidence rule)."""
    best, best_i = None, None
    for conf in record_dir.glob("confidence_*_model_*.json"):
        i = int(conf.stem.rsplit("_", 1)[1])
        c = json.loads(conf.read_text())
        v = float(c.get("iptm") or 0.0)
        if best is None or v > best:
            best, best_i = v, i
    return best_i


def _parse_record(record_dir: Path, binder_id: str, use_ipsae: bool,
                  n_binder_tokens: int | None = None) -> dict:
    out: dict = {}
    mi = _pick_best_model(record_dir)
    if mi is None:
        return out
    stem = record_dir.name
    conf_path = record_dir / f"confidence_{stem}_model_{mi}.json"
    cif_path = record_dir / f"{stem}_model_{mi}.cif"
    pae_path = record_dir / f"pae_{stem}_model_{mi}.npz"
    plddt_path = record_dir / f"plddt_{stem}_model_{mi}.npz"
    if not conf_path.exists():
        return out
    conf = json.loads(conf_path.read_text())
    out["iptm"] = conf.get("iptm")
    out["ptm"] = conf.get("ptm")
    out["complex_plddt"] = conf.get("complex_plddt")
    # boltz writes pair_chains_iptm keyed by internal numeric chain indices;
    # our YAML always orders target=0, binder=1
    pair = conf.get("pair_chains_iptm") or {}
    try:
        out["pair_iptm"] = pair["0"]["1"]
    except (KeyError, TypeError):
        try:
            out["pair_iptm"] = pair["1"]["0"]
        except (KeyError, TypeError):
            out["pair_iptm"] = conf.get("iptm")
    # per-token pLDDT from the npz: last n_binder tokens are the peptide
    if plddt_path.exists() and n_binder_tokens:
        import numpy as np

        arr = np.load(plddt_path)["plddt"]
        binder = arr[-n_binder_tokens:] * 100.0
        out["binder_plddt"] = [float(v) for v in binder]
        out["binder_avg_plddt"] = float(binder.mean())
    if use_ipsae and pae_path.exists() and cif_path.exists():
        from peplm.oracle.chain_ipsae import chain_pair_ipsae

        r = chain_pair_ipsae(cif_path, pae_path, binder_chain=binder_id)
        out["ipsae_dom"] = r.get("ipsae_dom")
        out["ligand_ipsae_max"] = r.get("ligand_ipsae_max")
        out["interface_pairs"] = r.get("interface_pairs")
        # interchain pAE (Latent-X min_ipae / AlphaProteo): min/mean over all
        # target x binder PAE entries, in Angstrom (lower = more confident)
        out["min_ipae"] = r.get("min_ipae")
        out["mean_ipae"] = r.get("mean_ipae")
    return out


# ------------------------------------------------------------------ oracle
class PeptideBoltzOracle:
    def __init__(self, target_sequence: str, work_dir, gpus=(0, 1, 2, 3),
                 base_cache: str = "/data/boltz_cache", model: str = "boltz2",
                 recycling_steps: int = 3, sampling_steps: int = 200,
                 diffusion_samples: int = 3, max_parallel_samples: int = 1,
                 use_ipsae: bool = True, timeout_s: int = 5400, seed: int = 42,
                 bicyclic: dict | None = None,
                 extra_molecules: list[dict] | None = None, log=print):
        """extra_molecules: user residue entries ({ccd, smiles, base,
        placement}) — registered into the run-local boltz CCD cache so any
        user-supplied amino acid scores identically to presets."""
        self.target_sequence = str(target_sequence).upper()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.gpus = list(gpus) or [0]
        self.model = model
        self.recycling_steps = recycling_steps
        self.sampling_steps = sampling_steps
        self.diffusion_samples = diffusion_samples
        self.max_parallel_samples = max_parallel_samples
        self.use_ipsae = use_ipsae
        self.timeout_s = timeout_s
        self.seed = seed
        self.bicyclic = bicyclic  # {"cys_positions": [...], "linker_ccd": "SEZ"}
        self.log = log
        self.cache = prepare_run_cache(self.work_dir / "oracle", base_cache,
                                       extra_molecules=extra_molecules)
        self.n_calls = 0
        self.wall_s = 0.0

    # ------------------------------------------------------------------
    def score(self, candidates: list[Candidate], tag: str = "b") -> list[Candidate]:
        if not candidates:
            return candidates
        self.n_calls += len(candidates)
        t0 = time.time()
        base = self.work_dir / "oracle" / tag
        indexed = list(enumerate(candidates))
        chunks = [indexed[i::len(self.gpus)] for i in range(len(self.gpus))]
        with ThreadPoolExecutor(max_workers=len(self.gpus)) as pool:
            futures = []
            for gi, chunk in enumerate(chunks):
                if not chunk:
                    continue
                futures.append(pool.submit(self._run_chunk, chunk,
                                           base / f"g{self.gpus[gi]}", self.gpus[gi]))
            for f in futures:
                f.result()
        self.wall_s += time.time() - t0
        return candidates

    def _run_chunk(self, indexed: list[tuple[int, Candidate]], out_dir: Path, gpu):
        if not indexed:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        yaml_dir = out_dir / "yaml"
        yaml_dir.mkdir(exist_ok=True)
        for _, cand in indexed:
            pass  # yamls written per attempt below
        ok = self._attempt(indexed, out_dir, gpu)
        if ok is not None:
            return
        if len(indexed) > 1:
            mid = len(indexed) // 2
            for part, name in ((indexed[:mid], "h1"), (indexed[mid:], "h2")):
                if part:
                    self._run_chunk(part, out_dir / name, gpu)
        else:
            self.log(f"[oracle] single candidate failed: "
                     f"{indexed[0][1].seq_str[:40]}")

    def _attempt(self, indexed, out_dir: Path, gpu) -> set[int] | None:
        """One boltz run over the chunk. Returns parsed indices or None on
        process failure (caller bisects). Per-candidate parse gaps are okay."""
        yaml_dir = out_dir / "yaml"
        for i, cand in indexed:
            (yaml_dir / f"{i:04d}.yaml").write_text(
                build_complex_yaml(self.target_sequence, cand,
                                   bicyclic=self.bicyclic))
        cmd = [
            BOLTZ_PY, "-m", "boltz.main", "predict", str(yaml_dir),
            "--out_dir", str(out_dir / "out"),
            "--cache", str(self.cache),
            "--model", self.model,
            "--accelerator", "gpu", "--devices", "1",
            "--recycling_steps", str(self.recycling_steps),
            "--sampling_steps", str(self.sampling_steps),
            "--diffusion_samples", str(self.diffusion_samples),
            "--max_parallel_samples", str(self.max_parallel_samples),
            "--override", "--seed", str(self.seed),
        ]
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        env["BOLTZ_CACHE"] = str(self.cache)
        r = subprocess.run(cmd, env=env, timeout=self.timeout_s,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        (out_dir / "cli.log").write_text(r.stdout[-20000:])
        if r.returncode != 0:
            raise RuntimeError(
                f"boltz oracle failed (rc={r.returncode}): {r.stdout[-400:]}")
        parsed: set[int] = set()
        pred_roots = sorted((out_dir / "out").glob("*/predictions"))
        if not pred_roots:
            return None
        pred_root = pred_roots[0]
        for i, cand in indexed:
            rd = pred_root / f"{i:04d}"
            if not rd.is_dir():
                continue
            metrics = _parse_record(rd, binder_id="B",
                                    use_ipsae=self.use_ipsae,
                                    n_binder_tokens=len(cand.residues))
            if metrics:
                cand.metrics.update(metrics)
                cand.metrics["record_dir"] = str(rd)
                parsed.add(i)
        # a chunk where nothing parsed counts as a failure -> bisect
        return parsed if parsed else None


# ------------------------------------------------------------------ mock
class MockPeptideOracle:
    """CPU stand-in for smoke tests: score tracks an artificial but learnable
    motif objective (hydrophobic-at-2/charged-at-center pattern), so GRPO has
    a real optimization problem without GPU calls."""

    def __init__(self, motif: str = "W..D", noise: float = 0.02, seed: int = 0):
        import random as _r

        self.rng = _r.Random(seed)
        self.noise = noise
        self.n_calls = 0
        self.wall_s = 0.0

    def score(self, candidates: list[Candidate], tag: str = "b") -> list[Candidate]:
        self.n_calls += len(candidates)
        from peplm.residues import HYDROPATHY
        for cand in candidates:
            res = cand.residues
            iptm = 0.30 + 0.05 * min(len(res), 20) / 20
            iptm += 0.25 * (1 if res and res[0] in "FWY" else 0)
            iptm += 0.20 * (1 if "D" in res or "E" in res else 0)
            iptm += 0.03 * sum(1 for t in res if t.startswith("["))
            iptm += self.rng.gauss(0, self.noise)
            iptm = max(0.0, min(0.95, iptm))
            plddt = [max(30.0, min(99.0, 55 + 40 * iptm + self.rng.gauss(0, 3)))
                     for _ in res]
            cand.metrics.update({
                "iptm": iptm,
                "pair_iptm": iptm,
                "ipsae_dom": max(0.0, iptm - 0.15),
                "binder_plddt": plddt,
                "binder_avg_plddt": sum(plddt) / len(plddt),
                "complex_plddt": sum(plddt) / len(plddt),
                "mock": True,
            })
        return candidates
