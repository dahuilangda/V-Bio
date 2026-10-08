#!/usr/bin/env python3
"""Backfill dead d_space_refined paths in a peptide-design checkpoint.

Checkpoint rows reference per-attempt tmp paths that get cleaned; after a
checkpoint transplant the export would ship structureless rows. Re-anchors
each dead path to the refine task's persisted output under
_runtime_tmp/dpeptide_task_* (survives cleanup), matched by peptide sequence
plus a target-sequence guard so another campaign's identically-named peptide
can never satisfy the match. Among the matching dirs the newest wins; within
it the highest-ipTM sample ships (a row stands for its best refine).

Usage: patch_checkpoint_structures.py <checkpoint.json> <since ISO> <target_seq_prefix>
Writes <checkpoint.json>.patched.
"""
import glob
import json
import os
import sys
import datetime

sys.path.insert(0, "/data/V-Bio/capabilities/peptide_lm")
import gemmi  # noqa: E402

T2O = {'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q',
       'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K',
       'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S', 'THR': 'T', 'TRP': 'W',
       'TYR': 'Y', 'VAL': 'V'}
BASE = "/data/boltz_central_results/_runtime_tmp"


def chain_seqs(cif_path):
    out = {}
    try:
        st = gemmi.read_structure(cif_path)
        st.setup_entities()
        for ch in st[0]:
            poly = [r for r in ch if r.het_flag != "H"]
            if poly:
                out[ch.name] = "".join(T2O.get(r.name, "X") for r in poly)
    except Exception:
        pass
    return out


def main():
    ckpt_path, since_s, target_prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    since = datetime.datetime.fromisoformat(since_s)
    data = json.load(open(ckpt_path))
    rows = data.get("all_results") or []
    dead = [r for r in rows
            if not (r.get("d_space_refined") and os.path.isfile(r["d_space_refined"]))]
    print(f"rows={len(rows)} dead-structure={len(dead)}")
    if not dead:
        print("nothing to do")
        return
    want_seqs = {str(r.get("sequence") or "") for r in dead}
    print(f"dead sequences: {len(want_seqs)}")

    dirs = [d for d in glob.glob(f"{BASE}/dpeptide_task_*")
            if datetime.datetime.utcfromtimestamp(os.path.getmtime(d)) >= since]
    dirs.sort(key=os.path.getmtime, reverse=True)  # newest first
    print(f"scanning {len(dirs)} refine dirs since {since}")

    by_seq = {}
    for n, d in enumerate(dirs):
        conf = os.path.join(d, "out", "confidence.json")
        if not os.path.isfile(conf):
            continue
        try:
            c = json.load(open(conf))
            best = c.get("best_cif")
            sdir = c.get("structure_dir") or (os.path.dirname(best) if best else "")
            if not sdir or not (best and os.path.isfile(best)):
                continue
            seqs = chain_seqs(best)
            pep = next((s for s in seqs.values() if 8 <= len(s) <= 40), None)
            if pep not in want_seqs:
                continue
            rec = next((s for s in seqs.values() if len(s) > 40), "")
            if not rec.startswith(target_prefix):
                continue  # another campaign's identical peptide — reject
            samples = []
            for cj in glob.glob(sdir + "/confidence_*.json"):
                try:
                    p = json.load(open(cj))
                    m = cj.split("_model_")[-1].split(".")[0]
                    cif = f"{sdir}/d_space_staged_model_{m}.cif"
                    if p.get("iptm") is not None and os.path.isfile(cif):
                        samples.append((float(p["iptm"]), cif))
                except Exception:
                    pass
            if samples and pep not in by_seq:
                by_seq[pep] = max(samples)  # newest dir, highest-iptm sample
        except Exception:
            pass
        if n % 100 == 0:
            print(f"  ...{n}/{len(dirs)} matched_seqs={len(by_seq)}", flush=True)
        if len(by_seq) == len(want_seqs):
            break

    filled = 0
    for r in dead:
        seq = str(r.get("sequence") or "")
        if seq in by_seq:
            r["d_space_refined"] = by_seq[seq][1]
            r["d_space_refined_backfilled"] = True
            filled += 1
    print(f"filled {filled}/{len(dead)}; unmatched {len(dead)-filled}")
    out = ckpt_path + ".patched"
    json.dump(data, open(out, "w"))
    print("wrote", out)


if __name__ == "__main__":
    main()
