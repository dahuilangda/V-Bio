import streamlit as st
import os
import string
import yaml
import pandas as pd
import time
import uuid
import json
import psutil

from frontend.utils import (
    get_available_chain_ids, 
    get_available_chain_ids_for_designer,
    get_smart_msa_default, 
    validate_designer_inputs, 
    has_cached_msa,
    read_cif_from_string,
    extract_protein_residue_bfactors
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

def render_bicyclic_designer_page():
    # 尝试从URL恢复状态
    URLStateManager.restore_state_from_url()
    
    st.markdown("### 🔗 双环肽设计")
    st.markdown("设计具有两个环状结构的双环肽，通过三个半胱氨酸残基的二硫键形成稳定的环状结构。")
    
    # 显示双环肽结构说明
    with st.expander("💡 双环肽设计说明", expanded=False):
        st.markdown("""
        **双环肽特点：**
        - 包含3个半胱氨酸(Cys)残基
        - 末端必须是半胱氨酸
        - 形成两个环状结构，增强结构稳定性
        - 具有更高的生物活性和抗酶解能力
        
        **二硫键连接模式：**
        - Cys1-Cys3: 形成第一个环
        - Cys2-Cys_terminal: 形成第二个环
        - 两个环共享部分序列，创造独特的结构特征
        
        **连接体类型：**
        - **SEZ** 1,3,5-trimethylbenzene
        - **29N** 1-[3,5-di(propanoyl)-1,3,5-triazinan-1-yl]propan-1-one
        """)
    
    designer_is_running = (
        st.session_state.bicyclic_task_id is not None and 
        st.session_state.bicyclic_results is None and 
        st.session_state.bicyclic_error is None
    )
    
    with st.expander("🎯 **步骤 1: 设置设计目标**", expanded=not designer_is_running and not st.session_state.bicyclic_results):
        st.markdown("配置您的双环肽设计任务参数。")
        
        # 确保总是有默认组件（即使URL恢复状态后也要检查）
        if 'bicyclic_components' not in st.session_state or not st.session_state.bicyclic_components:
            st.session_state.bicyclic_components = [
                {'id': str(uuid.uuid4()), 'type': 'protein', 'sequence': '', 'num_copies': 1, 'use_msa': False}
            ]
        
        if 'bicyclic_constraints' not in st.session_state:
            st.session_state.bicyclic_constraints = []
        
        # 目标分子设置
        st.subheader("🧬 目标分子", anchor=False)
        
        designer_id_to_delete = None
        for i, component in enumerate(st.session_state.bicyclic_components[:]):
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
                        "dna": "🧬 DNA",
                        "rna": "🧬 RNA", 
                        "ligand": "💊 辅酶/小分子"
                    }[x],
                    key=f"bicyclic_type_{component['id']}",
                    index=current_type_index,
                    disabled=designer_is_running,
                    help="选择此组分的分子类型：蛋白质、DNA、RNA或小分子配体。"
                )
                
                type_changed = new_type != old_type
                
                component['type'] = new_type
                
                if type_changed:
                    component['sequence'] = ''
                    
                    # 清理旧的字段
                    if 'use_msa' in component:
                        del component['use_msa']
                    if 'cyclic' in component:
                        del component['cyclic']
                    if 'input_method' in component:
                        del component['input_method']
                    
                    # 根据新类型设置默认字段
                    if new_type == 'protein':
                        component['use_msa'] = get_smart_msa_default(st.session_state.bicyclic_components)
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
                    key=f"bicyclic_copies_{component['id']}",
                    disabled=designer_is_running,
                    help="此组分的拷贝数。"
                )
            
            with cols_comp[2]:
                if len(st.session_state.bicyclic_components) > 1:
                    if st.button("🗑️", key=f"bicyclic_del_{component['id']}", help="删除此组分", disabled=designer_is_running):
                        designer_id_to_delete = component['id']
            
            num_copies = component.get('num_copies', 1)
            if num_copies > 1:
                st.caption(f"💡 此组分将创建 {num_copies} 个拷贝，自动分配链ID")
            
            # 根据类型显示序列输入
            if component['type'] == 'protein':
                old_sequence = component.get('sequence', '')
                
                new_sequence = st.text_area(
                    f"蛋白质序列 ({'单体' if num_copies == 1 else f'{num_copies}聚体'})",
                    height=100,
                    value=component.get('sequence', ''),
                    placeholder="例如: MVSKGEELFTGVVPILVELDGDVNGHKFSVSGEGEGDATYGKLTLKFICTTGKLPVPWPTLV...",
                    key=f"bicyclic_seq_{component['id']}",
                    disabled=designer_is_running,
                    help="输入此蛋白质链的完整氨基酸序列。"
                )
                
                sequence_changed = new_sequence != old_sequence
                
                component['sequence'] = new_sequence
                
                if sequence_changed:
                    protein_components = [comp for comp in st.session_state.bicyclic_components if comp.get('type') == 'protein']
                    if len(protein_components) == 1:
                        if new_sequence.strip():
                            if has_cached_msa(new_sequence.strip()):
                                component['use_msa'] = True
                            else:
                                component['use_msa'] = False
                        else:
                            component['use_msa'] = False
                    
                    st.rerun()
                
                bicyclic_sequence = component.get('sequence', '').strip()
                if bicyclic_sequence:
                    msa_value = st.checkbox(
                        "启用 MSA",
                        value=component.get('use_msa', True),
                        key=f"bicyclic_msa_{component['id']}",
                        help="为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。",
                        disabled=designer_is_running
                    )
                    if msa_value != component.get('use_msa', True):
                        component['use_msa'] = msa_value
                        if msa_value:
                            st.toast("✅ 已启用 MSA 生成", icon="🧬")
                        else:
                            st.toast("❌ 已禁用 MSA 生成", icon="⚡")
                        st.rerun()
                else:
                    component['use_msa'] = component.get('use_msa', True)
            
            elif component['type'] in ['dna', 'rna']:
                seq_type = "DNA" if component['type'] == 'dna' else "RNA"
                placeholder = "ATGCGTAAGGGATCCGCATGC..." if component['type'] == 'dna' else "AUGCGUAAGGAUCCGCAUGC..."
                
                sequence = st.text_area(
                    f"{seq_type}序列",
                    height=100,
                    value=component.get('sequence', ''),
                    placeholder=f"例如: {placeholder}",
                    key=f"bicyclic_seq_{component['id']}",
                    disabled=designer_is_running,
                    help=f"输入{seq_type}核苷酸序列。"
                )
                component['sequence'] = sequence
            
            elif component['type'] == 'ligand':
                from streamlit_ketcher import st_ketcher
                
                old_input_method = component.get('input_method', 'smiles')
                
                new_input_method = st.radio(
                    "小分子输入方式",
                    ["smiles", "ccd", "ketcher"],
                    key=f"bicyclic_method_{component['id']}",
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
                        key=f"bicyclic_seq_{component['id']}",
                        disabled=designer_is_running
                    )
                elif new_input_method == 'ccd':
                    component['sequence'] = st.text_input(
                        f"CCD 代码 ({'单分子' if num_copies == 1 else f'{num_copies}个分子'})",
                        value=component.get('sequence', ''),
                        placeholder="例如: HEM, NAD, ATP",
                        key=f"bicyclic_seq_{component['id']}",
                        disabled=designer_is_running
                    )
                else:  # ketcher
                    current_smiles = component.get('sequence', '')
                    smiles_from_ketcher = st_ketcher(
                        value=current_smiles,
                        key=f"bicyclic_ketcher_{component['id']}",
                        height=400
                    )
                    
                    if smiles_from_ketcher is not None and smiles_from_ketcher != current_smiles:
                        st.session_state.bicyclic_components[i]['sequence'] = smiles_from_ketcher
                        if smiles_from_ketcher:
                            st.toast("✅ SMILES 字符串已成功更新！", icon="🧪")
                        st.rerun()
                    
                    current_smiles_display = st.session_state.bicyclic_components[i].get('sequence', '')
                    if current_smiles_display:
                        st.caption("✨ 当前 SMILES 字符串:")
                        st.code(current_smiles_display, language='smiles')
                    else:
                        st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")
        
        if designer_id_to_delete:
            st.session_state.bicyclic_components = [c for c in st.session_state.bicyclic_components if c['id'] != designer_id_to_delete]
            st.rerun()
        
        def add_new_bicyclic_component():
            smart_msa_default = get_smart_msa_default(st.session_state.bicyclic_components)
            st.session_state.bicyclic_components.append({
                'id': str(uuid.uuid4()),
                'type': 'protein',
                'sequence': '',
                'num_copies': 1,
                'use_msa': smart_msa_default
            })
        
        if st.button("➕ 添加新组分", key="add_bicyclic_component", disabled=designer_is_running, help="添加新的蛋白质、DNA/RNA或小分子组分"):
            add_new_bicyclic_component()
            st.rerun()
        
        # 双环肽参数设置
        st.subheader("🎯 双环肽设计参数", anchor=False)
        
        # 基本设置 - 默认展开
        with st.expander("📝 **基本设置**", expanded=True):
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                binder_length = st.number_input(
                    "双环肽长度",
                    min_value=8,
                    max_value=30,
                    value=15,
                    step=1,
                    help="双环肽的氨基酸残基数量。建议8-30个残基以确保形成稳定的双环结构。",
                    disabled=designer_is_running
                )
            
            with col2:
                # 连接体选择
                linker_ccd = st.selectbox(
                    "连接体类型",
                    ["SEZ", "29N"],
                    format_func=lambda x: f"🔗 {x} - {'TRIS连接体' if x == 'SEZ' else '大环连接体'}",
                    help="选择用于形成双环结构的连接体分子。SEZ是传统的TRIS连接体，29N是新型大环连接体。",
                    disabled=designer_is_running,
                    key="bicyclic_linker_ccd"
                )            
            with col4:
                # 双环肽氨基酸组成控制 - 智能控制额外半胱氨酸
                st.markdown("**🧪 氨基酸组成**")
                include_extra_cysteine = st.checkbox(
                    "允许额外半胱氨酸",
                    value=False,  # 双环肽默认不需要额外Cys
                    help="是否允许在必需的3个半胱氨酸之外生成额外的半胱氨酸。通常不建议启用。",
                    disabled=designer_is_running,
                    key="bicyclic_include_extra_cys"
                )
                
                if include_extra_cysteine:
                    st.caption("⚠️ 额外的半胱氨酸可能干扰双环结构")
                else:
                    st.caption("✅ 仅使用必需的3个半胱氨酸")
            
            with col3:
                cys_position_mode = st.selectbox(
                    "Cys位置设置",
                    ["auto", "manual"],
                    format_func=lambda x: "🎲 自动优化" if x == "auto" else "✋ 手动指定",
                    help="选择半胱氨酸位置的设定方式。自动模式将通过演化算法优化位置。",
                    disabled=designer_is_running
                )
            
            with col4:
                fix_terminal_cys = st.checkbox(
                    "固定末端Cys",
                    value=True,
                    help="末端半胱氨酸位置固定不变，只优化其他两个Cys的位置。",
                    disabled=designer_is_running or cys_position_mode == "auto",
                    key="bicyclic_fix_terminal_cys"
                )
            
            # # 显示连接体信息
            # if linker_ccd == "SEZ":
            #     st.info("🔗 **SEZ连接体**: 经典的TRIS(三羟甲基氨基甲烷)衍生连接体，三个反应位点为CD、C1、C2，适合形成紧凑的双环结构。")
            # elif linker_ccd == "29N":
            #     st.info("🔗 **29N连接体**: 新型大环连接体，三个反应位点为C16、C19、C25，可形成更大、更灵活的双环结构，适合与较大的靶蛋白结合。")
            
            # 手动设置Cys位置
            cys_positions = None
            if cys_position_mode == "manual" and binder_length >= 8:
                st.markdown("**手动设置半胱氨酸位置：**")
                
                col_cys1, col_cys2, col_cys3 = st.columns(3)
                
                with col_cys1:
                    cys1_pos = st.number_input(
                        "第1个Cys位置",
                        min_value=1,
                        max_value=max(1, binder_length-2),
                        value=min(3, max(1, binder_length-2)),
                        step=1,
                        disabled=designer_is_running,
                        help="第一个半胱氨酸的位置（1-based索引）",
                        key="bicyclic_cys1_pos"
                    )
                
                with col_cys2:
                    max_cys2 = max(1, binder_length-1) if not fix_terminal_cys else max(1, binder_length-2)
                    cys2_pos = st.number_input(
                        "第2个Cys位置",
                        min_value=1,
                        max_value=max_cys2,
                        value=min(max_cys2, max(1, binder_length//2)),
                        step=1,
                        disabled=designer_is_running,
                        help="第二个半胱氨酸的位置（1-based索引）",
                        key="bicyclic_cys2_pos"
                    )
                
                with col_cys3:
                    if fix_terminal_cys:
                        st.text_input("第3个Cys位置", value=f"{binder_length} (末端)", disabled=True, key="bicyclic_cys3_display")
                        cys3_pos = binder_length
                    else:
                        cys3_pos = st.number_input(
                            "第3个Cys位置",
                            min_value=1,
                            max_value=binder_length,
                            value=binder_length,
                            step=1,
                            disabled=designer_is_running,
                            help="第三个半胱氨酸的位置（1-based索引）",
                            key="bicyclic_cys3_pos"
                        )
                
                # 验证Cys位置
                cys_positions_list = [cys1_pos, cys2_pos, cys3_pos]
                if len(set(cys_positions_list)) != 3:
                    st.error("❌ 三个半胱氨酸位置不能重复！")
                    cys_positions = None
                else:
                    cys_positions = [(pos-1) for pos in sorted(cys_positions_list[:-1])]  # 转为0-based，不包括末端
                    st.success(f"✅ Cys位置设置：{cys1_pos}, {cys2_pos}, {cys3_pos}")
        
        # 高级设置 - 默认折叠
        with st.expander("⚙️ **高级设置** (可选)", expanded=False):
            st.markdown("**🧬 初始序列设置**")
            use_initial_sequence = st.checkbox(
                "使用初始序列作为演化起点",
                value=False,
                help="提供一个初始双环肽序列作为演化起点，而不是完全随机生成。",
                disabled=designer_is_running,
                key="bicyclic_use_initial_sequence"
            )
            
            initial_sequence = ""
            if use_initial_sequence:
                initial_sequence = st.text_input(
                    "初始双环肽序列",
                    value="",
                    placeholder=f"例如: {'C'*3 + 'A'*(binder_length-3)}",
                    help=f"输入包含3个半胱氨酸的初始序列，长度应为{binder_length}。",
                    disabled=designer_is_running,
                    key="bicyclic_initial_sequence"
                )
                
                if initial_sequence:
                    cys_count = initial_sequence.count('C')
                    seq_len = len(initial_sequence)
                    if cys_count != 3:
                        st.warning(f"⚠️ 初始序列包含{cys_count}个Cys，双环肽需要恰好3个Cys。")
                    elif seq_len != binder_length:
                        if seq_len < binder_length:
                            st.warning(f"⚠️ 初始序列长度({seq_len})小于目标长度({binder_length})，将随机补全。")
                        else:
                            st.warning(f"⚠️ 初始序列长度({seq_len})大于目标长度({binder_length})，将截取前{binder_length}个氨基酸。")
                    else:
                        st.success("✅ 初始序列格式正确。")

            sequence_mask = st.text_input(
                "序列掩码",
                placeholder="例如: X-A-X-L-X-X-X-P-X-X",
                help="指定固定位置的氨基酸。格式: 'X-A-X-L-X'，其中X表示可变位置，字母表示固定氨基酸。长度必须与肽链长度匹配。支持使用'-'、'_'或空格作为分隔符。注意：双环肽的半胱氨酸位置由系统自动管理。",
                key="bicyclic_sequence_mask"
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
                            # 对双环肽的特殊提醒
                            cys_positions_in_mask = [i for i, char in enumerate(mask_clean) if char == 'C']
                            if cys_positions_in_mask:
                                st.info(f"ℹ️ 检测到掩码中包含半胱氨酸位置: {[i+1 for i in cys_positions_in_mask]}。这将与双环肽的自动半胱氨酸管理结合使用。")
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
                    "stable": "🎯 平稳优化",
                    "aggressive": "🔥 激进探索", 
                    "conservative": "🛡️ 保守设计"
                }[x],
                index=0,
                help="选择预设的优化策略。双环肽设计推荐平衡模式以确保结构稳定性。",
                disabled=designer_is_running,
                key="bicyclic_optimization_mode"
            )
            
            mode_descriptions = {
                "balanced": "⚖️ **平衡模式**: 综合考虑探索性和收敛性，适合双环肽的复杂结构优化。",
                "stable": "🎯 **平稳优化**: 稳定收敛，适用于需要可重复双环肽结构的场景。",
                "aggressive": "🔥 **激进探索**: 快速突破局部最优，适用于寻找新颖双环肽结构。",
                "conservative": "🛡️ **保守设计**: 小步优化，适用于基于已知双环肽结构的改进。"
            }
            st.info(mode_descriptions[optimization_mode])
            
            col_adv1, col_adv2, col_adv3 = st.columns(3)
            
            with col_adv1:
                generations = st.number_input(
                    "演化代数",
                    min_value=3,
                    max_value=25,
                    value=12,  # 双环肽推荐更多代数
                    step=1,
                    help="演化算法的迭代次数。双环肽设计推荐更多代数以确保结构优化。",
                    disabled=designer_is_running,
                    key="bicyclic_generations"
                )
            
            with col_adv2:
                population_size = st.number_input(
                    "种群大小",
                    min_value=8,
                    max_value=50,
                    value=16,
                    step=1,
                    help="每一代中的候选序列数量。",
                    disabled=designer_is_running,
                    key="bicyclic_population_size"
                )
            
            with col_adv3:
                max_elite_size = min(10, max(2, population_size//2))
                default_elite_size = max(2, min(max_elite_size, min(6, max(2, population_size//3))))
                
                elite_size = st.number_input(
                    "精英保留数",
                    min_value=2,
                    max_value=max_elite_size,
                    value=default_elite_size,
                    step=1,
                    help="每一代中保留的最优个体数量。",
                    disabled=designer_is_running,
                    key="bicyclic_elite_size"
                )
            
            mutation_rate = st.slider(
                "突变率",
                min_value=0.1,
                max_value=0.6,
                value=0.25,
                step=0.05,
                help="每一代中发生突变的概率。双环肽推荐适中的突变率以保持结构稳定性。",
                disabled=designer_is_running,
                key="bicyclic_mutation_rate"
            )
            
            # 设置预设参数
            preset_params = {
                "balanced": {
                    "convergence_window": 5,
                    "convergence_threshold": 0.001,
                    "max_stagnation": 4,  # 双环肽允许更多停滞
                    "initial_temperature": 1.2,
                    "min_temperature": 0.1,
                    "enable_enhanced": True
                },
                "stable": {
                    "convergence_window": 6,
                    "convergence_threshold": 0.0008,
                    "max_stagnation": 5,
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
                    "convergence_window": 7,
                    "convergence_threshold": 0.0005,
                    "max_stagnation": 6,
                    "initial_temperature": 0.8,
                    "min_temperature": 0.08,
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
        
        # 约束设置
        st.subheader("🔗 分子约束 (可选)", anchor=False)
        st.markdown("设置双环肽与目标分子的相互作用约束。")
        
        # 约束管理逻辑（类似原有代码）
        constraint_id_to_delete = None
        for i, constraint in enumerate(st.session_state.bicyclic_constraints[:]):
            constraint_type = constraint.get('type', 'contact')
            
            constraint_labels = {
                'bond': '🔗 键约束',
                'contact': '📍 接触约束',
                'pocket': '🕳️ 口袋约束'
            }
            
            with st.expander(f"{constraint_labels.get(constraint_type, '📍 约束')} {i+1}", expanded=True):
                col1, col2 = st.columns([5, 1])
                
                with col1:
                    constraint_type = st.selectbox(
                        "选择约束类型",
                        options=['contact', 'bond', 'pocket'],
                        format_func=lambda x: {
                            'contact': '📍 Contact - 接触约束 (两个残基间距离)',
                            'bond': '🔗 Bond - 键约束 (两个原子间共价键)',
                            'pocket': '🕳️ Pocket - 口袋约束 (双环肽与特定口袋的结合)'
                        }[x],
                        index=['contact', 'bond', 'pocket'].index(constraint.get('type', 'contact')),
                        key=f"bicyclic_constraint_type_{i}",
                        disabled=designer_is_running
                    )
                    
                    constraint['type'] = constraint_type
                    
                    # 获取可用链ID
                    total_chains = sum(comp.get('num_copies', 1) for comp in st.session_state.bicyclic_components if comp.get('sequence', '').strip())
                    binder_chain_id = string.ascii_uppercase[total_chains] if total_chains < 26 else f"Z{total_chains-25}"
                    available_chains, chain_descriptions = get_available_chain_ids_for_designer(st.session_state.bicyclic_components, binder_chain_id)
                    
                    st.markdown("---")
                    
                    if constraint_type == 'contact':
                        render_contact_constraint_ui(constraint, f"bicyclic_{i}", available_chains, chain_descriptions, designer_is_running)
                    elif constraint_type == 'bond':
                        render_bond_constraint_ui(constraint, f"bicyclic_{i}", available_chains, chain_descriptions, designer_is_running)
                    elif constraint_type == 'pocket':
                        render_pocket_constraint_ui(constraint, f"bicyclic_{i}", available_chains, chain_descriptions, designer_is_running)
                
                with col2:
                    if st.button("🗑️", key=f"bicyclic_del_constraint_{i}", help="删除此约束", disabled=designer_is_running):
                        constraint_id_to_delete = i
        
        if constraint_id_to_delete is not None:
            del st.session_state.bicyclic_constraints[constraint_id_to_delete]
            st.rerun()
        
        # 添加约束按钮
        st.markdown("---")
        add_constraint_cols = st.columns(3)
        
        target_chain_id = 'A'
        
        with add_constraint_cols[0]:
            if st.button("➕ 添加 Contact 约束", key="add_bicyclic_contact_constraint", disabled=designer_is_running):
                st.session_state.bicyclic_constraints.append({
                    'type': 'contact',
                    'token1_chain': target_chain_id,
                    'token1_residue': 1,
                    'token2_chain': 'BINDER_CHAIN',
                    'token2_residue': 1,
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
        
        with add_constraint_cols[1]:
            if st.button("➕ 添加 Bond 约束", key="add_bicyclic_bond_constraint", disabled=designer_is_running):
                st.session_state.bicyclic_constraints.append({
                    'type': 'bond',
                    'atom1_chain': target_chain_id,
                    'atom1_residue': 1,
                    'atom1_atom': 'CA',
                    'atom2_chain': 'BINDER_CHAIN',
                    'atom2_residue': 1,
                    'atom2_atom': 'CA'
                })
                st.rerun()
        
        with add_constraint_cols[2]:
            if st.button("➕ 添加 Pocket 约束", key="add_bicyclic_pocket_constraint", disabled=designer_is_running):
                st.session_state.bicyclic_constraints.append({
                    'type': 'pocket',
                    'binder': 'BINDER_CHAIN',
                    'contacts': [[target_chain_id, 1], [target_chain_id, 2]],
                    'max_distance': 5.0,
                    'force': False
                })
                st.rerun()
    
    # 输入验证
    bicyclic_is_valid, validation_message = validate_designer_inputs(st.session_state.bicyclic_components)
    
    if use_initial_sequence and initial_sequence:
        valid_amino_acids = set("ACDEFGHIKLMNPQRSTVWY")
        invalid_chars = set(initial_sequence.upper()) - valid_amino_acids
        if invalid_chars:
            bicyclic_is_valid = False
            validation_message = f"初始序列包含无效字符: {', '.join(invalid_chars)}"
        elif initial_sequence.count('C') != 3:
            bicyclic_is_valid = False
            validation_message = "双环肽初始序列必须包含恰好3个半胱氨酸(C)"
    
    if cys_position_mode == "manual" and cys_positions is None:
        bicyclic_is_valid = False
        validation_message = "手动模式下必须正确设置三个不重复的半胱氨酸位置"
    
    # 启动设计按钮
    if st.button("🚀 开始双环肽设计", key="start_bicyclic_designer", type="primary", 
                disabled=(not bicyclic_is_valid or designer_is_running), use_container_width=True):
        
        st.session_state.bicyclic_task_id = None
        st.session_state.bicyclic_results = None
        st.session_state.bicyclic_error = None
        
        with st.spinner("⏳ 正在启动双环肽设计任务..."):
            try:
                any_msa_enabled = any(comp.get('use_msa', True) for comp in st.session_state.bicyclic_components if comp['type'] == 'protein')
                
                template_yaml = create_designer_complex_yaml(
                    st.session_state.bicyclic_components, 
                    use_msa=any_msa_enabled,
                    constraints=st.session_state.bicyclic_constraints
                )
                
                # 准备双环肽特殊参数
                bicyclic_params = {
                    'cys_positions': cys_positions,
                    'cys_position_mode': cys_position_mode,
                    'fix_terminal_cys': fix_terminal_cys,
                    'linker_ccd': linker_ccd,  # 添加连接体参数
                }
                
                # 双环肽的半胱氨酸控制：include_extra_cysteine=False 意味着不包含额外半胱氨酸
                # 这对应于新系统中的 include_cysteine=False（除了必需的3个Cys外不生成额外Cys）
                include_cysteine_for_design = include_extra_cysteine
                
                result = submit_designer_job(
                    template_yaml_content=template_yaml,
                    design_type='bicyclic',  # 设置为双环肽设计
                    binder_length=binder_length,
                    target_chain_id='A',
                    generations=generations,
                    population_size=population_size,
                    elite_size=elite_size,
                    mutation_rate=mutation_rate,
                    convergence_window=convergence_window,
                    convergence_threshold=convergence_threshold,
                    max_stagnation=max_stagnation,
                    initial_temperature=initial_temperature,
                    min_temperature=min_temperature,
                    enable_enhanced=enable_enhanced,
                    use_initial_sequence=use_initial_sequence,
                    initial_sequence=initial_sequence if use_initial_sequence else None,
                    sequence_mask=sequence_mask,
                    cyclic_binder=False,  # 双环肽有特殊的环状逻辑
                    include_cysteine=include_cysteine_for_design,  # 控制是否允许额外半胱氨酸
                    use_msa=any_msa_enabled,
                    user_constraints=st.session_state.bicyclic_constraints,
                    bicyclic_params=bicyclic_params  # 传递双环肽参数
                )
                
                if result['success']:
                    st.session_state.bicyclic_task_id = result['task_id']
                    st.session_state.bicyclic_work_dir = result['work_dir']
                    st.session_state.bicyclic_config = result['params']
                    
                    # 更新URL参数
                    URLStateManager.update_url_for_designer_task(
                        task_id=result['task_id'], 
                        work_dir=result['work_dir'],
                        components=st.session_state.bicyclic_components,
                        constraints=st.session_state.bicyclic_constraints,
                        config=st.session_state.bicyclic_config,
                        task_type='bicyclic_designer'  # 指定为双环肽设计任务类型
                    )
                    
                    st.toast(f"🎉 双环肽设计任务已启动！任务ID: {result['task_id']}", icon="✅")
                    st.rerun()
                else:
                    st.error(f"❌ **任务启动失败**：{result['error']}")
                    st.session_state.bicyclic_error = {"error_message": result['error'], "type": "Task Start Error"}
                    
            except Exception as e:
                st.error(f"❌ **任务启动失败**：{e}")
                st.session_state.bicyclic_error = {"error_message": str(e), "type": "Client Error"}
    
    if not bicyclic_is_valid and not designer_is_running:
        has_user_input = any(comp.get('sequence', '').strip() for comp in st.session_state.bicyclic_components)
        if has_user_input:
            st.error(f"⚠️ **无法启动设计**: {validation_message}")
    
    # 进度监控（类似原有逻辑）
    if st.session_state.bicyclic_task_id and not st.session_state.bicyclic_results:
        st.divider()
        st.header("🔄 **步骤 2: 设计进度监控**", anchor=False)
        
        # 添加紧急停止按钮
        st.markdown("""
        <style>
        .stop-button {
            background: linear-gradient(135deg, #ff6b6b, #ff4757);
            border: none;
            color: white;
            font-weight: 600;
            border-radius: 8px;
            padding: 8px 16px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 12px rgba(255, 107, 107, 0.3);
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
                    help="安全终止正在进行的双环肽设计任务，已完成的工作将被保存",
                    key="stop_bicyclic_design_btn"):
            try:
                from designer.design_manager import design_manager
                
                # 尝试从状态文件获取进程信息
                work_dir = st.session_state.get('bicyclic_work_dir', None)
                process_found = False
                
                if work_dir and os.path.exists(work_dir):
                    status_file = os.path.join(work_dir, 'status.json')
                    if os.path.exists(status_file):
                        try:
                            with open(status_file, 'r') as f:
                                status_info = json.load(f)
                                process_id = status_info.get('process_id')
                                if process_id:
                                    design_manager.set_current_process_info(process_id, status_file)
                                    process_found = True
                                    st.info(f"🎯 找到设计进程 ID: {process_id}")
                                else:
                                    st.warning("⚠️ 状态文件中未找到进程ID")
                        except (json.JSONDecodeError, KeyError) as e:
                            st.error(f"❌ 读取状态文件失败: {e}")
                    else:
                        st.warning("⚠️ 任务状态文件不存在")
                else:
                    st.warning("⚠️ 任务工作目录不存在")
                
                # 如果没有找到进程信息，尝试通过进程名查找
                if not process_found:
                    try:
                        import psutil
                        design_processes = []
                        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                            try:
                                cmdline = proc.info.get('cmdline', [])
                                if any('run_design' in str(cmd) for cmd in cmdline):
                                    design_processes.append(proc.info['pid'])
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                        
                        if design_processes:
                            # 使用最新的设计进程
                            latest_pid = max(design_processes)
                            design_manager.set_current_process_info(latest_pid, None)
                            process_found = True
                            st.info(f"🔍 通过进程名找到设计进程 ID: {latest_pid}")
                        else:
                            st.warning("⚠️ 未找到正在运行的设计进程")
                    except Exception as e:
                        st.error(f"❌ 搜索设计进程失败: {e}")

                # 尝试停止进程
                if process_found or design_manager.current_process_id:
                    with st.spinner("⏳ 正在停止双环肽设计任务..."):
                        graceful_stop_success = design_manager.stop_current_design()
                        
                        if graceful_stop_success:
                            st.success("✅ 双环肽设计任务已成功停止")
                            # 更新状态文件标记任务已停止
                            if work_dir and os.path.exists(work_dir):
                                status_file = os.path.join(work_dir, 'status.json')
                                if os.path.exists(status_file):
                                    try:
                                        with open(status_file, 'r+') as f:
                                            status_data = json.load(f)
                                            status_data['state'] = 'CANCELLED'
                                            status_data['error'] = '用户手动停止任务'
                                            f.seek(0)
                                            json.dump(status_data, f, indent=2)
                                            f.truncate()
                                    except Exception as e:
                                        st.warning(f"⚠️ 更新状态文件失败: {e}")
                            
                            # 清理session状态
                            st.session_state.bicyclic_task_id = None
                            st.session_state.bicyclic_work_dir = None
                            st.session_state.bicyclic_results = None
                            st.session_state.bicyclic_error = {"error_message": "用户手动停止任务", "type": "User Cancelled"}
                            
                            # 清理URL参数
                            URLStateManager.clear_url_params()
                            
                            st.rerun()
                        else:
                            st.error("❌ 停止双环肽设计任务失败")
                else:
                    st.error("❌ 未找到要停止的设计进程")
                    
            except Exception as e:
                st.error(f"❌ 停止任务时发生错误: {e}")
                # 即使出错也清理状态，避免界面卡死
                st.session_state.bicyclic_task_id = None
                st.session_state.bicyclic_work_dir = None
                st.session_state.bicyclic_results = None
                st.session_state.bicyclic_error = {"error_message": f"停止任务出错: {e}", "type": "Stop Error"}
                st.rerun()
        
        if not st.session_state.bicyclic_error:
            try:
                work_dir = st.session_state.get('bicyclic_work_dir', None)
                status_data = get_designer_status(st.session_state.bicyclic_task_id, work_dir)
                
                if not status_data or 'state' not in status_data:
                    st.error("❌ 无法获取任务状态信息")
                    st.session_state.bicyclic_error = {"error_message": "无法获取任务状态", "type": "Status Error"}
                elif status_data.get('error'):
                    st.error(f"❌ 任务执行错误: {status_data['error']}")
                    st.session_state.bicyclic_error = {"error_message": status_data['error'], "type": "Task Error"}
                else:
                    current_state = status_data['state']
                    
                    if current_state in ['COMPLETED', 'SUCCESS']:
                        st.success("🎉 双环肽设计任务已完成！")
                        work_dir = st.session_state.get('bicyclic_work_dir', '/tmp')
                        results = load_designer_results(st.session_state.bicyclic_task_id, work_dir)
                        st.session_state.bicyclic_results = results
                        st.rerun()
                    
                    elif current_state in ['ERROR', 'FAILED', 'CANCELLED']:
                        error_msg = status_data.get('error', '任务失败')
                        st.error(f"❌ 双环肽设计任务失败: {error_msg}")
                        st.session_state.bicyclic_error = {"error_message": error_msg, "type": "Task Error"}
                    
                    elif current_state == 'RUNNING':
                        progress = status_data.get('progress', {})
                        current_status = progress.get('current_status', 'unknown')
                        
                        # 添加结构预测阶段的进度显示
                        if current_status == 'structure_prediction':
                            completed = progress.get('completed_tasks', 0)
                            total_tasks = progress.get('total_tasks', 1)
                            
                            if total_tasks > 0 and completed >= 0:
                                task_progress = min(completed / total_tasks, 1.0)
                                st.progress(task_progress, text=f"双环肽结构预测进度: {completed}/{total_tasks} 任务完成")
                            else:
                                st.progress(0.1, text="正在初始化双环肽结构预测任务...")
                        
                        elif current_status == 'evolving':
                            current_gen = progress.get('current_generation', 0)
                            total_gen = progress.get('total_generations', 1)
                            best_score = progress.get('best_score', 0.0)
                            current_best_sequences = progress.get('current_best_sequences', [])
                            
                            if current_gen > 0 and total_gen > 0:
                                gen_progress = min(current_gen / total_gen, 1.0)
                                st.progress(gen_progress, text=f"双环肽演化进度: 第 {current_gen}/{total_gen} 代 | 当前最佳评分: {best_score:.3f}")
                            else:
                                st.progress(0.0, text="准备开始双环肽演化...")
                            
                            if current_best_sequences:
                                display_gen = max(1, current_gen)
                                with st.expander(f"🏆 当前最佳双环肽序列 (第 {max(1, current_gen)} 代)", expanded=True):
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
                                        
                                        if sequence:
                                            st.markdown(f"**#{rank}** {score_color} 综合评分: {score:.3f} | ipTM: {iptm:.3f} | pLDDT: {plddt:.1f} | 代数: {generation}")
                                            
                                            # 高亮显示Cys位置
                                            highlighted_seq = ""
                                            for j, aa in enumerate(sequence):
                                                if aa == 'C':
                                                    highlighted_seq += f"**{aa}**"
                                                else:
                                                    highlighted_seq += aa
                                            
                                            st.markdown(f"序列: {highlighted_seq}")
                                            
                                            # 显示Cys位置
                                            cys_positions_display = [i+1 for i, aa in enumerate(sequence) if aa == 'C']
                                            st.caption(f"🔗 Cys位置: {', '.join(map(str, cys_positions_display))}")
                        
                        else:
                            # 通用状态和进度处理
                            st.progress(0.9, text="正在处理和分析双环肽设计结果...")
                        
                        # 显示整体进度信息
                        if 'progress_info' in progress:
                            progress_info = progress['progress_info']
                            if isinstance(progress_info, dict):
                                progress_value = progress_info.get('overall_progress', 0.5)
                                st.progress(progress_value, text=f"双环肽设计进度: {int(progress_value * 100)}%")
                        
                        time.sleep(5)
                        st.rerun()
                        
            except Exception as e:
                st.error(f"❌ 获取任务状态时发生错误: {e}")
                st.session_state.bicyclic_error = {"error_message": str(e), "type": "Status Check Error"}
    
    # 结果展示
    if st.session_state.bicyclic_results:
        st.divider()
        st.header("🏆 **步骤 3: 双环肽设计结果**", anchor=False)
        
        results = st.session_state.bicyclic_results
        best_sequences = results['best_sequences']
        evolution_history = results['evolution_history']
        
        st.subheader("📊 设计统计摘要", anchor=False)
        
        col_stats = st.columns(4)
        col_stats[0].metric("总设计数", len(best_sequences))
        high_quality_sequences = [seq for seq in best_sequences if seq.get('score', 0) >= 0.6]
        col_stats[1].metric("高质量设计", len(high_quality_sequences))
        col_stats[2].metric("Top 10 展示", min(10, len(high_quality_sequences)))
        if best_sequences:
            col_stats[3].metric("最高评分", f"{max(seq.get('score', 0) for seq in best_sequences):.3f}")
        
        st.subheader("🥇 最佳双环肽序列", anchor=False)
        
        top_sequences = high_quality_sequences[:10]
        
        if not top_sequences:
            st.warning("😔 没有找到高质量的双环肽设计。请尝试调整参数重新设计。")
        else:
            st.success(f"🎉 发现 {len(top_sequences)} 个高质量双环肽设计！")
            
            for i, seq_data in enumerate(top_sequences):
                rank = i + 1
                score = seq_data.get('score', 0)
                sequence = seq_data.get('sequence', '')
                
                score_color = "🟢" if score >= 0.8 else "🟡" if score >= 0.7 else "🟠"
                
                with st.expander(f"**第 {rank} 名** {score_color} 评分: {score:.3f}", expanded=(i < 3)):
                                        # 高亮显示Cys和环结构
                    highlighted_sequence = ""
                    cys_positions = []
                    
                    # 使用HTML样式而不是Markdown避免冲突
                    for j, aa in enumerate(sequence):
                        if aa == 'C':
                            highlighted_sequence += f'<span style="background-color: yellow; font-weight: bold; color: red;">{aa}</span>'
                            cys_positions.append(j+1)
                        else:
                            highlighted_sequence += aa
                    
                    st.markdown(f"**序列**: {highlighted_sequence}", unsafe_allow_html=True)
                    st.caption(f"🔗 半胱氨酸位置: {', '.join(map(str, cys_positions))}")
                    
                    # 显示连接体类型
                    bicyclic_config = st.session_state.get('bicyclic_config', {})
                    linker_type = bicyclic_config.get('linker_ccd', 'SEZ')
                    linker_descriptions = {
                        'SEZ': '1,3,5-trimethylbenzene (TRIS连接体)',
                        '29N': '1-[3,5-di(propanoyl)-1,3,5-triazinan-1-yl]propan-1-one (大环连接体)'
                    }
                    linker_desc = linker_descriptions.get(linker_type, f'{linker_type} 连接体')
                    st.info(f"🔗 **连接体类型**: {linker_type} - {linker_desc}", icon="⚡")
                    
                    # 显示预测的环结构
                    if len(cys_positions) == 3:
                        st.markdown("**🔗 预测环结构:**")
                        st.markdown(f"- 环1: Cys{cys_positions[0]} - Cys{cys_positions[2]} (包含残基 {cys_positions[0]}-{cys_positions[2]})")
                        st.markdown(f"- 环2: Cys{cys_positions[1]} - Cys{cys_positions[2]} (包含残基 {cys_positions[1]}-{cys_positions[2]})")
                        st.caption("💡 两个环在末端Cys处相交，形成独特的双环结构")
                    
                    col_metrics = st.columns(4)
                    col_metrics[0].metric("综合评分", f"{score:.3f}")
                    col_metrics[1].metric("ipTM", f"{seq_data.get('iptm', 0):.3f}")
                    col_metrics[2].metric("pLDDT", f"{seq_data.get('plddt', 0):.3f}")
                    col_metrics[3].metric("发现代数", seq_data.get('generation', 'N/A'))
                    
                    # 结构文件下载
                    results_path = seq_data.get('results_path', '')
                    if results_path and os.path.exists(results_path):
                        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
                        if cif_files:
                            cif_file = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
                            cif_path = os.path.join(results_path, cif_file)
                            
                            try:
                                with open(cif_path, 'r') as f:
                                    cif_data = f.read()
                                
                                col_download = st.columns(2)
                                with col_download[0]:
                                    st.download_button(
                                        label="📄 下载双环肽结构 (CIF)",
                                        data=cif_data,
                                        file_name=f"bicyclic_peptide_rank_{rank}.cif",
                                        mime="chemical/x-cif",
                                        use_container_width=True,
                                        key=f"download_bicyclic_cif_{i}"
                                    )
                                
                                with col_download[1]:
                                    if st.button("🔬 查看双环结构", use_container_width=True, key=f"view_bicyclic_{i}"):
                                        if f"show_bicyclic_3d_{i}" not in st.session_state:
                                            st.session_state[f"show_bicyclic_3d_{i}"] = False
                                        st.session_state[f"show_bicyclic_3d_{i}"] = not st.session_state.get(f"show_bicyclic_3d_{i}", False)
                                        st.rerun()
                                
                                if st.session_state.get(f"show_bicyclic_3d_{i}", False):
                                    st.markdown("---")
                                    st.markdown("**🔬 双环肽3D结构**")
                                    
                                    try:
                                        structure = read_cif_from_string(cif_data)
                                        protein_bfactors = extract_protein_residue_bfactors(structure)
                                        
                                        view_html = visualize_structure_py3dmol(
                                            cif_content=cif_data,
                                            residue_bfactors=protein_bfactors,
                                            protein_style='cartoon',
                                            ligand_style='ball-and-stick',
                                            spin=False,
                                            color_scheme='pLDDT'
                                        )
                                        st.components.v1.html(view_html, height=500, scrolling=False)
                                        
                                        st.markdown("**🎨 颜色编码:**")
                                        st.markdown("""
                                        - 🔵 **蓝色**: 高置信度区域 (pLDDT > 90)
                                        - 🟡 **黄色**: 中等置信度 (pLDDT 50-90)  
                                        - 🟠 **橙/红色**: 低置信度区域 (pLDDT < 50)
                                        - ⚡ **亮显**: 半胱氨酸残基及二硫键
                                        """)
                                        
                                    except Exception as e:
                                        st.error(f"❌ 3D结构显示失败: {str(e)}")
                            except Exception as e:
                                st.caption(f"⚠️ 结构文件读取失败: {str(e)}")
        
        # 演化历史图表
        st.subheader("📈 双环肽演化历史", anchor=False)
        
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
                        title="双环肽设计演化历史"
                    )
                    
                    st.altair_chart(chart, use_container_width=True)
                else:
                    st.line_chart(chart_data.set_index('代数'))
                    
            except ImportError:
                st.line_chart(chart_data.set_index('代数'))
        else:
            st.info("暂无演化历史数据。")
        
        # 结果下载
        st.markdown("**📥 下载双环肽设计结果**")
        
        col_download = st.columns(2)
        
        with col_download[0]:
            if top_sequences:
                sequences_csv = pd.DataFrame([
                    {
                        **seq_data,
                        'cys_positions': ', '.join([str(i+1) for i, aa in enumerate(seq_data.get('sequence', '')) if aa == 'C']),
                        'ring_structure': 'Bicyclic'
                    }
                    for seq_data in top_sequences
                ])
                sequences_csv_str = sequences_csv.to_csv(index=False)
                
                st.download_button(
                    label="📊 双环肽序列 (CSV)",
                    data=sequences_csv_str,
                    file_name=f"bicyclic_peptides_{st.session_state.bicyclic_task_id}.csv",
                    mime="text/csv",
                    use_container_width=True
                )
        
        with col_download[1]:
            results_json = json.dumps({
                'design_type': 'bicyclic',
                'summary': {
                    'total_sequences': len(best_sequences),
                    'high_quality_sequences': len(high_quality_sequences),
                    'top_selected': len(top_sequences)
                },
                'top_sequences': top_sequences,
                'evolution_history': evolution_history
            }, indent=2)
            
            st.download_button(
                label="📦 完整结果 (JSON)",
                data=results_json,
                file_name=f"bicyclic_design_results_{st.session_state.bicyclic_task_id}.json",
                mime="application/json",
                use_container_width=True
            )
    
    # 错误处理
    if st.session_state.bicyclic_error:
        st.error("ℹ️ 双环肽设计任务执行失败，详细信息如下：")
        st.json(st.session_state.bicyclic_error)
        
        col_reset = st.columns(2)
        with col_reset[0]:
            if st.button("🔄 重置设计器", key="reset_bicyclic_designer", type="secondary", use_container_width=True):
                URLStateManager.clear_url_params()
                for key in ['bicyclic_task_id', 'bicyclic_results', 'bicyclic_error', 'bicyclic_config', 'bicyclic_components', 'bicyclic_constraints']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        
        with col_reset[1]:
            if st.button("🔧 保留配置重新设计", key="redesign_bicyclic", type="primary", use_container_width=True):
                URLStateManager.clear_url_params()
                for key in ['bicyclic_task_id', 'bicyclic_results', 'bicyclic_error']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
