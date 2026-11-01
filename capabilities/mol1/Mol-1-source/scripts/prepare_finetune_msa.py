#!/usr/bin/env python3
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
"""Search protein MSAs for a fine-tuning set and index them for training.

Collects the protein sequences of the bioassembly dicts written by
`prepare_training_data.py`, searches each one with the same MSA service that
`mol msa` uses at inference, and writes `seq_to_msa_dir.json` mapping every
sequence to its result directory. Training looks a chain's sequence up in that
index and reads `pairing.a3m` and `non_pairing.a3m` from the directory; a chain
whose sequence is missing trains without an MSA.

Sequences already in an existing index are not searched again, so an
interrupted run can simply be repeated.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mol.runner.msa_search import msa_search  # noqa: E402
from mol.utils.file_io import load_gzip_pickle  # noqa: E402
from mol.web_service.colab_request_parser import COLABFOLD_SERVICE_HOST_URL  # noqa: E402

INDEX_NAME = "seq_to_msa_dir.json"


def protein_sequences(bioassembly_dir: Path) -> list[str]:
    """Return the unique protein sequences of every bioassembly dict, sorted."""
    sequences = set()
    for path in sorted(bioassembly_dir.glob("*.pkl.gz")):
        bioassembly = load_gzip_pickle(path)
        for entity_id, poly_type in bioassembly["entity_poly_type"].items():
            if poly_type == "polypeptide(L)" and entity_id in bioassembly["sequences"]:
                sequences.add(bioassembly["sequences"][entity_id])
    return sorted(sequences)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "-b",
        "--bioassembly_dir",
        type=Path,
        required=True,
        help="Directory of bioassembly dicts (*.pkl.gz).",
    )
    parser.add_argument(
        "-o",
        "--out_dir",
        type=Path,
        required=True,
        help=f"Directory for the MSAs and {INDEX_NAME}.",
    )
    args = parser.parse_args()
    print(f"MSA service: {COLABFOLD_SERVICE_HOST_URL}")

    sequences = protein_sequences(args.bioassembly_dir)
    if not sequences:
        sys.exit(f"No protein sequences in {args.bioassembly_dir}/*.pkl.gz")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.out_dir / INDEX_NAME
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    pending = [seq for seq in sequences if seq not in index]
    print(f"{len(sequences)} protein sequences, {len(pending)} to search")
    failed = 0
    for n, seq in enumerate(pending, 1):
        # Named by the sequence, so a rerun with more structures keeps the
        # directories already searched.
        name = hashlib.sha256(seq.encode()).hexdigest()[:16]
        try:
            result_dirs = msa_search([seq], str(args.out_dir / name))
        except Exception as e:  # one failed search should not end the run
            print(f"[{n}/{len(pending)}] {type(e).__name__}: {e}")
            result_dirs = []
        if not result_dirs or not os.path.exists(
            os.path.join(result_dirs[0], "non_pairing.a3m")
        ):
            failed += 1
            print(f"[{n}/{len(pending)}] search failed, left without an MSA: {seq[:30]}...")
            continue
        # Relative to out_dir, so the dataset can be moved as a whole.
        index[seq] = os.path.relpath(result_dirs[0], args.out_dir)
        index_path.write_text(json.dumps(index, indent=1))
        print(f"[{n}/{len(pending)}] {index[seq]}")

    print(f"{len(index)}/{len(sequences)} sequences indexed in {index_path}")
    if failed:
        sys.exit(f"{failed} searches failed; rerun to retry them")


if __name__ == "__main__":
    main()
