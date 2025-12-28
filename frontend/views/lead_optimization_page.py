import io
import json
import os
import time
import zipfile
import string
from typing import List, Tuple

import pandas as pd
import streamlit as st
import yaml
from frontend.lead_optimization_client import (
    submit_lead_optimization,
    get_lead_optimization_status,
    download_lead_optimization_results,
    terminate_task,
)
from frontend.ui_components import render_pocket_constraint_ui
from frontend.url_state import URLStateManager
from frontend.utils import (
    get_smart_msa_default,
    read_cif_from_string,
    extract_protein_residue_bfactors,
    visualize_structure_py3dmol,
    get_pair_iptm_from_confidence,
)
from frontend.constants import BACKEND_LABELS
from config import LEAD_OPTIMIZATION_OUTPUT_DIR


def _get_chain_id_by_index(index: int) -> str:
    if index < 26:
        return string.ascii_uppercase[index]
    return f"Z{index-25}"


def _get_next_chain_id(chain_counter: int) -> Tuple[str, int]:
    chain_id = _get_chain_id_by_index(chain_counter)
    return chain_id, chain_counter + 1


def _get_chain_ids_for_components(components: List[dict]) -> Tuple[List[str], dict]:
    chain_ids = []
    chain_descriptions = {}
    chain_counter = 0

    for comp in components:
        comp_type = comp.get('type', 'protein')
        sequence = comp.get('sequence', '').strip()
        num_copies = max(1, int(comp.get('num_copies', 1)))

        for copy_idx in range(num_copies):
            chain_id, chain_counter = _get_next_chain_id(chain_counter)
            chain_ids.append(chain_id)

            if comp_type == 'protein':
                type_icon = '🧬'
            elif comp_type == 'dna':
                type_icon = '🔗'
            elif comp_type == 'rna':
                type_icon = '📜'
            else:
                type_icon = '🔸'

            seq_status = "" if sequence else " (序列待输入)"
            if num_copies > 1:
                chain_descriptions[chain_id] = f"{type_icon} 链 {chain_id} ({comp_type.upper()} 拷贝 {copy_idx+1}/{num_copies}){seq_status}"
            else:
                chain_descriptions[chain_id] = f"{type_icon} 链 {chain_id} ({comp_type.upper()}){seq_status}"

    return chain_ids, chain_descriptions


def _normalize_pocket_constraints(constraints: List[dict], binder_chain_id: str) -> List[dict]:
    normalized = []
    for constraint in constraints:
        if constraint.get('type') != 'pocket':
            continue

        binder = constraint.get('binder', 'BINDER_CHAIN')
        if binder == 'BINDER_CHAIN':
            binder = binder_chain_id

        contacts = []
        for contact in constraint.get('contacts', []):
            if not isinstance(contact, list) or len(contact) < 2:
                continue
            chain_id = contact[0]
            if chain_id == 'BINDER_CHAIN':
                chain_id = binder_chain_id
            contacts.append([chain_id, contact[1]])

        normalized.append({
            'pocket': {
                'binder': binder,
                'contacts': contacts,
                'max_distance': constraint.get('max_distance', 5.0),
                'force': constraint.get('force', False)
            }
        })

    return normalized


def _build_target_yaml_from_components(
    components: List[dict],
    constraints: List[dict],
    backend: str
) -> Tuple[str, List[str], str]:
    sequences_list = []
    chain_order = []
    chain_counter = 0

    for comp in components:
        seq = comp.get('sequence', '').strip()
        if not seq:
            continue

        comp_type = comp.get('type', 'protein')
        num_copies = max(1, int(comp.get('num_copies', 1)))

        for _ in range(num_copies):
            chain_id, chain_counter = _get_next_chain_id(chain_counter)
            chain_order.append(chain_id)

            if comp_type == 'protein':
                protein_entry = {
                    'id': chain_id,
                    'sequence': seq
                }
                if not comp.get('use_msa', True):
                    protein_entry['msa'] = 'empty'
                sequences_list.append({'protein': protein_entry})
            elif comp_type == 'dna':
                sequences_list.append({
                    'dna': {
                        'id': chain_id,
                        'sequence': seq
                    }
                })
            elif comp_type == 'rna':
                sequences_list.append({
                    'rna': {
                        'id': chain_id,
                        'sequence': seq
                    }
                })

    if not sequences_list:
        return "", [], ""

    payload = {
        'version': 1,
        'sequences': sequences_list
    }

    if backend != 'alphafold3':
        binder_chain_id = _get_chain_id_by_index(chain_counter)
        normalized_constraints = _normalize_pocket_constraints(constraints or [], binder_chain_id)
        if normalized_constraints:
            payload['constraints'] = normalized_constraints
    else:
        binder_chain_id = _get_chain_id_by_index(chain_counter)

    return yaml.dump(payload, sort_keys=False, indent=2, default_flow_style=False), chain_order, binder_chain_id


def _extract_structure_map(zip_bytes: bytes) -> dict:
    structure_map = {}
    if not zip_bytes:
        return structure_map

    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_ref:
        for name in zip_ref.namelist():
            lower_name = name.lower()
            if not lower_name.endswith(('.cif', '.pdb')):
                continue

            parts = name.split('/')
            compound_id = None
            if 'results' in parts:
                idx = parts.index('results')
                if idx + 1 < len(parts):
                    compound_id = parts[idx + 1]

            if not compound_id:
                continue

            ext = os.path.splitext(name)[1].lower()
            prefer = compound_id not in structure_map or ext == '.cif'
            if not prefer:
                continue

            try:
                content = zip_ref.read(name).decode('utf-8', errors='ignore')
            except Exception:
                continue

            structure_map[compound_id] = {
                'content': content,
                'ext': ext or '.cif'
            }

    return structure_map


def _extract_pair_iptm_map(zip_bytes: bytes) -> dict:
    pair_map = {}
    if not zip_bytes:
        return pair_map

    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_ref:
        for name in zip_ref.namelist():
            lower_name = name.lower()
            if not lower_name.endswith('.json'):
                continue
            if 'summary_confidences' not in lower_name and 'confidence' not in lower_name:
                continue

            parts = name.split('/')
            compound_id = None
            if 'results' in parts:
                idx = parts.index('results')
                if idx + 1 < len(parts):
                    compound_id = parts[idx + 1]

            if not compound_id:
                continue

            try:
                content = json.loads(zip_ref.read(name).decode('utf-8'))
            except Exception:
                continue

            if not isinstance(content, dict):
                continue

            if content.get("chain_pair_iptm") or content.get("pair_chains_iptm"):
                pair_map[compound_id] = content

    return pair_map


def _load_pair_iptm_from_local(task_id: str, compound_id: str) -> dict:
    if not task_id or not compound_id:
        return {}
    base_dir = os.path.join(LEAD_OPTIMIZATION_OUTPUT_DIR, task_id, "results", compound_id)
    confidence_path = os.path.join(base_dir, "confidence_data_model_0.json")
    if not os.path.exists(confidence_path):
        return {}
    try:
        with open(confidence_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and (data.get("pair_chains_iptm") or data.get("chain_pair_iptm")):
            return data
    except Exception:
        return {}
    return {}


def _load_summary_from_local(task_id: str) -> dict:
    if not task_id:
        return {}
    summary_path = os.path.join(LEAD_OPTIMIZATION_OUTPUT_DIR, task_id, "optimization_summary.json")
    if not os.path.exists(summary_path):
        return {}
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _parse_summary_from_string(payload: str) -> dict:
    if not isinstance(payload, str):
        return {}
    result = {}
    for key in ("original_compound", "strategy"):
        marker = f"{key}='"
        if marker in payload:
            value = payload.split(marker, 1)[1].split("'", 1)[0]
            result[key] = value
    if "total_candidates=" in payload:
        value = payload.split("total_candidates=", 1)[1].split(",", 1)[0]
        try:
            result["total_candidates"] = int(value)
        except ValueError:
            pass
    if "success_rate=" in payload:
        value = payload.split("success_rate=", 1)[1].split(")", 1)[0].split(",", 1)[0]
        try:
            result["success_rate"] = float(value)
        except ValueError:
            pass
    return result


def _load_log_metadata(task_id: str) -> dict:
    log_path = os.path.join(LEAD_OPTIMIZATION_OUTPUT_DIR, task_id, "lead_optimization.log")
    if not os.path.exists(log_path):
        return {}
    strategy = None
    original_compound = None
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if "输入化合物:" in line and original_compound is None:
                    original_compound = line.split("输入化合物:", 1)[1].strip()
                if "优化策略:" in line and strategy is None:
                    strategy = line.split("优化策略:", 1)[1].strip()
                if original_compound and strategy:
                    break
    except Exception:
        return {}
    return {
        "original_compound": original_compound,
        "strategy": strategy
    }


def _build_summary(task_id: str, results_df: pd.DataFrame | None, raw_summary: dict) -> dict:
    summary = {}
    if raw_summary:
        summary.update(raw_summary)
        if isinstance(raw_summary.get("single_compound"), str):
            summary.update(_parse_summary_from_string(raw_summary["single_compound"]))
    summary.update({k: v for k, v in _load_log_metadata(task_id).items() if v})

    if results_df is not None and not results_df.empty:
        if "original_compound" not in summary or not summary.get("original_compound"):
            if "original_smiles" in results_df.columns:
                original_smiles = results_df["original_smiles"].dropna()
                if not original_smiles.empty:
                    summary["original_compound"] = str(original_smiles.iloc[0])

        if "total_candidates" not in summary:
            summary["total_candidates"] = len(results_df)

        if "success_rate" not in summary and "status" in results_df.columns:
            completed = int((results_df["status"] == "completed").sum())
            summary["success_rate"] = completed / len(results_df) if len(results_df) else 0.0

    return summary


def _load_results_from_zip(zip_bytes: bytes):
    summary = {}
    results_df = None

    if not zip_bytes:
        return summary, results_df, {}, {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zip_ref:
        names = zip_ref.namelist()
        summary_name = next((n for n in names if n.endswith("optimization_summary.json")), None)
        if summary_name:
            try:
                summary = json.loads(zip_ref.read(summary_name).decode('utf-8'))
            except Exception:
                summary = {}

        csv_name = next((n for n in names if n.endswith("optimization_results.csv")), None)
        if csv_name:
            try:
                results_df = pd.read_csv(io.BytesIO(zip_ref.read(csv_name)))
            except Exception:
                results_df = None

    structure_map = _extract_structure_map(zip_bytes)
    pair_iptm_map = _extract_pair_iptm_map(zip_bytes)
    return summary, results_df, structure_map, pair_iptm_map


def _render_smiles_2d(smiles: str):
    if not smiles:
        st.caption("⚠️ SMILES 为空，无法生成2D结构。")
        return

    try:
        from rdkit import Chem
        from rdkit.Chem import Draw
    except Exception:
        st.caption("⚠️ RDKit 未安装，无法渲染2D结构。")
        st.code(smiles, language="smiles")
        return

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        st.caption("⚠️ 无效的SMILES，无法生成2D结构。")
        st.code(smiles, language="smiles")
        return

    img = Draw.MolToImage(mol, size=(360, 240))
    st.image(img, use_container_width=False)


def render_lead_optimization_page():
    URLStateManager.restore_state_from_url()

    st.markdown("### 🧪 先导化合物优化")
    st.markdown("基于MMPDB与Boltz结构预测的先导化合物优化流程，支持进度监控与可视化结果。")

    if 'lead_optimization_task_id' not in st.session_state:
        st.session_state.lead_optimization_task_id = None
    if 'lead_optimization_results' not in st.session_state:
        st.session_state.lead_optimization_results = None
    if 'lead_optimization_error' not in st.session_state:
        st.session_state.lead_optimization_error = None
    if 'lead_optimization_raw_zip' not in st.session_state:
        st.session_state.lead_optimization_raw_zip = None
    if 'lead_optimization_components' not in st.session_state:
        st.session_state.lead_optimization_components = [{
            'id': 'protein_1',
            'type': 'protein',
            'sequence': '',
            'num_copies': 1,
            'use_msa': True
        }]
    if 'lead_optimization_constraints' not in st.session_state:
        st.session_state.lead_optimization_constraints = []
    if 'lead_optimization_backend' not in st.session_state:
        st.session_state.lead_optimization_backend = 'boltz'
    if 'lead_opt_input_method' not in st.session_state:
        st.session_state.lead_opt_input_method = 'smiles'
    if 'lead_opt_ketcher_smiles' not in st.session_state:
        st.session_state.lead_opt_ketcher_smiles = ''
    if 'lead_optimization_chain_order' not in st.session_state:
        st.session_state.lead_optimization_chain_order = []
    if 'lead_opt_pair_chain_a' not in st.session_state:
        st.session_state.lead_opt_pair_chain_a = 'B'
    if 'lead_opt_pair_chain_b' not in st.session_state:
        st.session_state.lead_opt_pair_chain_b = 'A'

    is_running = (
        st.session_state.lead_optimization_task_id is not None
        and st.session_state.lead_optimization_results is None
        and st.session_state.lead_optimization_error is None
    )

    with st.expander("🧾 **步骤 1: 任务配置**", expanded=not is_running):
        target_yaml = ""

        st.markdown("**目标分子设置**")
        st.caption("支持多个组分序列，系统将自动分配链 ID（预留 B 给配体）。")

        if not st.session_state.lead_optimization_components:
            st.session_state.lead_optimization_components = [{
                'id': 'protein_1',
                'type': 'protein',
                'sequence': '',
                'num_copies': 1,
                'use_msa': True
            }]

        st.subheader("🧬 目标分子", anchor=False)
        delete_id = None

        for idx, comp in enumerate(st.session_state.lead_optimization_components[:]):
            st.markdown("---")
            st.subheader(f"组分 {idx + 1}", anchor=False)

            cols_comp = st.columns([3, 1, 1])

            with cols_comp[0]:
                comp_type_options = ['protein', 'dna', 'rna']
                current_type = comp.get('type', 'protein')
                current_type_index = comp_type_options.index(current_type) if current_type in comp_type_options else 0

                old_type = current_type
                new_type = st.selectbox(
                    "组分类型",
                    options=comp_type_options,
                    format_func=lambda x: {
                        "protein": "🧬 蛋白质/肽链",
                        "dna": "🔗 DNA",
                        "rna": "📜 RNA"
                    }[x],
                    key=f"lead_opt_type_{comp['id']}_{idx}",
                    index=current_type_index,
                    disabled=is_running,
                    help="选择此组分的分子类型：蛋白质、DNA 或 RNA。"
                )

                comp['type'] = new_type
                if new_type != old_type:
                    comp['sequence'] = ''
                    if 'use_msa' in comp:
                        del comp['use_msa']
                    if new_type == 'protein':
                        if st.session_state.lead_optimization_backend == 'alphafold3':
                            comp['use_msa'] = True
                        else:
                            comp['use_msa'] = get_smart_msa_default(st.session_state.lead_optimization_components)
                    st.rerun()

            with cols_comp[1]:
                comp['num_copies'] = st.number_input(
                    "拷贝数",
                    min_value=1,
                    max_value=10,
                    value=comp.get('num_copies', 1),
                    step=1,
                    key=f"lead_opt_copies_{comp['id']}_{idx}",
                    disabled=is_running,
                    help="此组分的拷贝数。"
                )

            with cols_comp[2]:
                if len(st.session_state.lead_optimization_components) > 1:
                    if st.button("🗑️", key=f"lead_opt_remove_{comp['id']}_{idx}", help="删除此组分", disabled=is_running):
                        delete_id = comp['id']

            num_copies = comp.get('num_copies', 1)
            if num_copies > 1:
                st.caption(f"💡 此组分将创建 {num_copies} 个拷贝，自动分配链ID")

            if comp['type'] == 'protein':
                sequence_value = st.text_area(
                    "蛋白质序列",
                    value=comp.get('sequence', ''),
                    placeholder="例如: MVSKGEELFTGVVPILVELDGDVNGHKFSVSGEGEGDATYGKLTLKFICTTGKLPVPWPTLV...",
                    disabled=is_running,
                    height=100,
                    key=f"lead_opt_seq_{comp['id']}_{idx}",
                    help="输入此蛋白质链的完整氨基酸序列。"
                )
                comp['sequence'] = sequence_value

                if comp.get('sequence', '').strip():
                    msa_disabled = is_running or st.session_state.lead_optimization_backend == 'alphafold3'
                    msa_help = "AlphaFold3 引擎要求为所有蛋白质生成 MSA，已自动启用并锁定。" if st.session_state.lead_optimization_backend == 'alphafold3' else "为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。"
                    msa_value = st.checkbox(
                        "启用 MSA",
                        value=True if st.session_state.lead_optimization_backend == 'alphafold3' else comp.get('use_msa', True),
                        key=f"lead_opt_msa_{comp['id']}_{idx}",
                        help=msa_help,
                        disabled=msa_disabled
                    )
                    if st.session_state.lead_optimization_backend == 'alphafold3':
                        comp['use_msa'] = True
                    elif msa_value != comp.get('use_msa', True):
                        comp['use_msa'] = msa_value
                        st.rerun()
                else:
                    if st.session_state.lead_optimization_backend == 'alphafold3':
                        comp['use_msa'] = True

            elif comp['type'] in ['dna', 'rna']:
                seq_type = "DNA" if comp['type'] == 'dna' else "RNA"
                placeholder = "ATGCGTAAGGGATCCGCATGC..." if comp['type'] == 'dna' else "AUGCGUAAGGAUCCGCAUGC..."
                sequence_value = st.text_area(
                    f"{seq_type}序列",
                    value=comp.get('sequence', ''),
                    placeholder=f"例如: {placeholder}",
                    disabled=is_running,
                    height=100,
                    key=f"lead_opt_seq_{comp['id']}_{idx}",
                    help=f"输入{seq_type}核苷酸序列。"
                )
                comp['sequence'] = sequence_value

        if delete_id:
            st.session_state.lead_optimization_components = [
                comp for comp in st.session_state.lead_optimization_components if comp['id'] != delete_id
            ]
            st.rerun()

        if st.button(
            "➕ 添加新组分",
            key="add_lead_opt_component",
            disabled=is_running,
            use_container_width=True,
            help="添加新的组分"
        ):
            next_index = len(st.session_state.lead_optimization_components) + 1
            default_use_msa = True if st.session_state.lead_optimization_backend == 'alphafold3' else get_smart_msa_default(
                st.session_state.lead_optimization_components
            )
            st.session_state.lead_optimization_components.append({
                'id': f'protein_{next_index}',
                'type': 'protein',
                'sequence': '',
                'num_copies': 1,
                'use_msa': default_use_msa
            })
            st.rerun()

        backend_options = list(BACKEND_LABELS.keys())
        current_backend = st.session_state.lead_optimization_backend
        if current_backend not in backend_options:
            current_backend = 'boltz'

        selected_backend = st.selectbox(
            "选择预测后端",
            backend_options,
            index=backend_options.index(current_backend),
            format_func=lambda key: BACKEND_LABELS.get(key, key),
            disabled=is_running,
            help="Boltz 引擎支持口袋约束；AlphaFold3 不支持约束设置。"
        )

        if selected_backend != current_backend:
            st.session_state.lead_optimization_backend = selected_backend
            if selected_backend == 'alphafold3':
                for comp in st.session_state.lead_optimization_components:
                    if comp.get('type') == 'protein':
                        comp['use_msa'] = True
            st.rerun()

        chain_ids, chain_descriptions = _get_chain_ids_for_components(
            st.session_state.lead_optimization_components
        )
        binder_chain_id = _get_chain_id_by_index(len(chain_ids))
        available_chains = chain_ids + ['BINDER_CHAIN']
        chain_descriptions['BINDER_CHAIN'] = f"🎯 结合分子 (将分配链 {binder_chain_id})"
        target_chain_ids = chain_ids
        default_target_chain = target_chain_ids[0] if target_chain_ids else 'A'

        st.subheader("🧷 链对设置", anchor=False)
        st.caption("用于定义结合链对、默认口袋约束目标，并用于 pair ipTM 展示。")

        cols_pair = st.columns([1, 2])
        with cols_pair[0]:
            st.selectbox(
                "设计链",
                options=[binder_chain_id],
                index=0,
                disabled=True,
                help="先导化合物固定为设计链"
            )
            st.session_state.lead_opt_pair_chain_a = binder_chain_id

        with cols_pair[1]:
            if target_chain_ids:
                current_target_chain = st.session_state.lead_opt_pair_chain_b
                if current_target_chain not in target_chain_ids:
                    current_target_chain = default_target_chain
                target_chain = st.selectbox(
                    "被结合链",
                    options=target_chain_ids,
                    index=target_chain_ids.index(current_target_chain),
                    format_func=lambda c: chain_descriptions.get(c, c),
                    disabled=is_running
                )
                st.session_state.lead_opt_pair_chain_b = target_chain
            else:
                st.selectbox(
                    "被结合链",
                    options=["A"],
                    index=0,
                    disabled=True
                )
                st.session_state.lead_opt_pair_chain_b = "A"

        default_target_chain = st.session_state.lead_opt_pair_chain_b or default_target_chain

        st.subheader("🔗 分子约束 (可选)", anchor=False)
        if selected_backend == 'alphafold3':
            st.info("AlphaFold3 后端暂不支持约束设置，请切换回 Boltz 引擎。", icon="ℹ️")

        constraints_disabled = is_running or selected_backend == 'alphafold3'
        constraint_id_to_delete = None

        for i, constraint in enumerate(st.session_state.lead_optimization_constraints[:]):
            with st.expander(f"🕳️ Pocket 约束 {i+1}", expanded=True):
                col1, col2 = st.columns([5, 1])
                with col1:
                    render_pocket_constraint_ui(
                        constraint,
                        f"lead_opt_{i}",
                        available_chains,
                        chain_descriptions,
                        constraints_disabled,
                        components=st.session_state.lead_optimization_components
                    )
                with col2:
                    if st.button("🗑️", key=f"lead_opt_del_constraint_{i}", help="删除此约束", disabled=constraints_disabled):
                        constraint_id_to_delete = i

        if constraint_id_to_delete is not None:
            del st.session_state.lead_optimization_constraints[constraint_id_to_delete]
            st.rerun()

        if st.button(
            "➕ 添加 Pocket 约束",
            key="add_lead_opt_pocket_constraint",
            disabled=constraints_disabled or not target_chain_ids
        ):
            st.session_state.lead_optimization_constraints.append({
                'type': 'pocket',
                'binder': 'BINDER_CHAIN',
                'contacts': [[default_target_chain, 1], [default_target_chain, 2]],
                'max_distance': 5.0,
                'force': False
            })
            st.rerun()

        target_yaml, chain_order, binder_chain_id = _build_target_yaml_from_components(
            st.session_state.lead_optimization_components,
            st.session_state.lead_optimization_constraints,
            selected_backend
        )
        if chain_order:
            st.session_state.lead_optimization_chain_order = chain_order + [binder_chain_id]
            st.session_state.lead_opt_pair_chain_a = binder_chain_id
        else:
            st.session_state.lead_optimization_chain_order = []

        st.divider()

        st.markdown("**先导化合物输入**")
        input_mode = st.radio(
            "输入模式",
            ["单个", "批量文件"],
            horizontal=True,
            disabled=is_running
        )

        input_compound = ""
        input_file = None

        if input_mode == "单个":
            method_options = ["smiles", "ketcher"]
            new_input_method = st.radio(
                "输入方法",
                method_options,
                index=method_options.index(st.session_state.get('lead_opt_input_method', 'smiles')),
                disabled=is_running,
                horizontal=True
            )
            st.session_state.lead_opt_input_method = new_input_method

            if new_input_method == "smiles":
                input_compound = st.text_input(
                    "输入先导化合物 SMILES",
                    placeholder="例如: CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5",
                    disabled=is_running
                )
            else:
                from streamlit_ketcher import st_ketcher

                initial_smiles = st.session_state.get('lead_opt_ketcher_smiles', '')
                st.info("在下方 **Ketcher 编辑器** 中绘制分子，或直接粘贴 SMILES 字符串。完成后点击编辑器内的 Apply。", icon="💡")
                ketcher_smiles = st_ketcher(
                    value=initial_smiles,
                    key="lead_opt_ketcher",
                    height=400
                )
                if ketcher_smiles is not None:
                    ketcher_smiles = ketcher_smiles.strip()
                st.session_state.lead_opt_ketcher_smiles = ketcher_smiles
                input_compound = ketcher_smiles

                st.markdown("<hr style='margin-top: 0.5rem; margin-bottom: 0.5rem'>", unsafe_allow_html=True)
                st.caption("✨ Ketcher 生成的 SMILES 字符串:")
                current_smiles = st.session_state.get('lead_opt_ketcher_smiles', '')
                if current_smiles:
                    st.code(current_smiles, language='smiles')
                else:
                    st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")
        else:
            input_file = st.file_uploader(
                "上传 SMILES 文件 (CSV/SMI/TXT)",
                type=['csv', 'smi', 'smiles', 'txt'],
                disabled=is_running
            )

        with st.expander("⚙️ **点击设置：优化参数**", expanded=False):
            col1, col2 = st.columns(2)

            with col1:
                optimization_strategy = st.selectbox(
                    "优化策略",
                    ["scaffold_hopping", "fragment_replacement", "multi_objective"],
                    disabled=is_running
                )
                max_candidates = st.number_input(
                    "每轮最大候选数",
                    min_value=1,
                    max_value=500,
                    value=30,
                    step=1,
                    disabled=is_running
                )
                iterations = st.number_input(
                    "迭代轮数",
                    min_value=1,
                    max_value=20,
                    value=1,
                    step=1,
                    disabled=is_running
                )
                batch_size = st.number_input(
                    "批次大小",
                    min_value=1,
                    max_value=32,
                    value=4,
                    step=1,
                    disabled=is_running
                )

            with col2:
                top_k_per_iteration = st.number_input(
                    "每轮保留 Top K",
                    min_value=1,
                    max_value=50,
                    value=5,
                    step=1,
                    disabled=is_running
                )
                diversity_weight = st.slider(
                    "多样性权重",
                    min_value=0.0,
                    max_value=1.0,
                    value=0.3,
                    step=0.05,
                    disabled=is_running
                )
                similarity_threshold = st.slider(
                    "最小相似性",
                    min_value=0.0,
                    max_value=1.0,
                    value=0.5,
                    step=0.05,
                    disabled=is_running
                )
                max_similarity_threshold = st.slider(
                    "最大相似性",
                    min_value=0.0,
                    max_value=1.0,
                    value=0.9,
                    step=0.05,
                    disabled=is_running
                )

            diversity_selection_strategy = st.selectbox(
                "多样性选择策略",
                ["tanimoto_diverse", "scaffold_diverse", "property_diverse", "hybrid"],
                disabled=is_running
            )

            limit_chiral = st.checkbox(
                "限制最大手性中心数",
                value=False,
                disabled=is_running
            )
            max_chiral_centers = None
            if limit_chiral:
                max_chiral_centers = st.number_input(
                    "最大手性中心数",
                    min_value=1,
                    max_value=20,
                    value=4,
                    step=1,
                    disabled=is_running
                )

            generate_report = st.checkbox(
                "生成HTML报告",
                value=False,
                disabled=is_running
            )

        can_submit = bool(target_yaml.strip()) and (
            (input_mode == "单个" and input_compound.strip()) or
            (input_mode == "批量文件" and input_file is not None)
        )

        if st.button(
            "🚀 开始优化",
            type="primary",
            disabled=is_running or not can_submit,
            use_container_width=True
        ):
            st.session_state.lead_optimization_task_id = None
            st.session_state.lead_optimization_results = None
            st.session_state.lead_optimization_error = None
            st.session_state.lead_optimization_raw_zip = None

            options = {
                'optimization_strategy': optimization_strategy,
                'max_candidates': int(max_candidates),
                'iterations': int(iterations),
                'batch_size': int(batch_size),
                'top_k_per_iteration': int(top_k_per_iteration),
                'diversity_weight': float(diversity_weight),
                'similarity_threshold': float(similarity_threshold),
                'max_similarity_threshold': float(max_similarity_threshold),
                'diversity_selection_strategy': diversity_selection_strategy,
                'max_chiral_centers': int(max_chiral_centers) if max_chiral_centers else None,
                'generate_report': generate_report,
                'backend': st.session_state.lead_optimization_backend
            }

            try:
                if input_mode == "批量文件" and input_file is not None:
                    task_id = submit_lead_optimization(
                        target_config_content=target_yaml,
                        input_filename=input_file.name,
                        input_file_content=input_file.getvalue(),
                        options=options
                    )
                else:
                    task_id = submit_lead_optimization(
                        target_config_content=target_yaml,
                        input_compound=input_compound.strip(),
                        options=options
                    )

                st.session_state.lead_optimization_task_id = task_id
                URLStateManager.update_url_for_lead_optimization_config(
                    task_id=task_id,
                    components=st.session_state.lead_optimization_components,
                    constraints=st.session_state.lead_optimization_constraints,
                    backend=st.session_state.lead_optimization_backend,
                    pair_chain_a=st.session_state.lead_opt_pair_chain_a,
                    pair_chain_b=st.session_state.lead_opt_pair_chain_b
                )
                st.success(f"✅ 任务已提交，ID: {task_id[:8]}...")
                st.rerun()
            except Exception as e:
                st.session_state.lead_optimization_error = {"error_message": str(e), "type": "Submit Error"}
                st.error(f"❌ 提交任务失败: {e}")

    if is_running:
        st.divider()
        col_title, col_stop = st.columns([3, 2])
        with col_title:
            st.header("🔄 **步骤 2: 进度监控**", anchor=False)
        with col_stop:
            st.markdown("""
            <style>
            .stop-button {
                background: linear-gradient(135deg, #ff6b6b, #ee5a52);
                border: none;
                border-radius: 12px;
                color: white;
                padding: 10px 20px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s ease;
                box-shadow: 0 4px 12px rgba(255, 107, 107, 0.3);
                width: 100%;
                text-align: center;
                margin-top: 8px;
            }
            .stop-button:hover {
                background: linear-gradient(135deg, #ff5252, #d32f2f);
                transform: translateY(-2px);
                box-shadow: 0 6px 16px rgba(255, 107, 107, 0.4);
            }
            </style>
            """, unsafe_allow_html=True)
            if st.button(
                "🛑 紧急停止",
                type="secondary",
                use_container_width=True,
                help="安全终止正在进行的优化任务",
                key="stop_lead_opt_btn"
            ):
                try:
                    terminate_task(st.session_state.lead_optimization_task_id)
                    st.info("🔄 已发送停止信号，等待任务终止...")
                    st.session_state.lead_optimization_task_id = None
                    st.session_state.lead_optimization_results = None
                    st.session_state.lead_optimization_error = {"error_message": "用户手动停止任务", "type": "User Cancelled"}
                    URLStateManager.clear_url_params()
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 停止任务失败: {e}")
                    st.session_state.lead_optimization_error = {"error_message": str(e), "type": "Stop Error"}

        try:
            status_data = get_lead_optimization_status(st.session_state.lead_optimization_task_id)
            state = status_data.get('state', 'UNKNOWN')
            progress = status_data.get('progress', {}) or {}

            if state in ['SUCCESS', 'COMPLETED']:
                st.success("🎉 先导优化任务已完成，正在加载结果...")
                raw_zip = download_lead_optimization_results(st.session_state.lead_optimization_task_id)
                summary, results_df, structure_map, pair_iptm_map = _load_results_from_zip(raw_zip)
                st.session_state.lead_optimization_results = {
                    "summary": summary,
                    "results_df": results_df,
                    "structure_map": structure_map,
                    "pair_iptm_map": pair_iptm_map
                }
                st.session_state.lead_optimization_raw_zip = raw_zip
                st.rerun()
            elif state in ['FAILURE', 'REVOKED']:
                error_msg = status_data.get('error', '任务失败')
                st.session_state.lead_optimization_error = {"error_message": error_msg, "type": "Task Error"}
                st.error(f"❌ 任务失败: {error_msg}")
            else:
                progress_percent = progress.get('progress_percent')
                if progress_percent is None:
                    processed = progress.get('processed_candidates', 0)
                    expected = progress.get('expected_candidates') or progress.get('total_compounds') or 0
                    if expected:
                        progress_percent = min(processed / expected * 100, 100.0)

                if progress_percent is not None:
                    st.progress(min(max(progress_percent / 100.0, 0.0), 1.0),
                                text=f"总体进度: {progress_percent:.1f}%")

                col1, col2, col3 = st.columns(3)
                col1.metric("已处理候选", progress.get('processed_candidates', progress.get('completed_compounds', 0)))
                col2.metric("预计候选", progress.get('expected_candidates', progress.get('total_compounds', 0)) or "未知")
                col3.metric("剩余估计 (秒)", int(progress.get('estimated_remaining_seconds', 0) or 0))

                if progress.get('estimated_completion_time'):
                    st.caption(f"预计完成时间: {progress.get('estimated_completion_time')}")

                st.caption("🔄 页面将自动刷新以获取最新进度…")
                time.sleep(5)
                st.rerun()
        except Exception as e:
            st.session_state.lead_optimization_error = {"error_message": str(e), "type": "Status Error"}
            st.error(f"❌ 获取状态失败: {e}")

    if st.session_state.lead_optimization_error and not is_running:
        error_info = st.session_state.lead_optimization_error
        st.error(f"❌ 任务错误: {error_info.get('error_message', '未知错误')}")

    if st.session_state.lead_optimization_results:
        st.divider()
        st.header("🏆 **步骤 3: 优化结果展示**", anchor=False)

        results = st.session_state.lead_optimization_results
        results_df = results.get("results_df")
        summary = results.get("summary", {})
        if not summary:
            summary = _load_summary_from_local(st.session_state.lead_optimization_task_id)
        summary = _build_summary(st.session_state.lead_optimization_task_id, results_df, summary)
        structure_map = results.get("structure_map", {})
        pair_iptm_map = results.get("pair_iptm_map", {})
        chain_order = st.session_state.get("lead_optimization_chain_order", [])
        target_chain = st.session_state.get("lead_opt_pair_chain_b", chain_order[0] if chain_order else "A")
        ligand_chain = st.session_state.get("lead_opt_pair_chain_a", "B")

        if st.session_state.lead_optimization_raw_zip:
            st.download_button(
                label="📦 下载完整结果 (ZIP)",
                data=st.session_state.lead_optimization_raw_zip,
                file_name=f"{st.session_state.lead_optimization_task_id}_lead_optimization_results.zip",
                mime="application/zip",
                use_container_width=True
            )

        if summary:
            st.subheader("📊 结果摘要", anchor=False)
            col1, col2 = st.columns(2)
            col1.metric("总候选数", summary.get('total_candidates', summary.get('total_compounds', 'N/A')))
            success_rate = summary.get('success_rate')
            if isinstance(success_rate, str):
                try:
                    success_rate = float(success_rate)
                except ValueError:
                    success_rate = None
            success_rate_display = f"{success_rate:.2%}" if isinstance(success_rate, float) else "N/A"
            col2.metric("成功率", success_rate_display)
        elif results_df is not None and not results_df.empty:
            st.subheader("📊 结果摘要", anchor=False)
            total_candidates = len(results_df)
            completed = 0
            if 'status' in results_df.columns:
                completed = int((results_df['status'] == 'completed').sum())
            success_rate = (completed / total_candidates) if total_candidates else 0
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("原始化合物", "N/A")
            col2.metric("策略", "N/A")
            col3.metric("总候选数", total_candidates)
            col4.metric("成功率", f"{success_rate:.2%}")

        if results_df is None or results_df.empty:
            st.info("暂无可用结果数据。请检查输出文件是否完整。")
            return

        filtered_df = results_df.copy()
        if 'combined_score' in filtered_df.columns:
            filtered_df['combined_score'] = pd.to_numeric(filtered_df['combined_score'], errors='coerce')
        if 'status' in filtered_df.columns:
            filtered_df = filtered_df[filtered_df['status'] == 'completed']

        with st.expander("🎛️ **结果过滤设置**", expanded=False):
            min_score = st.slider("综合评分阈值", 0.0, 1.0, 0.6, 0.05)
            max_display = st.slider("最大展示数量", 5, 100, 10, 1)

        if 'combined_score' in filtered_df.columns:
            filtered_df = filtered_df[filtered_df['combined_score'] >= min_score]
            filtered_df = filtered_df.sort_values(by='combined_score', ascending=False)

        top_df = filtered_df.head(max_display)

        st.subheader("🥇 优化候选列表", anchor=False)

        if top_df.empty:
            st.warning("未找到满足条件的候选化合物。")
            return

        for idx, row in top_df.reset_index(drop=True).iterrows():
            rank = idx + 1
            compound_id = str(row.get('compound_id', f"candidate_{rank}"))
            smiles = row.get('optimized_smiles', '')
            score = row.get('combined_score', 0.0) if pd.notna(row.get('combined_score', None)) else 0.0

            score_color = "🟢" if score >= 0.8 else "🟡" if score >= 0.7 else "🟠"

            with st.expander(f"**第 {rank} 名** {score_color} 评分: {score:.3f}", expanded=(idx < 3)):
                col_smiles, col_structure = st.columns([1.4, 1])
                with col_smiles:
                    st.markdown("**SMILES**")
                    st.code(smiles, language="smiles")
                with col_structure:
                    st.markdown("**2D 结构**")
                    _render_smiles_2d(smiles)

                col_metrics = st.columns(4)
                col_metrics[0].metric("综合评分", f"{score:.3f}")
                col_metrics[1].metric("binding_probability", row.get('binding_probability', 'N/A'))
                col_metrics[2].metric("pLDDT", row.get('plddt', 'N/A'))

                pair_iptm_value = None
                pair_data = pair_iptm_map.get(compound_id, {})
                if not pair_data:
                    pair_data = _load_pair_iptm_from_local(
                        st.session_state.lead_optimization_task_id,
                        compound_id
                    )

                if pair_data:
                    inferred_chain_order = chain_order
                    if not inferred_chain_order and isinstance(pair_data.get("pair_chains_iptm"), dict):
                        size = len(pair_data["pair_chains_iptm"])
                        inferred_chain_order = [
                            _get_chain_id_by_index(i) for i in range(size)
                        ]

                    display_ligand_chain = ligand_chain
                    display_target_chain = target_chain
                    if inferred_chain_order:
                        if display_ligand_chain not in inferred_chain_order:
                            display_ligand_chain = inferred_chain_order[-1]
                        if display_target_chain not in inferred_chain_order:
                            display_target_chain = inferred_chain_order[0]

                    pair_iptm_value = get_pair_iptm_from_confidence(
                        pair_data,
                        display_ligand_chain,
                        display_target_chain,
                        chain_order=inferred_chain_order or None
                    )

                pair_iptm_display = f"{pair_iptm_value:.3f}" if isinstance(pair_iptm_value, (int, float)) else "N/A"
                col_metrics[3].metric("pair ipTM", pair_iptm_display)

                if compound_id in structure_map:
                    structure = structure_map[compound_id]
                    ext = structure.get('ext', '.cif')
                    content = structure.get('content', '')

                    col_download = st.columns(2)
                    with col_download[0]:
                        mime_type = "chemical/x-pdb" if ext == '.pdb' else "chemical/x-cif"
                        st.download_button(
                            label=f"📄 下载结构 ({ext.upper().lstrip('.')})",
                            data=content,
                            file_name=f"{compound_id}{ext}",
                            mime=mime_type,
                            use_container_width=True,
                            key=f"download_structure_{compound_id}"
                        )

                    with col_download[1]:
                        if st.button(
                            "🔬 查看相互作用",
                            use_container_width=True,
                            key=f"view_interaction_{compound_id}"
                        ):
                            st.session_state[f"show_interaction_{compound_id}"] = not st.session_state.get(
                                f"show_interaction_{compound_id}", False
                            )
                            st.rerun()

                    if st.session_state.get(f"show_interaction_{compound_id}", False):
                        st.markdown("---")
                        st.markdown("**🔬 3D结构与相互作用**")

                        if ext != '.cif':
                            st.caption("⚠️ 当前仅支持 CIF 结构的3D展示。")
                        else:
                            try:
                                structure_obj = read_cif_from_string(content)
                                residue_bfactors = extract_protein_residue_bfactors(structure_obj)
                                view_html = visualize_structure_py3dmol(
                                    cif_content=content,
                                    residue_bfactors=residue_bfactors,
                                    protein_style='cartoon',
                                    ligand_style='ball-and-stick',
                                    spin=False,
                                    color_scheme='pLDDT'
                                )
                                st.components.v1.html(view_html, height=500, scrolling=False)
                            except Exception as e:
                                st.error(f"❌ 3D结构显示失败: {e}")
                else:
                    st.caption("⚠️ 未找到该候选的结构文件。")
