#!/bin/bash
# 该脚本用于在服务器上手动下载、解压和索引所有 ColabFold 数据库。
set -e # 如果任何命令失败，立即退出

# --- 配置 ---
# !!! 重要: 请确保这个路径是你希望存放数据库的真实路径 !!!
# 所有数据将下载到这里。
DB_DIR="/home/dahuilangda/DATABASE"
# ----------------------------------------------------

# 临时工作区，用于存放下载的压缩包和解压文件
TMP_DIR="${DB_DIR}/tmp_workspace"
PDB_SERVER=rsync.wwpdb.org::ftp
PDB_PORT=33444

echo "数据库将安装在: ${DB_DIR}"
echo "临时文件将存放在: ${TMP_DIR}"
echo "开始操作..."

# --- 创建所需目录 ---
mkdir -p "${DB_DIR}"
mkdir -p "${TMP_DIR}"
mkdir -p "${DB_DIR}/pdb/divided"
mkdir -p "${DB_DIR}/pdb/obsolete"

# --- 健壮的数据库设置函数 ---
setup_database() {
    local DB_NAME="$1"
    local ORIGINAL_BASENAME="$2"
    local DOWNLOAD_URL="$3"

    # 检查完成标志，如果存在则跳过
    if [ ! -f "${DB_DIR}/${DB_NAME}.done" ]; then
        local EXTRACT_DIR="${TMP_DIR}/${DB_NAME}_extract"
        
        echo "--- 正在设置 ${DB_NAME} ---"
        echo "在 ${EXTRACT_DIR} 准备干净的提取环境"
        rm -rf "${EXTRACT_DIR}"
        mkdir -p "${EXTRACT_DIR}"

        local TAR_FILE="${TMP_DIR}/${ORIGINAL_BASENAME}.tar.gz"
        
        echo "下载 ${DB_NAME} (如果需要)..."
        aria2c -c -x 16 -s 16 -d "${TMP_DIR}" -o "${ORIGINAL_BASENAME}.tar.gz" "${DOWNLOAD_URL}"

        echo "提取 ${DB_NAME} 到隔离目录..."
        tar -xzf "${TAR_FILE}" -C "${EXTRACT_DIR}"

        local TAR_SUBDIR="${EXTRACT_DIR}/${ORIGINAL_BASENAME}"
        if [ -d "${TAR_SUBDIR}" ]; then
            echo "扁平化 ${DB_NAME} 的目录结构..."
            mv "${TAR_SUBDIR}"/* "${EXTRACT_DIR}/"
            rmdir "${TAR_SUBDIR}"
        fi

        # 注意: 这里的逻辑与 Docker 启动脚本中的不同，因为我们直接使用最终的名称。
        # 不需要重命名步骤。

        echo "清理最终目标目录中的旧文件: ${DB_DIR}"
        rm -f "${DB_DIR}/${DB_NAME}"*
        
        echo "移动最终的数据库文件到 ${DB_DIR}"
        mv "${EXTRACT_DIR}"/* "${DB_DIR}/"

        if [ -f "${DB_DIR}/${DB_NAME}.tsv" ] && [ ! -f "${DB_DIR}/${DB_NAME}" ]; then
            echo "应用兼容性修复: 重命名 '${DB_NAME}.tsv' 为 '${DB_NAME}'"
            mv "${DB_DIR}/${DB_NAME}.tsv" "${DB_DIR}/${DB_NAME}"
        fi

        echo "正在为 ${DB_NAME} 创建索引..."
        # 注意: 我们需要 mmseqs 命令。请确保它已安装并在你的 PATH 中。
        mmseqs createindex "${DB_DIR}/${DB_NAME}" "${TMP_DIR}/tmp_idx" --remove-tmp-files 1 --split 1
        
        echo "完成 ${DB_NAME} 的设置..."
        touch "${DB_DIR}/${DB_NAME}.done"
        
        rm -rf "${EXTRACT_DIR}"
    else
        echo "${DB_NAME} 已存在，跳过。"
    fi
}

# --- 检查依赖 ---
if ! command -v aria2c &> /dev/null; then
    echo "错误: aria2c 未安装。请运行 'sudo apt-get install aria2' 或使用你的包管理器安装。"
    exit 1
fi
if ! command -v rsync &> /dev/null; then
    echo "错误: rsync 未安装。请运行 'sudo apt-get install rsync' 或使用你的包管理器安装。"
    exit 1
fi
if ! command -v mmseqs &> /dev/null; then
    echo "错误: mmseqs 未安装。请参考 MMseqs2 文档进行安装。"
    exit 1
fi

# --- 为每个数据库调用设置函数 ---
# 使用你在脚本中提供的最终名称
setup_database "uniref30_2202" "uniref30_2202" "https://wwwuser.gwdg.de/~compbiol/colabfold/uniref30_2202.tar.gz"
setup_database "pdb100_230517" "pdb100_230517" "https://wwwuser.gwdg.de/~compbiol/colabfold/pdb100_230517.tar.gz"
setup_database "pdb70_220313" "pdb70_220313" "https://wwwuser.gwdg.de/~compbiol/colabfold/pdb70_220313.tar.gz"
setup_database "colabfold_envdb_202108" "colabfold_envdb_202108" "https://wwwuser.gwdg.de/~compbiol/colabfold/colabfold_envdb_202108.tar.gz"

# --- PDB mmCIF 文件 (用于模板) ---
if [ ! -f "${DB_DIR}/pdb.done" ]; then
    echo "通过 rsync 下载 PDB mmCIF 文件..."
    rsync -rlptP -z --delete --port=${PDB_PORT} ${PDB_SERVER}/data/structures/divided/mmCIF/ "${DB_DIR}/pdb/divided/"
    rsync -rlptP -z --delete --port=${PDB_PORT} ${PDB_SERVER}/data/structures/obsolete/mmCIF/ "${DB_DIR}/pdb/obsolete/"
    touch "${DB_DIR}/pdb.done"
else
    echo "PDB mmCIF 文件已存在，跳过。"
fi

# --- 收尾工作 ---
echo "清理临时的已下载压缩包..."
rm -f "${TMP_DIR}"/*.tar.gz
rm -rf "${TMP_DIR}"

echo "--- 所有数据库已成功准备就绪！---"
echo "数据位于: ${DB_DIR}"

