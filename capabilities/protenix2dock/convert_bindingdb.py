#!/usr/bin/env python3
"""Convert BindingDB_All TSV to the prepare_affinity_data.py input schema.

BindingDB TSV columns used:
  - MonomerID / PKi etc. affinity fields: "Ki (nM)", "IC50 (nM)", "Kd (nM)",
    "EC50 (nM)" (multiple numeric columns ending in (nM))
  - Ligand SMILES / SMILE
  - BindingDB Target Chain Sequence
  - UniProt (SwissProt) Primary ID of Target Chain  (target_id)
  - Assay ID / Reference / Temperature (when present)

Output columns: target_id, sequence, smiles, affinity_uM, affinity_type,
assay_id, temperature_c
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

AFFINITY_COLS = ["Ki (nM)", "IC50 (nM)", "Kd (nM)", "EC50 (nM)"]


def _parse_affinity(value: str):
    """'>10000' / '<1' / '1.2' style entries in nM -> uM float or None."""
    v = str(value or "").strip()
    if not v:
        return None
    inequality = 0
    if v.startswith(">"):
        inequality, v = 1, v[1:].strip()
    elif v.startswith("<"):
        inequality, v = -1, v[1:].strip()
    try:
        nm = float(v)
    except ValueError:
        return None
    if nm <= 0:
        return None
    return nm / 1000.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tsv", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max_rows", type=int, default=0, help="0 = all")
    parser.add_argument("--require_uniprot", action="store_true", default=True)
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_in = n_out = 0
    with open(args.tsv, encoding="utf-8", errors="replace") as fh, open(out, "w", newline="") as oh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        writer = csv.DictWriter(oh, fieldnames=[
            "target_id", "sequence", "smiles", "affinity_uM", "affinity_type",
            "assay_id", "temperature_c"])
        writer.writeheader()
        for row in reader:
            n_in += 1
            if args.max_rows and n_out >= args.max_rows:
                break
            seq = (row.get("BindingDB Target Chain Sequence 1") or "").strip()
            smiles = (row.get("Ligand SMILES") or row.get("SMILE") or "").strip()
            uniprot = (row.get("UniProt (SwissProt) Primary ID of Target Chain 1") or "").strip()
            if len(seq) < 50 or not smiles or not uniprot:
                continue
            best = None
            for col in AFFINITY_COLS:
                um = _parse_affinity(row.get(col))
                if um is not None:
                    best = (um, col.split(" ")[0])
                    break
            if best is None:
                continue
            um, atype = best
            temp = (row.get("Temperature") or "").strip()
            writer.writerow({
                "target_id": uniprot.split(",")[0],
                "sequence": seq,
                "smiles": smiles,
                "affinity_uM": f"{um:.6g}",
                "affinity_type": atype,
                "assay_id": (row.get("Assay ID") or row.get("Reference") or "")[:60],
                "temperature_c": temp,
            })
            n_out += 1
    print(f"rows in={n_in} out={n_out} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
