
import streamlit as st
import py3Dmol

from frontend.utils import (
    get_available_chain_ids, 
    get_residue_info, 
    get_available_atoms, 
    read_cif_from_string,
    extract_protein_residue_bfactors, 
    get_color_from_bfactor
)

def render_contact_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running, components=None):
    """渲染Contact约束的UI配置"""
    st.markdown("**Contact约束配置** - 定义两个残基间的接触距离")
    
    # 获取当前组件数据 - 优先使用传入的组件数据
    def _get_current_components():
        """获取当前上下文中的组件数据"""
        if components is not None:
            return components
        elif hasattr(st.session_state, 'bicyclic_components'):
            return st.session_state.bicyclic_components
        elif hasattr(st.session_state, 'designer_components'):
            return st.session_state.designer_components
        elif hasattr(st.session_state, 'components'):
            return st.session_state.components
        else:
            return []
    
    # 检查是否包含配体，如果是，显示警告
    from frontend.utils import get_chain_type
    current_components = _get_current_components()
    chain1_type = get_chain_type(current_components, constraint.get('token1_chain', 'A')) if current_components else 'protein'
    chain2_type = get_chain_type(current_components, constraint.get('token2_chain', 'A')) if current_components else 'protein'
    
    if chain1_type == 'ligand' or chain2_type == 'ligand':
        st.warning("⚠️ **建议使用Pocket约束**：Contact约束不能用于小分子配体，建议切换到Pocket约束以获得更好的效果。")
        st.info("💡 Pocket约束专为蛋白质-配体结合设计，能更准确地处理小分子与蛋白质口袋的相互作用。")
    
    # Token 1配置
    st.markdown("**Token 1 (残基 1)**")
    token1_cols = st.columns(2)
    
    with token1_cols[0]:
        current_token1_chain = constraint.get('token1_chain', 'A')
        if current_token1_chain not in available_chains and available_chains:
            current_token1_chain = available_chains[0]
        
        if available_chains:
            chain_index = available_chains.index(current_token1_chain) if current_token1_chain in available_chains else 0
            token1_chain = st.selectbox(
                "链 ID",
                options=available_chains,
                index=chain_index,
                format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                key=f"{key_prefix}_token1_chain",
                disabled=is_running,
                help="选择第一个残基所在的链。可选择BINDER_CHAIN来引用即将设计的结合肽"
            )
            
            if token1_chain != current_token1_chain:
                constraint['token1_chain'] = token1_chain
                st.rerun()
        else:
            token1_chain = st.text_input(
                "链 ID",
                value=current_token1_chain,
                key=f"{key_prefix}_token1_chain",
                disabled=is_running,
                help="请先添加组分序列"
            )
    
    with token1_cols[1]:
        current_token1_residue = constraint.get('token1_residue', 1)
        
        # 检查是否是配体类型
        token1_chain = constraint.get('token1_chain', 'A')
        chain1_type = 'protein'  # 默认值
        current_components = _get_current_components()
        if current_components and token1_chain != 'BINDER_CHAIN':
            from frontend.utils import get_chain_type
            chain1_type = get_chain_type(current_components, token1_chain)
        
        # 为配体类型调整提示信息
        if chain1_type == 'ligand':
            residue_help = "对于配体分子，通常使用1（系统会自动转换为Boltz所需的索引格式）"
            min_residue = 1
            max_residue = 1
        else:
            residue_help = "残基编号 (从1开始)"
            min_residue = 1
            max_residue = None
        
        token1_residue = st.number_input(
            "残基编号",
            min_value=min_residue,
            max_value=max_residue,
            value=current_token1_residue,
            key=f"{key_prefix}_token1_residue",
            disabled=is_running,
            help=residue_help
        )
        
        if token1_residue != current_token1_residue:
            constraint['token1_residue'] = token1_residue
            st.rerun()
        
        # 显示残基信息，对BINDER_CHAIN特殊处理
        if token1_chain == 'BINDER_CHAIN':
            st.caption(f"🎯 设计中的结合肽，残基 {token1_residue}")
        elif available_chains and token1_chain in available_chains:
            from frontend.utils import get_residue_info
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(current_components, token1_chain, token1_residue)
            if chain1_type == 'ligand':
                st.caption(f"💊 配体分子 (将自动使用原子名称或残基索引)")
                is_valid = True  # 配体总是有效的
            elif seq_length == 0:
                # 序列为空时的提示
                st.info(f"ℹ️ 请先完成链 {token1_chain} 的序列输入")
            elif is_valid:
                st.caption(f"📍 {residue_info}")
            else:
                st.error(f"❌ {residue_info} (序列长度: {seq_length})")
        else:
            molecule_type = 'protein'
    
    # Token 2配置
    st.markdown("**Token 2 (残基 2)**")
    token2_cols = st.columns(2)
    
    with token2_cols[0]:
        current_token2_chain = constraint.get('token2_chain', 'B')
        if current_token2_chain not in available_chains and available_chains:
            current_token2_chain = available_chains[1] if len(available_chains) > 1 else available_chains[0]
        
        if available_chains:
            chain_index = available_chains.index(current_token2_chain) if current_token2_chain in available_chains else (1 if len(available_chains) > 1 else 0)
            token2_chain = st.selectbox(
                "链 ID",
                options=available_chains,
                index=chain_index,
                format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                key=f"{key_prefix}_token2_chain",
                disabled=is_running,
                help="选择第二个残基所在的链。可选择BINDER_CHAIN来引用即将设计的结合肽"
            )
            
            if token2_chain != current_token2_chain:
                constraint['token2_chain'] = token2_chain
                st.rerun()
        else:
            token2_chain = st.text_input(
                "链 ID",
                value=current_token2_chain,
                key=f"{key_prefix}_token2_chain",
                disabled=is_running,
                help="请先添加组分序列"
            )
    
    with token2_cols[1]:
        current_token2_residue = constraint.get('token2_residue', 1)
        
        # 检查是否是配体类型
        token2_chain = constraint.get('token2_chain', 'B')
        chain2_type = 'protein'  # 默认值
        current_components = _get_current_components()
        if current_components and token2_chain != 'BINDER_CHAIN':
            from frontend.utils import get_chain_type
            chain2_type = get_chain_type(current_components, token2_chain)
        
        # 为配体类型调整提示信息
        if chain2_type == 'ligand':
            residue_help = "对于配体分子，通常使用1（系统会自动转换为Boltz所需的索引格式）"
            min_residue = 1
            max_residue = 1
        else:
            residue_help = "残基编号 (从1开始)"
            min_residue = 1
            max_residue = None
        
        token2_residue = st.number_input(
            "残基编号",
            min_value=min_residue,
            max_value=max_residue,
            value=current_token2_residue,
            key=f"{key_prefix}_token2_residue",
            disabled=is_running,
            help=residue_help
        )
        
        if token2_residue != current_token2_residue:
            constraint['token2_residue'] = token2_residue
            st.rerun()
        
        # 显示残基信息，对BINDER_CHAIN特殊处理
        if token2_chain == 'BINDER_CHAIN':
            st.caption(f"🎯 设计中的结合肽，残基 {token2_residue}")
        elif available_chains and token2_chain in available_chains:
            from frontend.utils import get_residue_info
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(current_components, token2_chain, token2_residue)
            if chain2_type == 'ligand':
                st.caption(f"💊 配体分子 (将自动使用原子名称或残基索引)")
                is_valid2 = True  # 配体总是有效的
            elif seq_length2 == 0:
                # 序列为空时的提示
                st.info(f"ℹ️ 请先完成链 {token2_chain} 的序列输入")
            elif is_valid2:
                st.caption(f"📍 {residue_info2}")
            else:
                st.error(f"❌ {residue_info2} (序列长度: {seq_length2})")
        else:
            molecule_type2 = 'protein'
            is_valid2 = True
    
    distance_cols = st.columns(2)
    with distance_cols[0]:
        current_max_distance = constraint.get('max_distance', 5.0)
        max_distance = st.number_input(
            "最大距离 (Å)",
            min_value=1.0,
            max_value=50.0,
            value=current_max_distance,
            step=0.5,
            key=f"{key_prefix}_max_distance",
            disabled=is_running,
            help="两个残基之间的最大允许距离（埃）"
        )
        
        if max_distance != current_max_distance:
            constraint['max_distance'] = max_distance
            st.rerun()
    
    with distance_cols[1]:
        current_force_constraint = constraint.get('force', False)
        force_constraint = st.checkbox(
            "强制执行约束",
            value=current_force_constraint,
            key=f"{key_prefix}_force",
            disabled=is_running,
            help="是否使用势能函数强制执行此约束"
        )
        
        if force_constraint != current_force_constraint:
            constraint['force'] = force_constraint
            st.rerun()
    
    constraint.update({
        'token1_chain': token1_chain,
        'token1_residue': token1_residue,
        'token2_chain': token2_chain,
        'token2_residue': token2_residue,
        'max_distance': max_distance,
        'force': force_constraint
    })

def render_bond_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running, components=None):
    """渲染Bond约束的UI配置"""
    st.markdown("**Bond约束配置** - 定义两个原子间的共价键")
    
    # 获取当前组件数据 - 优先使用传入的组件数据
    def _get_current_components():
        """获取当前上下文中的组件数据"""
        if components is not None:
            return components
        elif hasattr(st.session_state, 'bicyclic_components'):
            return st.session_state.bicyclic_components
        elif hasattr(st.session_state, 'designer_components'):
            return st.session_state.designer_components
        elif hasattr(st.session_state, 'components'):
            return st.session_state.components
        else:
            return []
    
    st.markdown("**Atom 1 (原子 1)**")
    atom1_cols = st.columns(3)
    
    with atom1_cols[0]:
        current_atom1_chain = constraint.get('atom1_chain', 'A')
        if current_atom1_chain not in available_chains and available_chains:
            current_atom1_chain = available_chains[0]
        
        if available_chains:
            chain_index = available_chains.index(current_atom1_chain) if current_atom1_chain in available_chains else 0
            atom1_chain = st.selectbox(
                "链 ID",
                options=available_chains,
                index=chain_index,
                format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                key=f"{key_prefix}_atom1_chain",
                disabled=is_running,
                help="选择第一个原子所在的链。可选择BINDER_CHAIN来引用即将设计的结合肽"
            )
            
            if atom1_chain != current_atom1_chain:
                constraint['atom1_chain'] = atom1_chain
                st.rerun()
        else:
            atom1_chain = st.text_input(
                "链 ID",
                value=current_atom1_chain,
                key=f"{key_prefix}_atom1_chain",
                disabled=is_running
            )
    
    with atom1_cols[1]:
        current_atom1_residue = constraint.get('atom1_residue', 1)
        atom1_residue = st.number_input(
            "残基编号",
            min_value=1,
            value=current_atom1_residue,
            key=f"{key_prefix}_atom1_residue",
            disabled=is_running,
            help="残基编号 (从1开始)"
        )
        
        if atom1_residue != current_atom1_residue:
            constraint['atom1_residue'] = atom1_residue
            st.rerun()
    
    with atom1_cols[2]:
        # 对BINDER_CHAIN特殊处理原子选择
        if atom1_chain == 'BINDER_CHAIN':
            # 为结合肽提供常见的蛋白质原子选项
            available_atoms = ['CA', 'CB', 'N', 'C', 'O', 'CG', 'CD', 'CE', 'CZ', 'OG', 'OH', 'SD', 'SG', 'NE', 'NH1', 'NH2', 'ND1', 'ND2', 'NE2']
            molecule_type = 'protein'
        elif available_chains and atom1_chain in available_chains:
            from frontend.utils import get_residue_info, get_available_atoms
            current_components = _get_current_components()
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(current_components, atom1_chain, atom1_residue)
            available_atoms = get_available_atoms(current_components, atom1_chain, atom1_residue, molecule_type)
        else:
            from frontend.utils import get_available_atoms
            available_atoms = get_available_atoms(None, None, None, 'protein')
            molecule_type = 'protein'
        
        available_atoms = [a for a in available_atoms if a.strip()]
        
        current_atom1_atom = constraint.get('atom1_atom', 'CA')
        if current_atom1_atom not in available_atoms and available_atoms:
            current_atom1_atom = available_atoms[0]
        
        atom_index = available_atoms.index(current_atom1_atom) if current_atom1_atom in available_atoms else 0
        atom1_atom = st.selectbox(
            "原子名 (必选)",
            options=available_atoms,
            index=atom_index,
            key=f"{key_prefix}_atom1_atom",
            disabled=is_running,
            help="必须选择具体的原子名称。对于BINDER_CHAIN，将根据生成的氨基酸类型动态匹配"
        )
        
        if atom1_atom != current_atom1_atom:
            constraint['atom1_atom'] = atom1_atom
            st.rerun()
        
        # 显示原子信息
        if atom1_chain == 'BINDER_CHAIN':
            st.caption(f"🎯 结合肽残基 {atom1_residue} 的 {atom1_atom} 原子")
        elif available_chains and atom1_chain in available_chains:
            from frontend.utils import get_residue_info
            current_components = _get_current_components()
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(current_components, atom1_chain, atom1_residue)
            if seq_length == 0:
                # 序列为空时的提示
                st.info(f"ℹ️ 请先完成链 {atom1_chain} 的序列输入")
            elif is_valid:
                st.caption(f"📍 {residue_info} - {atom1_atom}")
            else:
                st.error(f"❌ {residue_info}")
    
    st.markdown("**Atom 2 (原子 2)**")
    atom2_cols = st.columns(3)
    
    with atom2_cols[0]:
        current_atom2_chain = constraint.get('atom2_chain', 'B')
        if current_atom2_chain not in available_chains and available_chains:
            current_atom2_chain = available_chains[1] if len(available_chains) > 1 else available_chains[0]
        
        if available_chains:
            chain_index = available_chains.index(current_atom2_chain) if current_atom2_chain in available_chains else (1 if len(available_chains) > 1 else 0)
            atom2_chain = st.selectbox(
                "链 ID",
                options=available_chains,
                index=chain_index,
                format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                key=f"{key_prefix}_atom2_chain",
                disabled=is_running,
                help="选择第二个原子所在的链。可选择BINDER_CHAIN来引用即将设计的结合肽"
            )
            
            if atom2_chain != current_atom2_chain:
                constraint['atom2_chain'] = atom2_chain
                st.rerun()
        else:
            atom2_chain = st.text_input(
                "链 ID",
                value=current_atom2_chain,
                key=f"{key_prefix}_atom2_chain",
                disabled=is_running
            )
    
    with atom2_cols[1]:
        current_atom2_residue = constraint.get('atom2_residue', 1)
        atom2_residue = st.number_input(
            "残基编号",
            min_value=1,
            value=current_atom2_residue,
            key=f"{key_prefix}_atom2_residue",
            disabled=is_running,
            help="残基编号 (从1开始)"
        )
        
        if atom2_residue != current_atom2_residue:
            constraint['atom2_residue'] = atom2_residue
            st.rerun()
    
    with atom2_cols[2]:
        # 对BINDER_CHAIN特殊处理原子选择
        if atom2_chain == 'BINDER_CHAIN':
            # 为结合肽提供常见的蛋白质原子选项
            available_atoms2 = ['CA', 'CB', 'N', 'C', 'O', 'CG', 'CD', 'CE', 'CZ', 'OG', 'OH', 'SD', 'SG', 'NE', 'NH1', 'NH2', 'ND1', 'ND2', 'NE2']
        elif available_chains and atom2_chain in available_chains:
            from frontend.utils import get_residue_info, get_available_atoms
            current_components = _get_current_components()
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(current_components, atom2_chain, atom2_residue)
            available_atoms2 = get_available_atoms(current_components, atom2_chain, atom2_residue, molecule_type2)
        else:
            from frontend.utils import get_available_atoms
            available_atoms2 = get_available_atoms(None, None, None, 'protein')
        
        available_atoms2 = [a for a in available_atoms2 if a.strip()]
        
        current_atom2_atom = constraint.get('atom2_atom', 'CA')
        if current_atom2_atom not in available_atoms2 and available_atoms2:
            current_atom2_atom = available_atoms2[0]
        
        atom_index2 = available_atoms2.index(current_atom2_atom) if current_atom2_atom in available_atoms2 else 0
        atom2_atom = st.selectbox(
            "原子名 (必选)",
            options=available_atoms2,
            index=atom_index2,
            key=f"{key_prefix}_atom2_atom",
            disabled=is_running,
            help="必须选择具体的原子名称。对于BINDER_CHAIN，将根据生成的氨基酸类型动态匹配"
        )
        
        if atom2_atom != current_atom2_atom:
            constraint['atom2_atom'] = atom2_atom
            st.rerun()
        
        # 显示原子信息
        if atom2_chain == 'BINDER_CHAIN':
            st.caption(f"🎯 结合肽残基 {atom2_residue} 的 {atom2_atom} 原子")
        elif available_chains and atom2_chain in available_chains:
            from frontend.utils import get_residue_info
            current_components = _get_current_components()
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(current_components, atom2_chain, atom2_residue)
            if seq_length2 == 0:
                # 序列为空时的提示
                st.info(f"ℹ️ 请先完成链 {atom2_chain} 的序列输入")
            elif is_valid2:
                st.caption(f"📍 {residue_info2} - {atom2_atom}")
            else:
                st.error(f"❌ {residue_info2}")
    
    constraint.update({
        'atom1_chain': atom1_chain,
        'atom1_residue': atom1_residue,
        'atom1_atom': atom1_atom,
        'atom2_chain': atom2_chain,
        'atom2_residue': atom2_residue,
        'atom2_atom': atom2_atom
    })

def render_pocket_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running, components=None):
    """渲染Pocket约束的UI配置"""
    st.markdown("**Pocket约束配置** - 定义分子与蛋白质口袋的结合约束")
    st.info("💡 **Pocket约束专用于蛋白质-小分子相互作用**：精确处理小分子配体与蛋白质结合口袋的相互作用")
    
    # 获取当前组件数据 - 优先使用传入的组件数据
    def _get_current_components():
        """获取当前上下文中的组件数据"""
        if components is not None:
            return components
        elif hasattr(st.session_state, 'bicyclic_components'):
            return st.session_state.bicyclic_components
        elif hasattr(st.session_state, 'designer_components'):
            return st.session_state.designer_components
        elif hasattr(st.session_state, 'components'):
            return st.session_state.components
        else:
            return []
    
    # Binder配置
    # st.markdown("**结合分子 (Binder)**")
    binder_cols = st.columns(2)
    
    with binder_cols[0]:
        current_binder = constraint.get('binder', 'BINDER_CHAIN')
        if current_binder not in available_chains and available_chains:
            # 对于pocket约束，binder通常是BINDER_CHAIN
            current_binder = 'BINDER_CHAIN' if 'BINDER_CHAIN' in available_chains else available_chains[0]
        
        if available_chains:
            chain_index = available_chains.index(current_binder) if current_binder in available_chains else 0
            binder = st.selectbox(
                "结合分子链 ID",
                options=available_chains,
                index=chain_index,
                format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                key=f"{key_prefix}_binder",
                disabled=is_running,
                help="选择作为结合分子的链ID，通常是小分子配体或需要结合的分子"
            )
            
            if binder != current_binder:
                constraint['binder'] = binder
                st.rerun()
        else:
            binder = st.text_input(
                "结合分子链 ID",
                value=current_binder,
                key=f"{key_prefix}_binder",
                disabled=is_running,
                help="请先添加组分序列"
            )
    
    with binder_cols[1]:
        if binder == 'BINDER_CHAIN':
            st.caption("🎯 即将设计的结合分子")
        elif available_chains and binder in available_chains:
            # 检测分子类型并显示相应信息 - 使用正确的组件数据源
            from frontend.utils import get_chain_type
            current_components = _get_current_components()
            binder_type = get_chain_type(current_components, binder) if current_components else 'protein'
            if binder_type == 'ligand':
                st.caption(f"📍 {chain_descriptions.get(binder, f'链 {binder}')}")
            else:
                st.caption(f"📍 {chain_descriptions.get(binder, f'链 {binder}')}")
    
    # Contacts配置
    st.markdown("**口袋接触点 (Contacts)**")
    st.caption("定义构成结合口袋的残基/原子")
    
    contacts = constraint.get('contacts', [])
    if not contacts:
        contacts = [['A', 1]]  # 默认添加一个接触点
        constraint['contacts'] = contacts
    
    contacts_to_delete = []
    for j, contact in enumerate(contacts):
        contact_cols = st.columns([2, 2, 1])
        
        with contact_cols[0]:
            contact_chain = contact[0] if len(contact) > 0 else 'A'
            if contact_chain not in available_chains and available_chains:
                contact_chain = available_chains[0]
            
            if available_chains:
                chain_index = available_chains.index(contact_chain) if contact_chain in available_chains else 0
                new_contact_chain = st.selectbox(
                    f"接触点 {j+1} 链ID",
                    options=available_chains,
                    index=chain_index,
                    format_func=lambda x: chain_descriptions.get(x, f"链 {x}"),
                    key=f"{key_prefix}_contact_{j}_chain",
                    disabled=is_running,
                    help="构成口袋的残基所在链"
                )
                # 修复：添加链ID变更检测和更新
                if new_contact_chain != contact_chain:
                    contact[0] = new_contact_chain
                    constraint['contacts'] = contacts
                    st.rerun()
                else:
                    contact[0] = new_contact_chain
        
        with contact_cols[1]:
            # 检查是否为配体，如果是配体提供特殊处理
            from frontend.utils import get_chain_type, get_residue_info
            current_components = _get_current_components()
            contact_chain_type = get_chain_type(current_components, contact[0]) if current_components else 'protein'
            
            contact_residue = contact[1] if len(contact) > 1 else 1
            new_contact_residue = st.number_input(
                f"接触点 {j+1} 残基",
                min_value=1,
                value=contact_residue,
                key=f"{key_prefix}_contact_{j}_residue",
                disabled=is_running,
                help="配体残基编号或原子名称" if contact_chain_type == 'ligand' else "残基编号"
            )
            
            # 显示残基信息 - 与contact约束保持一致的显示方式
            if available_chains and contact[0] in available_chains:
                if contact_chain_type == 'ligand':
                    st.caption("💊 配体分子 (将自动使用原子名称)")
                else:
                    residue_info, molecule_type, seq_length, is_valid = get_residue_info(current_components, contact[0], new_contact_residue)
                    if seq_length == 0:
                        # 序列为空时的提示
                        st.info(f"ℹ️ 请先完成链 {contact[0]} 的序列输入")
                    elif is_valid:
                        st.caption(f"📍 {residue_info}")
                    else:
                        st.error(f"❌ {residue_info} (序列长度: {seq_length})")
            
            # 修复：添加残基变更检测和更新
            if new_contact_residue != contact_residue:
                contact[1] = new_contact_residue
                constraint['contacts'] = contacts
                st.rerun()
            else:
                contact[1] = new_contact_residue
        
        with contact_cols[2]:
            if st.button("🗑️", key=f"{key_prefix}_del_contact_{j}", help="删除此接触点", disabled=is_running):
                contacts_to_delete.append(j)
    
    # 删除标记的接触点
    for j in reversed(contacts_to_delete):
        del contacts[j]
    
    if contacts_to_delete:
        constraint['contacts'] = contacts
        st.rerun()
    
    # 添加新接触点按钮
    if st.button("➕ 添加接触点", key=f"{key_prefix}_add_contact", disabled=is_running, help="添加新的口袋接触点"):
        contacts.append(['A', 1])
        constraint['contacts'] = contacts
        st.rerun()
    
    # 距离和力参数
    distance_force_cols = st.columns(2)
    with distance_force_cols[0]:
        current_max_distance = constraint.get('max_distance', 5.0)
        max_distance = st.number_input(
            "最大距离 (Å)",
            min_value=1.0,
            max_value=50.0,
            value=current_max_distance,
            step=0.5,
            key=f"{key_prefix}_pocket_max_distance",
            disabled=is_running,
            help="结合肽与口袋接触点的最大允许距离（埃）"
        )
        
        if max_distance != current_max_distance:
            constraint['max_distance'] = max_distance
            st.rerun()
    
    with distance_force_cols[1]:
        current_force_constraint = constraint.get('force', False)
        force_constraint = st.checkbox(
            "强制执行约束",
            value=current_force_constraint,
            key=f"{key_prefix}_pocket_force",
            disabled=is_running,
            help="是否使用势能函数强制执行此口袋约束"
        )
        
        if force_constraint != current_force_constraint:
            constraint['force'] = force_constraint
            st.rerun()
    
    constraint.update({
        'binder': binder,
        'contacts': contacts,
        'max_distance': max_distance,
        'force': force_constraint
    })


