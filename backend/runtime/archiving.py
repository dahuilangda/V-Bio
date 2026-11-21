"""结果归档：各引擎输出树的 zip 打包与 MSA 缓存读取。"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from backend.runtime.af3_adapter import AF3Preparation, safe_filename, serialize_af3_json
from backend.runtime.msa_pipeline import MSA_CACHE_CONFIG, get_sequence_hash
from backend.runtime.protenix_adapter import serialize_protenix_json
from backend.services.common_utils import ProteinMsaMode, extract_protein_msa_policies


def assert_boltz_preprocessing_succeeded(base_dir: str, yaml_content: str) -> None:
    manifest_path = Path(base_dir) / "processed" / "manifest.json"
    if not manifest_path.exists():
        return

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return

    records = manifest_data.get("records") if isinstance(manifest_data, dict) else None
    if isinstance(records, list) and records:
        return

    template_hint = ""
    try:
        yaml_data = yaml.safe_load(yaml_content) or {}
        if yaml_data.get("templates"):
            template_hint = (
                " 检测到 templates 输入，模板可能包含不受支持的 CCD 组分。"
                "请移除该模板，或替换为标准氨基酸残基模板后重试。"
            )
    except Exception as yaml_exc:
        print(f"[WARN] boltz 输入 yaml 无法解析，无法给出模板提示: {yaml_exc}", file=sys.stderr)

    raise RuntimeError(
        "Boltz 输入预处理失败：没有生成任何有效记录，任务无法继续。"
        + template_hint
    )




def get_cached_a3m_files(yaml_content: str) -> list:
    """Collect sequence-addressed A3M cache files for declared MSA inputs."""
    cached_a3m_files = []

    if not MSA_CACHE_CONFIG['enable_cache']:
        return cached_a3m_files

    try:
        protein_sequences: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is ProteinMsaMode.DISABLED:
                continue
            for chain_id in policy.chain_ids:
                if policy.sequence:
                    protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("未找到蛋白质序列，跳过a3m文件收集", file=sys.stderr)
            return cached_a3m_files

        cache_dir = Path(MSA_CACHE_CONFIG['cache_dir'])
        if not cache_dir.is_dir():
            return cached_a3m_files

        print(f"查找缓存的a3m文件，蛋白质组分: {list(protein_sequences)}", file=sys.stderr)
        for protein_id, sequence in protein_sequences.items():
            seq_hash = get_sequence_hash(sequence)
            cache_file_path = cache_dir / f"msa_{seq_hash}.a3m"

            if cache_file_path.is_file():
                cached_a3m_files.append({
                    'path': str(cache_file_path),
                    'protein_id': protein_id,
                    'filename': f"{safe_filename(protein_id)}_msa.a3m",
                })
                print(f"找到缓存文件: {protein_id} -> {cache_file_path}", file=sys.stderr)

        print(f"总共找到 {len(cached_a3m_files)} 个a3m缓存文件", file=sys.stderr)

    except Exception as e:
        print(f"获取a3m缓存文件失败: {e}", file=sys.stderr)

    return cached_a3m_files



def create_archive_with_a3m(
    output_archive_path: str,
    output_directory_path: str,
    yaml_content: str,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
):
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
                
                print(f"成功添加 {len(cached_a3m_files)} 个a3m缓存文件到zip归档", file=sys.stderr)
            else:
                print("[WARN] 未找到相关的a3m缓存文件", file=sys.stderr)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        print(f"[WARN] 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)
        
        print(f"归档创建完成: {output_archive_path}", file=sys.stderr)
        
    except Exception:
        # A silent fallback here would produce a DIFFERENT archive (no MSA
        # files) at the same path — the caller could not tell. Fail loudly.
        print(f"[ERROR] 创建包含a3m文件的归档失败:", file=sys.stderr)
        raise






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
                print("[WARN] 未找到AF3所需的MSA文件，JSON中将留空", file=sys.stderr)

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
                print("AF3输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            instructions = (
                "AlphaFold3 input assets generated by V-Bio.\n"
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
                        print(f"[WARN] 额外文件不存在，跳过添加: {file_path}", file=sys.stderr)
                        continue
                    zipf.write(str(file_path), arcname)
                    print(f"添加额外文件: {arcname}", file=sys.stderr)

        print(f"AF3 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as e:
        raise RuntimeError(f"Failed to create AF3 archive: {e}") from e




def create_protenix_archive(
    output_archive_path: str,
    protenix_json: Any,
    yaml_content: str,
    input_name: str,
    chain_msa_paths: Dict[str, str],
    protenix_output_dir: Optional[str] = None,
    extra_files: Optional[List[Tuple[Path, str]]] = None,
) -> None:
    """
    Create an archive containing Protenix-compatible assets and outputs.
    """
    try:
        with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr(f"protenix/{input_name}.json", serialize_protenix_json(protenix_json))
            zipf.writestr("protenix/input.yaml", yaml_content)

            if chain_msa_paths:
                for chain_id, path in chain_msa_paths.items():
                    path_obj = Path(path)
                    if not path_obj.exists():
                        continue
                    arcname = f"protenix/msa/{safe_filename(chain_id)}.a3m"
                    zipf.write(str(path_obj), arcname)
                    print(f"添加 Protenix MSA 文件: {arcname}", file=sys.stderr)

            output_files_added = False
            if protenix_output_dir and os.path.isdir(protenix_output_dir):
                for root, _, files in os.walk(protenix_output_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, protenix_output_dir)
                        arcname = os.path.join("protenix/output", arcname)
                        zipf.write(file_path, arcname)
                        output_files_added = True
                        print(f"添加 Protenix 输出文件: {arcname}", file=sys.stderr)

            if not output_files_added:
                print("Protenix 输出目录为空或缺失，仅保留输入文件", file=sys.stderr)

            readme = (
                "Protenix input assets generated by V-Bio.\n"
                "Files included:\n"
                f" - {input_name}.json: Protenix input JSON\n"
                " - input.yaml: original request payload\n"
                " - msa/: external MSA files used by protein entities (if available)\n"
                " - output/: files produced by Protenix docker run (if succeeded)\n"
            )
            zipf.writestr("protenix/README.txt", readme)

            if extra_files:
                for file_path, arcname in extra_files:
                    if not file_path or not Path(file_path).exists():
                        continue
                    zipf.write(str(file_path), arcname)

        print(f"Protenix 归档创建完成: {output_archive_path}", file=sys.stderr)
    except Exception as exc:
        raise RuntimeError(f"Failed to create Protenix archive: {exc}") from exc



