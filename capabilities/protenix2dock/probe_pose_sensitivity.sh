#!/bin/bash
# Pose-sensitivity probe: same complex (cdk8 lig_0 rich pose), three poses:
#   A = p2d best sample        B = ligand shifted +10A off-site
#   C = B rotated 180deg at same site (alternate binding mode, plausible)
# For each pose, score-only the complex with the native head and compare the
# affinity values. Sensitive structure channel => A and C separate from B
# (B off-site must score markedly different); pose-selectivity => A vs C differ.
set -u
CKPT=$1
OUT=/data/affinity_training/pose_probe
mkdir -p $OUT
P0=/tmp/p2d_test/bench_p2d/lig_0/protenix2dock_job/seed_42/predictions
CIF=$(python3 - <<PYEOF
import json, pathlib
d = json.load(open('/tmp/p2d_test/bench_p2d/lig_0/protenix2dock_summary.json'))
p = pathlib.Path(d['best']['file'])
print(p.parent / (p.stem.replace('_summary_confidence_', '_') + '.cif'))
PYEOF
)
cp "$CIF" $OUT/pose_A.cif
echo "poses ready (pre-generated)"

# score each pose
for P in A B C; do
  docker run --rm --entrypoint= --gpus device=2 \
    -v /data/V-Bio:/workspace/vbio:ro -v /data/protenix/common_cache:/cache/common:ro \
    -v /data/protenix/model:/workspace/model:ro -v $OUT:/o \
    --env PYTHONPATH=/workspace/vbio/vendor/protenix-source --env PROTENIX_ROOT_DIR=/cache \
    --env PROTENIX_AFFINITY_CKPT=$CKPT \
    -w /workspace/vbio \
    vbio-protenix-v2-runtime:2.0.0 /usr/local/micromamba/envs/protenix/bin/python \
    capabilities/protenix2dock/protenix2dock.py \
    --mode score --input /o/pose_${P}.pdb --output_dir /o/out_${P} --seed 42 --low_vram \
    > /dev/null 2>&1
  docker run --rm --entrypoint= --gpus device=2 \
    -v /data/V-Bio:/workspace/vbio:ro -v /data/protenix/common_cache:/cache/common:ro \
    -v /data/protenix/model:/workspace/model:ro -v $OUT:/o \
    --env PYTHONPATH=/workspace/vbio/vendor/protenix-source --env PROTENIX_ROOT_DIR=/cache \
    --env PROTENIX_AFFINITY_CKPT=$CKPT -w /workspace/vbio \
    vbio-protenix-v2-runtime:2.0.0 /usr/local/micromamba/envs/protenix/bin/python \
    capabilities/protenix2dock/protenix2dock.py --mode score --input /o/pose_${P}.pdb \
    --output_dir /o/out_${P} --seed 42 --low_vram > $OUT/run_${P}.log 2>&1 || true
  V=$(P=$P python3 -c "
import json, os, pathlib
f = next(pathlib.Path('/o/out_'+os.environ['P']).glob('**/*_summary_confidence_sample_0.json'))
d = json.loads(f.read_text())
print(round(d.get('affinity_pred_value', float('nan')), 4), round(d.get('affinity_pred_std', float('nan')), 4))" 2>/dev/null)
  echo "pose_$P: affinity=$V"
done