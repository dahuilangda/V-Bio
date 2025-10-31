#!/bin/bash -ex


# --- Configuration (Absolute paths within the container) ---
APP_DIR="/app"
WORKDIR="${DB_DIR:-${APP_DIR}/databases}"
TMP_DIR="${WORKDIR}/tmp_workspace"

# --- Database & Server Configuration ---
UNIREF30DB="uniref30_2302"
COLABDB="colabfold_envdb_202108"
PDB100="pdb100_230517"

# PDB Sync configuration
PDB_SERVER="${PDB_SERVER:-"rsync.wwpdb.org::ftp"}"
PDB_PORT="${PDB_PORT:-"33444"}"
PDB_AWS_DOWNLOAD="${PDB_AWS_DOWNLOAD:-}"
PDB_AWS_SNAPSHOT="20240101"

# Performance configuration
ARIA_NUM_CONN=8
GPU="${GPU:-}"

# --- Create Directories ---
mkdir -p -- "${WORKDIR}"
mkdir -p -- "${TMP_DIR}"
mkdir -p -- "${APP_DIR}/jobs"
mkdir -p -- "${APP_DIR}/tmp"
cd "${WORKDIR}"

# --- Helper Functions ---
hasCommand () {
    command -v "$1" >/dev/null 2>&1
}

fail() {
    echo "Error: $1" >&2
    exit 1
}

# Determine the best available download tool
STRATEGY=""
if hasCommand aria2c; then STRATEGY="$STRATEGY ARIA"; fi
if hasCommand curl;   then STRATEGY="$STRATEGY CURL"; fi
if hasCommand wget;   then STRATEGY="$STRATEGY WGET"; fi
if [ "$STRATEGY" = "" ]; then
    fail "No download tool found. Please install aria2c, curl, or wget."
fi

if [ -n "${PDB_AWS_DOWNLOAD}" ] && ! hasCommand aws; then
    fail "PDB_AWS_DOWNLOAD is enabled, but 'aws' command not found. Please install AWS CLI."
fi

downloadFile() {
    local URL="$1"
    local OUTPUT="$2"
    echo "Downloading ${URL} to ${OUTPUT}..."
    set +e
    for i in $STRATEGY; do
        case "$i" in
        ARIA)
            local FILENAME=$(basename "${OUTPUT}")
            local DIR=$(dirname "${OUTPUT}")
            aria2c --max-connection-per-server="$ARIA_NUM_CONN" --allow-overwrite=true -c -o "$FILENAME" -d "$DIR" "$URL" && set -e && return 0
            ;;
        CURL)
            curl -L -C - -o "$OUTPUT" "$URL" && set -e && return 0
            ;;
        WGET)
            wget -c -O "$OUTPUT" "$URL" && set -e && return 0
            ;;
        esac
    done
    set -e
    fail "Could not download ${URL}"
}

# --- Main Setup Logic ---

# 1. Download all required archives first
if [ ! -f DOWNLOADS_READY ]; then
  echo "--- Step 1: Downloading all database archives ---"
  downloadFile "https://wwwuser.gwdg.de/~compbiol/colabfold/${UNIREF30DB}.tar.gz" "${UNIREF30DB}.tar.gz"
  downloadFile "https://wwwuser.gwdg.de/~compbiol/colabfold/${COLABDB}.tar.gz" "${COLABDB}.tar.gz"
  downloadFile "https://wwwuser.gwdg.de/~compbiol/colabfold/${PDB100}.fasta.gz" "${PDB100}.fasta.gz"
  downloadFile "https://wwwuser.gwdg.de/~compbiol/data/hhsuite/databases/hhsuite_dbs/pdb100_foldseek_230517.tar.gz" "pdb100_foldseek_230517.tar.gz"
  touch DOWNLOADS_READY
else
  echo "--- Step 1: Database archives already downloaded (DOWNLOADS_READY exists) ---"
fi

# 2. Synchronize PDB mmCIF files
if [ ! -f PDB_MMCIF_READY ]; then
  echo "--- Step 2: Setting up PDB mmCIF files ---"
  mkdir -p pdb/divided
  mkdir -p pdb/obsolete
  if [ -n "${PDB_AWS_DOWNLOAD}" ]; then
    echo "Downloading initial PDB snapshot from AWS S3..."
    aws s3 cp --no-sign-request --recursive "s3://pdbsnapshots/${PDB_AWS_SNAPSHOT}/pub/pdb/data/structures/divided/mmCIF/" pdb/divided/
  fi
  echo "Syncing latest PDB changes via rsync..."
  rsync -rlptP -z --delete --port=${PDB_PORT} ${PDB_SERVER}/data/structures/divided/mmCIF/ pdb/divided
  rsync -rlptP -z --delete --port=${PDB_PORT} ${PDB_SERVER}/data/structures/obsolete/mmCIF/ pdb/obsolete
  touch PDB_MMCIF_READY
else
  echo "--- Step 2: PDB mmCIF files already set up (PDB_MMCIF_READY exists) ---"
fi

# 3. Process and index each database
export MMSEQS_FORCE_MERGE=1

# Configure GPU parameters if enabled
GPU_PAR=""
GPU_INDEX_PAR=""
if [ -n "${GPU}" ]; then
  if ! mmseqs --help | grep -q 'gpuserver'; then
    fail "The installed MMseqs2 has no GPU support. Please update to a GPU-enabled version."
  fi
  echo "Enabling GPU for MMseqs2 processing."
  GPU_PAR="--gpu 1"
  GPU_INDEX_PAR="--split 1 --index-subset 2"
fi

# Uniref30 (Profile DB)
if [ ! -f UNIREF30_READY ]; then
  echo "--- Step 3a: Processing UniRef30 database ---"
  tar xzvf "${UNIREF30DB}.tar.gz"
  mmseqs tsv2exprofiledb "${UNIREF30DB}" "${UNIREF30DB}_db" ${GPU_PAR}
  mmseqs createindex "${UNIREF30DB}_db" "${TMP_DIR}/tmp1" --remove-tmp-files 1 ${GPU_INDEX_PAR}
  if [ -e "${UNIREF30DB}_db_mapping" ]; then ln -sf "${UNIREF30DB}_db_mapping" "${UNIREF30DB}_db.idx_mapping"; fi
  if [ -e "${UNIREF30DB}_db_taxonomy" ]; then ln -sf "${UNIREF30DB}_db_taxonomy" "${UNIREF30DB}_db.idx_taxonomy"; fi
  touch UNIREF30_READY
else
  echo "--- Step 3a: UniRef30 database already processed (UNIREF30_READY exists) ---"
fi

# ColabFold Environment DB (Profile DB)
if [ ! -f COLABDB_READY ]; then
  echo "--- Step 3b: Processing ColabFold Env database ---"
  tar xzvf "${COLABDB}.tar.gz"
  mmseqs tsv2exprofiledb "${COLABDB}" "${COLABDB}_db" ${GPU_PAR}
  mmseqs createindex "${COLABDB}_db" "${TMP_DIR}/tmp2" --remove-tmp-files 1 ${GPU_INDEX_PAR}
  touch COLABDB_READY
else
  echo "--- Step 3b: ColabFold Env database already processed (COLABDB_READY exists) ---"
fi

# PDB100 (Sequence DB)
if [ ! -f PDB_READY ]; then
  echo "--- Step 3c: Processing PDB100 sequence database ---"
  mmseqs createdb "${PDB100}.fasta.gz" "${PDB100}"
  mmseqs createindex "${PDB100}" "${TMP_DIR}/tmp3" --remove-tmp-files 1 ${GPU_INDEX_PAR}
  touch PDB_READY
else
    echo "--- Step 3c: PDB100 sequence database already processed (PDB_READY exists) ---"
fi

# PDB100 A3M files for Foldseek
if [ ! -f PDB100_A3M_READY ]; then
  echo "--- Step 3d: Extracting and renaming PDB100 A3M database for Foldseek ---"
  tar xzvf pdb100_foldseek_230517.tar.gz pdb100_a3m.ffdata pdb100_a3m.ffindex
  mv pdb100_a3m.ffdata "${PDB100}_a3m.ffdata"
  mv pdb100_a3m.ffindex "${PDB100}_a3m.ffindex"
  touch PDB100_A3M_READY
else
  echo "--- Step 3d: PDB100 A3M database already extracted (PDB100_A3M_READY exists) ---"
fi

# --- Database Verification ---
echo "--- Verifying database files ---"
echo "Checking if all required database files exist..."

required_files=(
    "${UNIREF30DB}_db"
    "${UNIREF30DB}_db.idx"
    "${COLABDB}_db"
    "${COLABDB}_db.idx"
    "${PDB100}"
    "${PDB100}.idx"
    "${PDB100}_a3m.ffdata"
    "${PDB100}_a3m.ffindex"
)

for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        echo "ERROR: Required file $file is missing!"
        exit 1
    else
        echo "OK: $file exists"
    fi
done

# --- Finalization ---
echo "--- Cleanup and Launch ---"
echo "Cleaning up temporary archives and folders..."
rm -f "${WORKDIR}"/*.tar.gz
rm -rf "${TMP_DIR}"

cd "${APP_DIR}"

# Test MMseqs2 before starting the server
echo "--- Testing MMseqs2 installation ---"
if ! mmseqs version; then
    echo "ERROR: MMseqs2 is not working properly"
    exit 1
fi

echo "All databases and indices are ready."
echo "Starting ColabFold API server with debug mode..."

# Add debug environment variables
export MMSEQS_THREADS=4
export MMSEQS_NUM_ITERATIONS=3
if [ -n "${MMSEQS_LOAD_MODE}" ]; then
    export MMSEQS_LOAD_MODE
    echo "MMSEQS_LOAD_MODE=${MMSEQS_LOAD_MODE}"
fi

# Start with more verbose output and error handling
echo "Configuration file content:"
cat config.json

echo "Starting server..."
./msa-server -local -config config.json
