# run_single_prediction.py
import sys
import os
import json
import tempfile
import shutil
import traceback
import yaml
import hashlib
import glob
import csv
import zipfile
import shlex
import requests
import time
import tarfile
import io
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Iterable
import subprocess

sys.path.append(os.getcwd())
from boltz_wrapper import predict
from config import (
    MSA_SERVER_URL,
    MSA_SERVER_MODE,
    COLABFOLD_JOBS_DIR,
    ALPHAFOLD3_DOCKER_IMAGE,
    ALPHAFOLD3_MODEL_DIR,
    ALPHAFOLD3_DATABASE_DIR,
    ALPHAFOLD3_DOCKER_EXTRA_ARGS,
)
from af3_adapter import (
    AF3Preparation,
    build_af3_fasta,
    build_af3_json,
    collect_chain_msa_paths,
    load_unpaired_msa,
    parse_yaml_for_af3,
    safe_filename,
    serialize_af3_json,
)

# MSA 缓存配置
MSA_CACHE_CONFIG = {
    'cache_dir': '/tmp/boltz_msa_cache',
    'enable_cache': True
}


def discover_cuda_devices() -> List[str]:
    """Return detected CUDA device indices present on the host."""
    devices: List[str] = []

    try:
        smi_proc = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        smi_proc = None

    if smi_proc and smi_proc.returncode == 0:
        for line in smi_proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("GPU "):
                continue
            prefix = line.split(':', 1)[0]
            parts = prefix.split()
            if len(parts) >= 2 and parts[1].isdigit():
                devices.append(parts[1])

    if devices:
        return sorted(set(devices), key=int)

    node_paths = Path('/dev').glob('nvidia[0-9]*')
    for node in node_paths:
        suffix = node.name.replace('nvidia', '', 1)
        if suffix.isdigit():
            devices.append(suffix)

    return sorted(set(devices), key=int)


def determine_docker_gpu_arg(visible_devices: Optional[str]) -> str:
    """Validate CUDA availability and build docker --gpus argument."""
    available = discover_cuda_devices()
    if not available:
        raise RuntimeError(
            "AlphaFold3 backend 需要 NVIDIA GPU，但当前环境未检测到可用的 CUDA 设备。"
        )

    if not visible_devices:
        return "all"

    tokens = [token.strip() for token in visible_devices.split(',') if token.strip()]
    if not tokens:
        raise RuntimeError("检测到 CUDA_VISIBLE_DEVICES 已设置，但未包含有效设备索引。")

    numeric_tokens = [token for token in tokens if token.isdigit()]
    invalid = [token for token in numeric_tokens if token not in available]
    if invalid:
        raise RuntimeError(
            "请求使用的 GPU 索引在当前机器上不可用: "
            f"{', '.join(invalid)}。可用索引: {', '.join(available)}"
        )

    return f"device={','.join(tokens)}"


def collect_gpu_device_group_ids() -> List[int]:
    """Capture host group IDs owning GPU device files to re-add inside the container."""
    candidate_nodes = [
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia-uvm"),
        Path("/dev/nvidia-uvm-tools"),
    ]

    candidate_nodes.extend(sorted(Path("/dev").glob("nvidia[0-9]*")))
    candidate_nodes.extend(sorted(Path("/dev/dri").glob("renderD*") if Path("/dev/dri").exists() else []))

    group_ids: List[int] = []
    for node in candidate_nodes:
        try:
            stat_result = node.stat()
        except FileNotFoundError:
            continue
        gid = stat_result.st_gid
        if gid not in group_ids:
            group_ids.append(gid)

    return group_ids


def sanitize_docker_extra_args(raw_args: list) -> list:
    """
    清理 Docker 额外参数，忽略不完整的 --env/-e 标志以免吞掉镜像名称。
    """
    sanitized = []
    i = 0

    while i < len(raw_args):
        token = raw_args[i]

        if token in ("--env", "-e"):
            if i + 1 >= len(raw_args):
                print(f"⚠️ 忽略无效的 Docker 参数: {token} (缺少值)", file=sys.stderr)
                i += 1
                continue

            value = raw_args[i + 1]
            if "=" not in value:
                print(f"⚠️ 忽略无效的 Docker 参数: {token} {value} (缺少 KEY=VALUE 形式)", file=sys.stderr)
                i += 2
                continue

            sanitized.extend([token, value])
            i += 2
            continue

        sanitized.append(token)
        i += 1

    return sanitized


def sanitize_a3m_content(content: str, context: str = "") -> str:
    """
    移除 A3M 内容中的非法控制字符（例如 \\x00）。
    """
    sanitized = content.replace("\x00", "")
    if sanitized != content:
        msg_context = f" ({context})" if context else ""
        print(f"⚠️ 检测到并移除非法字符\\x00{msg_context}", file=sys.stderr)
    return sanitized


def sanitize_a3m_file(path: str, context: str = "") -> None:
    """
    对 A3M 文件进行清理，移除非法控制字符。
    """
    if not os.path.exists(path):
        return

    try:
        with open(path, "r") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"⚠️ 无法读取 A3M 文件进行清理: {path}, {e}", file=sys.stderr)
        return

    sanitized = sanitize_a3m_content(content, context=context or path)
    if sanitized != content:
        try:
            with open(path, "w") as f:
                f.write(sanitized)
        except OSError as e:
            print(f"⚠️ 无法写入清理后的 A3M 文件: {path}, {e}", file=sys.stderr)


def _iter_affinity_entries(properties: Any) -> Iterable[Dict[str, Any]]:
    """标准化 properties 字段，支持 list / dict 等多种写法。"""
    if properties is None:
        return []

    if isinstance(properties, dict):
        # 单个字典，直接作为候选
        return [properties]

    if isinstance(properties, list):
        # 已经是列表，过滤出字典条目
        return [entry for entry in properties if isinstance(entry, dict)]

    # 其他类型不支持
    return []


def extract_affinity_config_from_yaml(yaml_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    从 YAML 数据中提取亲和力配置，兼容 list / dict 等写法。
    支持两种格式：
    1. affinity: true
    2. affinity: {binder: "B"}
    """
    for entry in _iter_affinity_entries(yaml_data.get("properties")):
        affinity_info = entry.get("affinity")

        # 格式1: affinity: {binder: "B"} 或 affinity: {chain: "B"}
        if isinstance(affinity_info, dict):
            binder = affinity_info.get("binder") or affinity_info.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

        # 格式2: affinity: true (需要单独查找binder)
        elif affinity_info is True:
            # 在同一层级或properties层级查找binder字段
            binder = entry.get("binder") or entry.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

            # 如果entry中没有binder，尝试从properties的其他条目中查找
            for other_entry in _iter_affinity_entries(yaml_data.get("properties")):
                binder = other_entry.get("binder") or other_entry.get("chain")
                if binder:
                    return {"binder": str(binder).strip()}

    return None


def _legacy_parse_ligand_from_text(cif_path: Path, binder_chain: str) -> Optional[str]:
    """在缺少 gemmi 时回退到文本解析。"""
    try:
        with cif_path.open("r") as cif_file:
            for line in cif_file:
                if not line.startswith("HETATM"):
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                comp_id = parts[5]
                chain_id = parts[6]
                if chain_id == binder_chain:
                    return comp_id
    except OSError as err:
        print(f"⚠️ 无法读取 CIF 文件 {cif_path}: {err}", file=sys.stderr)
    return None


def find_ligand_resname_in_cif(cif_path: Path, binder_chain: str) -> Optional[str]:
    """
    在结构文件中查找指定链的配体残基名称。
    优先使用 gemmi 解析 mmCIF / PDB，若不可用则退回文本解析。
    """
    try:
        import gemmi  # type: ignore
    except ImportError:
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as err:
        print(f"⚠️ 无法使用 gemmi 解析 {cif_path}: {err}", file=sys.stderr)
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    for model in structure:
        chain = next((ch for ch in model if ch.name == binder_chain), None)
        if chain is None:
            continue
        for residue in chain:
            resname = residue.name.strip()
            if resname:
                return resname
    return None


def _sanitize_atom_name_for_affinity(name: str) -> str:
    """Normalize atom names to avoid unsupported characters in Boltz featurizer."""
    cleaned = name.strip()
    if not cleaned:
        return name

    sanitized_chars: List[str] = []
    for ch in cleaned:
        if ch.isalpha():
            sanitized_chars.append(ch.upper())
        elif ch.isdigit():
            sanitized_chars.append(ch)
        else:
            sanitized_chars.append('X')

    sanitized = ''.join(sanitized_chars)
    return sanitized or name


def prepare_structure_for_affinity(source_path: Path, work_dir: Path) -> Path:
    """Create a sanitized copy of the structure with normalized atom names."""
    try:
        import gemmi  # type: ignore
    except ImportError:
        print(
            "⚠️ 未安装 gemmi，无法清理结构原子名，直接使用原始结构。",
            file=sys.stderr,
        )
        return source_path

    try:
        structure = gemmi.read_structure(str(source_path))
    except Exception as err:
        print(f"⚠️ 无法读取结构 {source_path} 进行清理: {err}", file=sys.stderr)
        return source_path

    changed = False
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    sanitized = _sanitize_atom_name_for_affinity(atom.name)
                    if sanitized != atom.name:
                        atom.name = sanitized
                        changed = True

    if not changed:
        return source_path

    work_dir.mkdir(parents=True, exist_ok=True)
    sanitized_path = work_dir / f"{source_path.stem}_sanitized{source_path.suffix}"

    try:
        if source_path.suffix.lower() == '.cif':
            doc = structure.make_mmcif_document()
            doc.write_file(str(sanitized_path))
        else:
            structure.write_minimal_pdb(str(sanitized_path))
    except Exception as err:
        print(f"⚠️ 写入清理后的结构失败，回退到原始结构: {err}", file=sys.stderr)
        return source_path

    print(
        f"🧼 已生成用于亲和力预测的清理结构: {sanitized_path}",
        file=sys.stderr,
    )
    return sanitized_path


def _structure_candidate_priority(name: str, base_priority: int, jobname: str) -> int:
    priority = base_priority
    suffix = Path(name).suffix.lower()
    if suffix == ".cif":
        priority -= 10
    elif suffix == ".pdb":
        priority -= 5

    lowered = name.lower()
    job_lower = jobname.lower()
    if job_lower and job_lower in lowered:
        priority -= 4
    if "ranked_0" in lowered:
        priority -= 2
    if "predicted" in lowered:
        priority -= 1
    if "model" in lowered:
        priority -= 1
    return priority


def locate_af3_structure_file(af3_output_dir: Path, jobname: str) -> Optional[Path]:
    """Locate the primary AlphaFold3 structure file (.cif or .pdb) for affinity post-processing."""
    base_dir = Path(af3_output_dir)
    if not base_dir.exists():
        return None

    candidates: List[Tuple[int, Path]] = []

    def register_candidate(path: Path, base_priority: int) -> None:
        if not path.is_file():
            return
        priority = _structure_candidate_priority(path.name, base_priority, jobname)
        candidates.append((priority, path))

    job_dir = base_dir / jobname
    search_roots: List[Tuple[int, Path]] = []
    if job_dir.exists():
        search_roots.append((0, job_dir))
    search_roots.append((10, base_dir))

    for base_priority, root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.cif"):
            register_candidate(path, base_priority)
        for path in root.rglob("*.pdb"):
            register_candidate(path, base_priority + 2)

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], len(str(item[1]))))
    return candidates[0][1]


def extract_af3_structure_from_archives(
    af3_output_dir: Path,
    scratch_dir: Path,
    jobname: str,
) -> Optional[Path]:
    archive_candidates: List[Tuple[int, Path, str, str]] = []

    job_dir = af3_output_dir / jobname
    archive_patterns = ["*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.xz", "*.tar.bz2"]

    for pattern in archive_patterns:
        for archive_path in af3_output_dir.rglob(pattern):
            base_priority = 60
            try:
                if job_dir.exists() and archive_path.is_relative_to(job_dir):  # type: ignore[attr-defined]
                    base_priority = 40
            except AttributeError:
                try:
                    archive_path.relative_to(job_dir)
                    base_priority = 40
                except ValueError:
                    base_priority = 60

            suffix = archive_path.suffix.lower()
            if archive_path.name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
                archive_type = "tar"
            elif suffix in {".tar"}:
                archive_type = "tar"
            else:
                archive_type = "zip"

            if archive_type == "zip":
                try:
                    with zipfile.ZipFile(archive_path) as zf:
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            entry_suffix = Path(info.filename).suffix.lower()
                            if entry_suffix not in {".cif", ".pdb"}:
                                continue
                            priority = _structure_candidate_priority(info.filename, base_priority + 10, jobname)
                            archive_candidates.append((priority, archive_path, info.filename, archive_type))
                except (zipfile.BadZipFile, OSError):
                    continue
            else:
                try:
                    with tarfile.open(archive_path, "r:*") as tf:
                        for member in tf.getmembers():
                            if not member.isreg():
                                continue
                            entry_suffix = Path(member.name).suffix.lower()
                            if entry_suffix not in {".cif", ".pdb"}:
                                continue
                            priority = _structure_candidate_priority(member.name, base_priority + 10, jobname)
                            archive_candidates.append((priority, archive_path, member.name, archive_type))
                except (tarfile.TarError, OSError):
                    continue

    if not archive_candidates:
        return None

    archive_candidates.sort(key=lambda item: (item[0], len(item[2])))
    _, selected_archive, selected_member, selected_type = archive_candidates[0]

    scratch_dir.mkdir(parents=True, exist_ok=True)
    member_path = Path(selected_member)
    stem = safe_filename(member_path.stem) or "structure"
    dest_name = stem + member_path.suffix.lower()
    dest_path = scratch_dir / dest_name

    counter = 1
    while dest_path.exists():
        dest_path = scratch_dir / f"{stem}_{counter}{member_path.suffix.lower()}"
        counter += 1

    try:
        if selected_type == "zip":
            with zipfile.ZipFile(selected_archive) as zf:
                with zf.open(selected_member) as source, open(dest_path, "wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            with tarfile.open(selected_archive, "r:*") as tf:
                member = tf.getmember(selected_member)
                extracted = tf.extractfile(member)
                if extracted is None:
                    return None
                with extracted, open(dest_path, "wb") as target:
                    shutil.copyfileobj(extracted, target)
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None

    print(
        f"🔍 从归档文件提取 AlphaFold3 结构: {selected_archive} -> {dest_path}",
        file=sys.stderr,
    )
    return dest_path


def run_af3_affinity_pipeline(
    temp_dir: str,
    yaml_data: Dict[str, Any],
    prep: AF3Preparation,
    af3_output_dir: str,
) -> List[Tuple[Path, str]]:
    """
    若 YAML 配置请求亲和力预测，则在 AlphaFold3 结果上运行 Boltz-2 亲和力流程。
    返回需要附加到归档中的额外文件列表 (Path, arcname)。
    """
    affinity_config = extract_affinity_config_from_yaml(yaml_data)
    if not affinity_config:
        return []

    binder_chain = affinity_config.get("binder")
    if not binder_chain:
        print("ℹ️ 亲和力配置未提供有效的 binder，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = str(binder_chain).strip()
    if not binder_chain:
        print("ℹ️ 亲和力配置 binder 为空，跳过亲和力预测。", file=sys.stderr)
        return []

    ligand_entries = [
        entry for entry in yaml_data.get("sequences", [])
        if isinstance(entry, dict) and "ligand" in entry
    ]
    if not ligand_entries:
        print("ℹ️ 未检测到配体条目，跳过亲和力预测。", file=sys.stderr)
        return []

    binder_chain = prep.chain_id_label_map.get(binder_chain, safe_filename(binder_chain))

    af3_output_path = Path(af3_output_dir)
    model_path = locate_af3_structure_file(af3_output_path, prep.jobname)

    if not model_path or not model_path.exists():
        extracted_path = extract_af3_structure_from_archives(
            af3_output_path,
            Path(temp_dir) / "af3_extracted_structures",
            prep.jobname,
        )
        model_path = extracted_path

    if not model_path or not model_path.exists():
        print(
            "⚠️ 未找到 AlphaFold3 预测的结构文件，无法进行亲和力预测。",
            file=sys.stderr,
        )
        return []

    print(
        f"🔍 使用 AlphaFold3 结构进行亲和力评估: {model_path}",
        file=sys.stderr,
    )

    ligand_resname = find_ligand_resname_in_cif(model_path, binder_chain)
    if not ligand_resname:
        print(
            f"⚠️ 未能在结构中找到链 {binder_chain} 的配体残基，跳过亲和力预测。",
            file=sys.stderr,
        )
        return []

    try:
        from affinity.main import Boltzina
    except ImportError as err:
        print(f"⚠️ 无法导入 Boltz-2 亲和力模块：{err}，跳过亲和力预测。", file=sys.stderr)
        return []

    affinity_base = Path(temp_dir) / "af3_affinity"
    output_dir = affinity_base / "boltzina_output"
    work_dir = affinity_base / "boltzina_work"
    sanitized_struct_dir = affinity_base / "sanitized_structures"

    model_for_affinity = prepare_structure_for_affinity(model_path, sanitized_struct_dir)

    affinity_entries: List[Tuple[Path, str]] = []
    try:
        print(
            f"⚙️ 开始运行 Boltz-2 亲和力评估，配体链: {binder_chain}, 残基名: {ligand_resname}",
            file=sys.stderr,
        )
        boltzina = Boltzina(
            output_dir=str(output_dir),
            work_dir=str(work_dir),
            ligand_resname=ligand_resname,
        )
        boltzina.predict([str(model_for_affinity)])

        if not boltzina.results:
            print("⚠️ 亲和力预测未产生结果，跳过生成 affinity_data.json。", file=sys.stderr)
            return []

        affinity_result = dict(boltzina.results[0])
        affinity_result["ligand_resname"] = ligand_resname
        affinity_result["binder_chain"] = binder_chain
        affinity_result["source"] = "alphafold3"

        affinity_base.mkdir(parents=True, exist_ok=True)
        affinity_json_path = affinity_base / "affinity_data.json"
        with affinity_json_path.open("w") as json_file:
            json.dump(affinity_result, json_file, indent=2)
        affinity_entries.append((affinity_json_path, "affinity_data.json"))

        affinity_csv_path = output_dir / "affinity_results.csv"
        if affinity_csv_path.exists():
            affinity_entries.append((affinity_csv_path, "af3/affinity_results.csv"))

        print("✅ 亲和力预测完成，结果已写入 affinity_data.json。", file=sys.stderr)
    except Exception as err:
        print(f"⚠️ 运行 Boltz-2 亲和力预测失败: {err}", file=sys.stderr)

    return affinity_entries


def get_sequence_hash(sequence: str) -> str:
    """计算序列的MD5哈希值作为缓存键"""
    return hashlib.md5(sequence.encode('utf-8')).hexdigest()

def request_msa_from_server(sequence: str, timeout: int = 600) -> dict:
    """
    从 ColabFold MSA 服务器请求多序列比对
    
    Args:
        sequence: 蛋白质序列（FASTA 格式）
        timeout: 请求超时时间（秒）
    
    Returns:
        包含 MSA 结果的字典，如果失败则返回 None
    """
    try:
        print(f"🔍 正在从 MSA 服务器请求多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        
        # 准备请求数据
        # 确保序列是 FASTA 格式
        if not sequence.startswith('>'):
            sequence = f">query\n{sequence}"
        
        # ColabFold MSA 服务器使用 form data 格式
        payload = {
            "q": sequence,
            "mode": MSA_SERVER_MODE
        }
        print(f"📦 MSA 请求参数: mode={MSA_SERVER_MODE}", file=sys.stderr)
        
        # 提交搜索任务
        submit_url = f"{MSA_SERVER_URL}/ticket/msa"
        print(f"📤 提交 MSA 搜索任务到: {submit_url}", file=sys.stderr)
        
        response = requests.post(submit_url, data=payload, timeout=30)
        if response.status_code != 200:
            print(f"❌ MSA 任务提交失败: {response.status_code} - {response.text}", file=sys.stderr)
            return None
        
        result = response.json()
        ticket_id = result.get("id")
        if not ticket_id:
            print(f"❌ 未获取到有效的任务 ID: {result}", file=sys.stderr)
            return None
        
        print(f"✅ MSA 任务已提交，任务 ID: {ticket_id}", file=sys.stderr)
        
        # 轮询结果
        result_url = f"{MSA_SERVER_URL}/ticket/{ticket_id}"
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                print(f"⏳ 检查 MSA 任务状态...", file=sys.stderr)
                response = requests.get(result_url, timeout=30)
                
                if response.status_code == 200:
                    result_data = response.json()
                    if result_data.get("status") == "COMPLETE":
                        print(f"✅ MSA 搜索完成，获取到结果", file=sys.stderr)
                        download_url = result_data.get("result_url") or f"{MSA_SERVER_URL}/result/download/{ticket_id}"
                        print(f"📥 下载 MSA 结果: {download_url}", file=sys.stderr)
                        try:
                            download_response = requests.get(download_url, timeout=60)
                        except requests.exceptions.RequestException as download_error:
                            print(f"❌ 下载 MSA 结果请求失败: {download_error}", file=sys.stderr)
                            return None
                        if download_response.status_code != 200:
                            print(
                                f"❌ 下载 MSA 结果失败: {download_response.status_code} - {download_response.text}",
                                file=sys.stderr,
                            )
                            return None

                        try:
                            tar_bytes = io.BytesIO(download_response.content)
                            with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
                                a3m_content = None
                                extracted_filename = None
                                for member in tar.getmembers():
                                    if member.name.lower().endswith(".a3m"):
                                        file_obj = tar.extractfile(member)
                                        if file_obj:
                                            a3m_content = file_obj.read().decode("utf-8")
                                            extracted_filename = member.name
                                            break

                            if not a3m_content:
                                print("❌ 未在下载的结果中找到 A3M 文件", file=sys.stderr)
                                return None

                            print(f"✅ 成功提取 A3M 文件: {extracted_filename}", file=sys.stderr)
                            a3m_content = sanitize_a3m_content(a3m_content, context=extracted_filename)
                            entries = parse_a3m_content(a3m_content)
                            return {
                                "entries": entries,
                                "a3m_content": a3m_content,
                                "source": extracted_filename,
                                "ticket_id": ticket_id,
                            }
                        except tarfile.TarError as tar_error:
                            print(f"❌ 解析 MSA 压缩包失败: {tar_error}", file=sys.stderr)
                            return None
                    elif result_data.get("status") == "ERROR":
                        print(f"❌ MSA 搜索失败: {result_data.get('error', '未知错误')}", file=sys.stderr)
                        print(
                            f"   ↳ 服务器返回: {json.dumps(result_data, ensure_ascii=False)}",
                            file=sys.stderr,
                        )
                        return None
                    else:
                        print(f"⏳ MSA 任务状态: {result_data.get('status', 'PENDING')}", file=sys.stderr)
                elif response.status_code == 404:
                    print(f"⏳ 任务尚未完成或不存在", file=sys.stderr)
                else:
                    print(f"⚠️ 检查状态时出现错误: {response.status_code}", file=sys.stderr)
                
            except requests.exceptions.RequestException as e:
                print(f"⚠️ 检查状态时网络错误: {e}", file=sys.stderr)
            
            # 等待一段时间再次检查
            time.sleep(10)
        
        print(f"⏰ MSA 搜索超时 ({timeout}秒)", file=sys.stderr)
        return None
        
    except Exception as e:
        print(f"❌ MSA 服务器请求失败: {e}", file=sys.stderr)
        return None

def save_msa_result_to_file(msa_result: dict, output_path: str) -> bool:
    """
    将 MSA 结果保存到文件
    
    Args:
        msa_result: MSA 服务器返回的结果
        output_path: 输出文件路径
    
    Returns:
        是否成功保存
    """
    try:
        # 根据结果格式保存为 A3M 文件
        if msa_result.get('a3m_content'):
            sanitized_content = sanitize_a3m_content(msa_result['a3m_content'], context=output_path)
            with open(output_path, 'w') as f:
                f.write(sanitized_content)
            return True
        elif 'entries' in msa_result:
            buffer = []
            for entry in msa_result['entries']:
                name = entry.get('name', 'unknown')
                sequence = entry.get('sequence', '')
                if sequence:
                    buffer.append(f">{name}\n{sequence}\n")

            sanitized_content = sanitize_a3m_content(''.join(buffer), context=output_path)
            with open(output_path, 'w') as f:
                f.write(sanitized_content)
            return True
        else:
            print(f"❌ MSA 结果格式不支持: {msa_result.keys()}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"❌ 保存 MSA 结果失败: {e}", file=sys.stderr)
        return False


def parse_a3m_content(a3m_content: str) -> list:
    """
    解析 A3M 文件内容为序列条目列表
    """
    sanitized_content = sanitize_a3m_content(a3m_content)
    entries = []
    current_name = None
    current_sequence_lines = []

    for line in sanitized_content.splitlines():
        if line.startswith('>'):
            if current_name is not None:
                entries.append({
                    'name': current_name or 'unknown',
                    'sequence': ''.join(current_sequence_lines),
                })
            current_name = line[1:].strip()
            current_sequence_lines = []
        else:
            current_sequence_lines.append(line.strip())

    if current_name is not None:
        entries.append({
            'name': current_name or 'unknown',
            'sequence': ''.join(current_sequence_lines),
        })

    return entries
def generate_msa_for_sequences(yaml_content: str, temp_dir: str) -> bool:
    """
    为 YAML 中的蛋白质序列生成 MSA
    
    Args:
        yaml_content: YAML 配置内容
        temp_dir: 临时目录
    
    Returns:
        是否成功生成 MSA
    """
    try:
        print(f"🧬 开始为蛋白质序列生成 MSA", file=sys.stderr)
        
        # 解析 YAML 获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("❌ 未找到蛋白质序列，跳过 MSA 生成", file=sys.stderr)
            return False
        
        print(f"🔍 找到 {len(protein_sequences)} 个蛋白质序列需要生成 MSA", file=sys.stderr)
        
        # 为每个蛋白质序列生成 MSA
        success_count = 0
        for protein_id, sequence in protein_sequences.items():
            print(f"🧬 正在为蛋白质 {protein_id} 生成 MSA...", file=sys.stderr)
            
            # 检查临时目录中是否已经存在
            output_path = os.path.join(temp_dir, f"{protein_id}_msa.a3m")
            if os.path.exists(output_path):
                print(f"✅ 临时目录中已存在 MSA 文件: {output_path}", file=sys.stderr)
                sanitize_a3m_file(output_path, context=f"{protein_id} 临时文件")
                success_count += 1
                continue
            
            # 检查缓存（统一使用 msa_ 前缀）
            sequence_hash = get_sequence_hash(sequence)
            cache_dir = MSA_CACHE_CONFIG['cache_dir']
            cached_msa_path = os.path.join(cache_dir, f"msa_{sequence_hash}.a3m")
            
            if MSA_CACHE_CONFIG['enable_cache'] and os.path.exists(cached_msa_path):
                print(f"✅ 找到缓存的 MSA 文件: {cached_msa_path}", file=sys.stderr)
                sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存原文件")
                # 复制到临时目录
                shutil.copy2(cached_msa_path, output_path)
                sanitize_a3m_file(output_path, context=f"{protein_id} 缓存复制")
                success_count += 1
                continue
            
            # 从服务器请求 MSA
            msa_result = request_msa_from_server(sequence)
            if msa_result:
                # 保存到临时目录
                if save_msa_result_to_file(msa_result, output_path):
                    sanitize_a3m_file(output_path, context=f"{protein_id} 下载写入")
                    success_count += 1
                    
                    # 缓存结果（统一使用 msa_ 前缀）
                    if MSA_CACHE_CONFIG['enable_cache']:
                        os.makedirs(cache_dir, exist_ok=True)
                        shutil.copy2(output_path, cached_msa_path)
                        sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存写入")
                        print(f"💾 MSA 结果已缓存: {cached_msa_path}", file=sys.stderr)
                else:
                    print(f"❌ 保存 MSA 文件失败: {protein_id}", file=sys.stderr)
            else:
                print(f"❌ 获取 MSA 失败: {protein_id}", file=sys.stderr)
        
        print(f"✅ MSA 生成完成: {success_count}/{len(protein_sequences)} 个成功", file=sys.stderr)
        return success_count > 0
        
    except Exception as e:
        print(f"❌ 生成 MSA 时出现错误: {e}", file=sys.stderr)
        return False

def cache_msa_files_from_temp_dir(temp_dir: str, yaml_content: str):
    """
    从临时目录中缓存生成的MSA文件
    支持从colabfold server生成的CSV格式MSA文件
    为每个蛋白质组分单独缓存MSA，适用于结构预测和分子设计
    """
    if not MSA_CACHE_CONFIG['enable_cache']:
        return
    
    try:
        # 解析YAML获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        # 提取所有蛋白质序列（支持结构预测和分子设计）
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("未找到蛋白质序列，跳过MSA缓存", file=sys.stderr)
            return
        
        print(f"需要缓存的蛋白质组分: {list(protein_sequences.keys())}", file=sys.stderr)
        
        # 设置缓存目录
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        os.makedirs(cache_dir, exist_ok=True)
        
        # 递归搜索临时目录中的MSA文件
        print(f"递归搜索临时目录中的MSA文件: {temp_dir}", file=sys.stderr)
        
        # 为每个蛋白质组分单独查找对应的MSA文件
        protein_msa_map = {}  # protein_id -> [msa_files]
        
        # 搜索所有MSA文件
        all_msa_files = []
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.csv') or file.endswith('.a3m'):
                    file_path = os.path.join(root, file)
                    all_msa_files.append(file_path)
        
        if not all_msa_files:
            print(f"在临时目录中未找到任何MSA文件: {temp_dir}", file=sys.stderr)
            return
        
        print(f"找到 {len(all_msa_files)} 个MSA文件: {[os.path.basename(f) for f in all_msa_files]}", file=sys.stderr)
        
        # 为每个蛋白质组分匹配对应的MSA文件
        for protein_id in protein_sequences.keys():
            protein_msa_map[protein_id] = []
            
            for msa_file in all_msa_files:
                filename = os.path.basename(msa_file)
                
                # 精确匹配：文件名包含protein ID
                if protein_id.lower() in filename.lower():
                    protein_msa_map[protein_id].append(msa_file)
                    continue
                    
                # 索引匹配：如果protein_id是字母，尝试匹配对应的数字索引
                # 例如：protein A -> _0.csv, protein B -> _1.csv
                if len(protein_id) == 1 and protein_id.isalpha():
                    protein_index = ord(protein_id.upper()) - ord('A')
                    if f"_{protein_index}." in filename:
                        protein_msa_map[protein_id].append(msa_file)
                        continue
                
                # 通用匹配：如果只有一个蛋白质组分，使用通用MSA文件
                if len(protein_sequences) == 1 and any(pattern in filename.lower() for pattern in ['msa', '_0.csv', '_0.a3m']):
                    protein_msa_map[protein_id].append(msa_file)
        
        # 处理每个蛋白质组分的MSA文件
        cached_count = 0
        for protein_id, msa_files in protein_msa_map.items():
            if not msa_files:
                print(f"❌ 蛋白质组分 {protein_id} 未找到对应的MSA文件", file=sys.stderr)
                continue
                
            print(f"🔍 处理蛋白质组分 {protein_id} 的 {len(msa_files)} 个MSA文件", file=sys.stderr)
            
            for msa_file in msa_files:
                if cache_single_protein_msa(protein_id, protein_sequences[protein_id], msa_file, cache_dir):
                    cached_count += 1
                    break  # 成功缓存一个就够了
        
        print(f"✅ MSA缓存完成，成功缓存 {cached_count}/{len(protein_sequences)} 个蛋白质组分", file=sys.stderr)
                
    except Exception as e:
        print(f"❌ 缓存MSA文件失败: {e}", file=sys.stderr)

def cache_single_protein_msa(protein_id: str, protein_sequence: str, msa_file: str, cache_dir: str) -> bool:
    """
    为单个蛋白质组分缓存MSA文件
    返回是否成功缓存
    """
    try:
        filename = os.path.basename(msa_file)
        file_ext = os.path.splitext(filename)[1].lower()
        
        print(f"  📂 处理MSA文件: {filename}", file=sys.stderr)
        
        if file_ext == '.csv':
            # 处理CSV格式的MSA文件（来自colabfold server）
            with open(msa_file, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header and len(header) >= 2 and 'sequence' in header:
                    sequences = []
                    for row in reader:
                        if len(row) >= 2 and row[1]:
                            sequences.append(row[1])
                    
                    if sequences:
                        # 第一个序列通常是查询序列
                        query_sequence = sequences[0]
                        print(f"    从CSV提取的查询序列: {query_sequence[:50]}...", file=sys.stderr)
                        
                        # 验证序列是否匹配
                        if is_sequence_match(protein_sequence, query_sequence):
                            # 转换CSV格式到A3M格式
                            a3m_content = f">{protein_id}\n{query_sequence}\n"
                            for i, seq in enumerate(sequences[1:], 1):
                                a3m_content += f">seq_{i}\n{seq}\n"
                            
                            # 缓存转换后的A3M文件
                            seq_hash = get_sequence_hash(protein_sequence)
                            cache_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
                            with open(cache_path, 'w') as cache_file:
                                cache_file.write(sanitize_a3m_content(a3m_content, context=f"{protein_id} CSV 转换"))
                            print(f"    ✅ 成功缓存蛋白质组分 {protein_id} 的MSA (从CSV转换): {cache_path}", file=sys.stderr)
                            print(f"       序列哈希: {seq_hash}", file=sys.stderr)
                            print(f"       MSA序列数: {len(sequences)}", file=sys.stderr)
                            return True
                        else:
                            print(f"    ❌ CSV文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
                            return False
        
        elif file_ext == '.a3m':
            # 处理A3M格式的MSA文件
            sanitize_a3m_file(msa_file, context=f"{protein_id} 源MSA")
            with open(msa_file, 'r') as f:
                msa_content = sanitize_a3m_content(f.read(), context=msa_file)
            
            # 从MSA内容中提取查询序列（第一个序列）
            lines = msa_content.strip().split('\n')
            if len(lines) >= 2 and lines[0].startswith('>'):
                query_sequence = lines[1]
                
                # 验证序列是否匹配
                if is_sequence_match(protein_sequence, query_sequence):
                    # 缓存MSA文件
                    seq_hash = get_sequence_hash(protein_sequence)
                    cache_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
                    with open(cache_path, 'w') as cache_file:
                        cache_file.write(msa_content)
                    print(f"    ✅ 成功缓存蛋白质组分 {protein_id} 的MSA: {cache_path}", file=sys.stderr)
                    print(f"       序列哈希: {seq_hash}", file=sys.stderr)
                    return True
                else:
                    print(f"    ❌ A3M文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
                    return False
        
        return False
        
    except Exception as e:
        print(f"    ❌ 处理蛋白质组分 {protein_id} 的MSA文件失败 {msa_file}: {e}", file=sys.stderr)
        return False

def is_sequence_match(protein_sequence: str, query_sequence: str) -> bool:
    """
    检查蛋白质序列和查询序列是否匹配
    支持完全匹配、容错匹配和相似度匹配
    """
    # 完全匹配
    if protein_sequence == query_sequence:
        return True
    
    # 容错匹配：去除空格和特殊字符后比较
    clean_protein = protein_sequence.replace('-', '').replace(' ', '').upper()
    clean_query = query_sequence.replace('-', '').replace(' ', '').upper()
    if clean_protein == clean_query:
        return True
    
    # 子序列匹配：查询序列可能是蛋白质序列的一部分
    if clean_query in clean_protein or clean_protein in clean_query:
        # 计算相似度
        similarity = len(set(clean_query) & set(clean_protein)) / max(len(clean_query), len(clean_protein))
        if similarity > 0.8:  # 80%相似度阈值
            return True
    
    return False

def find_results_dir(base_dir: str) -> str:
    result_path = None
    max_depth = -1
    for root, dirs, files in os.walk(base_dir):
        if any(f.endswith((".cif")) for f in files):
            depth = root.count(os.sep)
            if depth > max_depth:
                max_depth = depth
                result_path = root

    if result_path:
        print(f"Found results in directory: {result_path}", file=sys.stderr)
        return result_path

    raise FileNotFoundError(f"Could not find any directory containing result files within the base directory {base_dir}")

def get_cached_a3m_files(yaml_content: str) -> list:
    """
    获取与当前预测任务相关的a3m缓存文件
    返回缓存文件路径列表
    """
    cached_a3m_files = []
    
    if not MSA_CACHE_CONFIG['enable_cache']:
        return cached_a3m_files
    
    try:
        # 解析YAML获取蛋白质序列
        yaml_data = yaml.safe_load(yaml_content)
        protein_sequences = {}
        
        # 提取所有蛋白质序列
        for entity in yaml_data.get('sequences', []):
            if entity.get('protein', {}).get('id'):
                protein_id = entity['protein']['id']
                sequence = entity['protein'].get('sequence', '')
                if sequence:
                    protein_sequences[protein_id] = sequence
        
        if not protein_sequences:
            print("未找到蛋白质序列，跳过a3m文件收集", file=sys.stderr)
            return cached_a3m_files
        
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        if not os.path.exists(cache_dir):
            return cached_a3m_files
        
        print(f"查找缓存的a3m文件，蛋白质组分: {list(protein_sequences.keys())}", file=sys.stderr)
        
        # 为每个蛋白质序列查找对应的缓存文件
        for protein_id, sequence in protein_sequences.items():
            seq_hash = get_sequence_hash(sequence)
            cache_file_path = os.path.join(cache_dir, f"msa_{seq_hash}.a3m")
            
            if os.path.exists(cache_file_path):
                cached_a3m_files.append({
                    'path': cache_file_path,
                    'protein_id': protein_id,
                    'filename': f"{protein_id}_msa.a3m"
                })
                print(f"找到缓存文件: {protein_id} -> {cache_file_path}", file=sys.stderr)
        
        print(f"总共找到 {len(cached_a3m_files)} 个a3m缓存文件", file=sys.stderr)
        
    except Exception as e:
        print(f"获取a3m缓存文件失败: {e}", file=sys.stderr)
    
    return cached_a3m_files

def create_archive_with_a3m(output_archive_path: str, output_directory_path: str, yaml_content: str):
    """
    创建包含预测结果和a3m缓存文件的zip归档
    """
    try:
        # 获取相关的a3m缓存文件
        cached_a3m_files = get_cached_a3m_files(yaml_content)
        
        # 创建zip文件
        with zipfile.ZipFile(output_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 添加预测结果文件
            for root, dirs, files in os.walk(output_directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    # 计算相对路径，保持目录结构
                    arcname = os.path.relpath(file_path, output_directory_path)
                    zipf.write(file_path, arcname)
                    print(f"添加结果文件: {arcname}", file=sys.stderr)
            
            # 添加a3m缓存文件
            if cached_a3m_files:
                # 在zip中创建msa目录
                for a3m_info in cached_a3m_files:
                    cache_file_path = a3m_info['path']
                    filename = a3m_info['filename']
                    # 将a3m文件放在msa子目录中
                    arcname = f"msa/{filename}"
                    zipf.write(cache_file_path, arcname)
                    print(f"添加a3m缓存文件: {arcname}", file=sys.stderr)
                
                print(f"✅ 成功添加 {len(cached_a3m_files)} 个a3m缓存文件到zip归档", file=sys.stderr)
            else:
                print("⚠️ 未找到相关的a3m缓存文件", file=sys.stderr)
        
        print(f"✅ 归档创建完成: {output_archive_path}", file=sys.stderr)
        
    except Exception as e:
        print(f"❌ 创建包含a3m文件的归档失败: {e}", file=sys.stderr)
        # 如果失败，回退到原来的方式
        archive_base_name = output_archive_path.rsplit('.', 1)[0]
        created_archive_path = shutil.make_archive(
            base_name=archive_base_name,
            format='zip',
            root_dir=output_directory_path
        )
        print(f"回退到标准归档方式: {created_archive_path}", file=sys.stderr)


def create_af3_archive(
    output_archive_path: str,
    fasta_content: str,
    af3_json: dict,
    chain_msa_paths: dict,
    yaml_content: str,
    prep: AF3Preparation,
    af3_output_dir: Optional[str] = None,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
) -> None:
    """
    Create an archive containing AF3-compatible assets (FASTA, JSON, and MSAs).
    """
    try:
        with zipfile.ZipFile(output_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(f"af3/{prep.jobname}_input.fasta", fasta_content)
            zipf.writestr(f"af3/{prep.jobname}_input.json", serialize_af3_json(af3_json))
            zipf.writestr("af3/input.yaml", yaml_content)

            metadata = {
                "jobname": prep.jobname,
                "chain_labels": prep.header_labels,
                "sequence_cardinality": prep.query_sequences_cardinality,
                "chain_id_label_map": prep.chain_id_label_map,
            }
            zipf.writestr("af3/metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))

            if chain_msa_paths:
                for chain_id, path in chain_msa_paths.items():
                    if not path or not os.path.exists(path):
                        continue
                    arcname = f"af3/msa/{safe_filename(chain_id)}.a3m"
                    zipf.write(path, arcname)
                    print(f"添加AF3 MSA文件: {arcname}", file=sys.stderr)
            else:
                print("⚠️ 未找到AF3所需的MSA文件，JSON中将留空", file=sys.stderr)

            output_files_added = False
            if af3_output_dir and os.path.isdir(af3_output_dir):
                for root, _, files in os.walk(af3_output_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, af3_output_dir)
                        arcname = os.path.join("af3/output", arcname)
                        zipf.write(file_path, arcname)
                        print(f"添加AF3输出文件: {arcname}", file=sys.stderr)
                        output_files_added = True
            if not output_files_added:
                print("ℹ️ AF3输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            instructions = (
                "AlphaFold3 input assets generated by Boltz-WebUI.\n"
                "Files included:\n"
                " - af3_input.fasta / af3_input.json: ready for AlphaFold3 jobs\n"
                " - msa directory: cached MSAs per chain (if available)\n"
                " - input.yaml: original request payload\n"
                " - output/: files produced by AlphaFold3 (if the docker run succeeded)\n"
                "\n"
                "Upload the JSON file to AlphaFold3 alongside the FASTA sequence.\n"
            )
            zipf.writestr("af3/README.txt", instructions)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        print(f"⚠️ 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)

        print(f"✅ AF3 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as e:
        raise RuntimeError(f"Failed to create AF3 archive: {e}") from e


def run_boltz_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    predict_args: dict,
    model_name: Optional[str],
) -> None:
    tmp_yaml_path = os.path.join(temp_dir, 'data.yaml')
    with open(tmp_yaml_path, 'w') as tmp_yaml:
        tmp_yaml.write(yaml_content)

    cli_args = dict(predict_args)
    if model_name:
        cli_args['model'] = model_name
        print(f"DEBUG: Using model: {model_name}", file=sys.stderr)

    cli_args['data'] = tmp_yaml_path
    cli_args['out_dir'] = temp_dir

    if MSA_SERVER_URL and MSA_SERVER_URL != "":
        print(f"🧬 开始使用 MSA 服务器生成多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        msa_generated = generate_msa_for_sequences(yaml_content, temp_dir)
        if msa_generated:
            print(f"✅ MSA 生成成功，将用于结构预测", file=sys.stderr)
        else:
            print(f"⚠️ MSA 生成失败，将使用默认方法进行预测", file=sys.stderr)
    else:
        print(f"ℹ️ 未配置 MSA 服务器，跳过 MSA 生成", file=sys.stderr)

    POSITIONAL_KEYS = ['data']
    cmd_positional = []
    cmd_options = []

    for key, value in cli_args.items():
        if key in POSITIONAL_KEYS:
            cmd_positional.append(str(value))
        else:
            if value is None:
                continue
            if isinstance(value, bool):
                if value:
                    cmd_options.append(f'--{key}')
            else:
                cmd_options.append(f'--{key}')
                cmd_options.append(str(value))

    cmd_args = cmd_positional + cmd_options

    print(f"DEBUG: Invoking predict with args: {cmd_args}", file=sys.stderr)
    predict.main(args=cmd_args, standalone_mode=False)

    cache_msa_files_from_temp_dir(temp_dir, yaml_content)

    output_directory_path = find_results_dir(temp_dir)
    if not os.listdir(output_directory_path):
        raise NotADirectoryError(
            f"Prediction result directory was found but is empty: {output_directory_path}"
        )

    create_archive_with_a3m(output_archive_path, output_directory_path, yaml_content)


def run_alphafold3_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    use_msa_server: bool,
) -> None:
    print("🚀 Using AlphaFold3 backend (AF3 input preparation)", file=sys.stderr)

    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as err:
        print(f"⚠️ 无法解析 YAML，亲和力后处理将被跳过: {err}", file=sys.stderr)
        yaml_data = {}

    if use_msa_server and MSA_SERVER_URL and MSA_SERVER_URL != "":
        print(f"🧬 开始使用 MSA 服务器生成多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        msa_generated = generate_msa_for_sequences(yaml_content, temp_dir)
        if msa_generated:
            print(f"✅ MSA 生成成功，将用于AF3输入", file=sys.stderr)
        else:
            print(f"⚠️ 未能获取MSA，AF3 JSON将含空MSA字段", file=sys.stderr)
    else:
        print("ℹ️ 未配置 MSA 服务器或未请求使用，将尝试使用缓存的MSA", file=sys.stderr)

    prep = parse_yaml_for_af3(yaml_content)
    cache_dir = MSA_CACHE_CONFIG['cache_dir'] if MSA_CACHE_CONFIG['enable_cache'] else None
    chain_msa_paths = collect_chain_msa_paths(prep, temp_dir, cache_dir)
    unpaired_msa = load_unpaired_msa(prep, chain_msa_paths)
    fasta_content = build_af3_fasta(prep)
    af3_json = build_af3_json(prep, unpaired_msa)

    cache_msa_files_from_temp_dir(temp_dir, yaml_content)

    af3_input_dir = os.path.join(temp_dir, "af3_input")
    af3_output_dir = os.path.join(temp_dir, "af3_output")
    os.makedirs(af3_input_dir, exist_ok=True)
    os.makedirs(af3_output_dir, exist_ok=True)

    fasta_path = os.path.join(af3_input_dir, f"{prep.jobname}_input.fasta")
    json_path = os.path.join(af3_input_dir, "fold_input.json")

    with open(fasta_path, "w") as fasta_file:
        fasta_file.write(fasta_content)
    with open(json_path, "w") as json_file:
        json.dump(af3_json, json_file, indent=2, ensure_ascii=False)

    model_dir = ALPHAFOLD3_MODEL_DIR
    database_dir = ALPHAFOLD3_DATABASE_DIR
    image = ALPHAFOLD3_DOCKER_IMAGE or "alphafold3"
    raw_extra_args = shlex.split(ALPHAFOLD3_DOCKER_EXTRA_ARGS) if ALPHAFOLD3_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"⚠️ 已忽略部分 ALPHAFOLD3_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
            file=sys.stderr,
        )

    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("ALPHAFOLD3_MODEL_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")
    if not database_dir or not os.path.isdir(database_dir):
        raise FileNotFoundError("ALPHAFOLD3_DATABASE_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")

    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        gpu_arg = determine_docker_gpu_arg(visible_devices)
    except RuntimeError as gpu_err:
        print(f"❌ 无法准备 AlphaFold3 GPU 环境: {gpu_err}", file=sys.stderr)
        print("   ↳ 请确认此主机安装了 NVIDIA 驱动并正确设置 CUDA_VISIBLE_DEVICES。", file=sys.stderr)
        raise

    container_input_dir = "/workspace/af_input"
    container_output_dir = "/workspace/af_output"
    container_model_dir = "/workspace/models"
    container_database_dir = "/workspace/public_databases"
    container_colabfold_jobs_dir = "/app/jobs"

    runtime_overridden = any(token == "--runtime" for token in extra_args)

    docker_command = [
        "docker",
        "run",
        "--rm",
    ]

    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--volume",
            f"{af3_input_dir}:{container_input_dir}",
            "--volume",
            f"{af3_output_dir}:{container_output_dir}",
            "--volume",
            f"{model_dir}:{container_model_dir}",
            "--volume",
            f"{database_dir}:{container_database_dir}",
        ]
    )

    # 添加 ColabFold jobs 目录挂载（如果配置了 MSA 服务器）
    if MSA_SERVER_URL and COLABFOLD_JOBS_DIR and os.path.exists(COLABFOLD_JOBS_DIR):
        docker_command.extend([
            "--volume",
            f"{COLABFOLD_JOBS_DIR}:{container_colabfold_jobs_dir}",
        ])
        print(f"🔗 挂载 ColabFold jobs 目录: {COLABFOLD_JOBS_DIR} -> {container_colabfold_jobs_dir}", file=sys.stderr)
    else:
        print("⚠️ 未找到 ColabFold jobs 目录或未配置 MSA 服务器", file=sys.stderr)

    host_uid = os.getuid()
    host_gid = os.getgid()
    docker_command += [
        "--user",
        f"{host_uid}:{host_gid}",
    ]

    gpu_device_groups = collect_gpu_device_group_ids()
    if not gpu_device_groups:
        print("⚠️ 未能检测到 GPU 设备的所属用户组，容器可能无法访问 GPU。", file=sys.stderr)
    else:
        for gid in gpu_device_groups:
            docker_command.extend(["--group-add", str(gid)])
        print(
            f"🔐 为容器添加 GPU 相关用户组: {', '.join(str(g) for g in gpu_device_groups)}",
            file=sys.stderr,
        )

    docker_command.extend(extra_args)

    docker_command.append(image)
    docker_command.extend(
        [
            "python",
            "run_alphafold.py",
            f"--json_path={container_input_dir}/fold_input.json",
            f"--model_dir={container_model_dir}",
            f"--output_dir={container_output_dir}",
            f"--db_dir={container_database_dir}",
        ]
    )

    display_command = " ".join(shlex.quote(part) for part in docker_command)
    print(f"🐳 运行 AlphaFold3 Docker: {display_command}", file=sys.stderr)
    docker_proc = subprocess.run(
        docker_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if docker_proc.returncode != 0:
        print(f"❌ AlphaFold3 Docker 运行失败: {docker_proc.stderr}", file=sys.stderr)
        raise RuntimeError(
            f"AlphaFold3 Docker run failed with exit code {docker_proc.returncode}. "
            f"Stdout: {docker_proc.stdout}\nStderr: {docker_proc.stderr}"
        )

    print(f"✅ AlphaFold3 Docker 运行完成: {docker_proc.stdout}", file=sys.stderr)

    af3_output_contents = list(Path(af3_output_dir).rglob("*"))
    if not any(p.is_file() for p in af3_output_contents):
        print("⚠️ AlphaFold3 输出目录为空，可能推理未产生结果。", file=sys.stderr)

    extra_archive_files = run_af3_affinity_pipeline(
        temp_dir=temp_dir,
        yaml_data=yaml_data,
        prep=prep,
        af3_output_dir=af3_output_dir,
    )

    create_af3_archive(
        output_archive_path,
        fasta_content,
        af3_json,
        chain_msa_paths,
        yaml_content,
        prep,
        af3_output_dir=af3_output_dir,
        extra_files=extra_archive_files,
    )

def main():
    """
    Main function to run a single prediction based on arguments provided in a JSON file.
    The JSON file should contain the necessary parameters for the prediction, including:
    - output_archive_path: Path where the output archive will be saved.
    - yaml_content: YAML content as a string that will be written to a temporary file.
    - Other parameters that will be passed to the predict function as command-line arguments.
    """
    if len(sys.argv) != 2:
        print("Usage: python run_single_prediction.py <args_file_path>")
        sys.exit(1)

    args_file_path = sys.argv[1]

    try:
        with open(args_file_path, 'r') as f:
            predict_args = json.load(f)

        output_archive_path = predict_args.pop("output_archive_path")
        yaml_content = predict_args.pop("yaml_content")
        backend = str(predict_args.pop("backend", "boltz")).strip().lower()
        if backend not in ("boltz", "alphafold3"):
            raise ValueError(f"Unsupported backend '{backend}'.")

        model_name = predict_args.pop("model_name", None)

        use_msa_server = predict_args.get("use_msa_server", False)

        with tempfile.TemporaryDirectory() as temp_dir:
            if backend == "alphafold3":
                run_alphafold3_backend(temp_dir, yaml_content, output_archive_path, use_msa_server)
            else:
                run_boltz_backend(temp_dir, yaml_content, output_archive_path, predict_args, model_name)

            if not os.path.exists(output_archive_path):
                raise FileNotFoundError(
                    f"CRITICAL ERROR: Archive not found at {output_archive_path} immediately after creation."
                )

            print(f"DEBUG: Archive successfully created at: {output_archive_path}", file=sys.stderr)

    except Exception as e:
        print(f"Error during prediction subprocess: {e}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
