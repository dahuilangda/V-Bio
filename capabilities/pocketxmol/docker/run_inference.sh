#!/usr/bin/env bash
set -euo pipefail

: "${PXM_CONFIG_TASK:=configs/sample/examples/dock_smallmol.yml}"
: "${PXM_CONFIG_MODEL:=configs/sample/pxm.yml}"
: "${PXM_OUTDIR:=outputs_examples}"
: "${PXM_DEVICE:=cuda:0}"
: "${PXM_BATCH_SIZE:=0}"

mkdir -p "${PXM_OUTDIR}"

cmd=(
  python scripts/sample_use.py
  --config_task "${PXM_CONFIG_TASK}"
  --config_model "${PXM_CONFIG_MODEL}"
  --outdir "${PXM_OUTDIR}"
  --device "${PXM_DEVICE}"
)

if [ "${PXM_BATCH_SIZE}" != "0" ]; then
  cmd+=(--batch_size "${PXM_BATCH_SIZE}")
fi

if [ -n "${PXM_EXTRA_ARGS:-}" ]; then
  read -r -a extra_args <<< "${PXM_EXTRA_ARGS}"
  cmd+=("${extra_args[@]}")
fi

echo "[run_inference] ${cmd[*]}"
exec "${cmd[@]}"
