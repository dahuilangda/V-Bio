#!/usr/bin/env bash
# Copyright 2026 Molence Bio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Fine-tune the full Mol-1 model on a dataset you prepared.
# Usage: bash finetune_demo.sh  # data from ./finetune_data, weights from $MOL_ROOT_DIR/checkpoint/mol-1-base.pt
#        DRY_RUN=1 bash finetune_demo.sh
#        FINETUNE_DATA_DIR=/path/to/data CHECKPOINT_PATH=/path/to/model.pt \
#          BASE_DIR=/path/to/output bash finetune_demo.sh
# FINETUNE_DATA_DIR holds what scripts/prepare_training_data.py and, optionally,
# scripts/prepare_finetune_msa.py write (see docs/finetune.md):
#   bioassembly/*.pkl.gz   indices.csv   msa/seq_to_msa_dir.json (optional)
# Without the MSA index, training runs without MSAs. MOL_ROOT_DIR needs only
# the common/ files that inference uses. Relative paths are taken from the
# repository root.
# Extra train.py flags are appended last and override the defaults below.
# Fast LayerNorm is the default; LAYERNORM_TYPE=torch selects the torch path.

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
# Mirrors the config default load_checkpoint_dir/<model_name>.pt.
CHECKPOINT_PATH="${CHECKPOINT_PATH:-${MOL_ROOT_DIR:-${HOME}}/checkpoint/mol-1-base.pt}"
FINETUNE_DATA_DIR="${FINETUNE_DATA_DIR:-${SCRIPT_DIR}/finetune_data}"
MSA_INDEX="${FINETUNE_DATA_DIR}/msa/seq_to_msa_dir.json"

# The training set reuses the weighted-PDB slot of the data config, for its
# cluster-weighted sampler and cropping; only its paths change.
TRAIN_SET=weightedPDB_before210930_v20260101
if [[ -f "${MSA_INDEX}" ]]; then
    msa_args=(
        --data.msa.enable_prot_msa true
        --data.msa.prot_seq_or_filename_to_msadir_jsons "${MSA_INDEX}"
        --data.msa.prot_msadir_raw_paths "${FINETUNE_DATA_DIR}/msa"
    )
else
    msa_args=(--data.msa.enable_prot_msa false)
fi

finetune_command=(
    "${PYTHON:-python3}" "${SCRIPT_DIR}/mol/runner/train.py"
    --model_name mol-1-base
    --run_name "${RUN_NAME:-mol_finetune}"
    --seed 42
    --base_dir "${BASE_DIR:-${SCRIPT_DIR}/output}"
    --dtype bf16
    --project Mol-1
    --use_wandb false
    --diffusion_batch_size 48
    --log_interval 50
    --checkpoint_interval 400
    --ema_decay 0.999
    --train_crop_size 384
    --max_steps 100000
    --warmup_steps 2000
    --lr 0.001
    --model.N_cycle 4
    --sample_diffusion.N_step 20
    --triangle_attention cuequivariance
    --triangle_multiplicative cuequivariance
    --load_checkpoint_path "${CHECKPOINT_PATH}"
    --load_params_only true
    --load_strict true
    --data.train_sets "${TRAIN_SET}"
    --data."${TRAIN_SET}".base_info.bioassembly_dict_dir "${FINETUNE_DATA_DIR}/bioassembly"
    --data."${TRAIN_SET}".base_info.indices_fpath "${FINETUNE_DATA_DIR}/indices.csv"
    "${msa_args[@]}"
    # Protein MSAs only; RNA MSAs and templates need search results that the
    # preparation scripts do not produce.
    --data.msa.enable_rna_msa false
    --data.template.enable_prot_template false
    # No test set is configured. To evaluate during training, name one
    # configured in mol/config/data.py with --data.test_sets and set
    # --eval_interval.
    --data.test_sets ""
    --eval_interval -1
    "$@"
)

if [[ "${DRY_RUN:-0}" == "1" || "${DRY_RUN:-0}" == "true" ]]; then
    printf '%q ' "${finetune_command[@]}"
    printf '\n'
    exit 0
fi

if [[ ! -f "${FINETUNE_DATA_DIR}/indices.csv" ]]; then
    echo "No ${FINETUNE_DATA_DIR}/indices.csv. Prepare a dataset first (docs/finetune.md)," \
         "or point FINETUNE_DATA_DIR at one." >&2
    exit 1
fi
exec "${finetune_command[@]}"
