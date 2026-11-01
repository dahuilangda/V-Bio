#!/bin/bash

# Copyright 2024 ByteDance and/or its affiliates.
# Modifications Copyright 2026 Molence Bio
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
#
# This file has been modified by Molence Bio
# Based on Protenix: https://github.com/bytedance/Protenix
# Upstream revision: bd54a05d047b8925a241056f36d619700604068a
# Upstream file: scripts/database/download_protenix_data.sh
# Changes: Downloads the Mol-1 data release instead of the Protenix archives.

# --- Script Configuration & Safety Settings ---
set -euo pipefail

# Log functions for formatted output
info() { echo -e "\033[1;34m[INFO]\033[0m $1"; }
err() { echo -e "\033[1;31m[ERROR]\033[0m $1"; exit 1; }

# --- Help Message ---
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Downloads the Mol-1 reference data into \$MOL_ROOT_DIR."
    echo ""
    echo "Options:"
    echo "  --inference_only    common.tar.gz and search_database.tar.gz (default; about 0.3 GB)"
    echo "  --full              also mmcif_msa_template.tar.gz, the precomputed MSAs and"
    echo "                      template hits training reads (9.8 GB, 46 GB extracted)"
    echo "  -h, --help          Show this help message"
    exit 0
}

# --- Parse Arguments ---
DOWNLOAD_MODE="inference"
while [[ $# -gt 0 ]]; do
    case $1 in
        --inference_only)
            DOWNLOAD_MODE="inference"
            shift
            ;;
        --full)
            DOWNLOAD_MODE="full"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            err "Unknown option: $1"
            ;;
    esac
done

# --- Check Environment Variables ---
if [ -z "${MOL_ROOT_DIR:-}" ]; then
    err "MOL_ROOT_DIR is not set. Please set it before running this script.\nExample: export MOL_ROOT_DIR=/path/to/your/data_root"
fi
BASE_URL="https://mol-1-release.oss-cn-shanghai.aliyuncs.com"

info "Using MOL_ROOT_DIR: $MOL_ROOT_DIR"
info "Downloading from: $BASE_URL"
info "Download mode: $DOWNLOAD_MODE"
mkdir -p "$MOL_ROOT_DIR"
# --- Download & Extract Data Components ---
DATA_FILES=(
    "common.tar.gz"
    "search_database.tar.gz"
)
if [ "$DOWNLOAD_MODE" == "full" ]; then
    DATA_FILES+=("mmcif_msa_template.tar.gz")
fi

# wget or curl when installed, otherwise Python's standard library. Each resumes
# a partial file and leaves a complete one alone. Resolved once, so a missing
# downloader fails here rather than in the middle of a 10 GB fetch.
if command -v wget > /dev/null 2>&1; then
    fetch() { wget -c -P "$MOL_ROOT_DIR" "$1"; }
elif command -v curl > /dev/null 2>&1; then
    fetch() { curl -fL -C - -o "$MOL_ROOT_DIR/$(basename "$1")" "$1"; }
elif PYTHON_BIN=$(command -v "${PYTHON:-python3}"); then
    fetch() {
        "$PYTHON_BIN" - "$1" "$MOL_ROOT_DIR/$(basename "$1")" <<'PY'
import os, shutil, sys, urllib.request

url, dest = sys.argv[1], sys.argv[2]
have = os.path.getsize(dest) if os.path.exists(dest) else 0
with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as head:
    total = int(head.headers.get("Content-Length", -1))
if have == total:
    print(f"{dest}: already complete ({total} bytes)")
    sys.exit(0)
if have > total:
    have = 0
request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
with urllib.request.urlopen(request, timeout=60) as response:
    # 206 continues the partial file; 200 means the server sent it whole.
    resumed = response.status == 206
    with open(dest, "ab" if resumed else "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
size = os.path.getsize(dest)
print(f"{dest}: {size} bytes" + (f", resumed from {have}" if resumed else ""))
if total >= 0 and size != total:
    sys.exit(f"{dest}: expected {total} bytes, got {size}")
PY
    }
else
    err "None of wget, curl or python3 is available. Install one of them, or fetch the\nfiles from $BASE_URL by hand and extract them into MOL_ROOT_DIR."
fi

for file in "${DATA_FILES[@]}"; do
    info "Downloading $file from $BASE_URL/$file ..."
    fetch "$BASE_URL/$file"

    info "Extracting $file to $MOL_ROOT_DIR ..."
    tar -xzf "$MOL_ROOT_DIR/$file" -C "$MOL_ROOT_DIR"

    info "Removing $file ..."
    rm "$MOL_ROOT_DIR/$file"
done

info "Selected data components ($DOWNLOAD_MODE) have been downloaded and extracted successfully to $MOL_ROOT_DIR"
