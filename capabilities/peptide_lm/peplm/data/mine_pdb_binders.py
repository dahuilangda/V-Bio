"""Targeted PDB binder mining: formula_weight search + sibling-entity fetch.

The miner searches polymer entities BY MOLECULAR WEIGHT
(500-4000 Da = 5-35 aa chains) which returns the peptide entities directly,
then one GraphQL request per entity fetches both its sequence and every
sibling entity of the entry, so binder+receptor pairs come out in a single
pass.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"  # RCSB search API
GRAPHQL_URL = "https://data.rcsb.org/graphql"


def search_entries_brackets() -> list[tuple[str, str]]:
    """Resolution-bracket slices of the multimeric-X-ray query. The search
    API caps pagination at 10k rows per query; disjoint resolution brackets
    each yield their own 10k, multiplying coverage (~40k entities)."""
    seen: set[str] = set()
    ids: list[tuple[str, str]] = []
    brackets = [(None, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, None)]
    for lo, hi in brackets:
        nodes = [{"type": "terminal", "service": "text", "parameters": {
            "attribute": "rcsb_assembly_info.polymer_entity_instance_count",
            "operator": "greater_or_equal", "value": 2}}]
        if hi is not None:
            nodes.append({"type": "terminal", "service": "text", "parameters": {
                "attribute": "rcsb_entry_info.resolution_combined",
                "operator": "less_or_equal", "value": hi}})
        if lo is not None:
            nodes.append({"type": "terminal", "service": "text", "parameters": {
                "attribute": "rcsb_entry_info.resolution_combined",
                "operator": "greater", "value": lo}})
        start = 0
        while start < 10000:
            rows = min(100, 10000 - start)
            q = {"query": {"type": "group", "logical_operator": "and",
                           "nodes": nodes},
                 "return_type": "polymer_entity",
                 "request_options": {"paginate": {"start": start, "rows": rows},
                                     "results_content_type": ["experimental"]}}
            req = urllib.request.Request(
                SEARCH_URL, data=json.dumps(q).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read())
            except Exception as e:
                print(f"  bracket {lo}-{hi} page {start} failed: {e}", flush=True)
                break
            got = 0
            for x in d.get("result_set", []):
                key = x["identifier"]
                if key not in seen:
                    seen.add(key)
                    ids.append((key.split("_")[0], key.split("_")[1]))
                    got += 1
            if len(d.get("result_set", [])) < rows:
                break
            start += rows
        print(f"  bracket {lo}-{hi}: running total {len(ids)}", flush=True)
    return ids


def search_peptide_entities(max_rows: int = 4000) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []
    start = 0
    while start < max_rows:
        rows = min(100, max_rows - start)
        q = {
            "query": {"type": "group", "logical_operator": "and", "nodes": [
                {"type": "terminal", "service": "text", "parameters": {
                    "attribute": "rcsb_polymer_entity.formula_weight",
                    "operator": "greater_or_equal", "value": 500}},
                {"type": "terminal", "service": "text", "parameters": {
                    "attribute": "rcsb_polymer_entity.formula_weight",
                    "operator": "less_or_equal", "value": 4000}},
            ]},
            "return_type": "polymer_entity",
            "request_options": {"paginate": {"start": start, "rows": rows},
                                "results_content_type": ["experimental"]},
        }
        req = urllib.request.Request(
            SEARCH_URL, data=json.dumps(q).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        got = [(x["identifier"].split("_")[0], x["identifier"].split("_")[1])
               for x in d.get("result_set", [])]
        ids.extend(got)
        if len(got) < rows:
            break
        start += rows
    return ids


def graphql_entry_entities(ids: list[tuple[str, str]],
                           batch: int = 40,
                           workers: int = 8) -> list[dict[str, str]]:
    """One request per batch: peptide entity seq + all sibling entity seqs.
    Batches run in a thread pool — the nested entry query is server-heavy and
    sequential fetching would take an hour."""
    from concurrent.futures import ThreadPoolExecutor

    batches = [ids[i:i + batch] for i in range(0, len(ids), batch)]

    def fetch(chunk):
        aliases = []
        for j, (eid, ent) in enumerate(chunk):
            aliases.append(
                f'e{j}: polymer_entity(entry_id:"{eid}", entity_id:"{ent}")'
                "{entity_poly{pdbx_seq_one_letter_code_can} "
                "entry{polymer_entities{entity_poly"
                "{pdbx_seq_one_letter_code_can}}}}")
        q = "{" + " ".join(aliases) + "}"
        req = urllib.request.Request(
            GRAPHQL_URL, data=json.dumps({"query": q}).encode(),
            headers={"Content-Type": "application/json"})
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.loads(r.read()).get("data")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  batch failed: {e}", flush=True)
                time.sleep(3)
        out = []
        if not data:
            return out
        for j in range(len(chunk)):
            node = data.get(f"e{j}")
            if not node:
                continue
            pep = ((node.get("entity_poly") or {})
                   .get("pdbx_seq_one_letter_code_can") or "").upper()
            sibs = []
            for s in (((node.get("entry") or {}).get("polymer_entities"))
                      or []):
                seq = ((s.get("entity_poly") or {})
                       .get("pdbx_seq_one_letter_code_can") or "").upper()
                if seq:
                    sibs.append(seq)
            out.append({"peptide": pep, "siblings": sibs})
        return out

    entries: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(fetch, batches):
            entries.extend(part)
            done += 1
            if done % 10 == 0:
                print(f"  graphql {done}/{len(batches)} batches, "
                      f"{len(entries)} records", flush=True)
    return entries


AA = set("ACDEFGHIKLMNPQRSTVWY")


def extract(entries: list[dict]) -> tuple[set[str], list[tuple[str, str]]]:
    peptides: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for e in entries:
        pep = e["peptide"].replace("\n", "")
        if not (5 <= len(pep) <= 35) or not (set(pep) <= AA):
            continue
        receptors = [s.replace("\n", "") for s in e["siblings"]
                     if len(s) >= 40 and set(s.replace("\n", "")) <= AA]
        if not receptors:
            continue
        receptor = max(receptors, key=len)[:400]
        if pep not in peptides:
            peptides.add(pep)
            pairs.append((receptor, pep))
    return peptides, pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out_dir", default="runs/data_pdb")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "search_ids2.txt"
    if cache.exists():
        ids = [tuple(l.strip().split("_")) for l in cache.read_text().splitlines()
               if l.strip()]
        print(f"[pdb2] {len(ids)} entity ids from cache")
    else:
        ids = search_entries_brackets()
        cache.write_text("\n".join(f"{e}_{n}" for e, n in ids))
        print(f"[pdb2] {len(ids)} polymer entities from bracket search")
    entries = graphql_entry_entities(ids)
    print(f"[pdb2] {len(entries)} entity records with siblings")
    peptides, pairs = extract(entries)
    # deduplicate across runs
    old = out / "binder_peptides.txt"
    if old.exists():
        for s in old.read_text().splitlines():
            s = s.strip().upper()
            if s and s not in peptides:
                peptides.add(s)
    (out / "binder_peptides.txt").write_text("\n".join(sorted(peptides)))
    (out / "binder_pairs.tsv").write_text(
        "\n".join(f"{r}\t{p}" for r, p in pairs))
    print(f"[pdb2] {len(peptides)} unique binder peptides "
          f"(pairs: {len(pairs)})")


if __name__ == "__main__":
    main()


STD3TO1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M", "SEC": "U", "PYL": "O", "SEP": "S", "TPO": "T",
}


def parse_pdb_seqres(pdb_text: str) -> dict:
    """chain -> one-letter SEQRES sequence (non-standard residues -> X)."""
    chains: dict = {}
    for line in pdb_text.splitlines():
        if line.startswith("SEQRES"):
            chain = line[11]
            residues = line[19:].split()
            chains.setdefault(chain, []).extend(residues)
    out = {}
    for chain, residues in chains.items():
        aa = []
        for r in residues:
            if r in STD3TO1 and STD3TO1[r] in "ACDEFGHIKLMNPQRSTVWY":
                aa.append(STD3TO1[r])
            else:
                aa.append("X")
        out[chain] = "".join(aa)
    return out
