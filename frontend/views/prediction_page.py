
import streamlit as st
import requests
import json
import yaml
import string
import uuid
import time
import math

from frontend.constants import TYPE_TO_DISPLAY, TYPE_SPECIFIC_INFO
from frontend.utils import (
    get_available_chain_ids, 
    get_smart_msa_default, 
    validate_inputs, 
    generate_yaml_from_state, 
    get_cache_stats,
    has_cached_msa,
    visualize_structure_py3dmol
)
from frontend.prediction_client import submit_job, get_status, download_and_process_results
from frontend.ui_components import render_contact_constraint_ui, render_bond_constraint_ui, render_pocket_constraint_ui
from frontend.url_state import URLStateManager

BACKEND_LABELS = {
    'boltz': 'Boltz 引擎',
    'alphafold3': 'AlphaFold3 引擎'
}


def format_metric_value(value, precision: int = 2) -> str:
    """
    Format numeric metrics for display, returning 'N/A' for missing values.
    """
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return "N/A"


def get_smart_constraint_recommendations(components):
    """根据组分类型智能推荐约束类型"""
    has_ligand = any(comp.get('type') == 'ligand' for comp in components)
    has_biomolecules = any(comp.get('type') in ['protein', 'rna', 'dna'] for comp in components)
    
    if has_ligand:
        return ['pocket', 'bond'], "检测到小分子配体，推荐使用Pocket和Bond约束"
    elif has_biomolecules:
        return ['contact', 'bond'], "检测到蛋白质/DNA/RNA，推荐使用Contact和Bond约束"
    else:
        return ['contact', 'pocket', 'bond'], "可使用所有约束类型"

def render_prediction_page():
    st.markdown("### 🔬 分子复合物结构预测")
    st.markdown("输入您的生物分子序列，获得高精度的3D结构预测结果。")
    
    is_running = (
        st.session_state.task_id is not None and st.session_state.results is None and st.session_state.error is None
    )

    with st.expander("🔧 **步骤 1: 配置您的预测任务**", expanded=not is_running and not st.session_state.results):
        st.markdown("填写以下信息，定义您希望预测的生物大分子和小分子组分。")
        id_to_delete = None
        
        for i, component in enumerate(st.session_state.components[:]):
            st.markdown(f"---")
            st.subheader(f"组分 {i+1}", anchor=False)
            
            cols_type_copies = st.columns([3, 1])
            type_options = list(TYPE_TO_DISPLAY.keys())
            current_type = component.get('type', 'protein')
            current_type_index = type_options.index(current_type)
            
            selected_type = cols_type_copies[0].selectbox(
                "选择组分类型", options=type_options, format_func=lambda x: TYPE_TO_DISPLAY[x],
                key=f"type_{component['id']}", index=current_type_index, disabled=is_running,
                help="选择此组分是蛋白质、DNA、RNA 还是小分子。"
            )

            if selected_type != current_type:
                st.session_state.components[i]['sequence'] = ''
                st.session_state.components[i]['type'] = selected_type
                if selected_type != 'protein':
                    st.session_state.components[i]['cyclic'] = False
                st.rerun()

            st.session_state.components[i]['num_copies'] = cols_type_copies[1].number_input(
                "拷贝数", min_value=1, max_value=20, step=1, key=f"copies_{component['id']}",
                value=component.get('num_copies', 1), disabled=is_running,
                help="此组分的拷贝数。可设置为2（二聚体）、3（三聚体）等。每个拷贝将分配独立的链ID。"
            )

            if selected_type == 'ligand':
                method_options = ["smiles", "ccd", "ketcher"]
                current_method_index = method_options.index(component.get('input_method', 'smiles'))
                
                old_input_method = component.get('input_method', 'smiles')
                
                new_input_method = st.radio(
                    "小分子输入方式", method_options, key=f"ligand_type_{component['id']}",
                    index=current_method_index, disabled=is_running, horizontal=True,
                    help="选择通过SMILES字符串、PDB CCD代码或分子编辑器输入小分子。"
                )
                
                input_method_changed = new_input_method != old_input_method
                
                st.session_state.components[i]['input_method'] = new_input_method
                
                if input_method_changed:
                    st.session_state.components[i]['sequence'] = ''
                    
                    method_display_names = {
                        "smiles": "SMILES 字符串",
                        "ccd": "PDB CCD 代码", 
                        "ketcher": "分子编辑器"
                    }
                    st.toast(f"输入方式已更新为 {method_display_names.get(new_input_method, new_input_method)}", icon="🔄")
                    
                    st.rerun()
                
                num_copies = component.get('num_copies', 1)
                
                if new_input_method == 'smiles':
                    st.session_state.components[i]['sequence'] = st.text_input(
                        f"SMILES 字符串 ({'单分子' if num_copies == 1 else f'{num_copies}个分子'})",
                        value=component.get('sequence', ''),
                        placeholder="例如: CC(=O)NC1=CC=C(C=C1)O",
                        key=f"seq_{component['id']}",
                        disabled=is_running,
                        help="输入SMILES（简化分子线性输入系统）字符串来描述分子结构。"
                    )
                
                elif new_input_method == 'ccd':
                    st.session_state.components[i]['sequence'] = st.text_input(
                        f"CCD 代码 ({'单分子' if num_copies == 1 else f'{num_copies}个分子'})",
                        value=component.get('sequence', ''),
                        placeholder="例如: HEM, NAD, ATP",
                        key=f"seq_{component['id']}",
                        disabled=is_running,
                        help="输入标准化合物组件字典（CCD）中的三字母或多字母代码。"
                    )
                
                else:  # ketcher
                    from streamlit_ketcher import st_ketcher
                    initial_smiles = st.session_state.components[i].get('sequence', '')
                    
                    st.info("🎨 在下方 **Ketcher 编辑器** 中绘制分子，或直接粘贴 SMILES 字符串。**编辑完成后，请点击编辑器内部的 'Apply' 按钮，SMILES 字符串将自动更新。**", icon="💡")
                    
                    ketcher_current_smiles = st_ketcher(
                        value=initial_smiles,
                        key=f"ketcher_{component['id']}",
                        height=400
                    )
                    
                    if ketcher_current_smiles is not None:
                        ketcher_current_smiles = ketcher_current_smiles.strip()
                        if ketcher_current_smiles != initial_smiles:
                            st.session_state.components[i]['sequence'] = ketcher_current_smiles
                            if ketcher_current_smiles:
                                st.toast("✅ SMILES 字符串已成功更新！", icon="🧪")
                            else:
                                st.toast("📝 SMILES 字符串已清空", icon="🗑️")
                        
                    st.markdown("<hr style='margin-top: 0.5rem; margin-bottom: 0.5rem'>", unsafe_allow_html=True)
                    st.caption("✨ Ketcher 生成的 SMILES 字符串:")
                    current_smiles_in_state = st.session_state.components[i].get('sequence', '')
                    if current_smiles_in_state:
                        st.code(current_smiles_in_state, language='smiles')
                        
                        smiles_length = len(current_smiles_in_state)
                        atom_count = current_smiles_in_state.count('C') + current_smiles_in_state.count('N') + \
                                   current_smiles_in_state.count('O') + current_smiles_in_state.count('S')
                        st.caption(f"📊 长度: {smiles_length} 字符 | 主要原子数: ~{atom_count}")
                        
                        if not all(c in string.printable for c in current_smiles_in_state):
                            st.warning("⚠️ SMILES 字符串包含非打印字符，可能导致预测失败。", icon="⚠️")
                        elif len(current_smiles_in_state.strip()) == 0:
                            st.warning("⚠️ SMILES 字符串为空。", icon="⚠️")
                        else:
                            st.success("SMILES 格式看起来正常", icon="✅")
                    else:
                        st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。" )
            else:  # protein, dna, rna
                placeholder_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('placeholder', '')
                help_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('help', '')
                
                num_copies = component.get('num_copies', 1)
                if selected_type == 'protein':
                    label = f"蛋白质序列 ({'单体' if num_copies == 1 else f'{num_copies}聚体'})"
                elif selected_type == 'dna':
                    label = f"DNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})"
                elif selected_type == 'rna':
                    label = f"RNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})"
                else:
                    label = f"输入 {selected_type.capitalize()} 序列"
                
                old_sequence = component.get('sequence', '')
                
                new_sequence = st.text_area(
                    label, 
                    height=120, key=f"seq_{component['id']}",
                    value=component.get('sequence', ''),
                    placeholder=placeholder_text,
                    help=help_text,
                    disabled=is_running
                )
                
                sequence_changed = new_sequence != old_sequence
                
                st.session_state.components[i]['sequence'] = new_sequence
                
                if sequence_changed:
                    if selected_type == 'protein':
                        protein_components = [comp for comp in st.session_state.components if comp.get('type') == 'protein']
                        if len(protein_components) == 1:
                            if new_sequence.strip():
                                if has_cached_msa(new_sequence.strip()):
                                    st.session_state.components[i]['use_msa'] = True
                                else:
                                    st.session_state.components[i]['use_msa'] = False
                            else:
                                st.session_state.components[i]['use_msa'] = False
                    
                    st.rerun()
                
                if selected_type == 'protein':
                    protein_sequence = st.session_state.components[i].get('sequence', '').strip()
                    
                    if protein_sequence:
                        protein_opts_cols = st.columns([1.5, 1.5, 1, 1])
                        
                        with protein_opts_cols[0]:
                            cyclic_value = st.checkbox(
                                "环肽 (Cyclic)",
                                value=st.session_state.components[i].get('cyclic', False),
                                key=f"cyclic_{component['id']}",
                                help="勾选此项表示该蛋白质序列是一个环状肽。对于环肽，模型将尝试生成闭合的环状结构。",
                                disabled=is_running
                            )
                            if cyclic_value != st.session_state.components[i].get('cyclic', False):
                                st.session_state.components[i]['cyclic'] = cyclic_value
                                st.rerun()
                        
                        with protein_opts_cols[1]:
                            msa_value = st.checkbox(
                                "启用 MSA",
                                value=st.session_state.components[i].get('use_msa', True),
                                key=f"msa_{component['id']}",
                                help="为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。",
                                disabled=is_running
                            )
                            if msa_value != st.session_state.components[i].get('use_msa', True):
                                st.session_state.components[i]['use_msa'] = msa_value
                                st.rerun()
                        
                        with protein_opts_cols[2]:
                            if has_cached_msa(protein_sequence):
                                st.markdown("🟢&nbsp;**已缓存**", unsafe_allow_html=True)
                            else:
                                st.markdown("🟡&nbsp;**未缓存**", unsafe_allow_html=True)
                        
                        with protein_opts_cols[3]:
                            if has_cached_msa(protein_sequence):
                                st.markdown("⚡&nbsp;快速加载", unsafe_allow_html=True)
                            else:
                                st.markdown("🔄&nbsp;需要生成", unsafe_allow_html=True)
                    else:
                        cyclic_value = st.checkbox(
                            "环肽 (Cyclic Peptide)",
                            value=st.session_state.components[i].get('cyclic', False),
                            key=f"cyclic_{component['id']}",
                            help="勾选此项表示该蛋白质序列是一个环状肽。对于环肽，模型将尝试生成闭合的环状结构。",
                            disabled=is_running
                        )
                        if cyclic_value != st.session_state.components[i].get('cyclic', False):
                            st.session_state.components[i]['cyclic'] = cyclic_value
                            st.rerun()
                        st.session_state.components[i]['use_msa'] = st.session_state.components[i].get('use_msa', True)
            
            delete_col, _ = st.columns([10, 1])
            with delete_col:
                if len(st.session_state.components) > 1:
                    if st.button("🗑️ 删除此组分", key=f"del_{component['id']}", help="从任务中移除此组分", disabled=is_running):
                        id_to_delete = component['id']
        
        if id_to_delete:
            st.session_state.components = [c for c in st.session_state.components if c.get('id') != id_to_delete]
            st.rerun()

        st.markdown("---")
        
        def add_new_component():
            smart_msa_default = get_smart_msa_default(st.session_state.components)
            st.session_state.components.append({
                'id': str(uuid.uuid4()), 
                'type': 'protein', 
                'num_copies': 1, 
                'sequence': '', 
                'input_method': 'smiles', 
                'cyclic': False,
                'use_msa': smart_msa_default
            })
        
        st.button("➕ 添加新组分", on_click=add_new_component, disabled=is_running, use_container_width=True)

        st.subheader("全局与高级设置", anchor=False)
        
        col_global_left, col_global_right = st.columns(2)
        
        with col_global_left:
            protein_components = [comp for comp in st.session_state.components 
                                if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
            
            if protein_components:
                cached_count = sum(1 for comp in protein_components 
                                 if comp.get('use_msa', True) and has_cached_msa(comp['sequence']))
                enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
                total_proteins = len(protein_components)
                
                if enabled_count == 0:
                    strategy = "none"
                elif cached_count == enabled_count and enabled_count == total_proteins:
                    strategy = "cached"
                elif cached_count == 0 and enabled_count == total_proteins:
                    strategy = "auto"
                else:
                    strategy = "mixed"
                
                st.markdown("**MSA 使用概览**")
                if strategy == "none":
                    st.info(f"跳过所有 MSA 生成")
                elif strategy == "cached":
                    st.success(f"使用已缓存的 MSA")
                elif strategy == "auto":
                    st.info(f"自动生成全部 MSA")
                elif strategy == "mixed":
                    disabled_count = total_proteins - enabled_count
                    st.warning(f"混合MSA策略：{cached_count} 个缓存，{enabled_count - cached_count} 个自动生成，{disabled_count} 个跳过")
            else:
                st.info("👆 添加蛋白质组分后可配置MSA选项")
        
        with col_global_right:
            st.markdown("**MSA 缓存状态**")
            
            cache_stats = get_cache_stats()
            
            if cache_stats['total_files'] > 0:
                st.caption(f"📁 {cache_stats['total_files']} 个缓存文件 ({cache_stats['total_size_mb']:.1f} MB)")
                
                protein_components = [comp for comp in st.session_state.components 
                                    if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
                
                if protein_components:
                    st.markdown("**蛋白质组分缓存状态：**")
                    for i, comp in enumerate(protein_components):
                        sequence = comp['sequence']
                        comp_id = comp.get('id', f'protein_{i+1}')
                        if has_cached_msa(sequence):
                            st.success(f"✅ {comp_id}: 已缓存", icon="💾")
                        else:
                            st.info(f"ℹ️ {comp_id}: 未缓存", icon="💾")
            else:
                st.caption("暂无MSA缓存")

        backend_options = list(BACKEND_LABELS.keys())
        current_backend = st.session_state.get('prediction_backend', 'boltz')
        if current_backend not in backend_options:
            current_backend = 'boltz'
        backend_index = backend_options.index(current_backend)
        selected_backend = st.selectbox(
            "选择预测后端",
            backend_options,
            index=backend_index,
            format_func=lambda key: BACKEND_LABELS.get(key, key),
            disabled=is_running,
            help="Boltz 直接完成结构预测并返回复合物结果；AlphaFold3 生成含 af3/ 目录的输入与输出归档，可用于独立运行 AlphaFold3。"
        )
        if selected_backend != current_backend:
            st.session_state.prediction_backend = selected_backend
            st.rerun()

        if st.session_state.prediction_backend == 'alphafold3':
            st.caption("AlphaFold3 归档包含 `af3_input.json`、MSA 和 `af3/output/` 目录下的原始推理文件。")
        
        has_ligand_component = any(comp['type'] == 'ligand' for comp in st.session_state.components)
        if has_ligand_component:
            affinity_value = st.checkbox(
                "🔬 计算结合亲和力 (Affinity)",
                value=st.session_state.properties.get('affinity', False),
                disabled=is_running,
                help="勾选后，模型将尝试预测小分子与大分子组分之间的结合亲和力。请确保至少输入了一个小分子组分。"
            )
            if affinity_value != st.session_state.properties.get('affinity', False):
                st.session_state.properties['affinity'] = affinity_value
                st.rerun()
            if st.session_state.properties.get('affinity', False):
                chain_letter_idx = 0
                valid_ligand_chains = []
                for comp in st.session_state.components:
                    if comp.get('sequence', '').strip():
                        num_copies = comp.get('num_copies', 1)
                        if comp['type'] == 'ligand':
                            for j in range(num_copies):
                                if (chain_letter_idx + j) < len(string.ascii_uppercase):
                                    chain_id = string.ascii_uppercase[(chain_letter_idx + j)]
                                    valid_ligand_chains.append(chain_id)
                                else:
                                    chain_id = f"L{j}"
                                    valid_ligand_chains.append(chain_id)
                        chain_letter_idx += num_copies
                
                if valid_ligand_chains:
                    current_binder = st.session_state.properties.get('binder')
                    try:
                        binder_index = valid_ligand_chains.index(current_binder)
                    except ValueError:
                        binder_index = 0 if valid_ligand_chains else -1
                    
                    if binder_index != -1:
                        st.session_state.properties['binder'] = st.selectbox(
                            "选择作为结合体(Binder)的小分子链 ID",
                            options=valid_ligand_chains,
                            index=binder_index,
                            help="被选中的小分子链将被视为与其余所有链形成复合物的结合伙伴。预测结果将围绕此结合事件进行评估。",
                            disabled=is_running
                        )
                    else:
                        st.session_state.properties['binder'] = None
                        st.warning("请为至少一个小分子组分输入序列(SMILES/CCD)以选择结合体。", icon="⚠️")
                else:
                    st.session_state.properties['binder'] = None
                    st.warning("请为至少一个小分子组分输入序列(SMILES/CCD)以选择结合体。", icon="⚠️")
        else:
            if 'properties' in st.session_state:
                st.session_state.properties['affinity'] = False
                st.session_state.properties['binder'] = None

        st.markdown("---")
        st.subheader("🔗 分子约束 (可选)", anchor=False)
        st.markdown("设置分子结构约束，包括键约束、口袋约束和接触约束。")
        
        # 智能约束推荐
        recommended_constraints, recommendation_message = get_smart_constraint_recommendations(st.session_state.components)
        if recommendation_message:
            st.info(f"💡 **推荐**：{recommendation_message}")
        
        constraint_id_to_delete = None
        for i, constraint in enumerate(st.session_state.constraints[:]):
            constraint_type = constraint.get('type', 'contact')
            
            constraint_labels = {
                'bond': '🔗 键约束',
                'contact': '📍 接触约束',
                'pocket': '🕳️ 口袋约束'
            }
            
            with st.expander(f"{constraint_labels.get(constraint_type, '📍 约束')} {i+1}", expanded=True):
                col1, col2 = st.columns([5, 1])
                
                with col1:
                    st.markdown("**约束类型**")
                    
                    # 构建选项列表和格式函数
                    all_options = ['contact', 'bond', 'pocket']
                    format_funcs = {
                        'contact': '📍 Contact - 接触约束 (两个残基间距离)',
                        'bond': '🔗 Bond - 键约束 (两个原子间共价键)',
                        'pocket': '🕳️ Pocket - 口袋约束 (小分子与蛋白质口袋的相互作用)'
                    }
                    
                    # 为推荐的约束类型添加标记
                    def format_constraint_option(x):
                        base_text = format_funcs[x]
                        if x in recommended_constraints:
                            return f"{base_text}"
                        return base_text
                    
                    constraint_type = st.selectbox(
                        "选择约束类型",
                        options=all_options,
                        format_func=format_constraint_option,
                        index=all_options.index(constraint.get('type', 'contact')) if constraint.get('type', 'contact') in all_options else 0,
                        key=f"constraint_type_{i}",
                        disabled=is_running,
                        help="选择约束的类型。⭐标记表示根据您的分子组合推荐的约束类型。"
                    )
                    
                    if constraint_type != constraint.get('type', 'contact'):
                        constraint['type'] = constraint_type
                        # 清理不同约束类型的特定字段
                        if constraint_type == 'bond':
                            constraint.pop('binder', None)
                            constraint.pop('contacts', None)
                            constraint.pop('token1_chain', None)
                            constraint.pop('token1_residue', None)
                            constraint.pop('token2_chain', None)
                            constraint.pop('token2_residue', None)
                        elif constraint_type == 'contact':
                            constraint.pop('atom1_chain', None)
                            constraint.pop('atom1_residue', None)
                            constraint.pop('atom1_atom', None)
                            constraint.pop('atom2_chain', None)
                            constraint.pop('atom2_residue', None)
                            constraint.pop('atom2_atom', None)
                            constraint.pop('binder', None)
                            constraint.pop('contacts', None)
                        elif constraint_type == 'pocket':
                            constraint.pop('atom1_chain', None)
                            constraint.pop('atom1_residue', None)
                            constraint.pop('atom1_atom', None)
                            constraint.pop('atom2_chain', None)
                            constraint.pop('atom2_residue', None)
                            constraint.pop('atom2_atom', None)
                            constraint.pop('token1_chain', None)
                            constraint.pop('token1_residue', None)
                            constraint.pop('token2_chain', None)
                            constraint.pop('token2_residue', None)
                        st.rerun()
                    
                    available_chains, chain_descriptions = get_available_chain_ids(st.session_state.components)
                    
                    st.markdown("---")
                    
                    if constraint_type == 'contact':
                        render_contact_constraint_ui(constraint, f"constraint_{i}", available_chains, chain_descriptions, is_running)
                    elif constraint_type == 'bond':
                        render_bond_constraint_ui(constraint, f"constraint_{i}", available_chains, chain_descriptions, is_running)
                    elif constraint_type == 'pocket':
                        render_pocket_constraint_ui(constraint, f"constraint_{i}", available_chains, chain_descriptions, is_running)
                
                with col2:
                    if st.button("🗑️", key=f"del_constraint_{i}", help="删除此约束", disabled=is_running):
                        constraint_id_to_delete = i
        
        if constraint_id_to_delete is not None:
            del st.session_state.constraints[constraint_id_to_delete]
            st.rerun()
        
        st.markdown("---")
        st.markdown("**添加新约束**")
        
        # 根据智能推荐显示不同的按钮
        add_constraint_cols = st.columns(3)
        
        with add_constraint_cols[0]:
            button_text = "➕ 添加 Contact 约束"
            if 'contact' in recommended_constraints:
                button_text = "➕ 添加 Contact 约束"
            
            if st.button(button_text, key="add_contact_constraint", disabled=is_running, help="添加接触距离约束"):
                st.session_state.constraints.append({
                    'type': 'contact',
                    'token1_chain': 'A',
                    'token1_residue': 1,
                    'token2_chain': 'B',
                    'token2_residue': 1,
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
        
        with add_constraint_cols[1]:
            button_text = "➕ 添加 Pocket 约束"
            if 'pocket' in recommended_constraints:
                button_text = "➕ 添加 Pocket 约束"
                
            if st.button(button_text, key="add_pocket_constraint", disabled=is_running, help="添加小分子-蛋白质口袋结合约束"):
                st.session_state.constraints.append({
                    'type': 'pocket',
                    'binder': 'A',
                    'contacts': [['B', 1]],
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
        
        with add_constraint_cols[2]:
            button_text = "➕ 添加 Bond 约束"
            if 'bond' in recommended_constraints:
                button_text = "➕ 添加 Bond 约束"
                
            if st.button(button_text, key="add_bond_constraint", disabled=is_running, help="添加共价键约束"):
                st.session_state.constraints.append({
                    'type': 'bond',
                    'atom1_chain': 'A',
                    'atom1_residue': 1,
                    'atom1_atom': 'CA',
                    'atom2_chain': 'B',
                    'atom2_residue': 1,
                    'atom2_atom': 'CA'
                })
                st.rerun()
        
        if st.session_state.constraints:
            constraint_count = len(st.session_state.constraints)
            constraint_types = {}
            for c in st.session_state.constraints:
                ctype = c.get('type', 'contact')
                constraint_types[ctype] = constraint_types.get(ctype, 0) + 1
            
            constraint_type_names = {'contact': 'Contact', 'bond': 'Bond', 'pocket': 'Pocket'}
            type_summary = ', '.join([f"{count}个{constraint_type_names.get(ctype, ctype)}" 
                                    for ctype, count in constraint_types.items()])
            st.info(f"💡 已配置 {constraint_count} 个约束：{type_summary}")
        else:
            st.info("💡 暂无约束。可根据您的分子组合添加推荐的约束类型。")

    is_valid, validation_message = validate_inputs(st.session_state.components)
    yaml_preview = generate_yaml_from_state() if is_valid else None

    if yaml_preview and is_valid:
        with st.expander("📋 **预览生成的 YAML 配置**", expanded=False):
            st.markdown("以下是根据您的输入生成的 YAML 配置文件，将被发送给 Boltz 模型进行预测：")
            st.code(yaml_preview, language='yaml')
            
            has_ketcher = any(comp.get('type') == 'ligand' and comp.get('input_method') == 'ketcher' 
                            for comp in st.session_state.components)
            if has_ketcher:
                st.info("💡 **注意**: Ketcher 绘制的分子已自动转换为 `smiles` 字段，这是 Boltz 模型要求的格式。", icon="🔄")

    if st.button("🚀 提交预测任务", key="submit_prediction", type="primary", disabled=(not is_valid or is_running), use_container_width=True):
        st.session_state.task_id = None
        st.session_state.results = None
        st.session_state.raw_zip = None
        st.session_state.error = None
        
        protein_components = [comp for comp in st.session_state.components 
                            if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
        
        use_msa_for_job = False
        has_glycopeptide_modifications = False
        
        if protein_components:
            yaml_data = yaml.safe_load(yaml_preview)
            has_msa_in_yaml = False
            
            for sequence_item in yaml_data.get('sequences', []):
                if 'protein' in sequence_item:
                    protein_data = sequence_item['protein']
                    if protein_data.get('msa') and protein_data['msa'] != 'empty':
                        has_msa_in_yaml = True
                        break
            
            for sequence_item in yaml_data.get('sequences', []):
                if 'protein' in sequence_item:
                    protein_data = sequence_item['protein']
                    if protein_data.get('modifications'):
                        has_glycopeptide_modifications = True
                        break
            
            if not has_msa_in_yaml:
                use_msa_for_job = any(comp.get('use_msa', True) for comp in protein_components)
        
        model_name = "boltz1" if (has_glycopeptide_modifications and st.session_state.prediction_backend == 'boltz') else None
        
        with st.spinner("⏳ 正在提交任务，请稍候..."):
            try:
                task_id = submit_job(
                    yaml_content=yaml_preview,
                    use_msa=use_msa_for_job,
                    model_name=model_name,
                    backend=st.session_state.prediction_backend
                )
                st.session_state.task_id = task_id
                
                # 更新URL参数以保持任务状态和配置
                URLStateManager.update_url_for_prediction_task(
                    task_id=task_id, 
                    components=st.session_state.components,
                    constraints=st.session_state.constraints, 
                    properties=st.session_state.properties,
                    backend=st.session_state.prediction_backend
                )
                
                if use_msa_for_job:
                    msa_enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
                    st.toast(f"🎉 任务已提交！将为 {msa_enabled_count} 个蛋白质组分生成MSA", icon="✅")
                elif has_msa_in_yaml:
                    st.toast(f"🎉 任务已提交！使用缓存的MSA文件，预测将更快完成", icon="⚡")
                else:
                    st.toast(f"🎉 任务已提交！跳过MSA生成，预测将更快完成", icon="⚡")
                
                if model_name:
                    st.toast(f"🧬 检测到糖肽修饰，使用 {model_name} 模型进行预测", icon="🍬")
                
                backend_label = BACKEND_LABELS.get(st.session_state.prediction_backend, st.session_state.prediction_backend)
                st.toast(f"⚙️ 当前后端：{backend_label}", icon="🛠️")
                
                st.rerun()
            except requests.exceptions.RequestException as e:
                st.error(f"⚠️ **任务提交失败：无法连接到API服务器或服务器返回错误**。请检查后端服务是否运行正常。详情: {e}")
                st.session_state.error = {"error_message": str(e), "type": "API Connection Error"}
            except Exception as e:
                st.error(f"❌ **任务提交失败：发生未知错误**。详情: {e}")
                st.session_state.error = {"error_message": str(e), "type": "Client Error"}

    if not is_valid and not is_running:
        st.error(f"⚠️ **无法提交**: {validation_message}")

    if st.session_state.task_id and not st.session_state.results:
        st.divider()
        st.header("✨ **步骤 2: 查看预测结果**", anchor=False)
        if not st.session_state.error:
            spinner_and_status_placeholder = st.empty()
            
            while True:
                try:
                    status_data = get_status(st.session_state.task_id)
                    current_state = status_data.get('state', 'UNKNOWN')
                    
                    with spinner_and_status_placeholder.container():
                        if current_state == 'SUCCESS':
                            st.success("🎉 任务成功完成！正在下载并渲染结果...")
                            try:
                                processed_results, raw_zip_bytes = download_and_process_results(st.session_state.task_id)
                                st.session_state.results = processed_results
                                st.session_state.raw_zip = raw_zip_bytes
                                st.toast("✅ 结果已成功加载！", icon="🎊")
                                st.rerun()
                                break 
                            except (FileNotFoundError, json.JSONDecodeError) as e:
                                st.session_state.error = {"error_message": f"处理结果文件失败：{e}", "type": "Result File Error"}
                                st.error(f"❌ **结果文件处理失败**：{e}")
                                break
                            except requests.exceptions.RequestException as e:
                                st.session_state.error = {"error_message": f"下载结果文件失败：{e}", "type": "Download Error"}
                                st.error(f"❌ **下载结果文件失败**：{e}")
                                break
                        elif current_state == 'FAILURE':
                            st.session_state.error = status_data.get('info', {})
                            error_message = st.session_state.error.get('exc_message', '未知错误')
                            st.error(f"❌ **任务失败**：{error_message}")
                            
                            with st.expander("🔍 **调试信息**", expanded=False):
                                st.markdown("**任务ID：**")
                                st.code(st.session_state.task_id)
                                
                                st.markdown("**提交的 YAML 配置：**")
                                if yaml_preview:
                                    st.code(yaml_preview, language='yaml')
                                
                                st.markdown("**完整错误信息：**")
                                st.json(st.session_state.error)
                                
                                has_ketcher = any(comp.get('type') == 'ligand' and comp.get('input_method') == 'ketcher' 
                                                for comp in st.session_state.components)
                                if has_ketcher:
                                    st.markdown("**Ketcher 组分信息：**")
                                    ketcher_components = [comp for comp in st.session_state.components 
                                                        if comp.get('type') == 'ligand' and comp.get('input_method') == 'ketcher']
                                    for idx, comp in enumerate(ketcher_components):
                                        st.markdown(f"- 组分 {idx+1}: `{comp.get('sequence', 'empty')}`")
                            break
                        elif current_state == 'PENDING':
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info("🕒 任务正在排队中，请耐心等待...")
                        elif current_state == 'STARTED' or current_state == 'PROGRESS':
                            info_message = status_data.get('info', {}).get('message', f"当前状态: **{current_state}**")
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.info(f"🔬 **任务正在运行**：{info_message} (页面将每 10 秒自动刷新)", icon="⏳")
                        else:
                            st.markdown('<div class="loader"></div>', unsafe_allow_html=True)
                            st.warning(f"❓ 任务状态未知或正在初始化... (当前状态: **{current_state}**)")

                    time.sleep(10)
                except requests.exceptions.RequestException as e:
                    spinner_and_status_placeholder.error(f"🚨 **无法获取任务状态：API连接失败**。请检查后端服务是否运行正常。详情: {e}")
                    st.session_state.error = {"error_message": str(e), "type": "API Connection Error"}
                    break
                except Exception as e:
                    spinner_and_status_placeholder.error(f"🚨 **获取任务状态时发生未知错误**。详情: {e}")
                    st.session_state.error = {"error_message": str(e), "type": "Client Error"}
                    break

    if st.session_state.error:
        st.error("ℹ️ 任务执行失败，详细信息如下：")
        st.json(st.session_state.error)
        
        col_reset = st.columns(2)
        with col_reset[0]:
            if st.button("🔄 重置并重新开始", key="reset_prediction", type="secondary", use_container_width=True):
                # 清除URL参数
                URLStateManager.clear_url_params()
                # 清除所有相关的session state
                for key in ['task_id', 'results', 'raw_zip', 'error', 'components', 'constraints', 'properties', 'use_msa_server']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        
        with col_reset[1]:
            if st.button("🔧 保留配置重新设计", key="retry_prediction", type="primary", use_container_width=True):
                # 清除URL参数
                URLStateManager.clear_url_params()
                # 只清除任务相关的状态，保留配置
                for key in ['task_id', 'results', 'raw_zip', 'error']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()

    if st.session_state.results:
        st.divider()
        st.header("✅ **步骤 2: 预测结果展示**", anchor=False)

        cif_data = st.session_state.results.get('cif', '')
        confidence_data = st.session_state.results.get('confidence', {})
        affinity_data = st.session_state.results.get('affinity', {})

        col_vis, col_data = st.columns([3, 2])

        with col_vis:
            st.subheader("📊 3D 结构可视化", anchor=False)
            if cif_data:
                try:
                    from frontend.utils import read_cif_from_string, extract_protein_residue_bfactors
                    structure = read_cif_from_string(cif_data)
                    protein_bfactors = extract_protein_residue_bfactors(structure)
                    
                    with st.expander("⚙️ **视图设置**", expanded=True):
                        row1_col1, row1_col2 = st.columns(2)
                        with row1_col1:
                            st.selectbox("大分子样式", ['cartoon', 'stick', 'sphere'], key='protein_style_vis', help="选择蛋白质、DNA、RNA 等大分子的渲染样式。", index=0)
                        with row1_col2:
                            st.selectbox(
                                "着色方案",
                                ['pLDDT', 'Chain', 'Rainbow', 'Secondary Structure'],
                                key='color_scheme_vis',
                                help="""
                                选择整个复合物的着色方式：
                                - **pLDDT**: 根据预测置信度着色 (默认)，蓝色表示高置信，橙色表示低置信。
                                - **Chain**: 按不同的分子链着色。
                                - **Rainbow**: 从N端到C端按彩虹色渐变。
                                - **Secondary Structure**: 根据分子的二级结构（如螺旋、折叠）着色。
                                """,
                                index=0
                            )
                        row2_col1, row2_col2 = st.columns(2)
                        with row2_col1:
                            st.selectbox("配体样式", ['ball-and-stick', 'space-filling', 'stick', 'line'], key='ligand_style_vis', help="选择小分子的渲染样式。", index=0)
                        with row2_col2:
                            st.checkbox("🔄 旋转模型", key='spin_model_vis', value=False, help="勾选后，模型将自动围绕Z轴旋转。" )
                    
                    view_html = visualize_structure_py3dmol(
                        cif_content=cif_data,
                        residue_bfactors=protein_bfactors,
                        protein_style=st.session_state.protein_style_vis,
                        ligand_style=st.session_state.ligand_style_vis,
                        spin=st.session_state.spin_model_vis,
                        color_scheme=st.session_state.color_scheme_vis
                    )
                    st.components.v1.html(view_html, height=600, scrolling=False)
                except Exception as e:
                    st.error("加载 3D 结构时发生错误。请尝试刷新页面或检查输入数据。", icon="❌")
                    st.exception(e)
            else:
                st.warning("未能从结果中提取出有效的分子结构文件 (.cif/.pdb)，无法显示 3D 结构。", icon="⚠️")

        with col_data:
            st.subheader("📈 预测质量与亲和力评估", anchor=False)
            st.markdown("---")

            st.markdown("<b>pLDDT 置信度图例</b>", unsafe_allow_html=True)
            st.markdown("""
            <div style='display: flex; flex-wrap: wrap; gap: 10px; margin-top: 5px; margin-bottom: 25px;'>
                <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #0053D6; border-radius: 3px; margin-right: 5px;'></div><span><b>极高</b> (> 90)</span></div>
                <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #65CBF3; border-radius: 3px; margin-right: 5px;'></div><span><b>高</b> (70-90)</span></div>
                <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #FFDB13; border-radius: 3px; margin-right: 5px;'></div><span><b>中等</b> (50-70)</span></div>
                <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #FF7D45; border-radius: 3px; margin-right: 5px;'></div><span><b>低</b> (< 50)</span></div>
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("<b>全局结构质量指标</b>", unsafe_allow_html=True)
            cols_metrics = st.columns(2)
            cols_metrics[0].metric(
                "平均 pLDDT",
                format_metric_value(confidence_data.get('complex_plddt')),
                help="预测的局部距离差异检验 (pLDDT) 是一个 0-100 范围内的单残基置信度得分，代表模型对局部结构预测的信心。这是整个复合物所有残基的平均 pLDDT 分数。值越高越好。"
            )
            cols_metrics[1].metric(
                "pTM",
                format_metric_value(confidence_data.get('ptm'), precision=4),
                help="预测的模板建模评分 (pTM) 是一个 0-1 范围内的分数，用于衡量预测结构与真实结构在全局拓扑结构上的相似性。pTM > 0.5 通常表示预测了正确的折叠方式。值越高越好。"
            )
            cols_metrics[0].metric(
                "ipTM",
                format_metric_value(confidence_data.get('iptm'), precision=4),
                help="界面预测模板建模评分 (ipTM) 是专门用于评估链间相互作用界面准确性的指标 (0-1)。ipTM > 0.85 通常表明对复合物的相互作用方式有很高的置信度。值越高越好。"
            )
            cols_metrics[1].metric(
                "PAE (Å)",
                format_metric_value(confidence_data.get('complex_pde')),
                help="预测的对齐误差 (PAE) 表示残基对之间的预期位置误差（单位为埃 Å）。较低的值表示对不同结构域和链的相对位置和方向有更高的信心。这里显示的是整个复合物的平均误差。值越低越好。"
            )

            if confidence_data.get('backend') == 'alphafold3':
                extra_cols = st.columns(2)
                extra_cols[0].metric(
                    "Ranking Score",
                    format_metric_value(confidence_data.get('ranking_score')),
                    help="AlphaFold3 排名得分，越高代表该样本在模型集合中的排名越靠前。"
                )
                extra_cols[1].metric(
                    "Fraction Disordered",
                    format_metric_value(confidence_data.get('fraction_disordered')),
                    help="AlphaFold3 预测的无序区域比例（0-1）。数值越高，结构中无序残基比例越大。"
                )
            
            if affinity_data and st.session_state.properties.get('affinity'):
                st.markdown("<br><b>亲和力预测指标</b>", unsafe_allow_html=True)
                
                affinity_values = []
                for key in ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        affinity_values.append(value)
                
                if affinity_values:
                    import numpy as np
                    log_ic50_in_uM = np.mean(affinity_values)
                    affinity_std = np.std(affinity_values) if len(affinity_values) > 1 else 0.0
                    
                    ic50_uM = math.pow(10, log_ic50_in_uM)
                    if ic50_uM > 1000:
                        display_ic50 = f"{ic50_uM/1000:.3f} mM"
                    elif ic50_uM > 1000000:
                        display_ic50 = f"{ic50_uM/1000000:.3f} M"
                    else:
                        display_ic50 = f"{ic50_uM:.3f} µM"
                    
                    pIC50 = 6 - log_ic50_in_uM
                    delta_g = -1.364 * pIC50
                    
                    if len(affinity_values) > 1:
                        ic50_std_lower = math.pow(10, log_ic50_in_uM - affinity_std)
                        ic50_std_upper = math.pow(10, log_ic50_in_uM + affinity_std)
                        
                        if ic50_uM > 1000:
                            display_ic50_with_std = f"{ic50_uM/1000:.3f} ± {(ic50_std_upper-ic50_std_lower)/2000:.3f} mM"
                        elif ic50_uM > 1000000:
                            display_ic50_with_std = f"{ic50_uM/1000000:.3f} ± {(ic50_std_upper-ic50_std_lower)/2000000:.3f} M"
                        else:
                            display_ic50_with_std = f"{ic50_uM:.3f} ± {(ic50_std_upper-ic50_std_lower)/2:.3f} μM"
                            
                        st.metric("预测 IC50", display_ic50_with_std, help=f"预测的半数抑制浓度 (IC50)，基于 {len(affinity_values)} 个预测值的平均结果。数值越低表示预测的亲和力越强。" )
                    else:
                        st.metric("预测 IC50", display_ic50, help="预测的半数抑制浓度 (IC50) 是指结合体（Binder）抑制其靶标 50% 所需的浓度。它是衡量效力的常用指标，数值越低表示预测的亲和力越强。" )
                    
                binding_probabilities = []
                for key in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        binding_probabilities.append(value)
                
                if binding_probabilities:
                    binder_prob = np.mean(binding_probabilities)
                    binding_prob_std = np.std(binding_probabilities) if len(binding_probabilities) > 1 else 0.0
                    
                    if len(binding_probabilities) > 1:
                        st.metric("结合概率", f"{binder_prob:.2%} ± {binding_prob_std:.2%}", help=f"模型预测结合体与其余组分形成稳定复合物的概率，基于 {len(binding_probabilities)} 个预测值的平均结果。百分比越高，表明模型对这是一个真实的结合事件越有信心。" )
                    else:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测结合体与其余组分形成稳定复合物的概率。百分比越高，表明模型对这是一个真实的结合事件越有信心。" )
                else:
                    binder_prob = affinity_data.get("affinity_probability_binary")
                    if binder_prob is not None:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测结合体与其余组分形成稳定复合物的概率。百分比越高，表明模型对这是一个真实的结合事件越有信心。" )
            else:
                st.info("💡 如需亲和力预测结果，请在步骤1中勾选 **计算结合亲和力 (Affinity)** 选项。", icon="ℹ️")

            st.markdown("---")
            st.markdown("<b>📥 下载结果文件</b>", unsafe_allow_html=True)
            if st.session_state.get("raw_zip"):
                st.download_button(
                    label="📥 下载所有结果 (ZIP)",
                    data=st.session_state.raw_zip,
                    file_name=f"boltz_results_{st.session_state.task_id}.zip",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                    help="下载包含所有预测结果（CIF、JSON指标等）的原始ZIP文件。"
                )
            
            if cif_data:
                st.download_button("📄 下载 CIF", cif_data, "predicted_structure.cif", "chemical/x-cif", use_container_width=True, help="下载预测结构的CIF格式文件。" )
            
            all_json_data = {"confidence": confidence_data, "affinity": affinity_data}
            st.download_button(
                label="📦 下载指标数据 (JSON)",
                data=json.dumps(all_json_data, indent=2),
                file_name="prediction_metrics.json",
                mime="application/json",
                use_container_width=True,
                help="下载包含pLDDT、pTM、ipTM、PAE以及亲和力预测结果的JSON文件。"
            )
