"""MSA 请求与缓存管线：ColabFold 服务器请求、ticket 管理、a3m 缓存。

原 run_single_prediction.py 的 MSA 段整体迁入；由各后端与调度
经 `from backend.runtime.msa_pipeline import ...` 使用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

from backend.core.config import (
    MSA_SERVER_TIMEOUT_SECONDS,
    MSA_SERVER_URL,
)
from backend.runtime.af3_adapter import safe_filename
from backend.services.common_utils import (
    ProteinMsaMode,
    extract_protein_msa_policies,
)

# MSA 缓存配置（目录可通过 VBIO_MSA_CACHE_DIR 覆盖；默认指向 /data 挂载，
# 避免缓存放大容器可写层导致宿主机根分区膨胀）
MSA_CACHE_CONFIG = {
    'cache_dir': os.environ.get('VBIO_MSA_CACHE_DIR', '/data/boltz_msa_cache'),
    'enable_cache': True,
}


def _get_redis_client_optional():
    try:
        from gpu_manager import get_redis_client as get_redis_client_fn
        return get_redis_client_fn()
    except Exception:
        return None


def get_sequence_hash(sequence: str) -> str:
    """计算序列的MD5哈希值作为缓存键"""
    return hashlib.md5(sequence.encode('utf-8')).hexdigest()

def sanitize_a3m_content(content: str, context: str = "") -> str:
    """
    移除 A3M 内容中的非法控制字符（例如 \\x00）。
    """
    sanitized = content.replace("\x00", "")
    if sanitized != content:
        msg_context = f" ({context})" if context else ""
        print(f"[WARN] 检测到并移除非法字符\\x00{msg_context}", file=sys.stderr)
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
        print(f"[WARN] 无法读取 A3M 文件进行清理: {path}, {e}", file=sys.stderr)
        return

    sanitized = sanitize_a3m_content(content, context=context or path)
    if sanitized != content:
        try:
            with open(path, "w") as f:
                f.write(sanitized)
        except OSError as e:
            print(f"[WARN] 无法写入清理后的 A3M 文件: {path}, {e}", file=sys.stderr)




def _merge_a3m_texts(first: str, second: str) -> str:
    """Concatenate two a3m texts, dropping duplicate aligned sequences.

    The query (first entry of `first`) is kept exactly once; the second
    file's query row and any sequence already present are skipped."""
    def _entries(text):
        header, block = None, []
        for line in text.splitlines():
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(block)
                header, block = line, []
            elif header is not None:
                block.append(line)
        if header is not None:
            yield header, "".join(block)

    out, seen = [], set()
    for header, seq in list(_entries(first)) + list(_entries(second)):
        if seq in seen:
            continue
        seen.add(seq)
        out.append((header, seq))
    return "".join(f"{h}\n{s}\n" for h, s in out)


def _cancel_msa_ticket(ticket_id: str) -> None:
    """Best-effort DELETE of an MSA ticket: the patched colabfold server kills the
    search's processes and frees its GPU slot. Never raises — a plain (unpatched)
    server answers 405 and we simply keep the old abandon behaviour."""
    try:
        requests.delete(f"{MSA_SERVER_URL}/ticket/{ticket_id}", timeout=15)
    except requests.RequestException:
        pass


def _msa_search_concurrency() -> int:
    """Concurrent MSA searches against the shared colabfold server. Policy: one
    search per GPU (each envdb search pins a full card); default 4, override
    with VBIO_MSA_SEARCH_CONCURRENCY."""
    try:
        value = int(os.environ.get("VBIO_MSA_SEARCH_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(8, value))


def _register_active_msa_ticket(ticket_id: str) -> None:
    """把进行中的 MSA ticket 归档到任务名下,任务取消时由 terminate 链路撤销。

    以 SET 存储(一个任务可能并发请求多条序列的 MSA);无 VBIO_TASK_ID 环境
    (非 worker 子进程上下文)时跳过登记。
    """
    task_id = str(os.environ.get("VBIO_TASK_ID") or "").strip()
    if not task_id or not str(ticket_id or "").strip():
        return
    try:
        client = _get_redis_client_optional()
        if client is None:
            return
        key = f"msa_tickets:{task_id}"
        client.sadd(key, str(ticket_id))
        client.expire(key, 7 * 24 * 3600)
    except Exception as exc:
        print(f"[WARN] 登记 MSA ticket 失败: {exc}", file=sys.stderr)


def _release_active_msa_ticket(ticket_id: str) -> None:
    task_id = str(os.environ.get("VBIO_TASK_ID") or "").strip()
    if not task_id or not str(ticket_id or "").strip():
        return
    try:
        client = _get_redis_client_optional()
        if client is None:
            return
        client.srem(f"msa_tickets:{task_id}", str(ticket_id))
    except Exception:
        pass


def request_msa_from_server(
    sequence: str, timeout: Optional[int] = None, msa_mode: str = "auto",
) -> Optional[dict]:
    """
    从 ColabFold MSA 服务器请求多序列比对
    
    Args:
        sequence: 蛋白质序列（FASTA 格式）
        timeout: 请求超时时间（秒）
    
    Returns:
        包含 MSA 结果的字典，如果失败则返回 None
    """
    ticket_id = ""
    try:
        effective_timeout = timeout if timeout and timeout > 0 else MSA_SERVER_TIMEOUT_SECONDS
        print(f"正在从 MSA 服务器请求多序列比对: {MSA_SERVER_URL}", file=sys.stderr)
        
        # 准备请求数据
        # 确保序列是 FASTA 格式
        if not sequence.startswith('>'):
            sequence = f">query\n{sequence}"
        
        # ColabFold MSA 服务器使用 form data 格式。
        # mode=env 启用宏基因组库搜索并在结果包里附带
        # bfd.mgnify30.metaeuk30.smag30.a3m；只取第一个 a3m（uniref）会
        # 丢掉整个宏基因组深度，下面下载时合并两份。
        # 智能分层(auto): 长链(>=50aa, 蛋白)用 env — 良性成本;短链(肽)用
        # uniref — env 的 CPU result2msa 阶段对短查询爆炸(实测 20aa 设计肽
        # 30-60min vs 22aa 蛋白样序列 20s)。显式 env/uniref 可覆盖。
        if msa_mode == "auto":
            normalized = "".join(
                aa if aa in "ACDEFGHIKLMNPQRSTVWY" else "A"
                for aa in str(sequence or "").strip().upper())
            msa_mode = "env" if len(normalized) >= 50 else "uniref"
        payload = {
            "q": sequence,
            "mode": msa_mode
        }
        print(f"MSA 请求参数: mode={msa_mode}", file=sys.stderr)
        
        # 提交搜索任务
        submit_url = f"{MSA_SERVER_URL}/ticket/msa"
        print(f"提交 MSA 搜索任务到: {submit_url}", file=sys.stderr)
        
        response = requests.post(submit_url, data=payload, timeout=30)
        if response.status_code != 200:
            print(f"[ERROR] MSA 任务提交失败: {response.status_code} - {response.text}", file=sys.stderr)
            return None
        
        result = response.json()
        ticket_id = str(result.get("id") or "")
        if not ticket_id:
            print(f"[ERROR] 未获取到有效的任务 ID: {result}", file=sys.stderr)
            return None
        
        print(f"MSA 任务已提交，任务 ID: {ticket_id}", file=sys.stderr)
        _register_active_msa_ticket(ticket_id)

        # 轮询结果
        result_url = f"{MSA_SERVER_URL}/ticket/{ticket_id}"
        start_time = time.time()
        
        while time.time() - start_time < effective_timeout:
            try:
                print(f"检查 MSA 任务状态...", file=sys.stderr)
                response = requests.get(result_url, timeout=30)
                
                if response.status_code == 200:
                    result_data = response.json()
                    if result_data.get("status") == "COMPLETE":
                        _release_active_msa_ticket(ticket_id)
                        print(f"MSA 搜索完成，获取到结果", file=sys.stderr)
                        download_url = result_data.get("result_url") or f"{MSA_SERVER_URL}/result/download/{ticket_id}"
                        print(f"下载 MSA 结果: {download_url}", file=sys.stderr)
                        try:
                            download_response = requests.get(download_url, timeout=60)
                        except requests.exceptions.RequestException as download_error:
                            print(f"[ERROR] 下载 MSA 结果请求失败: {download_error}", file=sys.stderr)
                            return None
                        if download_response.status_code != 200:
                            print(
                                f"[ERROR] 下载 MSA 结果失败: {download_response.status_code} - {download_response.text}",
                                file=sys.stderr,
                            )
                            return None

                        try:
                            tar_bytes = io.BytesIO(download_response.content)
                            with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
                                a3m_content = None
                                env_content = None
                                extracted_filename = None
                                for member in tar.getmembers():
                                    name_lower = member.name.lower()
                                    if not name_lower.endswith(".a3m"):
                                        continue
                                    file_obj = tar.extractfile(member)
                                    if not file_obj:
                                        continue
                                    if "mgnify" in name_lower:
                                        env_content = file_obj.read().decode("utf-8")
                                        continue
                                    if a3m_content is None:
                                        a3m_content = file_obj.read().decode("utf-8")
                                        extracted_filename = member.name
                                if a3m_content and env_content:
                                    a3m_content = _merge_a3m_texts(
                                        a3m_content, env_content)
                                    extracted_filename = (
                                        f"{extracted_filename}+mgnify(merged)")

                            if not a3m_content:
                                print("[ERROR] 未在下载的结果中找到 A3M 文件", file=sys.stderr)
                                return None

                            print(f"成功提取 A3M 文件: {extracted_filename}", file=sys.stderr)
                            a3m_content = sanitize_a3m_content(a3m_content, context=extracted_filename)
                            # merged rows with mismatched match-column counts
                            # crash downstream featurizers — validate now
                            query_line = ""
                            for ln in a3m_content.splitlines():
                                if ln and not ln.startswith(">"):
                                    query_line = ln
                                    break
                            query_len = sum(1 for c in query_line if not c.islower())
                            if query_len:
                                a3m_content = _drop_malformed_rows_len(a3m_content, query_len)
                            entries = parse_a3m_content(a3m_content)
                            return {
                                "entries": entries,
                                "a3m_content": a3m_content,
                                "source": extracted_filename,
                                "ticket_id": ticket_id,
                            }
                        except tarfile.TarError as tar_error:
                            print(f"[ERROR] 解析 MSA 压缩包失败: {tar_error}", file=sys.stderr)
                            return None
                    elif result_data.get("status") == "ERROR":
                        _release_active_msa_ticket(ticket_id)
                        print(f"[ERROR] MSA 搜索失败: {result_data.get('error', '未知错误')}", file=sys.stderr)
                        print(
                            f"   ↳ 服务器返回: {json.dumps(result_data, ensure_ascii=False)}",
                            file=sys.stderr,
                        )
                        return None
                    else:
                        print(
                            f"MSA 任务状态: {result_data.get('status', 'PENDING')} (已等待 {int(time.time() - start_time)}s)",
                            file=sys.stderr,
                        )
                elif response.status_code == 404:
                    print(f"任务尚未完成或不存在", file=sys.stderr)
                else:
                    print(f"[WARN] 检查状态时出现错误: {response.status_code}", file=sys.stderr)
                
            except requests.exceptions.RequestException as e:
                print(f"[WARN] 检查状态时网络错误: {e}", file=sys.stderr)
            
            # 等待一段时间再次检查
            time.sleep(10)
        
        _release_active_msa_ticket(ticket_id)
        print(f"MSA 搜索超时 ({effective_timeout}秒)，取消服务器任务以释放 GPU", file=sys.stderr)
        _cancel_msa_ticket(ticket_id)
        return None
        
    except Exception as e:
        print(f"[ERROR] MSA 服务器请求失败: {e}", file=sys.stderr)
        if ticket_id:
            # 外层异常(如结果解压/清洗抛错):ticket 已终态则注销,仍在跑则撤销,
            # 不留"已登记但无人管"的搜索。
            _cancel_msa_ticket(ticket_id)
            _release_active_msa_ticket(ticket_id)
        return None

def _write_a3m_atomic(output_path: str, content: str) -> None:
    tmp_path = f"{output_path}.tmp.{os.getpid()}"
    with open(tmp_path, 'w') as f:
        f.write(content)
    os.replace(tmp_path, output_path)


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
        # 根据结果格式保存为 A3M 文件（tmp + os.replace 原子落盘，
        # 避免并发读者读到半截缓存）
        if msa_result.get('a3m_content'):
            sanitized_content = sanitize_a3m_content(msa_result['a3m_content'], context=output_path)
            _write_a3m_atomic(output_path, sanitized_content)
            return True
        elif 'entries' in msa_result:
            buffer = []
            for entry in msa_result['entries']:
                name = entry.get('name', 'unknown')
                sequence = entry.get('sequence', '')
                if sequence:
                    buffer.append(f">{name}\n{sequence}\n")

            sanitized_content = sanitize_a3m_content(''.join(buffer), context=output_path)
            _write_a3m_atomic(output_path, sanitized_content)
            return True
        else:
            print(f"[ERROR] MSA 结果格式不支持: {msa_result.keys()}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"[ERROR] 保存 MSA 结果失败: {e}", file=sys.stderr)
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
def generate_msa_for_sequences(yaml_content: str, temp_dir: str, msa_mode: str = "auto") -> bool:
    """
    为 YAML 中的蛋白质序列生成 MSA

    Args:
        yaml_content: YAML 配置内容
        temp_dir: 临时目录

    Returns:
        是否成功生成 MSA
    """
    try:
        print(f"开始为蛋白质序列生成 MSA", file=sys.stderr)

        protein_sequences: Dict[str, str] = {}
        output_names: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is not ProteinMsaMode.EXTERNAL:
                continue
            if not policy.sequence:
                raise ValueError("External MSA generation requires a protein sequence.")
            if not policy.chain_ids:
                raise ValueError("External MSA generation requires explicit protein chain IDs.")
            for chain_id in policy.chain_ids:
                output_name = f"{safe_filename(chain_id)}_msa.a3m"
                previous_chain = output_names.get(output_name)
                if previous_chain is not None and previous_chain != chain_id:
                    raise ValueError(
                        f"Protein chain IDs resolve to the same MSA file: {previous_chain}, {chain_id}"
                    )
                previous_sequence = protein_sequences.get(chain_id)
                if previous_sequence is not None and previous_sequence != policy.sequence:
                    raise ValueError(f"Protein chain ID is assigned more than one sequence: {chain_id}")
                output_names[output_name] = chain_id
                protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("没有需要外部 MSA 的蛋白质序列，跳过 MSA 生成", file=sys.stderr)
            return True

        msa_timeout = MSA_SERVER_TIMEOUT_SECONDS if MSA_SERVER_TIMEOUT_SECONDS > 0 else 600
        print(f"找到 {len(protein_sequences)} 个蛋白质序列需要生成 MSA", file=sys.stderr)
        print(f"当前 MSA 超时配置: {msa_timeout} 秒", file=sys.stderr)

        # 为每个蛋白质序列生成 MSA
        success_count = 0
        for protein_id, sequence in protein_sequences.items():
            print(f"正在为蛋白质 {protein_id} 生成 MSA...", file=sys.stderr)

            output_path = os.path.join(temp_dir, f"{safe_filename(protein_id)}_msa.a3m")
            if os.path.exists(output_path):
                if _ensure_nonempty_a3m_file(
                    output_path,
                    sequence,
                    context=f"{protein_id} 临时文件",
                    header=protein_id,
                ):
                    print(f"临时目录中已存在可用 MSA 文件: {output_path}", file=sys.stderr)
                    success_count += 1
                    continue
                print(f"[WARN] 临时目录中的 MSA 文件不可用，准备重新生成: {output_path}", file=sys.stderr)

            # 缓存键与 af3 / boltz2score / protenix2dock 共用
            sequence_hash = get_sequence_hash(sequence)
            cache_dir = MSA_CACHE_CONFIG['cache_dir']
            cached_msa_path = os.path.join(cache_dir, f"msa_{sequence_hash}.a3m")

            if MSA_CACHE_CONFIG['enable_cache'] and os.path.exists(cached_msa_path):
                print(f"找到缓存的 MSA 文件: {cached_msa_path}", file=sys.stderr)
                sanitize_a3m_file(cached_msa_path, context=f"{protein_id} 缓存原文件")
                shutil.copy2(cached_msa_path, output_path)
                if _ensure_nonempty_a3m_file(
                    output_path,
                    sequence,
                    context=f"{protein_id} 缓存复制",
                    header=protein_id,
                ):
                    success_count += 1
                    continue
                print(f"[WARN] 缓存中的 MSA 文件为空，准备重新生成: {cached_msa_path}", file=sys.stderr)

            # 从服务器请求 MSA
            msa_result = request_msa_from_server(sequence, timeout=msa_timeout, msa_mode=msa_mode)
            if msa_result:
                if save_msa_result_to_file(msa_result, output_path):
                    if _ensure_nonempty_a3m_file(
                        output_path,
                        sequence,
                        context=f"{protein_id} 下载写入",
                        header=protein_id,
                    ):
                        success_count += 1

                        if MSA_CACHE_CONFIG['enable_cache']:
                            os.makedirs(cache_dir, exist_ok=True)
                            shutil.copy2(output_path, cached_msa_path)
                            _ensure_nonempty_a3m_file(
                                cached_msa_path,
                                sequence,
                                context=f"{protein_id} 缓存写入",
                                header=protein_id,
                            )
                            print(f"MSA 结果已缓存: {cached_msa_path}", file=sys.stderr)
                    else:
                        print(f"[ERROR] 保存后的 MSA 文件仍不可用: {protein_id}", file=sys.stderr)
                else:
                    print(f"[ERROR] 保存 MSA 文件失败: {protein_id}", file=sys.stderr)
            else:
                print(f"[ERROR] 获取 MSA 失败: {protein_id}", file=sys.stderr)

        total_sequences = len(protein_sequences)
        print(f"MSA 生成完成: {success_count}/{total_sequences} 个成功", file=sys.stderr)
        if success_count != total_sequences:
            print("[ERROR] MSA 生成不完整：必须为所有蛋白序列生成 MSA。", file=sys.stderr)
            return False
        return True

    except Exception as e:
        print(f"[ERROR] 生成 MSA 时出现错误: {e}", file=sys.stderr)
        return False


def _require_complete_external_msa(yaml_content: str, temp_dir: str, backend_label: str, msa_mode: str = "auto") -> None:
    ok = generate_msa_for_sequences(yaml_content, temp_dir, msa_mode=msa_mode)
    if not ok:
        raise RuntimeError(
            f"{backend_label} requires ColabFold MSA for every protein sequence, but MSA generation was incomplete."
        )


def _inject_local_msa_paths_into_yaml(yaml_content: str, temp_dir: str) -> Tuple[str, int]:
    yaml_data = yaml.safe_load(yaml_content) or {}
    if not isinstance(yaml_data, dict):
        raise ValueError("MSA path injection expects a YAML mapping at the top level.")

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        return yaml_content, 0

    injected = 0
    for entity in sequences:
        if not isinstance(entity, dict):
            continue
        protein = entity.get("protein")
        if not isinstance(protein, dict):
            continue
        current_msa = protein.get("msa")
        if is_msa_disabled(current_msa):
            continue
        if isinstance(current_msa, str) and current_msa.strip() and current_msa.strip() not in {"0", "empty"}:
            continue
        ids = protein.get("id")
        if isinstance(ids, list):
            chain_ids = [str(item or "").strip() for item in ids if str(item or "").strip()]
        else:
            chain_ids = [str(ids or "").strip()] if str(ids or "").strip() else []
        if not chain_ids:
            continue

        selected_path: Optional[Path] = None
        for chain_id in chain_ids:
            candidate_path = Path(temp_dir) / f"{safe_filename(chain_id)}_msa.a3m"
            if not candidate_path.is_file():
                continue
            if not _ensure_nonempty_a3m_file(
                str(candidate_path),
                protein.get("sequence", ""),
                context=f"{chain_id} 注入校验",
                header=chain_id,
            ):
                continue
            selected_path = candidate_path
            if selected_path:
                break
        if not selected_path:
            continue
        protein["msa"] = str(selected_path)
        injected += 1

    if injected <= 0:
        return yaml_content, 0
    return yaml.safe_dump(yaml_data, sort_keys=False, default_flow_style=False), injected


def cache_msa_files_from_temp_dir(temp_dir: str, yaml_content: str):
    """Cache the declared external A3M output for each protein chain."""
    if not MSA_CACHE_CONFIG['enable_cache']:
        return

    try:
        protein_sequences: Dict[str, str] = {}
        for policy in extract_protein_msa_policies(yaml_content):
            if policy.mode is not ProteinMsaMode.EXTERNAL:
                continue
            if not policy.sequence or not policy.chain_ids:
                raise ValueError("External MSA caching requires protein sequences and chain IDs.")
            for chain_id in policy.chain_ids:
                previous_sequence = protein_sequences.get(chain_id)
                if previous_sequence is not None and previous_sequence != policy.sequence:
                    raise ValueError(f"Protein chain ID is assigned more than one sequence: {chain_id}")
                protein_sequences[chain_id] = policy.sequence

        if not protein_sequences:
            print("没有需要缓存的外部 MSA，跳过缓存", file=sys.stderr)
            return

        print(f"需要缓存的蛋白质组分: {list(protein_sequences)}", file=sys.stderr)
        cache_dir = MSA_CACHE_CONFIG['cache_dir']
        os.makedirs(cache_dir, exist_ok=True)

        cached_count = 0
        for protein_id, sequence in protein_sequences.items():
            msa_path = Path(temp_dir) / f"{safe_filename(protein_id)}_msa.a3m"
            if not msa_path.is_file():
                print(f"[ERROR] 蛋白质组分 {protein_id} 缺少声明的 A3M 文件", file=sys.stderr)
                continue
            if cache_single_protein_msa(protein_id, sequence, str(msa_path), cache_dir):
                cached_count += 1

        print(f"MSA缓存完成，成功缓存 {cached_count}/{len(protein_sequences)} 个蛋白质组分", file=sys.stderr)

    except Exception as e:
        print(f"[ERROR] 缓存MSA文件失败: {e}", file=sys.stderr)

def _drop_malformed_rows_len(a3m_text: str, query_len: int) -> str:
    out, dropped, header = [], 0, None
    for line in a3m_text.splitlines():
        if line.startswith(">"):
            header = line
        elif header is not None:
            if sum(1 for c in line if not c.islower()) == query_len:
                out.append(header)
                out.append(line)
            else:
                dropped += 1
            header = None
    if dropped:
        print(f"    [MSA] 丢弃 {dropped} 行匹配列数异常的序列", file=sys.stderr)
    return "".join(f"{l}\n" for l in out)


def _drop_malformed_rows(a3m_text: str, protein_sequence: str) -> str:
    """Drop a3m rows whose match-state count differs from the query length.

    Cross-source merges (uniref + metagenome) can carry rows aligned to a
    different match column count; downstream featurizers (Protenix, AF3)
    hard-fail on them. The query row defines the expected count."""
    query_len = len((protein_sequence or "").strip())
    if query_len == 0:
        return a3m_text
    out, dropped, header = [], 0, None
    for line in a3m_text.splitlines():
        if line.startswith(">"):
            header = line
        elif header is not None:
            if sum(1 for c in line if not c.islower()) == query_len:
                out.append(header)
                out.append(line)
            else:
                dropped += 1
            header = None
    if dropped:
        print(f"    [MSA] 丢弃 {dropped} 行匹配列数异常的序列", file=sys.stderr)
    return "".join(f"{l}\n" for l in out)


def cache_single_protein_msa(protein_id: str, protein_sequence: str, msa_file: str, cache_dir: str) -> bool:
    """Validate and cache one explicitly selected A3M file."""
    try:
        source_path = Path(msa_file)
        filename = source_path.name
        print(f"  处理MSA文件: {filename}", file=sys.stderr)
        if source_path.suffix.lower() != '.a3m' or not source_path.is_file():
            return False

        sanitize_a3m_file(str(source_path), context=f"{protein_id} 源MSA")
        msa_content = sanitize_a3m_content(source_path.read_text(), context=str(source_path))
        entries = parse_a3m_content(msa_content)
        if not entries:
            return False

        query_sequence = str(entries[0].get('sequence') or '')
        if not is_sequence_match(protein_sequence, query_sequence):
            print(f"    [ERROR] A3M文件中的查询序列与蛋白质组分 {protein_id} 不匹配", file=sys.stderr)
            return False

        seq_hash = get_sequence_hash(protein_sequence)
        validated = _drop_malformed_rows(msa_content, protein_sequence)
        cache_path = Path(cache_dir) / f"msa_{seq_hash}.a3m"
        tmp_cache_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        tmp_cache_path.write_text(validated)
        os.replace(tmp_cache_path, cache_path)
        print(f"    成功缓存蛋白质组分 {protein_id} 的MSA: {cache_path}", file=sys.stderr)
        print(f"       序列哈希: {seq_hash}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"    [ERROR] 处理蛋白质组分 {protein_id} 的MSA文件失败 {msa_file}: {e}", file=sys.stderr)
        return False


def is_sequence_match(protein_sequence: str, query_sequence: str) -> bool:
    """Compare normalized protein and A3M query sequences."""
    clean_protein = protein_sequence.replace('-', '').replace(' ', '').upper()
    clean_query = query_sequence.replace('-', '').replace(' ', '').upper()
    return clean_protein == clean_query


