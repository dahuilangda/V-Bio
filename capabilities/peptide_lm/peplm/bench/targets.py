"""Benchmark targets: classic peptide-binding proteins with co-crystal
peptides. Receptor sequences and seed binder peptides are extracted from the
downloaded PDB files at runtime (ground truth, never hand-typed)."""

from __future__ import annotations

import urllib.request
from pathlib import Path

from peplm.data.mine_pdb_binders import parse_pdb_seqres

TARGETS = {
    # MDM2 : p53 transactivation domain (1YCR) — stapled-peptide literature
    # target (ALRN-6924 clinical), the canonical peptide-design benchmark
    "mdm2": {"pdb": "1YCR", "receptor_chain": "A", "peptide_chain": "B"},
    # Keap1 Kelch domain : Nrf2 ETGE motif (2FLU) — PPI pocket target
    "keap1": {"pdb": "2FLU", "receptor_chain": "X", "peptide_chain": "P"},
    # BCL-xL : Bak BH3 (1BXL) — apoptosis PPI target
    "bclxl": {"pdb": "1BXL", "receptor_chain": "A", "peptide_chain": "B"},
}
CACHE = Path(__file__).resolve().parents[2] / "runs" / "pdb_cache"


def load_target(name: str) -> dict:
    spec = TARGETS[name]
    path = CACHE / f"{spec['pdb']}.pdb"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            f"https://files.rcsb.org/download/{spec['pdb']}.pdb", path)
    chains = parse_pdb_seqres(path.read_text())
    receptor_chain = spec["receptor_chain"]
    if receptor_chain not in chains:  # chain-id drift: longest chain is receptor
        receptor_chain = max(chains, key=lambda c: len(chains[c]))
    receptor = chains[receptor_chain]
    peptide = chains.get(spec["peptide_chain"])
    if peptide is None:  # chain id drift: take the shortest other chain
        peptide = min((s for c, s in chains.items() if c != spec["receptor_chain"]),
                      key=len)
    return {"name": name, "pdb": spec["pdb"], "receptor": receptor,
            "seed_peptide": peptide, "peptide_len": len(peptide)}
