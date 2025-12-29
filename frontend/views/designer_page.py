import streamlit as st
import os
import string
import yaml
import pandas as pd
import time
import uuid
import json
import psutil

from frontend.constants import BACKEND_LABELS
from frontend.utils import (
    get_available_chain_ids, 
    get_available_chain_ids_for_designer,  # 新增：设计器专用函数
    get_smart_msa_default, 
    validate_designer_inputs, 
    has_cached_msa,
    read_cif_from_string,
    extract_protein_residue_bfactors,
    find_best_structure_file,
    load_pair_iptm_data,
    get_pair_iptm_from_confidence
)
from frontend.designer_client import (
    create_designer_complex_yaml, 
    submit_designer_job, 
    get_designer_status, 
    load_designer_results
)
from frontend.ui_components import render_contact_constraint_ui, render_bond_constraint_ui, render_pocket_constraint_ui
from frontend.utils import visualize_structure_py3dmol
from frontend.url_state import URLStateManager

def render_designer_page():
    st.markdown("### 🧪 分子设计")
    st.markdown("使用演化算法设计分子结合体，优化其与目标复合物的结合亲和力。")
    
    # 添加设计类型选择器
    st.markdown("---")
    col_design_type, col_design_info = st.columns([1, 2])

    current_backend = st.session_state.get('designer_backend', 'boltz')
    if current_backend not in BACKEND_LABELS:
        current_backend = 'boltz'
        st.session_state.designer_backend = current_backend
    is_af3_backend = current_backend == 'alphafold3'

    design_type_options = ["peptide", "glycopeptide"]
    if is_af3_backend:
        design_type_options = ["peptide"]
    with col_design_type:
        design_type_selector = st.selectbox(
            "选择设计类型",
            options=design_type_options,
            format_func=lambda x: {
                "peptide": "🧬 多肽设计",
                "glycopeptide": "🍯 糖肽设计"
            }[x],
            help="选择要设计的分子类型。多肽设计适合大多数蛋白质结合需求，糖肽设计可添加糖基修饰。",
            key="main_design_type_selector"
        )
    
    with col_design_info:
        if is_af3_backend:
            st.warning("AlphaFold3 后端暂不支持糖肽设计，已自动切换为多肽模式。", icon="⚠️")
        elif design_type_selector == "peptide":
            st.info("**多肽设计**: 设计天然或修饰的氨基酸序列，具有优化的结合亲和力和特异性。", icon="🧬")
        else:  # glycopeptide
            st.info("**糖肽设计**: 设计含有糖基修饰的多肽，增强稳定性和生物活性，常用于免疫调节和细胞识别。", icon="🍯")
    
    designer_is_running = (
        st.session_state.designer_task_id is not None and 
        st.session_state.designer_results is None and 
        st.session_state.designer_error is None
    )

    if is_af3_backend:
        for comp in st.session_state.get('designer_components', []):
            if comp.get('type') == 'protein':
                comp.setdefault('use_msa', True)
                msa_key = f"designer_msa_{comp.get('id')}"
                if msa_key not in st.session_state:
                    st.session_state[msa_key] = comp.get('use_msa', True)
        if st.session_state.get('designer_cyclic_binder'):
            st.session_state.designer_cyclic_binder = False
    
    with st.expander("🎯 **步骤 1: 设置设计目标**", expanded=not designer_is_running and not st.session_state.designer_results):
        st.markdown("配置您的分子设计任务参数。")
        
        if 'designer_components' not in st.session_state:
            st.session_state.designer_components = [
                {'id': str(uuid.uuid4()), 'type': 'protein', 'sequence': '', 'num_copies': 1, 'use_msa': False}
            ]
        
        if 'designer_constraints' not in st.session_state:
            st.session_state.designer_constraints = []
        
        designer_id_to_delete = None
        for i, component in enumerate(st.session_state.designer_components[:]):
            st.markdown(f"---")
            st.subheader(f"组分 {i+1}", anchor=False)
            
            cols_comp = st.columns([3, 1, 1])
            
            with cols_comp[0]:
                comp_type_options = ['protein', 'dna', 'rna', 'ligand']
                current_type = component.get('type', 'protein')
                current_type_index = comp_type_options.index(current_type) if current_type in comp_type_options else 0
                
                old_type = current_type
                
                new_type = st.selectbox(
                    "组分类型",
                    options=comp_type_options,
                    format_func=lambda x: {
                        "protein": "🧬 蛋白质/肽链",
                        "dna": "🔗 DNA",
                        "rna": "📜 RNA", 
                        "ligand": "💊 辅酶/小分子"
                    }[x],
                    key=f"designer_type_{component['id']}",
                    index=current_type_index,
                    disabled=designer_is_running,
                    help="选择此组分的分子类型：蛋白质、DNA、RNA或小分子配体。"
                )
                
                type_changed = new_type != old_type
                
                component['type'] = new_type
                
                if type_changed:
                    component['sequence'] = ''
                    
                    if 'use_msa' in component:
                        del component['use_msa']
                    if 'cyclic' in component:
                        del component['cyclic']
                    if 'input_method' in component:
                        del component['input_method']
                    
                    if new_type == 'protein':
                        smart_default = get_smart_msa_default(st.session_state.designer_components)
                        component['use_msa'] = True if is_af3_backend else smart_default
                    elif new_type == 'ligand':
                        component['input_method'] = 'smiles'
                    
                    type_display_names = {
                        "protein": "🧬 蛋白质/肽链",
                        "dna": "🧬 DNA",
                        "rna": "🧬 RNA", 
                        "ligand": "💊 辅酶/小分子"
                    }
                    st.toast(f"组分类型已更新为 {type_display_names.get(new_type, new_type)}", icon="🔄")
                    
                    st.rerun()
            
            with cols_comp[1]:
                component['num_copies'] = st.number_input(
                    "拷贝数",
                    min_value=1,
                    max_value=10,
                    value=component.get('num_copies', 1),
                    step=1,
                    key=f"designer_copies_{component['id']}",
                    disabled=designer_is_running,
                    help="此组分的拷贝数。可设置为2（二聚体）、3（三聚体）等。每个拷贝将分配独立的链ID。"
                )
            
            with cols_comp[2]:
                if len(st.session_state.designer_components) > 1:
                    if st.button("🗑️", key=f"designer_del_{component['id']}", help="删除此组分", disabled=designer_is_running):
                        designer_id_to_delete = component['id']
            
            num_copies = component.get('num_copies', 1)
            if num_copies > 1:
                st.caption(f"💡 此组分将创建 {num_copies} 个拷贝，自动分配链ID")
            
            if component['type'] == 'protein':
                old_sequence = component.get('sequence', '')
                
                new_sequence = st.text_area(
                    f"蛋白质序列 ({'单体' if num_copies == 1 else f'{num_copies}聚体'})",
                    height=100,
                    value=component.get('sequence', ''),
                    placeholder="例如: MVSKGEELFTGVVPILVELDGDVNGHKFSVSGEGEGDATYGKLTLKFICTTGKLPVPWPTLV...",
                    key=f"designer_seq_{component['id']}",
                    disabled=designer_is_running,
                    help="输入此蛋白质链的完整氨基酸序列。"
                )
                
                sequence_changed = new_sequence != old_sequence
                
                component['sequence'] = new_sequence
                
                if sequence_changed:
                    if not is_af3_backend:
                        protein_components = [comp for comp in st.session_state.designer_components if comp.get('type') == 'protein']
                        if len(protein_components) == 1:
                            if new_sequence.strip():
                                if has_cached_msa(new_sequence.strip()):
                                    component['use_msa'] = True
                                else:
                                    component['use_msa'] = False
                            else:
                                component['use_msa'] = False
                    
                    st.rerun()
                
                designer_sequence = component.get('sequence', '').strip()
                if designer_sequence:
                    msa_disabled = designer_is_running
                    msa_help = (
                        "勾选时调用外部 MSA（MMseqs 缓存/服务器），不勾选时让 AlphaFold3 使用内置流程（不使用外部 MSA 缓存）。"
                        if is_af3_backend
                        else "为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。"
                    )
                    msa_value = st.checkbox(
                        "启用 MSA",
                        value=component.get('use_msa', True),
                        key=f"designer_msa_{component['id']}",
                        help=msa_help,
                        disabled=msa_disabled
                    )
                    if msa_value != component.get('use_msa', True):
                        component['use_msa'] = msa_value
                        if msa_value:
                            st.toast("已启用 MSA 生成", icon="✅")
                        else:
                            st.toast("已禁用 MSA 生成", icon="❌")
                        st.rerun()
                    if is_af3_backend:
                        st.caption("未勾选时将跳过外部 MSA，使用 AlphaFold3 自带的推理流程。")
                    
                if 'cyclic' in component:
                    del component['cyclic']
            elif component['type'] == 'dna':
                dna_sequence = st.text_area(
                    f"DNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})",
                    height=100,
                    value=component.get('sequence', ''),
                    placeholder="例如: ATGCGTAAGGGATCCGCATGC...",
                    key=f"designer_seq_{component['id']}",
                    disabled=designer_is_running,
                    help="输入DNA核苷酸序列（A、T、G、C）。"
                )
                component['sequence'] = dna_sequence
            elif component['type'] == 'rna':
                rna_sequence = st.text_area(
                    f"RNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})",
                    height=100,
                    value=component.get('sequence', ''),
                    placeholder="例如: AUGCGUAAGGAUCCGCAUGC...",
                    key=f"designer_seq_{component['id']}",
                    disabled=designer_is_running,
                    help="输入RNA核苷酸序列（A、U、G、C）。"
                )
                component['sequence'] = rna_sequence
            else:  # ligand
                from streamlit_ketcher import st_ketcher
                old_input_method = component.get('input_method', 'smiles')
                
                new_input_method = st.radio(
                    "小分子输入方式",
                    ["smiles", "ccd", "ketcher"],
                    key=f"designer_method_{component['id']}",
                    horizontal=True,
                    disabled=designer_is_running,
                    help="选择通过SMILES字符串、PDB CCD代码或分子编辑器输入小分子。"
                )
                
                input_method_changed = new_input_method != old_input_method
                
                component['input_method'] = new_input_method
                
                if input_method_changed:
                    component['sequence'] = ''
                    
                    method_display_names = {
                        "smiles": "SMILES 字符串",
                        "ccd": "PDB CCD 代码", 
                        "ketcher": "分子编辑器"
                    }
                    st.toast(f"输入方式已更新为 {method_display_names.get(new_input_method, new_input_method)}", icon="🔄")
                    
                    st.rerun()
                
                if new_input_method == 'smiles':
                    component['sequence'] = st.text_input(
                        f"SMILES 字符串 ({'单分子' if num_copies == 1 else f'{num_copies}个分子'})",
                        value=component.get('sequence', ''),
                        placeholder="例如: CC(=O)NC1=CC=C(C=C1)O",
                        key=f"designer_seq_{component['id']}",
                        disabled=designer_is_running
                    )
                elif new_input_method == 'ccd':
                    component['sequence'] = st.text_input(
                        f"CCD 代码 ({'单分子' if num_copies == 1 else f'{num_copies}个分子'})",
                        value=component.get('sequence', ''),
                        placeholder="例如: HEM, NAD, ATP",
                        key=f"designer_seq_{component['id']}",
                        disabled=designer_is_running
                    )
                else:  # ketcher
                    current_smiles = component.get('sequence', '')
                    smiles_from_ketcher = st_ketcher(
                        value=current_smiles,
                        key=f"designer_ketcher_{component['id']}",
                        height=400
                    )
                    
                    if smiles_from_ketcher is not None and smiles_from_ketcher != current_smiles:
                        st.session_state.designer_components[i]['sequence'] = smiles_from_ketcher
                        if smiles_from_ketcher:
                            st.toast("SMILES 字符串已成功更新！", icon="✅")
                        st.rerun()
                    
                    current_smiles_display = st.session_state.designer_components[i].get('sequence', '')
                    if current_smiles_display:
                        st.caption("✨ 当前 SMILES 字符串:")
                        st.code(current_smiles_display, language='smiles')
                    else:
                        st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")
        
        if designer_id_to_delete:
            st.session_state.designer_components = [c for c in st.session_state.designer_components if c['id'] != designer_id_to_delete]
            st.rerun()
        
        def add_new_designer_component():
            smart_msa_default = get_smart_msa_default(st.session_state.designer_components)
            default_use_msa = True if is_af3_backend else smart_msa_default
            st.session_state.designer_components.append({
                'id': str(uuid.uuid4()),
                'type': 'protein',
                'sequence': '',
                'num_copies': 1,
                'use_msa': default_use_msa
            })
        
        if st.button("➕ 添加新组分", key="add_new_component", disabled=designer_is_running, help="添加新的蛋白质、DNA/RNA或小分子组分"):
            add_new_designer_component()
            st.rerun()
        
        components_with_sequence = [
            comp for comp in st.session_state.designer_components
            if comp.get('sequence', '').strip()
        ]
        target_chain_ids, target_chain_descriptions = (
            get_available_chain_ids(components_with_sequence)
            if components_with_sequence else ([], {})
        )
        total_chains = len(target_chain_ids)

        if total_chains > 0:
            binder_chain_id = string.ascii_uppercase[total_chains] if total_chains < 26 else f"Z{total_chains-25}"
            target_chain_id = target_chain_ids[0] if target_chain_ids else 'A'
        else:
            target_chain_id = 'A'
            binder_chain_id = 'B'

        if total_chains > 0:
            st.subheader("🧭 界面链对选择", anchor=False)
            st.caption("用于运行前定义设计链与被结合链，并用于 pair ipTM 展示和默认约束目标。")

            chain_a_key = "designer_pair_chain_a"
            chain_b_key = "designer_pair_chain_b"
            cols_pair = st.columns(2)
            with cols_pair[0]:
                design_chain = st.selectbox(
                    "设计链",
                    options=[binder_chain_id],
                    key=chain_a_key,
                    format_func=lambda c: f"🎯 设计链 {c}",
                    disabled=True
                )

            chain_b_options = target_chain_ids
            if chain_b_options:
                if st.session_state.get(chain_b_key) not in chain_b_options:
                    st.session_state[chain_b_key] = chain_b_options[0]
                with cols_pair[1]:
                    target_chain_id = st.selectbox(
                        "被结合链",
                        options=chain_b_options,
                        key=chain_b_key,
                        format_func=lambda c: target_chain_descriptions.get(c, c),
                        disabled=designer_is_running
                    )
            st.session_state.designer_target_chain_id = target_chain_id
        else:
            if st.session_state.get("designer_pair_chain_a") not in [binder_chain_id]:
                st.session_state.designer_pair_chain_a = binder_chain_id
            if st.session_state.get("designer_pair_chain_b") not in [target_chain_id]:
                st.session_state.designer_pair_chain_b = target_chain_id
            st.session_state.designer_target_chain_id = target_chain_id

        backend_options = list(BACKEND_LABELS.keys())
        if current_backend not in backend_options:
            current_backend = 'boltz'
        backend_index = backend_options.index(current_backend)
        selected_backend = st.selectbox(
            "选择预测后端",
            backend_options,
            index=backend_index,
            format_func=lambda key: BACKEND_LABELS.get(key, key),
            disabled=designer_is_running,
            help="Boltz 引擎直接完成结构预测；AlphaFold3 引擎生成 AF3 归档并附带 AlphaFold3 预测指标。",
            key="designer_backend_select"
        )
        if selected_backend != current_backend:
            st.session_state.designer_backend = selected_backend
            if selected_backend == 'alphafold3':
                for comp in st.session_state.designer_components:
                    if comp.get('type') == 'protein':
                        comp['use_msa'] = True
            st.rerun()
        if selected_backend == 'alphafold3':
            st.info("AlphaFold3 后端：勾选 MSA 使用外部 MMseqs 结果，不勾选则跳过外部 MSA，直接使用 AlphaFold3 自带流程。", icon="ℹ️")
        
        st.subheader("🔗 分子约束 (可选)", anchor=False)
        st.markdown("设置分子结构约束，包括键约束、口袋约束和接触约束。")
        
        # 移除链ID说明信息
        # st.info(f"💡 **可用链ID说明**: 目标分子链已分配，设计的结合肽将分配到链 **{binder_chain_id}**。在约束设置中，您可以使用 **'BINDER_CHAIN'** 作为占位符来引用即将生成的结合肽。", icon="🧬")
        
        constraint_id_to_delete = None
        for i, constraint in enumerate(st.session_state.designer_constraints[:]):
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
                    constraint_type = st.selectbox(
                        "选择约束类型",
                        options=['contact', 'bond', 'pocket'],
                        format_func=lambda x: {
                            'contact': '📍 Contact - 接触约束 (两个残基间距离)',
                            'bond': '🔗 Bond - 键约束 (两个原子间共价键)',
                            'pocket': '🕳️ Pocket - 口袋约束 (结合肽与特定口袋的结合)'
                        }[x],
                        index=['contact', 'bond', 'pocket'].index(constraint.get('type', 'contact')) if constraint.get('type', 'contact') in ['contact', 'bond', 'pocket'] else 0,
                        key=f"designer_constraint_type_{i}",
                        disabled=designer_is_running,
                        help="选择约束的类型：接触距离、共价键或口袋结合"
                    )
                    
                    if constraint_type != constraint.get('type', 'contact'):
                        constraint['type'] = constraint_type
                        if constraint_type == 'bond':
                            constraint.pop('binder', None)
                            constraint.pop('contacts', None)
                            constraint.pop('token1_chain', None)
                            constraint.pop('token1_residue', None)
                            constraint.pop('token2_chain', None)
                            constraint.pop('token2_residue', None)
                            constraint.pop('max_distance', None)
                            constraint.pop('force', None)
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
                            # 初始化pocket约束的默认值
                            if 'binder' not in constraint:
                                constraint['binder'] = 'BINDER_CHAIN'
                            if 'contacts' not in constraint:
                                constraint['contacts'] = [['A', 1], ['A', 2]]
                            if 'max_distance' not in constraint:
                                constraint['max_distance'] = 5.0
                            if 'force' not in constraint:
                                constraint['force'] = False
                        st.rerun()
                    
                    available_chains, chain_descriptions = get_available_chain_ids_for_designer(st.session_state.designer_components, binder_chain_id)
                    
                    st.markdown("---")
                    
                    if constraint_type == 'contact':
                        render_contact_constraint_ui(constraint, f"designer_{i}", available_chains, chain_descriptions, designer_is_running, st.session_state.designer_components)
                    elif constraint_type == 'bond':
                        render_bond_constraint_ui(constraint, f"designer_{i}", available_chains, chain_descriptions, designer_is_running, st.session_state.designer_components)
                    elif constraint_type == 'pocket':
                        render_pocket_constraint_ui(constraint, f"designer_{i}", available_chains, chain_descriptions, designer_is_running, st.session_state.designer_components)
                
                with col2:
                    if st.button("🗑️", key=f"designer_del_constraint_{i}", help="删除此约束", disabled=designer_is_running):
                        constraint_id_to_delete = i
        
        if constraint_id_to_delete is not None:
            del st.session_state.designer_constraints[constraint_id_to_delete]
            st.rerun()
        
        st.markdown("---")
        add_constraint_cols = st.columns(3)
        
        with add_constraint_cols[0]:
            if st.button("➕ 添加 Contact 约束", key="add_designer_contact_constraint", disabled=designer_is_running, help="添加接触距离约束"):
                st.session_state.designer_constraints.append({
                    'type': 'contact',
                    'token1_chain': target_chain_id,  # 默认指向目标链
                    'token1_residue': 1,
                    'token2_chain': 'BINDER_CHAIN',  # 使用占位符指向结合肽
                    'token2_residue': 1,
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
        
        with add_constraint_cols[1]:
            if st.button("➕ 添加 Bond 约束", key="add_designer_bond_constraint", disabled=designer_is_running, help="添加共价键约束"):
                st.session_state.designer_constraints.append({
                    'type': 'bond',
                    'atom1_chain': target_chain_id,  # 默认指向目标链
                    'atom1_residue': 1,
                    'atom1_atom': 'CA',
                    'atom2_chain': 'BINDER_CHAIN',  # 使用占位符指向结合肽
                    'atom2_residue': 1,
                    'atom2_atom': 'CA'
                })
                st.rerun()
        
        with add_constraint_cols[2]:
            if st.button("➕ 添加 Pocket 约束", key="add_designer_pocket_constraint", disabled=designer_is_running, help="添加口袋结合约束"):
                st.session_state.designer_constraints.append({
                    'type': 'pocket',
                    'binder': 'BINDER_CHAIN',
                    'contacts': [[target_chain_id, 1], [target_chain_id, 2]],
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
        
        if st.session_state.designer_constraints:
            constraint_count = len(st.session_state.designer_constraints)
            constraint_types = {c.get('type', 'contact'): 0 for c in st.session_state.designer_constraints}
            for c in st.session_state.designer_constraints:
                constraint_types[c.get('type', 'contact')] += 1
            
            constraint_type_names = {'contact': 'Contact', 'bond': 'Bond', 'pocket': 'Pocket'}
            type_summary = ', '.join([f"{count}个{constraint_type_names.get(ctype, ctype)}" 
                                    for ctype, count in constraint_types.items()])
            st.info(f"💡 已配置 {constraint_count} 个约束：{type_summary}")
        else:
            st.info("💡 暂无约束。可根据需要添加Contact、Bond或Pocket约束。")
        
        st.markdown("---")
        
        st.subheader("🎯 设计参数", anchor=False)
        
        # 简化设计参数设置
        with st.expander("📝 **基本设置**", expanded=True):
            col1, col2, col3 = st.columns(3)
            
            with col1:
                # Persist binder length to avoid resetting dependent fields on rerun.
                default_binder_length = st.session_state.get("designer_binder_length", 20)
                binder_length = st.number_input(
                    "结合肽长度",
                    min_value=5,
                    max_value=50,
                    value=default_binder_length,
                    step=1,
                    help="设计的结合肽的氨基酸残基数量。",
                    disabled=designer_is_running,
                    key="designer_binder_length",
                )
            
            with col2:
                cyclic_disabled = designer_is_running or is_af3_backend
                cyclic_checkbox_value = st.checkbox(
                    "环状结构",
                    value=False,
                    help="勾选此项将设计的结合肽设计为环状肽，具有闭合的环状结构。",
                    disabled=cyclic_disabled,
                    key="designer_cyclic_binder"
                )
                if is_af3_backend:
                    st.caption("AlphaFold3 后端暂不支持环肽设计，此选项已禁用。")
                cyclic_binder = False if is_af3_backend else cyclic_checkbox_value
            
            with col3:
                if design_type_selector == "glycopeptide":
                    glycan_options = {
                        "NAGS": "NAG-Ser (N-乙酰葡糖胺-丝氨酸)",
                        "NAGT": "NAG-Thr (N-乙酰葡糖胺-苏氨酸)",
                        "NAGN": "NAG-Asn (N-乙酰葡糖胺-天冬酰胺)",
                        "MANS": "MAN-Ser (甘露糖-丝氨酸)",
                        "MANT": "MAN-Thr (甘露糖-苏氨酸)",
                        "GALS": "GAL-Ser (半乳糖-丝氨酸)",
                        "GALT": "GAL-Thr (半乳糖-苏氨酸)"
                    }
                    
                    glycan_type = st.selectbox(
                        "糖基类型",
                        options=["请选择..."] + list(glycan_options.keys()),
                        format_func=lambda x: glycan_options[x] if x in glycan_options else x,
                        index=0,
                        help="选择要使用的糖基修饰类型。",
                        disabled=designer_is_running
                    )
                    
                    if glycan_type != "请选择..." and glycan_type in glycan_options:
                        glycosylation_site = st.number_input(
                            "糖基化位点",
                            min_value=1,
                            max_value=binder_length,
                            value=min(5, binder_length),
                            step=1,
                            help=f"肽链上用于应用糖基修饰的氨基酸位置 (1-{binder_length})。",
                            disabled=designer_is_running
                        )
                    else:
                        glycan_type = None
                        glycosylation_site = None
                else:
                    glycan_type = None
                    glycosylation_site = None
                    st.write("")  # 占位符
        
        # 高级设置 - 默认折叠
        with st.expander("⚙️ **高级设置** (可选)", expanded=False):
            st.markdown("**🧬 初始序列设置**")
            use_initial_sequence = st.checkbox(
                "使用初始序列作为演化起点",
                value=False,
                help="启用后可以提供一个初始序列作为演化算法的起点，而不是完全随机生成。",
                disabled=designer_is_running
            )
            
            initial_sequence = None
            if use_initial_sequence:
                # Keep user input across binder-length changes instead of clearing it.
                if "designer_initial_sequence" not in st.session_state:
                    st.session_state.designer_initial_sequence = ""
                stored_init = st.session_state.get("designer_initial_sequence", "")
                initial_sequence = st.text_input(
                    "初始序列",
                    value=stored_init,
                    placeholder="例如: MVSKGEELFTGVVPILVELD...",
                    help=f"输入初始氨基酸序列。长度应该等于结合肽长度({binder_length})。",
                    disabled=designer_is_running,
                    key="designer_initial_sequence",
                )

                if initial_sequence:
                    seq_len = len(initial_sequence)
                    if seq_len != binder_length:
                        if seq_len < binder_length:
                            st.warning(f"⚠️ 初始序列长度({seq_len})小于目标长度({binder_length})，将随机补全。")
                        else:
                            st.warning(f"⚠️ 初始序列长度({seq_len})大于目标长度({binder_length})，将截取前{binder_length}个氨基酸。")
                    else:
                        st.success(f"✅ 初始序列长度匹配。")

            sequence_mask = st.text_input(
                "序列掩码",
                placeholder="例如: X-A-X-L-X-X-X-P-X-X",
                help="指定固定位置的氨基酸。格式: 'X-A-X-L-X'，其中X表示可变位置，字母表示固定氨基酸。长度必须与肽链长度匹配。支持使用'-'、'_'或空格作为分隔符。",
                key="designer_sequence_mask"
            )
            
            if sequence_mask and sequence_mask.strip():
                # 验证sequence_mask格式
                mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
                if len(mask_clean) != binder_length:
                    st.error(f"❌ 序列掩码长度 ({len(mask_clean)}) 与肽链长度 ({binder_length}) 不匹配。")
                else:
                    # 验证字符是否有效
                    valid_chars = set('ACDEFGHIKLMNPQRSTVWYX')
                    invalid_chars = set(mask_clean) - valid_chars
                    if invalid_chars:
                        st.error(f"❌ 序列掩码包含无效字符: {invalid_chars}。只允许标准氨基酸字符和X（表示可变位置）。")
                    else:
                        fixed_positions = [(i, char) for i, char in enumerate(mask_clean) if char != 'X']
                        if fixed_positions:
                            pos_info = ', '.join([f"位置{i+1}={char}" for i, char in fixed_positions])
                            st.success(f"✅ 序列掩码有效。固定位置: {pos_info}")
                        else:
                            st.info("ℹ️ 序列掩码中所有位置都是可变的。")
            else:
                sequence_mask = ""  # 确保为空字符串而不是None
            
            st.markdown("**🚀 演化算法参数**")
            optimization_mode = st.selectbox(
                "优化策略",
                options=["balanced", "stable", "aggressive", "conservative"],
                format_func=lambda x: {
                    "balanced": "⚖️ 平衡模式 (推荐)",
                    "stable": "� 平稳优化",
                    "aggressive": "🔥 激进探索", 
                    "conservative": "🛡️ 保守设计"
                }[x],
                index=0,
                help="选择预设的优化策略。平衡模式适用于大多数设计任务。",
                disabled=designer_is_running
            )
            
            mode_descriptions = {
                "balanced": "⚖️ **平衡模式**: 综合考虑探索性和收敛性，适用于大多数设计任务。",
                "stable": "🎯 **平稳优化**: 稳定收敛，减少分数波动，适用于需要可重复结果的场景。",
                "aggressive": "🔥 **激进探索**: 快速突破局部最优，适用于初始分数较低的场景。",
                "conservative": "🛡️ **保守设计**: 小步优化，适用于已有较好序列的场景。"
            }
            st.info(mode_descriptions[optimization_mode])
            
            col_adv1, col_adv2, col_adv3 = st.columns(3)
            
            with col_adv1:
                generations = st.number_input(
                    "演化代数",
                    min_value=2,
                    max_value=20,
                    value=12,
                    step=1,
                    help="演化算法的迭代次数。",
                    disabled=designer_is_running
                )
            
            with col_adv2:
                population_size = st.number_input(
                    "种群大小",
                    min_value=2,
                    max_value=50,
                    value=16,
                    step=1,
                    help="每一代中的候选序列数量。",
                    disabled=designer_is_running
                )
            
            with col_adv3:
                max_elite_size = min(10, max(1, population_size//2))
                default_elite_size = max(1, min(max_elite_size, min(5, max(1, population_size//3))))
                
                elite_size = st.number_input(
                    "精英保留数",
                    min_value=1,
                    max_value=max_elite_size,
                    value=default_elite_size,
                    step=1,
                    help="每一代中保留的最优个体数量。",
                    disabled=designer_is_running
                )
            
            mutation_rate = st.slider(
                "突变率",
                min_value=0.1,
                max_value=0.8,
                value=0.3,
                step=0.05,
                help="每一代中发生突变的概率。",
                disabled=designer_is_running
            )
            
            # 设置预设参数
            preset_params = {
                "balanced": {
                    "convergence_window": 5,
                    "convergence_threshold": 0.001,
                    "max_stagnation": 3,
                    "initial_temperature": 1.0,
                    "min_temperature": 0.1,
                    "enable_enhanced": True
                },
                "stable": {
                    "convergence_window": 5,
                    "convergence_threshold": 0.001,
                    "max_stagnation": 3,
                    "initial_temperature": 1.0,
                    "min_temperature": 0.1,
                    "enable_enhanced": True
                },
                "aggressive": {
                    "convergence_window": 3,
                    "convergence_threshold": 0.002,
                    "max_stagnation": 2,
                    "initial_temperature": 2.0,
                    "min_temperature": 0.2,
                    "enable_enhanced": True
                },
                "conservative": {
                    "convergence_window": 6,
                    "convergence_threshold": 0.0005,
                    "max_stagnation": 5,
                    "initial_temperature": 0.5,
                    "min_temperature": 0.05,
                    "enable_enhanced": True
                }
            }
            
            params = preset_params[optimization_mode]
            convergence_window = params["convergence_window"]
            convergence_threshold = params["convergence_threshold"]
            max_stagnation = params["max_stagnation"]
            initial_temperature = params["initial_temperature"]
            min_temperature = params["min_temperature"]
            enable_enhanced = params["enable_enhanced"]
        
        # 添加半胱氨酸控制选项 - 根据设计类型智能控制
        st.subheader("🧪 氨基酸组成控制", anchor=False)
        
        # 根据设计类型确定默认值和是否禁用
        if design_type_selector == "peptide":
            # 多肽设计：用户可选择，默认不包含（避免复杂的二硫键）
            cys_default = False
            cys_disabled = False
            cys_help = "是否在多肽设计中包含半胱氨酸(Cys)。建议禁用以避免不必要的二硫键形成。"
        elif design_type_selector == "glycopeptide":
            # 糖肽设计：用户可选择，默认不包含（糖基修饰已提供稳定性）
            cys_default = False
            cys_disabled = False
            cys_help = "是否在糖肽设计中包含半胱氨酸(Cys)。糖基修饰已提供额外稳定性，建议禁用半胱氨酸。"
        else:
            # 默认情况
            cys_default = False
            cys_disabled = False
            cys_help = "是否在设计的序列中包含半胱氨酸(Cys)。取消勾选将避免生成含有半胱氨酸的序列。"
        
        col_cys, col_cys_desc = st.columns([1, 2])
        
        with col_cys:
            include_cysteine = st.checkbox(
                "包含半胱氨酸",
                value=cys_default,
                help=cys_help,
                disabled=designer_is_running or cys_disabled,
                key="designer_include_cysteine"
            )
        
        with col_cys_desc:
            if include_cysteine:
                if design_type_selector == "peptide":
                    st.info("✅ 允许使用半胱氨酸(C)，可形成二硫键增强结构稳定性")
                    st.caption("💡 提示：多肽中的半胱氨酸可能形成复杂的二硫键网络")
                elif design_type_selector == "glycopeptide":
                    st.info("✅ 允许使用半胱氨酸(C)，与糖基修饰协同增强稳定性")
                    st.caption("💡 提示：糖肽中的半胱氨酸可提供额外的结构约束")
            else:
                if design_type_selector == "peptide":
                    st.warning("⚠️ 禁用半胱氨酸(C)，避免不必要的二硫键形成")
                    st.caption("推荐：多肽设计中禁用半胱氨酸可简化结构并避免错误折叠")
                    # with st.expander("📝 说明：何时仍可能出现C"):
                    #     st.write("""
                    #     即使禁用半胱氨酸，以下情况仍可能出现C：
                    #     • **初始序列**：如果提供的初始序列包含C
                    #     • **序列掩码**：如果序列掩码中固定了C位置  
                    #     • **目标蛋白质**：设计目标的蛋白质序列可能包含C（这不会影响设计的结合肽）
                        
                    #     ✅ **设计生成的新序列**中不会包含C
                    #     """)
                elif design_type_selector == "glycopeptide":
                    st.warning("⚠️ 禁用半胱氨酸(C)，依靠糖基修饰提供稳定性")
                    st.caption("推荐：糖肽的糖基修饰已提供足够稳定性，无需额外二硫键")
                    # with st.expander("📝 说明：何时仍可能出现C"):
                    #     st.write("""
                    #     即使禁用半胱氨酸，以下情况仍可能出现C：
                    #     • **初始序列**：如果提供的初始序列包含C
                    #     • **序列掩码**：如果序列掩码中固定了C位置
                    #     • **目标蛋白质**：设计目标的蛋白质序列可能包含C（这不会影响设计的结合肽）
                        
                    #     ✅ **设计生成的新序列**中不会包含C
                    #     """)
            
            # # 添加双环肽设计的提示链接
            # st.info("💡 需要双环肽设计？请使用专门的[双环肽设计器](/bicyclic_designer)页面")
    
    # 检查输入验证
    designer_is_valid, validation_message = validate_designer_inputs(st.session_state.designer_components)
    
    if design_type_selector == "glycopeptide":
        if not glycan_type:
            designer_is_valid = False
            validation_message = "糖肽设计模式需要选择糖基类型。"
        elif not glycosylation_site or glycosylation_site < 1 or glycosylation_site > binder_length:
            designer_is_valid = False
            validation_message = f"糖基化位点必须在 1 到 {binder_length} 范围内。"
    
    if use_initial_sequence:
        if not initial_sequence or not initial_sequence.strip():
            designer_is_valid = False
            validation_message = "启用初始序列时必须提供有效的氨基酸序列。"
        else:
            valid_amino_acids = set("ACDEFGHIKLMNPQRSTVWY")
            invalid_chars = set(initial_sequence.upper()) - valid_amino_acids
            if invalid_chars:
                designer_is_valid = False
                validation_message = f"初始序列包含无效字符: {', '.join(invalid_chars)}。请只使用标准的20种氨基酸字母。"
    
    # 设置目标链ID（用于约束默认值和结果展示）
    target_chain_id = st.session_state.get('designer_target_chain_id', target_chain_id)
    
    if st.button("🚀 开始分子设计", key="start_designer", type="primary", disabled=(not designer_is_valid or designer_is_running), use_container_width=True):
        st.session_state.designer_task_id = None
        st.session_state.designer_results = None
        st.session_state.designer_error = None
        
        with st.spinner("⏳ 正在启动设计任务，请稍候..."):
            try:
                any_msa_enabled = any(comp.get('use_msa', True) for comp in st.session_state.designer_components 
                                    if comp['type'] == 'protein' and comp.get('sequence', '').strip())
                
                template_yaml = create_designer_complex_yaml(
                    st.session_state.designer_components, 
                    use_msa=any_msa_enabled,
                    constraints=None  # 不在模板中添加约束，让后端处理
                )
                
                # 映射UI的design_type到实际命令行参数
                actual_design_type = "linear" if design_type_selector == "peptide" else design_type_selector
                
                result = submit_designer_job(
                    template_yaml_content=template_yaml,
                    design_type=actual_design_type,
                    binder_length=binder_length,
                    target_chain_id=target_chain_id,
                    generations=generations,
                    population_size=population_size,
                    elite_size=elite_size,
                    mutation_rate=mutation_rate,
                    glycan_type=glycan_type,
                    glycosylation_site=glycosylation_site,
                    convergence_window=convergence_window,
                    convergence_threshold=convergence_threshold,
                    max_stagnation=max_stagnation,
                    initial_temperature=initial_temperature,
                    min_temperature=min_temperature,
                    enable_enhanced=enable_enhanced,
                    use_initial_sequence=use_initial_sequence,
                    initial_sequence=initial_sequence if use_initial_sequence else None,
                    sequence_mask=sequence_mask,
                    cyclic_binder=cyclic_binder,
                    include_cysteine=include_cysteine,
                    use_msa=any_msa_enabled,
                    user_constraints=st.session_state.designer_constraints,  # 新增：传递用户约束
                    backend=st.session_state.designer_backend
                )
                
                if result['success']:
                    st.session_state.designer_task_id = result['task_id']
                    st.session_state.designer_work_dir = result['work_dir']
                    st.session_state.designer_config = result['params']
                    st.session_state.designer_backend = result['params'].get('backend', st.session_state.designer_backend)
                    st.session_state.designer_config['backend'] = st.session_state.designer_backend
                    
                    # 确保session state中保存的是实际的design_type
                    if 'design_type' in st.session_state.designer_config:
                        st.session_state.designer_config['design_type'] = actual_design_type
                    
                    # 更新URL参数以保持设计任务状态和配置
                    URLStateManager.update_url_for_designer_task(
                        task_id=result['task_id'], 
                        work_dir=result['work_dir'],
                        components=st.session_state.designer_components,
                        constraints=st.session_state.designer_constraints,
                        config=st.session_state.designer_config,
                        backend=st.session_state.designer_backend
                    )
                    
                    st.toast(f"🎉 设计任务已成功启动！任务ID: {result['task_id']}", icon="🎉")
                    st.rerun()
                else:
                    st.error(f"❌ **任务启动失败**：{result['error']}")
                    st.session_state.designer_error = {"error_message": result['error'], "type": "Task Start Error"}
                    
            except Exception as e:
                st.error(f"❌ **任务启动失败：发生未知错误**。详情: {e}")
                st.session_state.designer_error = {"error_message": str(e), "type": "Client Error"}
    
    if not designer_is_valid and not designer_is_running:
        has_user_input = any(comp.get('sequence', '').strip() for comp in st.session_state.designer_components)
        if has_user_input:
            st.error(f"⚠️ **无法启动设计**: {validation_message}")
    
    if st.session_state.designer_task_id and not st.session_state.designer_results:
        st.divider()
        
        col_title, col_stop = st.columns([3, 2])
        with col_title:
            st.header("🔄 **步骤 2: 设计进度监控**", anchor=False)
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
            
            if st.button("🛑 紧急停止", 
                        type="secondary", 
                        use_container_width=True, 
                        help="安全终止正在进行的设计任务，已完成的工作将被保存",
                        key="stop_design_btn"):
                try:
                    try:
                        from designer.design_manager import design_manager
                        
                        # Ensure the design_manager has the correct process info
                        work_dir = st.session_state.get('designer_work_dir', None)
                        if work_dir:
                            status_file = os.path.join(work_dir, 'status.json')
                            if os.path.exists(status_file):
                                with open(status_file, 'r') as f:
                                    status_info = json.load(f)
                                    process_id = status_info.get('process_id')
                                    if process_id:
                                        design_manager.set_current_process_info(process_id, status_file)
                                    else:
                                        st.warning("⚠️ 无法获取进程ID，可能无法优雅停止。")
                            else:
                                st.warning("⚠️ 任务状态文件不存在，可能无法优雅停止。")
                        else:
                            st.warning("⚠️ 任务工作目录不存在，可能无法优雅停止。")

                        graceful_stop_success = design_manager.stop_current_design()
                        if graceful_stop_success:
                            st.info("🔄 已发送停止信号，等待任务终止...")
                            # Clear session state to reflect the stop
                            st.session_state.designer_task_id = None
                            st.session_state.designer_work_dir = None
                            st.session_state.designer_results = None
                            st.session_state.designer_error = {"error_message": "用户手动停止任务", "type": "User Cancelled"}
                            st.rerun()
                        else:
                            st.error("❌ 停止设计任务失败。")
                    except Exception as e:
                        st.error(f"❌ 停止任务时发生错误: {e}")
                        
                except Exception as e:
                    st.error(f"❌ 停止任务时发生错误: {e}")
        
        if not st.session_state.designer_error:
            try:
                work_dir = st.session_state.get('designer_work_dir', None)
                status_data = get_designer_status(st.session_state.designer_task_id, work_dir)
                
                if not status_data or 'state' not in status_data:
                    st.error("❌ 无法获取任务状态信息，任务可能已失败或被中断")
                    st.session_state.designer_error = {"error_message": "无法获取任务状态", "type": "Status Error"}
                elif status_data.get('error'):
                    st.error(f"❌ 任务执行错误: {status_data['error']}")
                    st.session_state.designer_error = {"error_message": status_data['error'], "type": "Task Error"}
                else:
                    current_state = status_data['state']
                    
                    if current_state in ['COMPLETED', 'SUCCESS']:
                        st.success("🎉 设计任务已完成！正在加载结果...")
                        try:
                            work_dir = st.session_state.get('designer_work_dir', '/tmp')
                            results = load_designer_results(st.session_state.designer_task_id, work_dir)
                            st.session_state.designer_results = results
                            st.toast("设计任务已完成！", icon="🎊")
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ 加载结果时发生错误: {e}")
                            st.session_state.designer_error = {"error_message": str(e), "type": "Result Loading Error"}
                    
                    elif current_state in ['ERROR', 'FAILED', 'CANCELLED']:
                        error_msg = status_data.get('error', status_data.get('message', '任务失败，原因未知'))
                        st.error(f"❌ 设计任务失败: {error_msg}")
                        st.session_state.designer_error = {"error_message": error_msg, "type": "Task Error"}
                    
                    elif current_state == 'RUNNING':
                        progress = status_data.get('progress', {})
                        progress_value = min(1.0, max(0.0, progress.get('estimated_progress', 0.0)))
                        status_msg = progress.get('status_message', '设计进行中')
                        current_status = progress.get('current_status', 'unknown')
                        
                        if current_status == 'waiting_for_prediction':
                            pending = progress.get('pending_tasks', 0)
                            completed = progress.get('completed_tasks', 0)
                            total_tasks = pending + completed
                            if total_tasks > 0:
                                task_progress = completed / total_tasks
                                st.progress(task_progress, text=f"结构预测进度: {completed}/{total_tasks} 任务完成")
                            else:
                                st.progress(0.1, text="正在初始化结构预测任务...")
                            st.info(f"🔬 {status_msg}")
                        
                        elif current_status == 'evolving':
                            current_gen = progress.get('current_generation', 0)
                            total_gen = progress.get('total_generations', 1)
                            best_score = progress.get('best_score', 0.0)
                            debug_info = progress.get('debug_info', {})
                            
                            current_best_sequences = progress.get('current_best_sequences', [])
                            
                            task_completed = False
                            
                            if current_gen > total_gen and total_gen > 0:
                                task_completed = True
                            elif current_gen == total_gen and total_gen > 0 and current_best_sequences:
                                try:
                                    work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                    log_file = os.path.join(work_dir, 'design.log')
                                    if os.path.exists(log_file):
                                        with open(log_file, 'r') as f:
                                            log_content = f.read()
                                            if any(indicator in log_content for indicator in [
                                                'Design Run Finished', 
                                                '设计任务完成', 
                                                'Successfully created results package',
                                                'All generations completed',
                                                f'Finished all {total_gen} generations'
                                            ]):
                                                task_completed = True
                                            else:
                                                csv_files = [f for f in os.listdir(work_dir) 
                                                           if f.startswith('design_summary_') and f.endswith('.csv')]
                                                for csv_file in csv_files:
                                                    csv_path = os.path.join(work_dir, csv_file)
                                                    if os.path.exists(csv_path):
                                                        file_age = time.time() - os.path.getmtime(csv_path)
                                                        if file_age < 15:
                                                            try:
                                                                df = pd.read_csv(csv_path)
                                                                if len(df) > 0:
                                                                    max_gen_in_csv = df['generation'].max() if 'generation' in df.columns else 0
                                                                    if max_gen_in_csv >= total_gen:
                                                                        task_completed = True
                                                                        break
                                                            except:
                                                                pass
                                except Exception:
                                    pass
                            
                            if not task_completed:
                                try:
                                    work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                    status_file_path = os.path.join(work_dir, 'status.json')
                                    design_process_running = False
                                    
                                    if os.path.exists(status_file_path):
                                        with open(status_file_path, 'r') as f:
                                            status_info = json.load(f)
                                            saved_pid = status_info.get('process_id')
                                            
                                            if saved_pid and psutil:
                                                try:
                                                    if psutil.pid_exists(saved_pid):
                                                        proc = psutil.Process(saved_pid)
                                                        cmdline = proc.cmdline()
                                                        if cmdline and 'run_design.py' in ' '.join(cmdline):
                                                            design_process_running = True
                                                except (psutil.NoSuchProcess, psutil.AccessDenied):
                                                    design_process_running = False
                                    
                                    if not design_process_running and current_best_sequences:
                                        csv_files = [f for f in os.listdir(work_dir) 
                                                   if f.startswith('design_summary_') and f.endswith('.csv')]
                                        for csv_file in csv_files:
                                            csv_path = os.path.join(work_dir, csv_file)
                                            if os.path.exists(csv_path):
                                                file_age = time.time() - os.path.getmtime(csv_path)
                                                if file_age < 30:
                                                    task_completed = True
                                                    break
                                except Exception:
                                    pass
                            
                            if task_completed:
                                st.success("🎉 设计任务已完成！正在加载最终结果...")
                                try:
                                    work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                    results = load_designer_results(st.session_state.designer_task_id, work_dir)
                                    st.session_state.designer_results = results
                                    st.toast("设计任务已完成！", icon="🎊")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"❌ 加载结果时发生错误: {e}")
                                    st.session_state.designer_error = {"error_message": str(e), "type": "Result Loading Error"}
                            else:
                                if current_gen > 0:
                                    gen_progress = min(current_gen / total_gen, 1.0)
                                    
                                    st.progress(gen_progress, text=f"演化进度: 第 {current_gen}/{total_gen} 代 | 当前最佳评分: {best_score:.3f}")
                                    
                                    if current_gen == total_gen:
                                        st.info("🧬 正在完成最后一代演化，请稍候...")
                                else:
                                    st.progress(0.0, text="准备开始演化...")
                                
                                st.info(f"🧬 {status_msg}")
                                
                                if current_best_sequences:
                                    display_gen = max(1, current_gen)
                                    with st.expander(f"🏆 当前最佳序列 (第 {display_gen} 代)", expanded=True):
                                        for i, seq_info in enumerate(current_best_sequences[:3]):
                                            rank = i + 1
                                            score = seq_info.get('score', 0)
                                            sequence = seq_info.get('sequence', '')
                                            iptm = seq_info.get('iptm', 0)
                                            plddt = seq_info.get('plddt', 0)
                                            generation = seq_info.get('generation', display_gen)
                                            
                                            if not sequence or len(sequence) < 8:
                                                continue
                                            
                                            if score >= 0.8:
                                                score_color = "🟢"
                                            elif score >= 0.7:
                                                score_color = "🟡"
                                            elif score >= 0.6:
                                                score_color = "🟠"
                                            else:
                                                score_color = "🔴"
                                            
                                            st.markdown(f"**#{rank}** {score_color} 综合评分: {score:.3f} | ipTM: {iptm:.3f} | pLDDT: {plddt:.1f} | 代数: {generation}")
                                            
                                            designer_config = st.session_state.get('designer_config', {})
                                            if designer_config.get('design_type') == 'glycopeptide':
                                                glycan_type = designer_config.get('glycan_type')
                                                glycosylation_site = designer_config.get('glycosylation_site')
                                                
                                                if glycan_type and glycosylation_site and 1 <= glycosylation_site <= len(sequence):
                                                    glycan_info_map = {
                                                        "NAGS": "S", "NAGT": "T", "NAGN": "N", "NAGY": "Y",
                                                        "MANS": "S", "MANT": "T", "MANN": "N", "MANY": "Y",
                                                        "GALS": "S", "GALT": "T", "GALN": "N", "GALY": "Y",
                                                        "FUCS": "S", "FUCT": "T", "FUCN": "N", "FUCY": "Y",
                                                        "NANS": "S", "NANT": "T", "NANN": "N", "NANY": "Y",
                                                        "GLCS": "S", "GLCT": "T", "GLCN": "N", "GLCY": "Y"
                                                    }
                                                    
                                                    expected_aa = glycan_info_map.get(glycan_type, glycan_type[-1])
                                                    glycan_base = glycan_type[:3]
                                                    
                                                    modified_sequence_parts = list(sequence)
                                                    modified_sequence_parts[glycosylation_site - 1] = f"{expected_aa}({glycan_base})"
                                                    
                                                    if glycosylation_site < len(sequence):
                                                        modified_sequence_display = "".join(modified_sequence_parts[:glycosylation_site]) + "-" + "".join(modified_sequence_parts[glycosylation_site:])
                                                    else:
                                                        modified_sequence_display = "".join(modified_sequence_parts)
                                                    
                                                    st.code(modified_sequence_display, language="text")
                                                else:
                                                    st.code(sequence, language="text")
                                            else:
                                                st.code(sequence, language="text")
                                        
                                        if len(current_best_sequences) > 3:
                                            st.caption(f"还有 {len(current_best_sequences) - 3} 个候选序列...")
                                else:
                                    st.caption("💡 当前代最佳序列将在演化过程中显示")
                        
                        elif current_status == 'processing_results':
                            st.progress(0.9, text="正在处理和分析结果...")
                            st.info(f"⚙️ {status_msg}")
                        
                        else:
                            st.progress(progress_value, text=f"设计进度: {int(progress_value * 100)}%")
                            st.info(f"🔄 {status_msg}")
                        
                        countdown_placeholder = st.empty()
                        for remaining in range(10, 0, -1):
                            countdown_placeholder.caption(f"🔄 将在 {remaining} 秒后自动刷新...")
                            time.sleep(1)
                        
                        st.rerun()
                    
                    else:
                        progress = status_data.get('progress', {})
                        current_gen = progress.get('current_generation', 0)
                        total_gen = progress.get('total_generations', 1)
                        csv_sequences = progress.get('current_best_sequences', [])
                        
                        task_likely_completed = False
                        
                        if current_gen > total_gen and total_gen > 0:
                            task_likely_completed = True
                        elif current_gen == total_gen and total_gen > 0 and csv_sequences:
                            try:
                                work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                log_file = os.path.join(work_dir, 'design.log')
                                if os.path.exists(log_file):
                                    with open(log_file, 'r') as f:
                                        log_content = f.read()
                                        if any(indicator in log_content for indicator in [
                                            'Design Run Finished', 
                                            '设计任务完成', 
                                            'Successfully created results package',
                                            'All generations completed',
                                            f'Finished all {total_gen} generations'
                                        ]):
                                            task_likely_completed = True
                                        else:
                                            csv_files = [f for f in os.listdir(work_dir) 
                                                       if f.startswith('design_summary_') and f.endswith('.csv')]
                                            for csv_file in csv_files:
                                                csv_path = os.path.join(work_dir, csv_file)
                                                if os.path.exists(csv_path):
                                                    file_age = time.time() - os.path.getmtime(csv_path)
                                                    if file_age < 15:
                                                        try:
                                                            df = pd.read_csv(csv_path)
                                                            if len(df) > 0:
                                                                max_gen_in_csv = df['generation'].max() if 'generation' in df.columns else 0
                                                                if max_gen_in_csv >= total_gen:
                                                                    task_likely_completed = True
                                                                    break
                                                        except:
                                                            pass
                            except Exception:
                                pass
                        
                        if not task_likely_completed:
                            try:
                                work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                status_file_path = os.path.join(work_dir, 'status.json')
                                design_process_running = False
                                
                                if os.path.exists(status_file_path):
                                    with open(status_file_path, 'r') as f:
                                        status_info = json.load(f)
                                        saved_pid = status_info.get('process_id')
                                        
                                        if saved_pid and psutil:
                                            try:
                                                if psutil.pid_exists(saved_pid):
                                                    proc = psutil.Process(saved_pid)
                                                    cmdline = proc.cmdline()
                                                    if cmdline and 'run_design.py' in ' '.join(cmdline):
                                                        design_process_running = True
                                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                                design_process_running = False
                                
                                if not design_process_running and csv_sequences:
                                    csv_files = [f for f in os.listdir(work_dir) 
                                               if f.startswith('design_summary_') and f.endswith('.csv')]
                                    for csv_file in csv_files:
                                        csv_path = os.path.join(work_dir, csv_file)
                                        if os.path.exists(csv_path):
                                            file_age = time.time() - os.path.getmtime(csv_path)
                                            if file_age < 30:
                                                task_likely_completed = True
                                                break
                            except Exception:
                                pass
                        
                        if task_likely_completed:
                            st.success("🎉 设计任务已完成！正在加载结果...")
                            try:
                                work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                results = load_designer_results(st.session_state.designer_task_id, work_dir)
                                st.session_state.designer_results = results
                                st.toast("设计任务已完成！", icon="🎊")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ 加载结果时发生错误: {e}")
                                st.session_state.designer_error = {"error_message": str(e), "type": "Result Loading Error"}
                        else:
                            st.info(f"🕒 设计任务状态: {current_state}，正在检查完成状态...")
                            if current_gen > 0 and total_gen > 0:
                                st.caption(f"当前进度: 第 {current_gen}/{total_gen} 代")
                            if csv_sequences:
                                st.caption(f"已找到 {len(csv_sequences)} 个候选序列")
                            time.sleep(3)
                            st.rerun()
                        
            except Exception as e:
                st.error(f"❌ 获取任务状态时发生错误: {e}")
                st.session_state.designer_error = {"error_message": str(e), "type": "Status Check Error"}

    if st.session_state.designer_results:
        st.divider()
        st.header("🏆 **步骤 2: 设计结果展示**", anchor=False)
        
        results = st.session_state.designer_results
        best_sequences = results['best_sequences']
        evolution_history = results['evolution_history']
        
        st.subheader("📊 设计统计摘要", anchor=False)
        
        score_threshold = 0.6
        high_quality_sequences = [seq for seq in best_sequences if seq.get('score', 0) >= score_threshold]
        top_sequences = high_quality_sequences[:10]
        
        col_stats = st.columns(4)
        col_stats[0].metric("总设计数", len(best_sequences))
        col_stats[1].metric("高质量设计", len(high_quality_sequences), help=f"评分 ≥ {score_threshold}")
        col_stats[2].metric("Top 10 选中", len(top_sequences))
        if best_sequences:
            col_stats[3].metric("最高评分", f"{max(seq.get('score', 0) for seq in best_sequences):.3f}")
        
        with st.expander("🎛️ **结果过滤设置**", expanded=False):
            st.markdown("调整以下参数来筛选和显示设计结果：")
            col_filter1, col_filter2 = st.columns(2)
            
            with col_filter1:
                custom_threshold = st.slider(
                    "评分阈值",
                    min_value=0.0,
                    max_value=1.0,
                    value=score_threshold,
                    step=0.05,
                    help="只显示评分高于此阈值的设计"
                )
                
            with col_filter2:
                max_display = st.slider(
                    "最大显示数量",
                    min_value=5,
                    max_value=250,  # Increased from 20 to 250
                    value=10,
                    step=1,
                    help="最多显示多少个符合条件的设计"
                )
            
            if custom_threshold != score_threshold:
                high_quality_sequences = [seq for seq in best_sequences if seq.get('score', 0) >= custom_threshold]
                top_sequences = high_quality_sequences[:max_display]
                
                col_stats[1].metric("高质量设计", len(high_quality_sequences), help=f"评分 ≥ {custom_threshold}")
                col_stats[2].metric(f"Top {max_display} 选中", len(top_sequences))
        
        st.subheader("🥇 最佳设计序列", anchor=False)

        results_components = [
            comp for comp in st.session_state.get('designer_components', [])
            if comp.get('sequence', '').strip()
        ]
        result_chain_ids, _ = get_available_chain_ids(results_components) if results_components else ([], {})
        result_total_chains = len(result_chain_ids)
        result_binder_chain_id = string.ascii_uppercase[result_total_chains] if result_total_chains < 26 else f"Z{result_total_chains-25}"
        result_chain_order = result_chain_ids + [result_binder_chain_id] if result_chain_ids else []
        pair_chain_a = st.session_state.get("designer_pair_chain_a", result_binder_chain_id)
        pair_chain_b = st.session_state.get("designer_pair_chain_b", result_chain_ids[0] if result_chain_ids else 'A')

        if not top_sequences:
            st.warning(f"😔 没有找到评分高于 {custom_threshold} 的设计序列。请尝试降低阈值或检查设计参数。")
        else:
            st.success(f"🎉 找到 {len(top_sequences)} 个高质量设计序列！")
            
            for i, seq_data in enumerate(top_sequences):
                rank = i + 1
                score = seq_data.get('score', 0)
                
                if score >= 0.8:
                    score_color = "🟢"
                elif score >= 0.7:
                    score_color = "🟡"
                elif score >= 0.6:
                    score_color = "🟠"
                else:
                    score_color = "🔴"
                
                with st.expander(
                    f"**第 {rank} 名** {score_color} 评分: {score:.3f}", 
                    expanded=(i < 3)
                ):
                    designer_config = st.session_state.get('designer_config', {})
                    sequence = seq_data['sequence']
                    
                    if designer_config.get('design_type') == 'glycopeptide':
                        glycan_type = designer_config.get('glycan_type')
                        glycosylation_site = designer_config.get('glycosylation_site')
                        
                        if glycan_type and glycosylation_site:
                            glycan_info_map = {
                                "NAGS": ("S", "N-乙酰葡糖胺丝氨酸糖基化"),
                                "NAGT": ("T", "N-乙酰葡糖胺苏氨酸糖基化"),
                                "NAGN": ("N", "N-乙酰葡糖胺天冬酰胺糖基化"),
                                "NAGY": ("Y", "N-乙酰葡糖胺酪氨酸糖基化"),
                                "MANS": ("S", "甘露糖丝氨酸糖基化"),
                                "MANT": ("T", "甘露糖苏氨酸糖基化"),
                                "MANN": ("N", "甘露糖天冬酰胺糖基化"),
                                "MANY": ("Y", "甘露糖酪氨酸糖基化"),
                                "GALS": ("S", "半乳糖丝氨酸糖基化"),
                                "GALT": ("T", "半乳糖苏氨酸糖基化"),
                                "GALN": ("N", "半乳糖天冬酰胺糖基化"),
                                "GALY": ("Y", "半乳糖酪氨酸糖基化"),
                                "FUCS": ("S", "岩藻糖丝氨酸糖基化"),
                                "FUCT": ("T", "岩藻糖苏氨酸糖基化"),
                                "FUCN": ("N", "岩藻糖天冬酰胺糖基化"),
                                "FUCY": ("Y", "岩藻糖酪氨酸糖基化"),
                                "NANS": ("S", "神经氨酸丝氨酸糖基化"),
                                "NANT": ("T", "神经氨酸苏氨酸糖基化"),
                                "NANN": ("N", "神经氨酸天冬酰胺糖基化"),
                                "NANY": ("Y", "神经氨酸酪氨酸糖基化"),
                                "GLCS": ("S", "葡萄糖丝氨酸糖基化"),
                                "GLCT": ("T", "葡萄糖苏氨酸糖基化"),
                                "GLCN": ("N", "葡萄糖天冬酰胺糖基化"),
                                "GLCY": ("Y", "葡萄糖酪氨酸糖基化")
                            }
                            
                            glycan_info = glycan_info_map.get(glycan_type, (glycan_type[-1], f"{glycan_type} 糖基化修饰"))
                            expected_aa, glycan_description = glycan_info
                            
                            if 1 <= glycosylation_site <= len(sequence):
                                st.info(
                                    f"**糖基化修饰**: 位点 {glycosylation_site} ({expected_aa}) - {glycan_description}",
                                    icon="🍯"
                                )
                                
                                glycan_base = glycan_type[:3]
                                modified_sequence_parts = list(sequence)
                                modified_sequence_parts[glycosylation_site - 1] = f"{expected_aa}({glycan_base})"
                                
                                if glycosylation_site < len(sequence):
                                    modified_sequence_display = "".join(modified_sequence_parts[:glycosylation_site]) + "-" + "".join(modified_sequence_parts[glycosylation_site:])
                                else:
                                    modified_sequence_display = "".join(modified_sequence_parts)
                                
                                st.code(modified_sequence_display, language="text")
                            else:
                                st.warning(
                                    f"**糖基化位点异常**: 预设位点 {glycosylation_site} 超出序列长度 ({len(sequence)})",
                                    icon="⚠️"
                                )
                        else:
                            st.code(sequence, language="text")
                    else:
                        st.code(sequence, language="text")
                    
                    col_metrics = st.columns(4)
                    col_metrics[0].metric("综合评分", f"{score:.3f}")
                    iptm_label = "ipTM"
                    iptm_value = seq_data.get('iptm', 0)
                    results_path = seq_data.get('results_path', '')
                    pair_data = load_pair_iptm_data(results_path) if results_path else {}
                    pair_iptm_value = get_pair_iptm_from_confidence(
                        pair_data,
                        pair_chain_a,
                        pair_chain_b,
                        chain_order=result_chain_order
                    )
                    if pair_iptm_value is not None:
                        iptm_label = "pair ipTM"
                        iptm_value = pair_iptm_value
                    col_metrics[1].metric(iptm_label, f"{iptm_value:.3f}")
                    col_metrics[2].metric("pLDDT", f"{seq_data.get('plddt', 0):.3f}")
                    col_metrics[3].metric("发现代数", seq_data.get('generation', 'N/A'))
                    
                    structure_path = find_best_structure_file(results_path) if results_path else None
                    if structure_path and os.path.exists(structure_path):
                        try:
                            with open(structure_path, 'r') as f:
                                structure_data = f.read()

                            _, ext = os.path.splitext(structure_path)
                            safe_ext = ext or '.cif'
                            mime_type = "chemical/x-pdb" if safe_ext.lower() == '.pdb' else "chemical/x-cif"
                            download_name = f"rank_{rank}_designed_structure{safe_ext}"
                            ext_label = safe_ext.upper().lstrip('.')

                            col_download = st.columns(2)
                            with col_download[0]:
                                st.download_button(
                                    label=f"📄 下载结构 ({ext_label})",
                                    data=structure_data,
                                    file_name=download_name,
                                    mime=mime_type,
                                    use_container_width=True,
                                    key=f"download_structure_{i}",
                                    help="下载该设计序列的3D结构文件"
                                )

                            with col_download[1]:
                                if st.button(
                                    "🔬 查看相互作用",
                                    use_container_width=True,
                                    key=f"view_interaction_{i}",
                                    help="在3D视图中查看该设计序列与目标的相互作用"
                                ):
                                    if f"show_3d_{i}" not in st.session_state:
                                        st.session_state[f"show_3d_{i}"] = False
                                    st.session_state[f"show_3d_{i}"] = not st.session_state.get(f"show_3d_{i}", False)
                                    st.rerun()

                            if st.session_state.get(f"show_3d_{i}", False):
                                st.markdown("---")
                                st.markdown("**🔬 3D结构与相互作用**")

                                try:
                                    structure = read_cif_from_string(structure_data)
                                    protein_bfactors = extract_protein_residue_bfactors(structure)

                                    view_html = visualize_structure_py3dmol(
                                        cif_content=structure_data,
                                        residue_bfactors=protein_bfactors,
                                        protein_style='cartoon',
                                        ligand_style='ball-and-stick',
                                        spin=False,
                                        color_scheme='pLDDT'
                                    )
                                    st.components.v1.html(view_html, height=500, scrolling=False)

                                    st.markdown("**颜色说明：**")
                                    st.markdown("""
                                    - 🔵 **蓝色**：高置信度区域 (pLDDT > 90)
                                    - 🟦 **浅蓝色**：较高置信度 (pLDDT 70-90)  
                                    - 🟡 **黄色**：中等置信度 (pLDDT 50-70)
                                    - 🟠 **橙色**：低置信度区域 (pLDDT < 50)
                                    """)

                                    if st.button("❌ 关闭3D视图", key=f"close_3d_{i}", help="隐藏3D结构显示"):
                                        st.session_state[f"show_3d_{i}"] = False
                                        st.rerun()

                                except Exception as e:
                                    st.error(f"❌ 3D结构显示失败: {str(e)}")
                                    st.exception(e)

                        except Exception as e:
                            st.caption(f"⚠️ 结构文件读取失败: {str(e)}")
                    elif results_path:
                        st.caption("⚠️ 未找到结构文件")
                    else:
                        st.caption("⚠️ 结构文件路径不可用")
        
        st.subheader("📈 演化历史", anchor=False)
        
        chart_data = pd.DataFrame({
            '代数': evolution_history.get('generations', []),
            '最佳评分': evolution_history.get('best_scores', []),
            '平均评分': evolution_history.get('avg_scores', [])
        })
        
        if not chart_data.empty:
            try:
                import altair as alt
                
                all_scores = []
                if '最佳评分' in chart_data.columns:
                    all_scores.extend(chart_data['最佳评分'].dropna().tolist())
                if '平均评分' in chart_data.columns:
                    all_scores.extend(chart_data['平均评分'].dropna().tolist())
                
                if all_scores:
                    min_score = min(all_scores)
                    max_score = max(all_scores)
                    score_range = max_score - min_score
                    
                    if score_range > 0:
                        y_min = max(0, min_score - score_range * 0.1)
                        y_max = min(1, max_score + score_range * 0.1)
                    else:
                        y_min = max(0, min_score - 0.05)
                        y_max = min(1, max_score + 0.05)
                    
                    chart_data_melted = chart_data.melt(id_vars=['代数'], 
                                                       value_vars=['最佳评分', '平均评分'],
                                                       var_name='指标', value_name='评分')
                    
                    chart = alt.Chart(chart_data_melted).mark_line(point=True).encode(
                        x=alt.X('代数:O', title='演化代数', axis=alt.Axis(labelAngle=0)),
                        y=alt.Y('评分:Q', title='评分', scale=alt.Scale(domain=[y_min, y_max])),
                        color=alt.Color('指标:N', 
                                      scale=alt.Scale(range=['#1f77b4', '#ff7f0e']),
                                      legend=alt.Legend(title="评分类型")),
                        tooltip=['代数:O', '指标:N', '评分:Q']
                    ).properties(
                        width=600,
                        height=300,
                        title="分子设计演化历史"
                    )
                    
                    st.altair_chart(chart, use_container_width=True)
                else:
                    st.line_chart(chart_data.set_index('代数'))
                    
            except ImportError:
                st.line_chart(chart_data.set_index('代数'))
        else:
            st.info("暂无演化历史数据可显示。")
        
        st.markdown("<b>📥 下载设计结果</b>", unsafe_allow_html=True)
        
        col_download = st.columns(2)
        
        with col_download[0]:
            if top_sequences:
                sequences_for_csv = []
                designer_config = st.session_state.get('designer_config', {})
                
                for seq_data in top_sequences:
                    enhanced_seq_data = seq_data.copy()
                    
                    if designer_config.get('design_type') == 'glycopeptide':
                        glycan_type = designer_config.get('glycan_type')
                        glycosylation_site = designer_config.get('glycosylation_site')
                        
                        if glycan_type and glycosylation_site:
                            glycan_info_map = {
                                "NAGS": ("S", "NAG+Ser"), "NAGT": ("T", "NAG+Thr"), "NAGN": ("N", "NAG+Asn"), "NAGY": ("Y", "NAG+Tyr"),
                                "MANS": ("S", "MAN+Ser"), "MANT": ("T", "MAN+Thr"), "MANN": ("N", "MAN+Asn"), "MANY": ("Y", "MAN+Tyr"),
                                "GALS": ("S", "GAL+Ser"), "GALT": ("T", "GAL+Thr"), "GALN": ("N", "GAL+Asn"), "GALY": ("Y", "GAL+Tyr"),
                                "FUCS": ("S", "FUC+Ser"), "FUCT": ("T", "FUC+Thr"), "FUCN": ("N", "FUC+Asn"), "FUCY": ("Y", "FUC+Tyr"),
                                "NANS": ("S", "NAN+Ser"), "NANT": ("T", "NAN+Thr"), "NANN": ("N", "NAN+Asn"), "NANY": ("Y", "NAN+Tyr"),
                                "GLCS": ("S", "GLC+Ser"), "GLCT": ("T", "GLC+Thr"), "GLCN": ("N", "GLC+Asn"), "GLCY": ("Y", "GLC+Tyr")
                            }
                            
                            enhanced_seq_data['glycan_type'] = glycan_type
                            enhanced_seq_data['glycosylation_site'] = glycosylation_site
                            
                            glycan_info = glycan_info_map.get(glycan_type, (glycan_type[-1], glycan_type))
                            expected_aa, short_description = glycan_info
                            enhanced_seq_data['glycan_description'] = short_description
                            
                            sequence = seq_data.get('sequence', '')
                            if sequence and 1 <= glycosylation_site <= len(sequence):
                                actual_aa = sequence[glycosylation_site - 1]
                                enhanced_seq_data['modified_residue'] = f"{expected_aa}{glycosylation_site}"
                                enhanced_seq_data['actual_residue'] = f"{actual_aa}{glycosylation_site}"
                            else:
                                enhanced_seq_data['modified_residue'] = f"{expected_aa}{glycosylation_site}(out_of_range)"
                                enhanced_seq_data['actual_residue'] = f"Position{glycosylation_site}(out_of_range)"
                    
                    sequences_for_csv.append(enhanced_seq_data)
                
                sequences_csv = pd.DataFrame(sequences_for_csv)
                sequences_csv_str = sequences_csv.to_csv(index=False)
                
                st.download_button(
                    label="📊 Top序列 (CSV)",
                    data=sequences_csv_str,
                    file_name=f"top_designed_sequences_{st.session_state.designer_task_id}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    help=f"下载前 {len(top_sequences)} 个高质量设计序列（包含糖基化修饰信息）"
                )
            else:
                st.button("📊 CSV下载", disabled=True, help="无符合条件的序列")
        
        with col_download[1]:
            results_json = json.dumps({
                'summary': {
                    'total_sequences': len(best_sequences),
                    'high_quality_sequences': len(high_quality_sequences),
                    'threshold_applied': custom_threshold,
                    'top_selected': len(top_sequences)
                },
                'top_sequences': top_sequences,
                'evolution_history': evolution_history
            }, indent=2)
            
            st.download_button(
                label="📦 完整结果 (JSON)",
                data=results_json,
                file_name=f"design_results_{st.session_state.designer_task_id}.json",
                mime="application/json",
                use_container_width=True,
                help="下载包含演化历史的完整结果数据"
            )
    
    if st.session_state.designer_error:
        st.error("ℹ️ 设计任务执行失败，详细信息如下：")
        st.json(st.session_state.designer_error)
        
        col_reset = st.columns(2)
        with col_reset[0]:
            if st.button("🔄 重置设计器", key="reset_designer", type="secondary", use_container_width=True):
                # 清除URL参数
                URLStateManager.clear_url_params()
                for key in ['designer_task_id', 'designer_results', 'designer_error', 'designer_config', 'designer_components', 'designer_constraints']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        
        with col_reset[1]:
            if st.button("🔧 保留配置重新设计", key="redesign_with_config", type="primary", use_container_width=True):
                # 清除URL参数
                URLStateManager.clear_url_params()
                for key in ['designer_task_id', 'designer_results', 'designer_error']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
