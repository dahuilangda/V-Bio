"""Residue monomer metadata.

Natural amino acids plus the V-Bio preset non-natural amino acids (NCAAs).
CCD codes / SMILES / base-residue mapping / placement rules mirror the
production tables in backend/runtime/run_single_prediction.py so any candidate
we generate is directly consumable by the existing Boltz YAML pipeline
(modifications + custom_ccd_molecules).

Glyco-residue presets (MANS..XYLS) are intentionally excluded here: they need
on-the-fly CCD cache building (glycopeptide_generator) and are a separate
design mode, not part of the therapeutic-peptide NCAA pool.
"""

from __future__ import annotations

from functools import lru_cache

# --- natural amino acids -------------------------------------------------
NATURAL_AA = "ACDEFGHIKLMNPQRSTVWY"

# Kyte-Doolittle hydropathy (positive = hydrophobic); NCAA values assigned by
# side-chain analogy (documented per entry).
HYDROPATHY = {
    "A": 1.8, "R": -4.5, "N": -3.5, "D": -3.5, "C": 2.5, "Q": -3.5,
    "E": -3.5, "G": -0.4, "H": -3.2, "I": 4.5, "L": 3.8, "K": -3.9,
    "M": 1.9, "F": 2.8, "P": -1.6, "S": -0.8, "T": -0.7, "W": -0.9,
    "Y": -1.3, "V": 4.2,
    # NCAAs (side-chain analogues of their base residue)
    "[AIB]": 1.8,    # dimethyl-Ala: like A, slightly more hydrophobic
    "[NLE]": 3.8,    # norleucine ~ Leu
    "[NVA]": 4.2,    # norvaline ~ Val
    "[ORN]": -3.9,   # ornithine ~ Lys (one CH2 shorter)
    "[CIT]": -3.5,   # citrulline ~ Gln (neutral ureido)
    "[HSE]": -0.8,   # homoserine ~ Ser
    "[HCY]": 2.5,    # homocysteine ~ Cys
    "[MSE]": 1.9,    # selenomethionine ~ Met
    "[SEC]": 1.5,    # selenocysteine ~ Cys
    "[HYP]": -1.6,   # hydroxyproline ~ Pro
    "[PCA]": -3.5,   # pyroglutamate ~ Glu (lactam, neutral)
    "[SEP]": -0.8,   # phosphoserine: very polar
    "[TPO]": -0.7,   # phosphothreonine
    "[PTR]": -1.3,   # phosphotyrosine
    "[CSO]": -0.8,   # S-hydroxycysteine
    "[MLY]": -3.9,   # N6-methyllysine ~ Lys
    "[DAL]": 1.8,    # D-alanine = Ala
    "[BALA]": -0.4,  # beta-alanine ~ Gly
}

# side-chain / terminal pKa for net-charge computation at pH 7.4
PKA = {
    "C": 8.3, "D": 3.9, "E": 4.1, "H": 6.0, "K": 10.5, "R": 12.5, "Y": 10.1,
    "[HCY]": 8.3, "[SEC]": 5.2, "[MLY]": 10.5, "[ORN]": 10.5,
    "[CIT]": None,  # neutral ureido side chain
}
PKA_N_TERM = 9.6
PKA_C_TERM = 2.3

# --- NCAA presets (V-Bio production table, glyco entries excluded) --------
# placement: any | n_term | c_term | terminal
NCAA_PRESETS: dict[str, dict] = {
    "AIB":  {"smiles": "NC(C)(C)C(=O)O", "base": "A", "label": "alpha-aminoisobutyric acid"},
    "NLE":  {"smiles": "N[C@@H](CCCCC)C(=O)O", "base": "L", "label": "norleucine"},
    "NVA":  {"smiles": "N[C@@H](CCC)C(=O)O", "base": "V", "label": "norvaline"},
    "ORN":  {"smiles": "N[C@@H](CCCN)C(=O)O", "base": "K", "label": "ornithine"},
    "CIT":  {"smiles": "N[C@@H](CCCNC(N)=O)C(=O)O", "base": "R", "label": "citrulline"},
    "HSE":  {"smiles": "N[C@@H](CCO)C(=O)O", "base": "S", "label": "homoserine"},
    "HCY":  {"smiles": "N[C@@H](CCS)C(=O)O", "base": "C", "label": "homocysteine"},
    "MSE":  {"smiles": "N[C@@H](CC[Se]C)C(=O)O", "base": "M", "label": "selenomethionine"},
    "SEC":  {"smiles": "N[C@@H](C[SeH])C(=O)O", "base": "C", "label": "selenocysteine"},
    "HYP":  {"smiles": "O=C(O)[C@@H]1CC(O)CN1", "base": "P", "label": "hydroxyproline"},
    "PCA":  {"smiles": "O=C(O)[C@@H]1CCC(=O)N1", "base": "E", "label": "pyroglutamic acid",
             "placement": "n_term"},
    "SEP":  {"smiles": "N[C@@H](COP(=O)(O)O)C(=O)O", "base": "S", "label": "phosphoserine"},
    "TPO":  {"smiles": "N[C@@H]([C@H](C)OP(=O)(O)O)C(=O)O", "base": "T", "label": "phosphothreonine"},
    "PTR":  {"smiles": "N[C@@H](Cc1ccc(OP(=O)(O)O)cc1)C(=O)O", "base": "Y", "label": "phosphotyrosine"},
    "CSO":  {"smiles": "N[C@@H](CSO)C(=O)O", "base": "C", "label": "S-hydroxycysteine"},
    "MLY":  {"smiles": "N[C@@H](CCCCNC)C(=O)O", "base": "K", "label": "N6-methyllysine"},
    "DAL":  {"smiles": "N[C@H](C)C(=O)O", "base": "A", "label": "D-alanine"},
    "BALA": {"smiles": "NCCC(=O)O", "base": "A", "label": "beta-alanine"},
}

# all presets are catalog Fmoc building blocks; specialty flag raises the
# synthesizability cost slightly (Se chemistry, beta-aa coupling)
SPECIALTY_SPPS = {"MSE", "SEC", "BALA", "PTR", "SEP", "TPO"}
D_RESIDUES = {"DAL"}
BETA_RESIDUES = {"BALA"}

NCAA_TOKENS = [f"[{ccd}]" for ccd in NCAA_PRESETS]
NCAA_TOKEN_TO_CCD = {f"[{ccd}]": ccd for ccd in NCAA_PRESETS}
NCAA_CCD_TO_TOKEN = {ccd: f"[{ccd}]" for ccd in NCAA_PRESETS}

# --- user-supplied residues (runtime registration) ------------------------
# Arbitrary non-natural residues beyond the presets: users register entries
# {ccd, smiles, base, placement} and every component (vocab extension,
# placement masks, oracle CCD cache, mutation channel) consults this table.
USER_RESIDUES: dict[str, dict] = {}


def register_user_residues(entries: list[dict]) -> list[str]:
    """Register user residue definitions; returns newly added CCD codes.
    Validation mirrors the production custom_ccd_molecules contract."""
    added = []
    for e in entries or []:
        ccd = str(e.get("ccd") or "").strip().upper()
        smiles = str(e.get("smiles") or "").strip()
        if not ccd or not smiles:
            continue
        if ccd in NCAA_PRESETS:
            continue  # preset wins
        USER_RESIDUES[ccd] = {
            "ccd": ccd, "smiles": smiles,
            "base": str(e.get("base") or "A").upper()[:1],
            "label": str(e.get("label") or ccd),
            "placement": str(e.get("placement") or "any").lower(),
        }
        added.append(ccd)
    return added


def all_residue_ccds() -> list[str]:
    return list(NCAA_PRESETS) + [m["ccd"] for m in USER_RESIDUES.values()]


def residue_meta(ccd: str) -> dict | None:
    ccd = str(ccd).strip().upper()
    if ccd in NCAA_PRESETS:
        return {"ccd": ccd, **NCAA_PRESETS[ccd],
                "placement": NCAA_PRESETS[ccd].get("placement", "any")}
    return USER_RESIDUES.get(ccd)


def placement_lookup(token_or_ccd: str) -> str:
    """Placement rule for a bracket token '[CCD]' or bare CCD (user table
    overrides nothing preset — presets are looked up first)."""
    s = str(token_or_ccd).strip()
    ccd = s[1:-1] if s.startswith("[") else s
    ccd = ccd.strip().upper()
    if ccd in NCAA_PRESETS:
        return NCAA_PRESETS[ccd].get("placement", "any")
    m = USER_RESIDUES.get(ccd)
    return (m or {}).get("placement", "any")


def placement_of(token: str) -> str:
    return placement_lookup(token)


@lru_cache(maxsize=1)
def residue_masses() -> dict[str, float]:
    """Monoisotopic residue masses (free amino acid - H2O), NCAAs computed
    from SMILES with RDKit so the numbers cannot drift from the structures
    (user-registered residues included on cache miss after registration)."""
    masses: dict[str, float] = {}
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors

        water = 18.010565
        for ccd, meta in NCAA_PRESETS.items():
            m = Chem.MolFromSmiles(meta["smiles"])
            if m is not None:
                masses[f"[{ccd}]"] = Descriptors.MolWt(m) - water
        for ccd, meta in USER_RESIDUES.items():
            m = Chem.MolFromSmiles(meta["smiles"])
            if m is not None:
                masses[f"[{ccd}]"] = Descriptors.MolWt(m) - water
    except Exception:
        pass
    # natural residues: standard monoisotopic residue masses
    masses.update({
        "A": 71.03711, "R": 156.10111, "N": 114.04293, "D": 115.02694,
        "C": 103.00919, "E": 129.04259, "Q": 128.05858, "G": 57.02146,
        "H": 137.05891, "I": 113.08406, "L": 113.08406, "K": 128.09496,
        "M": 131.04049, "F": 147.06841, "P": 97.05276, "S": 87.03203,
        "T": 101.04768, "W": 186.07931, "Y": 163.06333, "V": 99.06841,
    })
    return masses


def custom_ccd_molecules(tokens_or_ccds, extra_entries: list[dict] | None = None) -> list[dict]:
    """custom_ccd_molecules entries for every referenced NCAA (presets plus
    user-registered residues; extra_entries registers more on the fly)."""
    if extra_entries:
        register_user_residues(extra_entries)
    out: list[dict] = []
    seen: set[str] = set()
    for item in tokens_or_ccds:
        ccd = item[1:-1] if isinstance(item, str) and item.startswith("[") else str(item)
        ccd = ccd.strip().upper()
        meta = residue_meta(ccd)
        if ccd in seen or meta is None:
            continue
        seen.add(ccd)
        out.append({
            "ccd": ccd,
            "smiles": meta["smiles"],
            "base_residue": meta.get("base", "A"),
            "label": meta.get("label", ccd),
            "kind": "residue",
        })
    return out
