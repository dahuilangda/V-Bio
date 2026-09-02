#!/bin/bash
# protenix2dock + Boltz2Score affinity bridge.
#
# Protenix refines/docks the pose; the Boltz2 affinity head (from
# capabilities/boltz2score) then scores that exact pose. Cross-engine
# benchmark (cdk8, 33 ligands): Spearman vs pIC50 improves from +0.28
# (protenix confidence alone) to +0.40 (p<0.05) with this bridge.
#
# Usage:
#   ./run_with_affinity.sh -- <protenix2dock args...>
# Example:
#   ./run_with_affinity.sh -- --mode dock --protein_file t.pdb \
#        --ligand_smiles 'CCO' --center_x 0 --center_y 0 --center_z 0 \
#        --output_dir /tmp/out --msa_server_url http://host:8080
#
# Outputs (in <output_dir>):
#   protenix2dock_summary.json      engine's own summary
#   affinity_bridge_summary.json    combined: pose + Boltz2 affinity on that pose
set -euo pipefail

PROTENIX_ARGS=()
if [ "${1:-}" = "--" ]; then shift; PROTENIX_ARGS=("$@"); else
  echo "usage: $0 -- <protenix2dock args...>" >&2; exit 2
fi

OUT_DIR=""
parse_out() { # extract --output_dir from the args
  local i=0
  while [ $i -lt ${#PROTENIX_ARGS[@]} ]; do
    if [ "${PROTENIX_ARGS[$i]}" = "--output_dir" ]; then echo "${PROTENIX_ARGS[$((i+1))]}"; return; fi
    i=$((i+1))
  done
}
OUT_DIR=$(parse_out)
[ -z "$OUT_DIR" ] && { echo "--output_dir is required" >&2; exit 2; }

# 1) protenix2dock (protenix runtime image)
docker run --rm --entrypoint= --gpus all \
  -v /data/V-Bio:/workspace/vbio:ro \
  -v /data/protenix/common_cache:/cache/common:ro \
  -v /data/protenix/model:/workspace/model:ro \
  -v /data/boltz_msa_cache:/data/msa_cache:ro \
  -v "$OUT_DIR:$OUT_DIR" -v /dev/shm:/dev/shm \
  --env PYTHONPATH=/workspace/vbio/vendor/protenix-source \
  --env PROTENIX_ROOT_DIR=/cache \
  vbio-protenix-v2-runtime:2.0.0 \
  /usr/local/micromamba/envs/protenix/bin/python \
  /workspace/vbio/capabilities/protenix2dock/protenix2dock.py \
  "${PROTENIX_ARGS[@]}"

# 2) Boltz2Score affinity on the best pose (boltz2 runtime image)
python3 - "$OUT_DIR" <<'PYEOF'
import json, pathlib, subprocess, sys, shlex

out = pathlib.Path(sys.argv[1])
summary = json.loads((out / "protenix2dock_summary.json").read_text())
best = summary.get("best")
if not best:
    sys.exit("no best sample in protenix2dock summary; nothing to score")
conf = pathlib.Path(best["file"])
pose_cif = conf.parent / (conf.stem.replace("_summary_confidence_", "_") + ".cif")
bridge_out = out / "_affinity_bridge"

cmd = [
    "docker", "run", "--rm", "--entrypoint=", "--gpus", "all",
    "--env", "NUMBA_CACHE_DIR=/tmp/numba_cache",
    "-v", "/data/V-Bio:/workspace/vbio:ro",
    "-v", "/data/boltz_cache:/root/.boltz",
    "-v", "/data/boltz_msa_cache:/msa_cache:ro",
    "-v", f"{out}:{out}",
    "-w", "/workspace/vbio",
    "vbio-boltz2-runtime", "python",
    "capabilities/boltz2score/boltz2score.py",
    "--mode", "score", "--input", str(pose_cif),
    "--output_dir", str(bridge_out),
    "--output_format", "mmcif", "--devices", "1", "--accelerator", "gpu",
    "--num_workers", "0", "--target_chain", "A", "--ligand_chain", "B",
    "--seed", "42",
    "--ipsae_pae_cutoff", "12.0", "--ipsae_dist_cutoff", "5.0",
    "--msa_server_url", "http://172.17.3.200:8080",
    "--msa_pairing_strategy", "greedy", "--max_msa_seqs", "8192",
    "--compute_interactions", "--enable_affinity", "--use_msa_server",
]
print("[bridge] running Boltz2Score affinity on", pose_cif.name)
subprocess.run(cmd, check=True)

aff_file = next(bridge_out.glob("*/affinity_*.json"), None)
conf_file = next(bridge_out.glob("*/best_confidence.json"), None)
combined = {
    "pose_source": "protenix2dock",
    "pose_file": str(pose_cif),
    "protenix_best": {k: best.get(k) for k in ("ranking_score", "iptm", "ptm", "plddt")},
}
if aff_file:
    combined["boltz2_affinity"] = json.loads(aff_file.read_text())
if conf_file:
    combined["boltz2_confidence"] = json.loads(conf_file.read_text())
(out / "affinity_bridge_summary.json").write_text(json.dumps(combined, indent=2))
a = (combined.get("boltz2_affinity") or {}).get("affinity_pic50_mw")
print(f"[bridge] done. affinity_pic50_mw={a}")
PYEOF
