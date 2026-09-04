"""V-Bio native prediction oracle: score candidates through the platform's own
structure-prediction engines.

Unlike BoltzOracle (boltz2score score-mode CLI) this submits one complex
prediction per candidate via the local V-Bio API — backend protenix2dock
(default), boltz2dock, or alphafold3 — with the affinity post-process enabled,
then parses the resulting archive:

  confidence_data_model_0.json / best_confidence.json → iptm, ligand_iptm
  best_ipsae.json                                       → ipsae_dom
  affinity_data.json                                    → affinity_pic50*
  best structure .cif B-factors (ligand chain)          → ligand_plddt_mean

Interface-compatible with the other oracles: score_smiles(list, tag) ->
DataFrame in input order with the columns the reward/surrogate stack consumes.
Failed predictions surface as rows with missing metric columns — the same
contract BoltzOracle uses for pose-failed molecules.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pandas as pd

_TERMINAL_OK = {"success", "succeeded", "completed"}
_TERMINAL_BAD = {"failure", "failed", "revoked", "rejected"}

_STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "MSE", "SEC", "PYL",
}

_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "MSE": "M", "SEC": "U", "PYL": "O",
}


def extract_protein_sequence_from_pdb(pdb_path: Path, chain: str | None = None) -> str:
    """Canonical one-letter sequence of the (first requested) protein chain."""
    seq: list[str] = []
    seen: set[str] = set()
    with open(pdb_path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                continue
            resname = line[17:20].strip().upper()
            if resname not in _STANDARD_AA:
                continue
            line_chain = line[21:22].strip()
            if chain and line_chain and line_chain != chain:
                continue
            key = f"{line_chain}:{line[22:27].strip()}"
            if key in seen:
                continue
            seen.add(key)
            seq.append(_THREE_TO_ONE.get(resname, "X"))
            if not chain and line_chain:
                chain = line_chain
    return "".join(seq)


def _ligand_plddt_from_cif(cif_text: str, ligand_chain_hint: str | None) -> float | None:
    """Mean pLDDT (B-factor column) over the ligand chain of a predicted model."""
    lines = cif_text.splitlines()
    fields: list[str] = []
    in_atom_site = False
    values: list[float] = []
    for line in lines:
        if line.strip().startswith("_atom_site."):
            if not in_atom_site:
                in_atom_site = True
                fields = []
            fields.append(line.strip().split(".", 1)[1])
            continue
        if in_atom_site:
            token = line.strip()
            if not token or token in ("#", "loop_") or token.startswith("_") or token.startswith("data_"):
                if values:
                    break
                continue
            parts = token.split()
            if len(parts) < len(fields):
                continue

            def col(name: str) -> str:
                idx = fields.index(name) if name in fields else -1
                return parts[idx] if idx >= 0 else ""

            resname = col("label_comp_id").upper()
            if resname in _STANDARD_AA or resname in ("HOH", "WAT"):
                continue
            chain_id = col("auth_asym_id") or col("label_asym_id")
            if ligand_chain_hint and chain_id and chain_id != ligand_chain_hint:
                continue
            try:
                values.append(float(col("B_iso_or_equiv")))
            except ValueError:
                continue
    if not values:
        return None
    return sum(values) / len(values)


class PredictOracle:
    """Oracle backed by V-Bio's own prediction pipeline (protenix2dock default)."""

    SUPPORTED_BACKENDS = ("protenix2dock", "boltz2dock", "alphafold3")
    DEFAULT_BACKEND = "protenix2dock"

    def __init__(
        self,
        target,
        work_dir,
        backend: str = DEFAULT_BACKEND,
        timeout_s: int = 7200,
        api_url: str | None = None,
        api_token: str | None = None,
        priority: str = "default",
        seed: int | None = None,
        log=print,
    ):
        self.target = target
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        normalized = str(backend or "").strip().lower()
        if normalized not in self.SUPPORTED_BACKENDS:
            raise ValueError(
                f"Unsupported prediction backend '{backend}'. "
                f"Supported: {', '.join(self.SUPPORTED_BACKENDS)}."
            )
        self.backend = normalized
        self.timeout_s = int(timeout_s)
        self.base = (api_url or os.environ.get("VBIO_API_URL", "http://127.0.0.1:5000")).rstrip("/")
        self.token = api_token or os.environ.get("VBIO_API_TOKEN", "")
        if not self.token:
            raise RuntimeError(
                "PredictOracle needs the runtime API token (VBIO_API_TOKEN or api_token); "
                "without it every submission would be rejected 403."
            )
        self.priority = priority
        self.seed = seed
        self.log = log
        self.n_calls = 0
        self.n_gpu_seconds = 0.0
        self._sequence: str | None = None

    # ------------------------------------------------------------- plumbing
    def _protein_sequence(self) -> str:
        if self._sequence is None:
            chain = str(self.target.target_chain or "").strip() or None
            seq = extract_protein_sequence_from_pdb(Path(self.target.protein_pdb), chain)
            if not seq:
                raise ValueError(
                    f"No protein sequence could be extracted from {self.target.protein_pdb}"
                )
            self._sequence = seq
        return self._sequence

    def _build_yaml(self, smiles: str) -> str:
        import yaml

        payload = {
            "version": 1,
            "sequences": [
                # msa disabled keeps the request local: the uploaded template
                # conditions the fold, so no MSA round-trip per candidate.
                {"protein": {"id": "A", "sequence": self._protein_sequence(), "msa": False}},
                {"ligand": {"id": "B", "smiles": smiles}},
            ],
            # Canonical affinity request form (see extract_affinity_config_from_yaml).
            "properties": [{"affinity": {"binder": "B"}}],
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    def _submit(self, smiles: str, tag: str) -> str | None:
        import requests

        protein_bytes = Path(self.target.protein_pdb).read_bytes()
        data = {
            "backend": self.backend,
            "workflow": "lead_optimization",
            "priority": self.priority,
            "properties": json.dumps({"affinity": True, "ligand": "B", "binder": "B", "target": "A"}),
            "require_ipsae": "true",
            # Pocket-bound complexes: no MSA round-trip per candidate.
            "use_msa_server": "false",
            "template_meta": json.dumps([
                {"file_name": "template.pdb", "format": "pdb", "chain_id": "A", "target_chain_ids": ["A"]}
            ]),
        }
        if self.seed is not None:
            data["seed"] = str(self.seed)
        files = {
            "yaml_file": ("input.yaml", self._build_yaml(smiles).encode(), "text/yaml"),
            "template_files": ("template.pdb", protein_bytes, "chemical/x-pdb"),
        }
        try:
            r = requests.post(
                f"{self.base}/predict",
                data=data,
                files=files,
                headers={"X-API-Token": self.token},
                timeout=120,
            )
        except Exception as exc:
            self.log(f"[halo-oracle] submit error for {smiles[:32]}…: {exc}")
            return None
        if r.status_code != 202:
            self.log(f"[halo-oracle] submit rejected ({r.status_code}): {r.text[:200]}")
            return None
        return r.json().get("task_id")

    def _fetch_result(self, task_id: str, out_dir: Path) -> bool:
        import requests

        r = requests.get(
            f"{self.base}/results/{task_id}",
            headers={"X-API-Token": self.token},
            timeout=600,
        )
        if r.status_code != 200:
            self.log(f"[halo-oracle] results download failed ({r.status_code}) for {task_id}")
            return False
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(out_dir)
            return True
        except Exception as exc:
            self.log(f"[halo-oracle] archive extract failed for {task_id}: {exc}")
            return False

    def _parse_result(self, out_dir: Path) -> dict:
        row: dict = {}

        def read_json(name_glob: str) -> dict | None:
            matches = sorted(out_dir.rglob(name_glob))
            if not matches:
                return None
            try:
                return json.loads(matches[0].read_text())
            except Exception:
                return None

        conf = read_json("best_confidence.json") or read_json("confidence_data_model_0.json")
        if conf:
            row["iptm"] = conf.get("iptm")
            row["ligand_iptm"] = conf.get("ligand_iptm")
            row["confidence_score"] = conf.get("confidence_score")
        ipsae = read_json("best_ipsae.json")
        if ipsae:
            row["ipsae"] = ipsae.get("ipsae_dom")
        affinity = read_json("affinity_data.json")
        if affinity:
            row["affinity_pic50"] = affinity.get("affinity_pic50")
            row["affinity_pic50_mw"] = affinity.get("affinity_pic50_mw")
            ligand_chain = affinity.get("binder_chain") or affinity.get("requested_ligand_chain")
        else:
            ligand_chain = None
        # Ligand pLDDT from the best model's B-factors.
        for pattern in ("best_model.cif", "data_model_0.cif"):
            structures = sorted(out_dir.rglob(pattern))
            if not structures:
                continue
            try:
                value = _ligand_plddt_from_cif(structures[0].read_text(), ligand_chain)
            except Exception:
                value = None
            if value is not None:
                row["ligand_plddt_mean"] = value
                break
        return row

    # ----------------------------------------------------------------- main
    def score_smiles(self, smiles_list, tag="batch") -> pd.DataFrame:
        """Submit the whole batch, then poll until all results land.

        No orchestrator-side concurrency cap: the runtime's shared GPU pool is
        what schedules the submitted tasks, so every candidate is dispatched
        immediately and freed GPUs are picked up as they release.
        """
        import requests

        t0 = time.time()
        deadline = t0 + self.timeout_s
        self.n_calls += len(smiles_list)
        rows: list = [None] * len(smiles_list)

        pending: dict[str, int] = {}
        for idx, smiles in enumerate(smiles_list):
            task_id = self._submit(smiles, tag)
            if task_id is None:
                rows[idx] = {"smiles": smiles, "pose_method": "failed"}
                continue
            pending[task_id] = idx

        while pending:
            if time.time() > deadline:
                self.log(
                    f"[halo-oracle] batch {tag} exceeded timeout ({self.timeout_s}s); "
                    f"{len(pending)} in flight"
                )
                break
            time.sleep(10)
            for task_id in list(pending):
                if time.time() > deadline:
                    break
                idx = pending[task_id]
                try:
                    r = requests.get(
                        f"{self.base}/status/{task_id}",
                        headers={"X-API-Token": self.token},
                        timeout=60,
                    )
                    state = str((r.json() if r.status_code == 200 else {}).get("state") or "").lower()
                except Exception as exc:
                    self.log(f"[halo-oracle] poll error for {task_id}: {exc}")
                    continue
                if state not in _TERMINAL_OK and state not in _TERMINAL_BAD:
                    continue
                del pending[task_id]
                out_dir = self.work_dir / f"{tag}_{idx}"
                row = {"smiles": smiles_list[idx], "pose_method": "predicted"}
                if state in _TERMINAL_OK and self._fetch_result(task_id, out_dir):
                    row.update(self._parse_result(out_dir))
                else:
                    row["pose_method"] = "failed"
                rows[idx] = row

        # Anything still pending at deadline (or left None) is a failed row.
        for idx in pending.values():
            rows[idx] = {"smiles": smiles_list[idx], "pose_method": "failed"}
        for i, smiles in enumerate(smiles_list):
            if rows[i] is None:
                rows[i] = {"smiles": smiles, "pose_method": "failed"}

        self.n_gpu_seconds += time.time() - t0
        df = pd.DataFrame(rows)
        df.attrs["wall_s"] = time.time() - t0
        return df
