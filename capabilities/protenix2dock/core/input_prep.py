"""Input preparation for protenix2dock.

Builds the Protenix input json, aligns user-structure coordinates onto the
Protenix-assembled atom order (for diffusion initialisation), computes pocket
contact pairs for TFG guidance, and resolves MSAs (cache-first, then the
ColabFold MSA server, mirroring the V-Bio backend flow).

This module runs in the Protenix runtime image: it imports protenix's own
json_parser on the same input json the dataloader will consume, so the atom
order is guaranteed to match.
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

from core.structure import ProteinChainData, parse_protein_chains


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
    weak-hit count, which explodes for SHORT queries: measured 2026-09-04 —
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
                f"{base}/ticket/msa", data={"q": q, "mode": "env"}, timeout=30
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
        # burning a worker slot + GPU lease on an orphan (measured: env-db
        # result2msa for a 20-aa designed peptide runs 30-60 min on CPU;
        # an abandoned ticket otherwise occupies the queue long after every
        # client is gone). Best-effort — cancellation must never mask the
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


def align_init_coords(
    input_json_path: Path,
    chains: list[ProteinChainData],
    ligand_mol: Chem.Mol,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Align user coordinates onto Protenix's assembled atom order.

    Runs the featurization pipeline as the inference dataloader
    (SampleDictToFeatures) on the input json, so the atom order is guaranteed
    to match what the model will see.

    Returns (coords [N_atom,3] float32, mask [N_atom] float32, info dict with
    ligand row indices).
    """
    from protenix.data.inference.json_to_feature import SampleDictToFeatures

    with open(input_json_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)[0]
    sample2feat = SampleDictToFeatures(job, extract_features_for_tfg=False)
    feat, atom_array, _ = sample2feat.get_feature_dict()

    n = int(atom_array.array_length())
    coords = np.zeros((n, 3), dtype=np.float32)
    mask = np.zeros(n, dtype=np.float32)

    asym = np.asarray(atom_array.asym_id_int)
    res_id = np.asarray(atom_array.res_id)
    atom_names = [str(a) for a in np.asarray(atom_array.atom_name)]

    # Chain (asym) blocks appear in input.json entity order: proteins first,
    # then the ligand. Map each asym value to its entity index by first row.
    asym_to_entity: dict[int, int] = {}
    for i, value in enumerate(asym):
        v = int(value)
        if v not in asym_to_entity:
            asym_to_entity[v] = len(asym_to_entity)

    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity < len(chains):
            chain = chains[entity]
            key_res = int(res_id[i]) - 1
            if not 0 <= key_res < len(chain.residues):
                continue
            pos = chain.residues[key_res]["atoms"].get(atom_names[i])
            if pos is not None:
                coords[i] = pos
                mask[i] = 1.0

    ligand_entity = len(chains)
    ligand_rows = np.array(
        [i for i in range(n) if asym_to_entity[int(asym[i])] == ligand_entity],
        dtype=np.int64,
    )
    conf = ligand_mol.GetConformer()
    ligand_xyz = np.array(
        [list(conf.GetAtomPosition(i)) for i in range(ligand_mol.GetNumAtoms())],
        dtype=np.float32,
    )
    if len(ligand_rows) == ligand_xyz.shape[0]:
        coords[ligand_rows] = ligand_xyz
        mask[ligand_rows] = 1.0
    else:
        print(
            f"[Warning] Ligand atom mismatch (mol={ligand_xyz.shape[0]}, "
            f"assembled={len(ligand_rows)}); ligand starts from noise."
        )
        ligand_rows = np.array([], dtype=np.int64)

    n_missing = int((mask == 0).sum())
    if n_missing:
        completed = _complete_missing_atoms(coords, mask, feat)
        if completed:
            print(f"[Info] Rebuilt {completed}/{n_missing} atom(s) absent "
                  f"from the source from the CCD reference geometry.")

    print(
        f"[Info] Init coords aligned: {int(mask.sum())}/{n} atoms "
        f"(ligand rows: {len(ligand_rows)})."
    )
    return coords, mask, {"ligand_rows": ligand_rows}


def compute_contact_pairs(
    coords: np.ndarray,
    mask: np.ndarray,
    ligand_rows: np.ndarray,
    contact_cutoff: float,
    max_distance: float,
    max_pairs: int = 240,
    anchor_slack: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Ligand/peptide-pocket contact pairs for TFG guidance.

    For every protein atom within contact_cutoff of the placed ligand, pair it
    with its nearest ligand atom.

    anchor_slack: when set, the per-pair upper bound is tightened to the
    PLACED geometry (d_i + slack, capped by max_distance) instead of a flat
    max_distance. PairwiseDistancePotential is flat-bottomed and its
    projection only fires on out-of-bounds pairs, so a flat 8 A bound lets
    the denoiser drag a whole peptide away while every pair stays
    "satisfied". Per-pair bounds turn the contacts into a real geometric
    anchor: local relaxation inside the slack, projection pulls back any
    larger excursion.
    """
    if ligand_rows.size == 0:
        return None
    ligand_row_set = set(ligand_rows.tolist())
    protein_rows = np.array([i for i in range(len(coords)) if mask[i] > 0 and i not in ligand_row_set])
    if protein_rows.size == 0:
        return None
    lig = coords[ligand_rows]  # [L,3]
    pro = coords[protein_rows]  # [P,3]
    d = np.linalg.norm(pro[:, None, :] - lig[None, :, :], axis=-1)  # [P,L]
    nearest_lig = d.argmin(axis=1)
    nearest_dist = d.min(axis=1)
    selected = np.where(nearest_dist <= contact_cutoff)[0]
    if selected.size == 0:
        # Nothing in range: anchor to the closest 8 protein atoms so the
        # guidance still has a pocket signal.
        order = np.argsort(nearest_dist)[:8]
        selected = order
    if selected.size > max_pairs:
        order = np.argsort(nearest_dist[selected])[:max_pairs]
        selected = selected[order]
    pairs = np.stack(
        [protein_rows[selected], ligand_rows[nearest_lig[selected]]], axis=1
    ).astype(np.int64)
    if anchor_slack is not None:
        upper = np.minimum(
            nearest_dist[selected] + float(anchor_slack),
            float(max_distance),
        ).astype(np.float32)
        # matching lower bound: without it the projection can push the
        # peptide into the receptor wall
        lower = np.maximum(2.2, nearest_dist[selected] - float(anchor_slack)).astype(np.float32)
        print(
            f"[Info] Anchored guidance constraints prepared: {len(pairs)} pairs, "
            f"cutoff={contact_cutoff:.1f}A, per-pair band = d±{anchor_slack:.2f}A "
            f"(upper capped {max_distance:.1f}A, lower floor 2.2A)."
        )
    else:
        upper = np.full(len(pairs), max_distance, dtype=np.float32)
        lower = np.full(len(pairs), 2.2, dtype=np.float32)
        print(
            f"[Info] Anchored guidance constraints prepared: {len(pairs)} pairs, "
            f"cutoff={contact_cutoff:.1f}A, max_distance={max_distance:.1f}A."
        )
    return pairs, upper, lower


def _bond_pairs_as_rows(
    info: dict[str, Any],
    bond_pairs: list[tuple[tuple[str, int, str], tuple[str, int, str]]],
    staged_to_auto: dict[str, str],
    lower: float,
    upper: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Covalent bond pairs as hard-anchor rows (same resolution as
    compute_bond_contact_pairs, plus explicit lower bounds for the band)."""
    resolved = compute_bond_contact_pairs(
        info, bond_pairs, upper, staged_to_auto=staged_to_auto)
    if resolved is None:
        return None
    index, up = resolved
    lo = np.full(len(index), float(lower), dtype=np.float32)
    return index, up, lo


def _assembled_residue_order(
    info: dict[str, Any],
) -> tuple[dict[int, list[int]], dict[tuple[int, int], list[int]]]:
    """Residue structure of the assembled atom table.

    Returns ({asym: [res_id by first appearance]}, {(asym, res_id): rows}).
    First-appearance order (not res_id arithmetic) defines residue
    neighbours: author numbering may be non-contiguous.
    """
    asym = info["asym"]
    res_id = info["res_id"]
    order: dict[int, list[int]] = {}
    groups: dict[tuple[int, int], list[int]] = {}
    for i in range(len(asym)):
        asym_v, rid = int(asym[i]), int(res_id[i])
        seq = order.setdefault(asym_v, [])
        if rid not in seq:
            seq.append(rid)
        groups.setdefault((asym_v, rid), []).append(i)
    return order, groups


# rdkit VDW radii (A) by element; the official clash floor in
# protenix.tfg.potentials is 0.35 + 0.5 * (ri + rj).
_VDW_RADII = {
    "C": 1.70, "N": 1.55, "O": 1.52, "S": 1.80, "P": 1.80,
    "SE": 1.90, "CL": 1.75, "BR": 1.85, "I": 1.98, "F": 1.47,
    "B": 1.92, "ZN": 1.39, "MG": 1.73, "CA": 2.31, "FE": 1.94,
}
_VDW_DEFAULT = 1.70


# Element pairs that form covalent bonds in biomolecules (protein/peptide
# chemistry; keyed as sorted element-pair strings). O-O, N-N, S-O etc. are
# never bonded here — a sub-cutoff pair of those elements marks a deformed
# input, not a bond to preserve.
_PLAUSIBLE_BOND_ELEMENTS = {key: True for key in (
    "CC", "CN", "CO", "CS", "SS", "CP", "OP", "NP", "SP")}


# Standard amino-acid heavy-atom bond graphs (per residue type). Chemistry,
# not geometry: bonds exist because of the residue's covalent structure, so a
# deformed or partially rebuilt input cannot lose a bond from the constraint
# set the way a distance cut-off can. Peptide C(i)-N(i+1) links and the C=O
# carbonyl are added from the backbone.
_STD_AA_BONDS = {
    'ALA': [('CA', 'CB')],
    'ARG': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'NE'),
            ('NE', 'CZ'), ('CZ', 'NH1'), ('CZ', 'NH2')],
    'ASN': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'OD1'), ('CG', 'ND2')],
    'ASP': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'OD1'), ('CG', 'OD2')],
    'CYS': [('CA', 'CB'), ('CB', 'SG')],
    'GLN': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'OE1'),
            ('CD', 'NE2')],
    'GLU': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'OE1'),
            ('CD', 'OE2')],
    'GLY': [],
    'HIS': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'ND1'), ('CG', 'CD2'),
            ('ND1', 'CE1'), ('CD2', 'NE2'), ('CE1', 'NE2')],
    'ILE': [('CA', 'CB'), ('CB', 'CG1'), ('CB', 'CG2'), ('CG1', 'CD1')],
    'LEU': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2')],
    'LYS': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'CE'),
            ('CE', 'NZ')],
    'MET': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'SD'), ('SD', 'CE')],
    'PHE': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'CE1'), ('CD2', 'CE2'), ('CE1', 'CZ'), ('CE2', 'CZ')],
    'PRO': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD'), ('CD', 'N')],
    'SER': [('CA', 'CB'), ('CB', 'OG')],
    'THR': [('CA', 'CB'), ('CB', 'OG1'), ('CB', 'CG2')],
    'TRP': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'NE1'), ('CD2', 'CE2'), ('CD2', 'CE3'), ('NE1', 'CE2'),
            ('CE2', 'CZ2'), ('CE3', 'CZ3'), ('CZ2', 'CH2'), ('CH2', 'CZ3')],
    'TYR': [('CA', 'CB'), ('CB', 'CG'), ('CG', 'CD1'), ('CG', 'CD2'),
            ('CD1', 'CE1'), ('CD2', 'CE2'), ('CE1', 'CZ'), ('CE2', 'CZ'),
            ('CZ', 'OH')],
    'VAL': [('CA', 'CB'), ('CB', 'CG1'), ('CB', 'CG2')],
}
# ideal heavy-atom bond lengths (A) by element pair
_IDEAL_BOND = {('C', 'C'): 1.52, ('C', 'N'): 1.47, ('C', 'O'): 1.43,
               ('C', 'S'): 1.81, ('S', 'S'): 2.05, ('N', 'O'): 1.40,
               ('C', 'P'): 1.84, ('O', 'P'): 1.60, ('N', 'P'): 1.70,
               ('S', 'P'): 2.05}


def _ideal_bond_length(elem_a: str, elem_b: str) -> float:
    key = tuple(sorted((elem_a, elem_b)))
    return _IDEAL_BOND.get(key, 1.50)


def compute_free_chain_tfg_constraints(
    info: dict[str, Any],
    coords: np.ndarray,
    mask: np.ndarray,
    free_entities: set[int] | None = None,
    bond_band: float = 0.15,
) -> dict[str, np.ndarray] | None:
    """Bond and angle constraints for the free chains in official TFG format.

    Follows extract_pairwise_distance_bounds_from_mol semantics
    (protenix/data/core/geometry_featurizer.py): bonds from the residue
    chemical graph with ideal element-pair lengths, angles from bond
    triples (pairs sharing a bonded centre). The output feeds the TFG
    PairwiseDistancePotential, which projects x0 with the official
    angles-then-bonds ordering and a minimum-norm solve.

    Standard residues use _STD_AA_BONDS (chemistry, independent of input
    deformation); non-standard components use distance detection on the
    assembled coordinates (CCD conformer geometry is intact). The peptide
    C(i)-N(i+1) link and the terminal C-OXT bond always apply.

    Returns the pairwise_distance_* arrays or None.
    """
    asym = info["asym"]
    res_id = info["res_id"]
    comp_source = info.get("comp_ids")
    if comp_source is None:
        comp_source = info.get("res_names")
    comp_of = np.asarray(comp_source if comp_source is not None
                         else [""] * len(asym))
    atom_names = [str(a) for a in np.asarray(info["atom_names"]).astype(str)]
    elements = np.char.upper(np.asarray(info["elements"]).astype(str))
    asym_to_entity = info["asym_to_entity"]

    groups: dict[tuple[int, int], list[int]] = {}
    order: dict[int, list[int]] = {}
    for i in range(len(asym)):
        if mask[i] <= 0:
            continue
        asym_v, rid = int(asym[i]), int(res_id[i])
        if free_entities is not None and asym_to_entity[asym_v] not in free_entities:
            continue
        groups.setdefault((asym_v, rid), []).append(i)
        seq = order.setdefault(asym_v, [])
        if rid not in seq:
            seq.append(rid)

    index: list[list[int]] = []
    upper: list[float] = []
    lower: list[float] = []
    is_bond: list[int] = []
    is_angle: list[int] = []

    for asym_v, rids in order.items():
        residues: list[tuple[int, str, dict[str, int]]] = []
        for rid in rids:
            rows = groups.get((asym_v, rid), [])
            comp = str(comp_of[rows[0]]).upper() if rows else ""
            residues.append((rid, comp, {atom_names[i]: i for i in rows}))

        chain_bonds: set[tuple[int, int]] = set()

        def bond(i: int, j: int) -> None:
            key = (min(i, j), max(i, j))
            if key in chain_bonds:
                return
            chain_bonds.add(key)
            rest = _ideal_bond_length(elements[i], elements[j])
            index.append([i, j])
            upper.append(rest + bond_band)
            lower.append(max(rest - bond_band, 0.5))
            is_bond.append(1)
            is_angle.append(0)

        for rid, comp, atoms in residues:
            graph = _STD_AA_BONDS.get(comp)
            if graph is not None:
                for a, b in graph:
                    if a in atoms and b in atoms:
                        bond(atoms[a], atoms[b])
            else:
                names = sorted(atoms)
                for x in range(len(names)):
                    for y in range(x + 1, len(names)):
                        i, j = atoms[names[x]], atoms[names[y]]
                        d = float(np.linalg.norm(coords[i] - coords[j]))
                        cutoff = 2.15 if 'S' in (elements[i], elements[j]) else 1.95
                        if d <= cutoff:
                            bond(i, j)
            for a, b in (('N', 'CA'), ('CA', 'C'), ('C', 'O'), ('C', 'OXT')):
                if a in atoms and b in atoms:
                    bond(atoms[a], atoms[b])
        for pos in range(len(residues) - 1):
            c_atom = residues[pos][2].get('C')
            n_atom = residues[pos + 1][2].get('N')
            if c_atom is not None and n_atom is not None:
                bond(c_atom, n_atom)
        # disulfide: CYS SG-SG within bonding distance is chemistry
        sg_atoms = [(rid, atoms['SG']) for rid, comp, atoms in residues
                    if comp == 'CYS' and 'SG' in atoms]
        for x in range(len(sg_atoms)):
            for y in range(x + 1, len(sg_atoms)):
                i, j = sg_atoms[x][1], sg_atoms[y][1]
                if float(np.linalg.norm(coords[i] - coords[j])) <= 2.15:
                    bond(i, j)

        # angle pairs: two bonds sharing a centre, official
        # build_angle_triples_from_bonds semantics
        neighbours: dict[int, set[int]] = {}
        for a, b in chain_bonds:
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
        seen: set[tuple[int, int]] = set(chain_bonds)
        for shared, partners in neighbours.items():
            for i, j in combinations(sorted(partners), 2):
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                l1 = _ideal_bond_length(elements[i], elements[shared])
                l2 = _ideal_bond_length(elements[shared], elements[j])
                rest = math.sqrt(max(
                    l1 * l1 + l2 * l2
                    - 2 * l1 * l2 * math.cos(math.radians(109.5)), 0.25))
                index.append([key[0], key[1]])
                upper.append(rest + 0.40)
                lower.append(max(rest - 0.40, 0.5))
                is_bond.append(0)
                is_angle.append(1)

    if not index:
        return None
    return {
        "pairwise_distance_index": np.asarray(index, dtype=np.int64).T,
        "pairwise_distance_upper_bound": np.asarray(upper, dtype=np.float32),
        "pairwise_distance_lower_bound": np.asarray(lower, dtype=np.float32),
        "pairwise_distance_is_bond": np.asarray(is_bond, dtype=np.float32),
        "pairwise_distance_is_angle": np.asarray(is_angle, dtype=np.float32),
    }



def compute_ligand_covalent_bands(
    ligand_rows: np.ndarray,
    ligand_mol: Any,
    band: float = 0.12,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Ligand covalent bonds from the RDKit graph as hard-anchor bands.

    Dock-mode counterpart of compute_covalent_bond_bands: the molecular
    graph gives the exact topology (no distance heuristics) and the input
    conformer the rest lengths. ligand_rows holds HEAVY-atom rows in order
    (the assembled table carries no hydrogens, and the align contract
    writes the conformer through those rows), so the molecule is stripped
    to heavy atoms before its graph is read; a molecule whose heavy-atom
    count does not match the rows has no valid row correspondence and is
    rejected (the alignment drops the ligand in that case as well).
    """
    if ligand_rows is None or len(ligand_rows) == 0 or ligand_mol is None:
        return None
    from rdkit import Chem

    mol = Chem.RemoveHs(ligand_mol) if any(
        a.GetAtomicNum() == 1 for a in ligand_mol.GetAtoms()) else ligand_mol
    if mol.GetNumAtoms() != len(ligand_rows):
        return None

    conf = mol.GetConformer()
    pairs: list[list[int]] = []
    rest: list[float] = []
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        p, q = conf.GetAtomPosition(i), conf.GetAtomPosition(j)
        d = float(np.linalg.norm(np.array(p) - np.array(q)))
        pairs.append([int(ligand_rows[i]), int(ligand_rows[j])])
        rest.append(d)
    if not pairs:
        return None
    index = np.asarray(pairs, dtype=np.int64)
    upper = np.asarray(rest, dtype=np.float32) + float(band)
    lower = np.maximum(np.asarray(rest, dtype=np.float32) - float(band), 0.5)
    return index, upper, lower


def tfg_constraints_as_bands(
    constraints: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Bond constraints from the TFG set in hard-band format for x_t.

    The TFG soft channel guides x0 (the denoiser's clean prediction);
    pocket anchors constrain x_t directly. When a pocket anchor pushes an
    atom against its covalent network, the soft channel cannot prevent
    x_t from drifting between steps. The bond subset therefore also
    projects x_t after every pocket/clash anchor sweep — covalent bonds
    outrank pocket geometry in the physical hierarchy, so chemistry wins
    the negotiation on the trajectory itself.
    """
    is_bond = np.asarray(constraints["pairwise_distance_is_bond"]) > 0.5
    if not is_bond.any():
        return None
    idx = np.asarray(constraints["pairwise_distance_index"])[:, is_bond]
    upper = np.asarray(constraints["pairwise_distance_upper_bound"])[is_bond]
    lower = np.asarray(constraints["pairwise_distance_lower_bound"])[is_bond]
    return idx, upper, lower


def compute_clash_floor_pairs(
    info: dict[str, Any],
    coords: np.ndarray,
    mask: np.ndarray,
    free_entities: set[int],
    cutoff: float = 8.0,
    max_pairs: int = 4000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Receptor x free-chain VDW clash floors as anchor bands.

    Official semantics (protenix.tfg.potentials
    PairwiseDistancePotential): a non-bond constrained pair's lower bound is
    the VDW contact distance 0.35 + 0.5 * (ri + rj) with no upper bound.
    The pocket contact pairs anchor each receptor atom only to its staged
    NEAREST free atom; this builder floors EVERY receptor/free heavy pair
    within `cutoff` on the assembled table, so any atom the diffusion moves
    into the receptor surface is pushed back out.

    Pairs span entity families by construction (pinned receptor x free
    chains). Returns (pairs [M,2] int64, upper [M] float32 (=inf as 1e3),
    lower [M] float32) or None.
    """
    asym = info["asym"]
    asym_to_entity = info["asym_to_entity"]
    elements = [str(e).upper() for e in np.asarray(info["elements"])]

    pinned: list[tuple[int, float]] = []
    free: list[tuple[int, float]] = []
    for i in range(len(asym)):
        if mask[i] <= 0:
            continue
        radius = _VDW_RADII.get(elements[i], _VDW_DEFAULT)
        entity = asym_to_entity[int(asym[i])]
        (free if entity in free_entities else pinned).append((i, radius))
    if not pinned or not free:
        return None
    pin_idx = np.array([r for r, _ in pinned], dtype=np.int64)
    pin_rad = np.array([rad for _, rad in pinned], dtype=np.float32)
    free_idx = np.array([r for r, _ in free], dtype=np.int64)
    free_rad = np.array([rad for _, rad in free], dtype=np.float32)

    pin_xyz = np.asarray(coords[pin_idx], dtype=np.float64)
    free_xyz = np.asarray(coords[free_idx], dtype=np.float64)
    d2 = ((pin_xyz[:, None, :] - free_xyz[None, :, :]) ** 2).sum(-1)
    sel = np.argwhere(d2 <= cutoff * cutoff)
    if sel.size == 0:
        return None
    if len(sel) > max_pairs:
        order = np.argsort(d2[sel[:, 0], sel[:, 1]])[:max_pairs]
        sel = sel[order]
    lo = (0.35 + 0.5 * (pin_rad[sel[:, 0]] + free_rad[sel[:, 1]])).astype(np.float32)
    pairs = np.stack([pin_idx[sel[:, 0]], free_idx[sel[:, 1]]], axis=1).astype(np.int64)
    upper = np.full(len(pairs), 1.0e3, dtype=np.float32)  # unbounded above
    return pairs, upper, lo


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


def _structure_atom_lookup(
    structure_path: Path,
) -> dict[tuple[str, int, str], tuple[float, float, float]]:
    """{(chain, residue_ordinal, atom_name): (x, y, z)} heavy atoms.

    The ordinal is the residue's 1-based position within its chain in file
    order. The assembled atom table numbers each entity's residues 1..n by
    sequence position, so ordinals — not the source file's seqid — are the
    correspondence key (crystal files routinely start at author numbering
    26, 2, ...; keying by seqid silently shifts the sequence against the
    coordinates).
    """
    import gemmi

    structure = gemmi.read_structure(str(structure_path))
    structure.setup_entities()
    lookup: dict[tuple[str, int, str], tuple[float, float, float]] = {}
    for chain in structure[0]:
        for ordinal, residue in enumerate(chain, start=1):
            for atom in residue:
                name = atom.name.strip()
                if atom.element == gemmi.Element("H") or name.startswith("H"):
                    continue
                lookup[(chain.name, ordinal, name)] = (
                    atom.pos.x, atom.pos.y, atom.pos.z,
                )
    return lookup


def _complete_missing_atoms(
    coords: np.ndarray,
    mask: np.ndarray,
    feat: dict,
) -> int:
    """Rebuild atoms absent from the source from the CCD reference geometry.

    A noise-start atom is isolated from its residue and diffusion at the
    small-sigma dock ladder never reliably relocates it (atoms parked near
    the coordinate origin in redock outputs). Instead: rigidly fit each
    residue's KNOWN atoms onto their ref_pos (the engine's own CCD
    conformer) and carry the missing atoms through the same transform.
    Deterministic, exact for the known part, ideal geometry for the rest.
    Mutates coords/mask in place; returns the number of atoms completed.
    """
    a2t = feat["atom_to_token_idx"]
    if a2t.dim() >= 2:
        a2t = a2t[0] if a2t.shape[0] == 1 else a2t.argmax(dim=-1)
    token_of = np.asarray(a2t.detach().cpu().long().numpy()).flatten()
    ref = np.asarray(feat["ref_pos"].detach().cpu().float().numpy())

    by_token: dict[int, list[int]] = {}
    for i in np.where(mask == 0)[0]:
        by_token.setdefault(int(token_of[i]), []).append(int(i))

    completed = 0
    for token, missing in by_token.items():
        rows = np.where((token_of == token) & (mask > 0))[0]
        if len(rows) < 3:
            continue
        src = ref[rows]
        if np.linalg.matrix_rank(src - src.mean(0)) < 2:
            continue
        m = src.mean(0)
        t = coords[rows].mean(0)
        H = (src - m).T @ (coords[rows] - t)
        U, _, Vt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
        for i in missing:
            coords[i] = (ref[i] - m) @ R.T + t
            mask[i] = 1.0
            completed += 1
    return completed


def align_complex_init_coords(
    input_json_path: Path,
    source_structure_path: Path,
    entity_chain_names: list[str],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Align the source complex's coordinates onto the assembled atom order.

    Works for any entity mix (proteins, CCD ligands) by matching
    (chain letter, res_id, atom name) against the staged complex: the
    input.json sequences order defines the auto chain letters (A, B, C, ...),
    and `entity_chain_names` maps each entity to the staged chain it came
    from.

    Returns (coords [N_atom,3] float32, mask [N_atom] float32, info dict with
    per-entity row indices and the assembled atom table).
    """
    from protenix.data.inference.json_to_feature import SampleDictToFeatures

    with open(input_json_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)[0]
    sample2feat = SampleDictToFeatures(job, extract_features_for_tfg=False)
    feat, atom_array, _ = sample2feat.get_feature_dict()

    n = int(atom_array.array_length())
    coords = np.zeros((n, 3), dtype=np.float32)
    mask = np.zeros(n, dtype=np.float32)

    asym = np.asarray(atom_array.asym_id_int)
    res_id = np.asarray(atom_array.res_id)
    atom_names = [str(a) for a in np.asarray(atom_array.atom_name)]

    asym_to_entity: dict[int, int] = {}
    for value in asym:
        v = int(value)
        if v not in asym_to_entity:
            asym_to_entity[v] = len(asym_to_entity)

    lookup = _structure_atom_lookup(Path(source_structure_path))

    # per-entity residue ordinal: the assembled table numbers each entity's
    # residues 1..n in sequence order, so the lookup key is the res_id's
    # rank within its entity (first-appearance order), never the raw res_id
    entity_ordinal: dict[tuple[int, int], int] = {}
    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity >= len(entity_chain_names):
            continue
        entity_ordinal.setdefault((entity, int(res_id[i])), len(
            {rid for (ent, rid) in entity_ordinal if ent == entity}) + 1)

    matched = 0
    unmatched: list[str] = []
    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity >= len(entity_chain_names):
            continue
        staged_chain = entity_chain_names[entity]
        ordinal = entity_ordinal[(entity, int(res_id[i]))]
        pos = lookup.get((staged_chain, ordinal, atom_names[i]))
        if pos is not None:
            coords[i] = pos
            mask[i] = 1.0
            matched += 1
        else:
            unmatched.append(f"{staged_chain}#{ordinal}({res_id[i]}){atom_names[i]}")
    if unmatched:
        # atoms absent from the source carry no coordinates; they are
        # rebuilt below from the CCD reference geometry where possible and
        # only the rest stay mask=0 (the sampler's designed unknown-atom
        # start). Callers must never pin a mask=0 row — a zero coordinate
        # row would clamp the atom to the origin.
        print(
            f"[Warning] {len(unmatched)} assembled atom(s) absent from the "
            f"source complex (first: {', '.join(unmatched[:10])})"
        )
        completed = _complete_missing_atoms(coords, mask, feat)
        if completed:
            print(f"[Info] Rebuilt {completed} atom(s) from the CCD reference "
                  f"geometry.")

    entity_rows: dict[int, np.ndarray] = {}
    for entity in range(len(entity_chain_names)):
        entity_rows[entity] = np.array(
            [i for i in range(n) if asym_to_entity[int(asym[i])] == entity],
            dtype=np.int64,
        )

    print(
        f"[Info] Complex init coords aligned: {matched}/{n} atoms "
        f"({len(entity_chain_names)} entities)."
    )
    return coords, mask, {
        "entity_rows": entity_rows,
        "elements": np.asarray(
            [str(e) for e in np.asarray(atom_array.element)]),
        "comp_ids": np.asarray(
            [str(cn) for cn in np.asarray(atom_array.res_name)]),
        "entity_chain_names": entity_chain_names,
        "asym": asym,
        "res_id": res_id,
        "atom_names": np.asarray(atom_names),
        "asym_to_entity": asym_to_entity,
    }


def compute_bond_contact_pairs(
    info: dict[str, Any],
    bond_pairs: list[tuple[tuple[str, int, str], tuple[str, int, str]]],
    upper: float,
    staged_to_auto: dict[str, str] | None = None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Covalent bond pairs (peptide atom, linker atom) as TFG contacts.

    Pair indices resolve through the assembled-atom table in `info`
    (returned by align_complex_init_coords) keyed by
    (chain letter, residue ordinal, atom name) — chain letters are the
    input.json auto ids ("A", "B", "C", ...) and residue ordinals are 1..n
    positions within the staged chain (sequence order, not author seqid;
    the production writer numbers the peptide 1..n). `staged_to_auto` maps
    the staged complex chain names (what the caller's bond_pairs
    references) to those auto letters; without it the pair chain letters
    are assumed to already be auto letters.
    The finite upper bound keeps the projection hard (the vendored TFG marks
    these pairs as the angle category so clash logic cannot drop them).
    """
    asym = info["asym"]
    res_id = info["res_id"]
    atom_names = info["atom_names"]
    asym_to_entity = info["asym_to_entity"]
    asym_to_letter: dict[int, str] = {}
    for value in asym:
        v = int(value)
        if v not in asym_to_letter:
            asym_to_letter[v] = chr(ord("A") + len(asym_to_letter))
    atom_key_of: dict[tuple[str, int, str], int] = {}
    for i in range(len(asym)):
        atom_key_of[(asym_to_letter[int(asym[i])], int(res_id[i]), atom_names[i])] = i

    def _auto(ref: tuple[str, int, str]) -> tuple[str, int, str]:
        if staged_to_auto is None:
            return ref
        chain = staged_to_auto.get(ref[0], ref[0])
        return (chain, ref[1], ref[2])

    pairs: list[list[int]] = []
    for a1, a2 in bond_pairs:
        i1 = atom_key_of.get(_auto(a1))
        i2 = atom_key_of.get(_auto(a2))
        if i1 is None:
            print(f"[Warning] bond atom {a1} not found in the assembled atom table")
            continue
        if i2 is None:
            print(f"[Warning] bond atom {a2} not found in the assembled atom table")
            continue
        pairs.append([i1, i2])
    if not pairs:
        return None
    index = np.asarray(pairs, dtype=np.int64)
    upper_arr = np.full(len(pairs), float(upper), dtype=np.float32)
    print(f"[Info] Bond TFG contacts prepared: {len(pairs)} pairs (upper={upper:.2f}A).")
    return index, upper_arr
