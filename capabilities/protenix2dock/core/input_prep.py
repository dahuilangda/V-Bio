"""Input preparation for protenix2dock.

Builds the Protenix input json, resolves MSAs (cache-first, then the
ColabFold MSA server), and places ligand conformers. Coordinate
alignment lives in alignment.py; TFG constraint builders live in
constraints.py; geometry reference tables live in geometry_tables.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import math
from itertools import combinations

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem

from core.structure import ProteinChainData


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a pid-unique temp file + os.replace: concurrent workers never
    share the temp file, so a published cache entry is always complete."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _first_msa_seq_stripped(a3m_text: str) -> str:
    """First aligned sequence of an a3m with insertion (lowercase) columns removed,
    mirroring the vendor's MSACore.sequences_to_array shape check."""
    lines = a3m_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(">"):
            seq = lines[i + 1].strip() if i + 1 < len(lines) else ""
            return "".join(ch for ch in seq if not ch.islower())
    return ""


MSA_ENV_MIN_AA = 50


def auto_msa_mode(sequence: str, min_env_aa: int = MSA_ENV_MIN_AA) -> str:
    """Length-predicted search tier (the a-priori cost model).

    The env-db cost sits in the CPU result2msa stage and scales with the
    weak-hit count, which explodes for SHORT queries --
    a 20-aa designed peptide ran 30-60 min while a 22-aa protein-like
    sequence took 20 s and an 83-aa receptor minutes (long chains have
    specific k-mer matches, so the env stage stays well-behaved). Length is
    the best predictor available before running the search; explicit tiers
    override it when the caller knows better.
    """
    seq = "".join(
        aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
        for aa in str(sequence or "").strip().upper())
    return "env" if len(seq) >= int(min_env_aa) else "uniref"


def resolve_msa(
    sequence: str,
    chain_label: str,
    msa_cache_dir: Path | None,
    msa_server_url: str | None,
    msa_dir: Path,
    timeout: int = 600,
    msa_mode: str = "auto",
) -> str:
    """Return the path of an a3m MSA for the sequence (cache-first).

    Official contract (AF3 / ColabFold / Boltz): the MSA is OPTIONAL input.
      - search completes with no homologs -> the a3m keeps the query row only
        (N_msa=1); the engine featurizes it natively. This is the requested
        input, not a degraded run.
      - no MSA server configured      -> same representation (query-only a3m);
        the caller opted out of external MSA generation.
      - transport failure / timeout with a server configured -> raises:
        infrastructure errors must surface, never masquerade as "no hits".

    Two search tiers (efficiency policy, both real searches — never a stub):
      env     UniRef30 (3 iterations) + metagenome env-db. Minutes for
              proteins; 30-60 min for designed short peptides (thousands of
              weak env hits make the CPU result2msa stage explode — the GPU
              prefilter is not the bottleneck).
      uniref  UniRef30 only. Seconds; the screening tier for peptide chains.

    The sequence is normalized to the standard 20 amino acids before hashing
    (same rule boltz2score applies) so both engines share cache keys. The
    scratch file is keyed by chain label + sequence hash + tier: label-only
    keys made later samples read the first protein's MSA, and tier-blind keys
    would serve a uniref screening MSA to an env request.
    """
    msa_dir.mkdir(parents=True, exist_ok=True)
    sequence = "".join(
        aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
        for aa in sequence.strip().upper())
    msa_mode = (msa_mode or "auto").strip().lower() or "auto"
    if msa_mode == "auto":
        msa_mode = auto_msa_mode(sequence)
    h = _md5(sequence)
    cache_name = f"msa_{h}.a3m" if msa_mode == "env" else f"msa_{h}_{msa_mode}.a3m"
    out = msa_dir / f"{chain_label}_{h}_{msa_mode}_msa.a3m"

    def _first_seq_len(text: bytes) -> int:
        return len(_first_msa_seq_stripped(text.decode("utf-8", "replace")))

    query_len = len((sequence or "").strip())
    if msa_cache_dir:
        cached = msa_cache_dir / cache_name
        if cached.exists():
            if _first_seq_len(cached.read_bytes()) != query_len:
                raise ValueError(
                    f"cached MSA {cached} does not match the query length "
                    f"({query_len}); purge the cache entry")
            _atomic_write(out, cached.read_bytes())
            print(f"[Info] MSA cache hit for chain {chain_label} "
                  f"({h}, tier={msa_mode}).")
            return str(out)
    if not msa_server_url:
        # Official semantics: MSA is optional input. No server == the caller
        # opted out; represent it exactly like a completed no-hits search.
        _atomic_write(out, f">query\n{sequence}\n".encode("utf-8"))
        print(f"[Info] no MSA server configured for chain {chain_label}; "
              "using the query-only MSA (official N_msa=1 contract).")
        return str(out)
    a3m = _fetch_msa_from_server(sequence, msa_server_url, timeout, msa_mode)
    if _first_seq_len(a3m.encode("utf-8")) != query_len:
        raise ValueError(
            f"MSA server returned a mismatched alignment for chain "
            f"{chain_label} (first sequence length != {query_len})")
    _atomic_write(out, a3m.encode("utf-8"))
    if msa_cache_dir:
        # 回写共享缓存，避免各链路重复拉取
        try:
            _atomic_write((msa_cache_dir / cache_name), a3m.encode("utf-8"))
        except OSError as exc:
            print(f"[Warn] could not write back shared MSA cache ({exc}); continuing.")
    print(f"[Info] MSA fetched from server for chain {chain_label} "
          f"(tier={msa_mode}).")
    return str(out)


def _merge_a3m(first: str, second: str) -> str:
    """Concatenate two a3m texts, dropping duplicate aligned sequences.

    The query (first entry) is kept exactly once; the second file's query
    row and any sequence already present are skipped."""
    def _entries(text):
        header, block = None, []
        for line in text.splitlines():
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(block)
                header, block = line, []
            elif header is not None:
                block.append(line)
        if header is not None:
            yield header, "".join(block)

    out, seen = [], set()
    for header, seq in list(_entries(first)) + list(_entries(second)):
        if seq in seen:
            continue
        seen.add(seq)
        out.append((header, seq))
    return "".join(f"{h}\n{s}\n" for h, s in out)


def _fetch_msa_from_server(
    sequence: str, server_url: str, timeout: int, msa_mode: str = "env",
) -> str:
    """ColabFold-compatible MSA fetch: submit ticket, poll, download, extract
    and merge the uniref + metagenome a3m (mode=env enables the server's
    environmental database stage — metagenome hits raise interface
    confidence substantially over UniRef alone).  Raises on transport,
    ticket-status, or timeout failure."""
    import gzip
    import io
    import tarfile
    import zipfile

    import requests

    base = server_url.rstrip("/")
    q = sequence if sequence.startswith(">") else f">query\n{sequence}"
    # 提交对瞬时故障（服务器恰在重启/网络抖动）做有限重试：一次连接错误直接失败
    # 会把一个本可正常完成的候选降级成无 MSA。
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{base}/ticket/msa", data={"q": q, "mode": msa_mode}, timeout=30
            )
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"MSA submit failed: HTTP {getattr(resp, 'status_code', 'no response')}")
    ticket = resp.json().get("id")
    if not ticket:
        raise RuntimeError("MSA submit returned no ticket id")

    deadline = time.time() + timeout
    download_url = None
    while time.time() < deadline:
        # 轮询途中的瞬时故障（服务器重启、502、非 JSON 响应）不立即失败，
        # 继续在同一个 deadline 内重试——deadline 本身就是总超时上限。
        try:
            st = requests.get(f"{base}/ticket/{ticket}", timeout=30).json()
            status = st.get("status")
        except (requests.RequestException, ValueError) as exc:
            print(f"[Warn] MSA ticket {ticket} poll hiccup ({exc}); retrying within deadline.")
            time.sleep(5)
            continue
        if status == "COMPLETE":
            # Prefer the server-provided result_url; the conventional
            # /result/download endpoint is the documented fallback.
            download_url = st.get("result_url") or f"{base}/result/download/{ticket}"
            break
        if status in ("ERROR", "FAILURE"):
            raise RuntimeError(f"MSA ticket {ticket} failed: {status}")
        time.sleep(5)
    else:
        # Client gave up: cancel the ticket so the server does not keep
        # burning a worker slot + GPU lease on an orphan. Best-effort — cancellation must never mask the
        # original timeout.
        try:
            requests.delete(f"{base}/ticket/{ticket}", timeout=15)
        except requests.RequestException:
            pass
        raise TimeoutError(f"MSA ticket {ticket} not complete after {timeout}s")

    r = requests.get(download_url, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"MSA download failed: HTTP {r.status_code}")
    blob = r.content
    if blob[:2] == b"\x1f\x8b":  # gzip container — unwrap first
        blob = gzip.decompress(blob)

    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace").replace("\x00", "")

    if blob.lstrip()[:1] == b">":  # bare a3m text
        return _decode(blob)
    if blob[:2] == b"PK":  # zip
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name.endswith(".a3m"):
                    raw = zf.read(name)
                    if name.endswith(".gz"):
                        raw = gzip.decompress(raw)
                    return _decode(raw)
        raise ValueError(
            f"zip payload carries no .a3m member: {sorted(zf.namelist())[:5]}")
    # tar container (tarfile handles gz/bz2 transparently)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
            members = {m.name: m for m in tf.getmembers()}
            uniref = next((m for m in members.values()
                           if m.name.endswith(".a3m")
                           and "mgnify" not in m.name), None)
            env = members.get("bfd.mgnify30.metaeuk30.smag30.a3m")
            if uniref is not None:
                merged = _decode(tf.extractfile(uniref).read())
                if env is not None:
                    merged = _merge_a3m(
                        merged, _decode(tf.extractfile(env).read()))
                return merged
    except tarfile.TarError as exc:
        raise ValueError(f"MSA payload not recognized as a3m/zip/tar: {blob[:8]!r}") from exc
    raise ValueError(f"MSA payload not recognized as a3m/zip/tar: {blob[:8]!r}")


def place_dock_conformer(
    smiles: str,
    center: tuple[float, float, float],
    out_sdf: Path,
    seed: int = 42,
) -> Chem.Mol:
    """Embed a 3D conformer for the SMILES and translate it to the pocket centre."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"Invalid ligand SMILES: {smiles}")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise ValueError(f"Conformer generation failed for SMILES: {smiles}")
    AllChem.MMFFOptimizeMolecule(mol)
    mol_no_h = Chem.RemoveHs(mol)
    conf = mol_no_h.GetConformer()
    centroid = np.array([list(conf.GetAtomPosition(i)) for i in range(mol_no_h.GetNumAtoms())]).mean(axis=0)
    offset = np.asarray(center, dtype=np.float64) - centroid
    for i in range(mol_no_h.GetNumAtoms()):
        pos = np.array(list(conf.GetAtomPosition(i))) + offset
        conf.SetAtomPosition(i, pos.tolist())
    out_sdf.parent.mkdir(parents=True, exist_ok=True)
    w = Chem.SDWriter(str(out_sdf))
    w.write(mol_no_h)
    w.close()
    return mol_no_h


def load_ligand_pose(ligand_path: Path) -> Chem.Mol:
    """Load a posed ligand (SDF) and strip explicit Hs to match Protenix order."""
    if ligand_path.suffix.lower() != ".sdf":
        raise ValueError(f"protenix2dock expects an SDF ligand, got {ligand_path}")
    suppl = Chem.SDMolSupplier(str(ligand_path), removeHs=False)
    mol = next(iter(suppl), None)
    if mol is None or mol.GetNumConformers() == 0:
        raise ValueError(f"Failed to read a 3D ligand pose from {ligand_path}")
    return Chem.RemoveHs(mol)


def build_input_json(
    *,
    chains: list[ProteinChainData],
    ligand_sdf: Path,
    sample_name: str,
    msa_paths: dict[str, str],
    seeds: list[int],
) -> dict:
    sequences: list[dict] = []
    for chain in chains:
        protein: dict[str, Any] = {"sequence": chain.sequence, "count": 1}
        mods = chain.modifications
        if mods:
            protein["modifications"] = mods
        msa = msa_paths.get(chain.chain_name)
        if msa:
            protein["unpairedMsaPath"] = msa
        sequences.append({"proteinChain": protein})
    sequences.append({"ligand": {"ligand": f"FILE_{ligand_sdf}", "count": 1}})
    return {
        "name": sample_name,
        "sequences": sequences,
        "modelSeeds": seeds,
        "bondedAtomPairs": [],
        "userCCD": [],
    }


def build_peptide_complex_input(
    *,
    receptor_chains: list[ProteinChainData],
    peptide_chain: ProteinChainData,
    msa_paths: dict[str, str],
    linker_ccd: str | None,
    covalent_bonds: list[dict[str, Any]],
    sample_name: str,
    seeds: list[int],
) -> dict:
    """Input json for receptor-fixed peptide design/refinement.

    The peptide is a first-class proteinChain (never an SDF ligand): the
    pairformer and the diffusion condition on it exactly like the protein it
    is. An optional linker is a CCD ligand covalently bonded to the peptide
    via input.json covalent_bonds — the bicyclic ring constraint.
    """
    sequences: list[dict] = []
    for chain in receptor_chains:
        protein: dict[str, Any] = {"sequence": chain.sequence, "count": 1}
        mods = chain.modifications
        if mods:
            protein["modifications"] = mods
        msa = msa_paths.get(chain.chain_name)
        if msa:
            protein["unpairedMsaPath"] = msa
        sequences.append({"proteinChain": protein})
    peptide: dict[str, Any] = {"sequence": peptide_chain.sequence, "count": 1}
    mods = peptide_chain.modifications
    if mods:
        peptide["modifications"] = mods
    peptide_msa = msa_paths.get(peptide_chain.chain_name)
    if peptide_msa:
        # MSA is enabled for the designed peptide too (user policy: MSA everywhere);
        # a hit-free search degrades to a single-sequence MSA server-side.
        peptide["unpairedMsaPath"] = peptide_msa
    sequences.append({"proteinChain": peptide})
    if linker_ccd:
        sequences.append({"ligand": {"ligand": f"CCD_{linker_ccd}", "count": 1}})
    return {
        "name": sample_name,
        "sequences": sequences,
        "modelSeeds": seeds,
        "covalent_bonds": covalent_bonds,
        "bondedAtomPairs": [],
        "userCCD": [],
    }



# --- Backward-compatible re-exports ---
# Constraint builders and coordinate alignment now live in their own modules;
# these aliases keep existing imports working.
from core.alignment import (  # noqa: F401
    align_init_coords,
    align_complex_init_coords,
)
from core.constraints import (  # noqa: F401
    compute_free_chain_tfg_constraints,
    compute_ligand_covalent_bands,
    compute_bond_contact_pairs,
    compute_vdw_shell_constraints,
)
