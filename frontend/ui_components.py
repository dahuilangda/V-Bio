
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

def render_contact_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running):
    """渲染Contact约束的UI配置"""
    st.markdown("**Contact约束配置** - 定义两个残基间的接触距离")
    
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
                help="选择第一个残基所在的链"
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
        token1_residue = st.number_input(
            "残基编号",
            min_value=1,
            value=current_token1_residue,
            key=f"{key_prefix}_token1_residue",
            disabled=is_running,
            help="残基编号 (从1开始)"
        )
        
        if token1_residue != current_token1_residue:
            constraint['token1_residue'] = token1_residue
            st.rerun()
        
        if available_chains and token1_chain in available_chains:
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(st.session_state.components, token1_chain, token1_residue)
            if is_valid:
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
                help="选择第二个残基所在的链"
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
        token2_residue = st.number_input(
            "残基编号",
            min_value=1,
            value=current_token2_residue,
            key=f"{key_prefix}_token2_residue",
            disabled=is_running,
            help="残基编号 (从1开始)"
        )
        
        if token2_residue != current_token2_residue:
            constraint['token2_residue'] = token2_residue
            st.rerun()
        
        if available_chains and token2_chain in available_chains:
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(st.session_state.components, token2_chain, token2_residue)
            if is_valid2:
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

def render_bond_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running):
    """渲染Bond约束的UI配置"""
    st.markdown("**Bond约束配置** - 定义两个原子间的共价键")
    
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
                help="选择第一个原子所在的链"
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
        if available_chains and atom1_chain in available_chains:
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(st.session_state.components, atom1_chain, atom1_residue)
            available_atoms = get_available_atoms(st.session_state.components, atom1_chain, atom1_residue, molecule_type)
        else:
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
            help="必须选择具体的原子名称"
        )
        
        if atom1_atom != current_atom1_atom:
            constraint['atom1_atom'] = atom1_atom
            st.rerun()
    
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
                help="选择第二个原子所在的链"
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
        if available_chains and atom2_chain in available_chains:
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(st.session_state.components, atom2_chain, atom2_residue)
            available_atoms2 = get_available_atoms(st.session_state.components, atom2_chain, atom2_residue, molecule_type2)
        else:
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
            help="必须选择具体的原子名称"
        )
        
        if atom2_atom != current_atom2_atom:
            constraint['atom2_atom'] = atom2_atom
            st.rerun()
    
    constraint.update({
        'atom1_chain': atom1_chain,
        'atom1_residue': atom1_residue,
        'atom1_atom': atom1_atom,
        'atom2_chain': atom2_chain,
        'atom2_residue': atom2_residue,
        'atom2_atom': atom2_atom
    })


