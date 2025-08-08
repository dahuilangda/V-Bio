import streamlit as st
import requests
import time
import json
import zipfile
import io
import yaml
import string
import uuid
import py3Dmol
import re
import subprocess
from Bio.PDB import MMCIFParser, PDBIO
from Bio.PDB.Structure import Structure
import math
import os
import pandas as pd
import glob
from datetime import datetime
import tempfile
import random
from streamlit_ketcher import st_ketcher
import hashlib
import shutil

try:
    import psutil
except ImportError:
    psutil = None

API_URL = "http://127.0.0.1:5000"
TYPE_TO_DISPLAY = {
    'protein': '🧬 蛋白质',
    'ligand': '💊 小分子',
    'dna': '🔗 DNA',
    'rna': '📜 RNA'
}

TYPE_SPECIFIC_INFO = {
    'protein': {
        'placeholder': "例如: MVSKGEELFTGVVPILVELDGDVNGHKFSVSGEGEGDATYGKLTLKFICTTGKLPVPWPTLV...",
        'help': "请输入标准的单字母氨基酸序列。"
    },
    'dna': {
        'placeholder': "例如: GTCGAC... (A, T, C, G)",
        'help': "请输入标准的单字母脱氧核糖核酸序列 (A, T, C, G)。"
    },
    'rna': {
        'placeholder': "例如: GUCGAC... (A, U, C, G)",
        'help': "请输入标准的单字母核糖核酸序列 (A, U, C, G)。"
    }
}

# Designer 相关配置
DESIGNER_CONFIG = {
    'work_dir': '/tmp/boltz_designer',
    'api_token': os.getenv('API_SECRET_TOKEN', 'your_default_api_token'),
    'server_url': API_URL
}

# MSA 缓存配置
MSA_CACHE_CONFIG = {
    'cache_dir': '/tmp/boltz_msa_cache',
    'max_cache_size_gb': 5.0,  # 最大缓存大小（GB）
    'cache_expiry_days': 30,   # 缓存过期时间（天）
    'enable_cache': True       # 是否启用缓存
}

# 氨基酸三字母到单字母的映射
AMINO_ACID_MAPPING = {
    'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C',
    'GLU': 'E', 'GLN': 'Q', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I',
    'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P',
    'SER': 'S', 'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V'
}

# 氨基酸特异性原子名
AMINO_ACID_ATOMS = {
    'A': ['N', 'CA', 'C', 'O', 'CB'],  # Alanine
    'R': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'NE', 'CZ', 'NH1', 'NH2'],  # Arginine
    'N': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'ND2'],  # Asparagine
    'D': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'OD2'],  # Aspartic acid
    'C': ['N', 'CA', 'C', 'O', 'CB', 'SG'],  # Cysteine
    'E': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OE1', 'OE2'],  # Glutamic acid
    'Q': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OE1', 'NE2'],  # Glutamine
    'G': ['N', 'CA', 'C', 'O'],  # Glycine
    'H': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'ND1', 'CD2', 'CE1', 'NE2'],  # Histidine
    'I': ['N', 'CA', 'C', 'O', 'CB', 'CG1', 'CG2', 'CD1'],  # Isoleucine
    'L': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2'],  # Leucine
    'K': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'CE', 'NZ'],  # Lysine
    'M': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'SD', 'CE'],  # Methionine
    'F': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ'],  # Phenylalanine
    'P': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD'],  # Proline
    'S': ['N', 'CA', 'C', 'O', 'CB', 'OG'],  # Serine
    'T': ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2'],  # Threonine
    'W': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'NE1', 'CE2', 'CE3', 'CZ2', 'CZ3', 'CH2'],  # Tryptophan
    'Y': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ', 'OH'],  # Tyrosine
    'V': ['N', 'CA', 'C', 'O', 'CB', 'CG1', 'CG2']  # Valine
}

# DNA核苷酸特异性原子名
DNA_BASE_ATOMS = {
    'A': ['N1', 'C2', 'N3', 'C4', 'C5', 'C6', 'N6', 'N7', 'C8', 'N9'],  # Adenine
    'T': ['N1', 'C2', 'O2', 'N3', 'C4', 'O4', 'C5', 'C7', 'C6'],  # Thymine
    'G': ['N1', 'C2', 'N2', 'N3', 'C4', 'C5', 'C6', 'O6', 'N7', 'C8', 'N9'],  # Guanine
    'C': ['N1', 'C2', 'O2', 'N3', 'C4', 'N4', 'C5', 'C6']  # Cytosine
}

# RNA核苷酸特异性原子名
RNA_BASE_ATOMS = {
    'A': ['N1', 'C2', 'N3', 'C4', 'C5', 'C6', 'N6', 'N7', 'C8', 'N9'],  # Adenine
    'U': ['N1', 'C2', 'O2', 'N3', 'C4', 'O4', 'C5', 'C6'],  # Uracil
    'G': ['N1', 'C2', 'N2', 'N3', 'C4', 'C5', 'C6', 'O6', 'N7', 'C8', 'N9'],  # Guanine
    'C': ['N1', 'C2', 'O2', 'N3', 'C4', 'N4', 'C5', 'C6']  # Cytosine
}

# 通用原子名（作为备选）
COMMON_ATOMS = {
    'protein': ['CA', 'CB', 'CG', 'CD', 'CE', 'CZ', 'N', 'C', 'O', 'OG', 'OH', 'SD', 'SG', 'NE', 'NH1', 'NH2', 'ND1', 'ND2', 'NE2'],
    'dna': ['P', "O5'", "C5'", "C4'", "O4'", "C3'", "O3'", "C2'", "C1'", 'N1', 'N2', 'N3', 'N4', 'N6', 'N7', 'N9', 'O2', 'O4', 'O6'],
    'rna': ['P', "O5'", "C5'", "C4'", "O4'", "C3'", "O3'", "C2'", "O2'", "C1'", 'N1', 'N2', 'N3', 'N4', 'N6', 'N7', 'N9', 'O2', 'O4', 'O6'],
    'ligand': ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'N1', 'N2', 'N3', 'O1', 'O2', 'O3', 'S1', 'P1']
}

def get_available_chain_ids(components):
    """
    根据组分计算可用的链ID列表
    返回: (all_chain_ids, chain_descriptions)
    """
    chain_ids = []
    chain_descriptions = {}
    chain_counter = 0
    
    for comp in components:
        if comp.get('sequence', '').strip():
            comp_type = comp.get('type', 'protein')
            num_copies = comp.get('num_copies', 1)
            
            for copy_idx in range(num_copies):
                if chain_counter < 26:
                    chain_id = string.ascii_uppercase[chain_counter]
                else:
                    chain_id = f"Z{chain_counter-25}"
                
                chain_ids.append(chain_id)
                
                # 生成链描述
                if comp_type == 'protein':
                    type_icon = '🧬'
                elif comp_type == 'dna':
                    type_icon = '🔗'
                elif comp_type == 'rna':
                    type_icon = '📜'
                elif comp_type == 'ligand':
                    type_icon = '💊'
                else:
                    type_icon = '🔸'
                
                if num_copies > 1:
                    chain_descriptions[chain_id] = f"{type_icon} 链 {chain_id} ({comp_type.upper()} 拷贝 {copy_idx+1}/{num_copies})"
                else:
                    chain_descriptions[chain_id] = f"{type_icon} 链 {chain_id} ({comp_type.upper()})"
                
                chain_counter += 1
    
    return chain_ids, chain_descriptions

def get_chain_type(components, chain_id):
    """
    根据链ID获取链的类型
    返回: 'protein', 'dna', 'rna', 'ligand', 或 'unknown'
    """
    if not components or not chain_id:
        return 'unknown'
    
    # 找到对应的组分
    chain_counter = 0
    for comp in components:
        if comp.get('sequence', '').strip():
            num_copies = comp.get('num_copies', 1)
            for copy_idx in range(num_copies):
                current_chain = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
                
                if current_chain == chain_id:
                    return comp.get('type', 'unknown')
                
                chain_counter += 1
    
    return 'unknown'

def get_residue_info(components, chain_id, residue_number):
    """
    根据链ID和残基编号获取残基信息
    返回: (residue_name, residue_type, sequence_length, is_valid_residue)
    """
    # 找到对应的组分
    chain_counter = 0
    for comp in components:
        if comp.get('sequence', '').strip():
            num_copies = comp.get('num_copies', 1)
            for copy_idx in range(num_copies):
                current_chain = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
                
                if current_chain == chain_id:
                    comp_type = comp.get('type', 'protein')
                    sequence = comp.get('sequence', '').strip()
                    sequence_length = len(sequence)
                    is_valid_residue = 1 <= residue_number <= sequence_length
                    
                    if comp_type == 'protein':
                        if is_valid_residue:
                            amino_acid = sequence[residue_number - 1].upper()
                            # 查找三字母代码
                            three_letter = None
                            for three, one in AMINO_ACID_MAPPING.items():
                                if one == amino_acid:
                                    three_letter = three
                                    break
                            
                            if three_letter:
                                return f"{three_letter} ({amino_acid})", comp_type, sequence_length, True
                            else:
                                return f"残基 {amino_acid}", comp_type, sequence_length, True
                        else:
                            return f"残基 {residue_number} (超出序列范围)", comp_type, sequence_length, False
                    
                    elif comp_type in ['dna', 'rna']:
                        if is_valid_residue:
                            nucleotide = sequence[residue_number - 1].upper()
                            return f"核苷酸 {nucleotide}", comp_type, sequence_length, True
                        else:
                            return f"核苷酸 {residue_number} (超出序列范围)", comp_type, sequence_length, False
                    
                    elif comp_type == 'ligand':
                        # 对于小分子，残基编号通常为1
                        if residue_number == 1:
                            return f"小分子", comp_type, 1, True
                        else:
                            return f"小分子残基 {residue_number} (通常为1)", comp_type, 1, False
                
                chain_counter += 1
    
    return f"残基 {residue_number}", "unknown", 0, False

def parse_smiles_atoms(smiles_string):
    """
    从SMILES字符串解析可能的原子类型
    这是一个简化的SMILES解析器，用于提取原子类型
    """
    if not smiles_string or not smiles_string.strip():
        return []
    
    import re
    
    # 提取所有原子符号（考虑常见的有机原子）
    atom_pattern = r'[CNOSPF]|Br|Cl|[cnospf]'  # 大写为芳香性，小写为脂肪性
    atoms = re.findall(atom_pattern, smiles_string)
    
    # 统计原子类型并生成可能的原子名
    atom_counts = {}
    for atom in atoms:
        atom_upper = atom.upper()
        atom_counts[atom_upper] = atom_counts.get(atom_upper, 0) + 1
    
    # 生成原子名列表
    atom_names = []
    for atom_type, count in atom_counts.items():
        for i in range(1, min(count + 1, 10)):  # 限制最多显示9个同类原子
            atom_names.append(f"{atom_type}{i}")
    
    # 添加一些常见的小分子原子名
    common_ligand_atoms = ['C1', 'C2', 'C3', 'N1', 'N2', 'O1', 'O2', 'S1', 'P1']
    for atom in common_ligand_atoms:
        if atom not in atom_names:
            atom_names.append(atom)
    
    return sorted(atom_names)

def get_available_atoms(components, chain_id, residue_number, molecule_type=None):
    """
    根据具体的残基信息获取可用原子列表
    """
    atom_list = ['']  # 空选项表示整个残基
    
    if not components or not chain_id:
        return atom_list + COMMON_ATOMS.get(molecule_type or 'protein', [])
    
    # 获取残基的详细信息
    try:
        residue_info, mol_type, seq_length, is_valid = get_residue_info(components, chain_id, residue_number)
        
        if not is_valid:
            return atom_list + COMMON_ATOMS.get(mol_type, [])
        
        if mol_type == 'protein':
            # 获取对应的组分和残基
            chain_counter = 0
            for comp in components:
                if comp.get('sequence', '').strip():
                    num_copies = comp.get('num_copies', 1)
                    for copy_idx in range(num_copies):
                        current_chain = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
                        
                        if current_chain == chain_id:
                            sequence = comp.get('sequence', '').strip()
                            if 1 <= residue_number <= len(sequence):
                                amino_acid = sequence[residue_number - 1].upper()
                                # 返回该氨基酸特有的原子名
                                specific_atoms = AMINO_ACID_ATOMS.get(amino_acid, [])
                                if specific_atoms:
                                    return atom_list + specific_atoms
                                else:
                                    return atom_list + COMMON_ATOMS['protein']
                        
                        chain_counter += 1
        
        elif mol_type in ['dna', 'rna']:
            # 获取对应的核苷酸
            chain_counter = 0
            for comp in components:
                if comp.get('sequence', '').strip():
                    num_copies = comp.get('num_copies', 1)
                    for copy_idx in range(num_copies):
                        current_chain = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
                        
                        if current_chain == chain_id:
                            sequence = comp.get('sequence', '').strip()
                            if 1 <= residue_number <= len(sequence):
                                nucleotide = sequence[residue_number - 1].upper()
                                # 添加骨架原子
                                backbone_atoms = ['P', "O5'", "C5'", "C4'", "O4'", "C3'", "O3'", "C2'", "C1'"]
                                if mol_type == 'rna':
                                    backbone_atoms.append("O2'")
                                
                                # 添加碱基特异性原子
                                base_atoms = []
                                if mol_type == 'dna':
                                    base_atoms = DNA_BASE_ATOMS.get(nucleotide, [])
                                elif mol_type == 'rna':
                                    base_atoms = RNA_BASE_ATOMS.get(nucleotide, [])
                                
                                return atom_list + backbone_atoms + base_atoms
                        
                        chain_counter += 1
        
        elif mol_type == 'ligand':
            # 获取对应的小分子SMILES
            chain_counter = 0
            for comp in components:
                if comp.get('sequence', '').strip():
                    num_copies = comp.get('num_copies', 1)
                    for copy_idx in range(num_copies):
                        current_chain = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
                        
                        if current_chain == chain_id:
                            smiles = comp.get('sequence', '').strip()
                            # 从SMILES解析原子名
                            smiles_atoms = parse_smiles_atoms(smiles)
                            if smiles_atoms:
                                return atom_list + smiles_atoms
                            else:
                                return atom_list + COMMON_ATOMS['ligand']
                        
                        chain_counter += 1
    
    except Exception as e:
        print(f"Error in get_available_atoms: {e}")
    
    # 默认返回通用原子名
    return atom_list + COMMON_ATOMS.get(molecule_type or 'protein', [])

# ========== 约束UI渲染函数 ==========

def render_contact_constraint_ui(constraint, key_prefix, available_chains, chain_descriptions, is_running):
    """渲染Contact约束的UI配置"""
    st.markdown("**Contact约束配置** - 定义两个残基间的接触距离")
    
    # Token 1配置
    st.markdown("**Token 1 (残基 1)**")
    token1_cols = st.columns(2)
    
    with token1_cols[0]:
        # 链ID选择下拉框
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
            
            # 检测链ID变化并触发更新
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
        
        # 检测残基编号变化并触发更新
        if token1_residue != current_token1_residue:
            constraint['token1_residue'] = token1_residue
            st.rerun()
        
        # 显示残基信息和验证
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
        # 链ID选择下拉框
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
            
            # 检测链ID变化并触发更新
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
        
        # 检测残基编号变化并触发更新
        if token2_residue != current_token2_residue:
            constraint['token2_residue'] = token2_residue
            st.rerun()
        
        # 显示残基信息和验证
        if available_chains and token2_chain in available_chains:
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(st.session_state.components, token2_chain, token2_residue)
            if is_valid2:
                st.caption(f"📍 {residue_info2}")
            else:
                st.error(f"❌ {residue_info2} (序列长度: {seq_length2})")
        else:
            molecule_type2 = 'protein'
            is_valid2 = True
    
    # 距离和强制执行设置
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
        
        # 检测距离变化并触发更新
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
        
        # 检测强制约束变化并触发更新
        if force_constraint != current_force_constraint:
            constraint['force'] = force_constraint
            st.rerun()
    
    # 更新约束数据
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
    
    # Atom 1配置
    st.markdown("**Atom 1 (原子 1)**")
    atom1_cols = st.columns(3)
    
    with atom1_cols[0]:
        # 链ID选择
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
        # 原子名选择（Bond约束必须指定原子）
        if available_chains and atom1_chain in available_chains:
            residue_info, molecule_type, seq_length, is_valid = get_residue_info(st.session_state.components, atom1_chain, atom1_residue)
            available_atoms = get_available_atoms(st.session_state.components, atom1_chain, atom1_residue, molecule_type)
        else:
            available_atoms = get_available_atoms(None, None, None, 'protein')
            molecule_type = 'protein'
        
        # 移除空选项，Bond必须指定原子
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
    
    # Atom 2配置
    st.markdown("**Atom 2 (原子 2)**")
    atom2_cols = st.columns(3)
    
    with atom2_cols[0]:
        # 链ID选择
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
        # 原子名选择（Bond约束必须指定原子）
        if available_chains and atom2_chain in available_chains:
            residue_info2, molecule_type2, seq_length2, is_valid2 = get_residue_info(st.session_state.components, atom2_chain, atom2_residue)
            available_atoms2 = get_available_atoms(st.session_state.components, atom2_chain, atom2_residue, molecule_type2)
        else:
            available_atoms2 = get_available_atoms(None, None, None, 'protein')
        
        # 移除空选项
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
    
    # 更新约束数据
    constraint.update({
        'atom1_chain': atom1_chain,
        'atom1_residue': atom1_residue,
        'atom1_atom': atom1_atom,
        'atom2_chain': atom2_chain,
        'atom2_residue': atom2_residue,
        'atom2_atom': atom2_atom
    })

def read_cif_from_string(cif_content: str) -> Structure:
    """Parses a CIF string into a BioPython Structure object."""
    parser = MMCIFParser(QUIET=True)
    file_like = io.StringIO(cif_content)
    structure = parser.get_structure('protein', file_like)
    return structure

def extract_protein_residue_bfactors(structure: Structure):
    """Extracts b-factors for protein/rna/dna residues only."""
    residue_bfactors = {}
    for model in structure:
        for chain in model:
            for residue in chain:
                hetfield = residue.get_id()[0]
                if hetfield.strip() == "":
                    resseq = residue.get_id()[1]
                    chain_id = chain.id
                    atom_bfactors = [atom.get_bfactor() for atom in residue]
                    if atom_bfactors:
                        avg_bfactor = sum(atom_bfactors) / len(atom_bfactors)
                        residue_bfactors[(chain_id, resseq)] = avg_bfactor
    return residue_bfactors

def get_color_from_bfactor(bfactor: float) -> str:
    """Maps a b-factor (pLDDT score) to a specific color."""
    if bfactor >= 90: return '#0053D6'
    elif 70 <= bfactor < 90: return '#65CBF3'
    elif 50 <= bfactor < 70: return '#FFDB13'
    else: return '#FF7D45'

def visualize_structure_py3dmol(
    cif_content: str,
    residue_bfactors: dict,
    protein_style: str = 'cartoon',
    ligand_style: str = 'ball-and-stick',
    spin: bool = False,
    color_scheme: str = 'pLDDT'
) -> str:
    view = py3Dmol.view(width='100%', height=600)
    view.addModel(cif_content, 'cif')

    if color_scheme == 'pLDDT':
        if protein_style == 'cartoon':
            view.setStyle({'model': -1, 'hetflag': False}, {'cartoon': {'color': 'white'}})
            for (chain_id, resseq), avg_bfactor in residue_bfactors.items():
                color = get_color_from_bfactor(avg_bfactor)
                view.setStyle({'chain': chain_id, 'resi': resseq}, {'cartoon': {'color': color}})
        elif protein_style == 'stick':
            view.setStyle({'model': -1, 'hetflag': False}, {'stick': {'colorscheme': 'default'}})
        elif protein_style == 'sphere':
            view.setStyle({'model': -1, 'hetflag': False}, {'sphere': {'colorscheme': 'default'}})

        ligand_color_map = {}
        try:
            parsed_structure = read_cif_from_string(cif_content)
            for atom in parsed_structure.get_atoms():
                residue = atom.get_parent()
                if residue.get_id()[0].strip() != "":
                    serial = atom.get_serial_number()
                    bfactor = atom.get_bfactor()
                    color = get_color_from_bfactor(bfactor)
                    ligand_color_map[serial-1] = color
        except Exception as e:
            print(f"Error parsing CIF for ligand coloring: {e}")
        
        custom_colorscheme = {'prop': 'serial', 'map': ligand_color_map}
        if ligand_color_map:
            if ligand_style == 'ball-and-stick':
                view.setStyle({'hetflag': True}, {'stick': {'colorscheme': custom_colorscheme, 'radius': 0.15}})
                view.addStyle({'hetflag': True}, {'sphere': {'colorscheme': custom_colorscheme, 'scale': 0.25}})
            elif ligand_style == 'space-filling':
                view.setStyle({'hetflag': True}, {'sphere': {'colorscheme': custom_colorscheme}})
            elif ligand_style == 'stick':
                view.setStyle({'hetflag': True}, {'stick': {'colorscheme': custom_colorscheme, 'radius': 0.25}})
            elif ligand_style == 'line':
                view.setStyle({'hetflag': True}, {'line': {'colorscheme': custom_colorscheme}})
        else:
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': 'default'}})

    else:
        common_scheme_name = 'chain'
        if color_scheme == 'Rainbow':
            common_scheme_name = 'spectrum'
        elif color_scheme == 'Secondary Structure':
            if protein_style == 'cartoon':
                common_scheme_name = 'ssPyMOL'
            else:
                common_scheme_name = 'chain'

        view.setStyle({'hetflag': False}, {protein_style: {'colorscheme': common_scheme_name}})

        if ligand_style == 'ball-and-stick':
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': common_scheme_name, 'radius': 0.15}})
            view.addStyle({'hetflag': True}, {'sphere': {'colorscheme': common_scheme_name, 'scale': 0.25}})
        elif ligand_style == 'space-filling':
            view.setStyle({'hetflag': True}, {'sphere': {'colorscheme': common_scheme_name}})
        elif ligand_style == 'stick':
            view.setStyle({'hetflag': True}, {'stick': {'colorscheme': common_scheme_name, 'radius': 0.25}})
        elif ligand_style == 'line':
            view.setStyle({'hetflag': True}, {'line': {'colorscheme': common_scheme_name}})

    view.setBackgroundColor('#F0F2F6')
    view.zoomTo()
    if spin:
        view.spin(True)

    return view._make_html()

def export_to_pdb(cif_content: str) -> str:
    """Converts CIF content to PDB string."""
    structure = read_cif_from_string(cif_content)
    pdb_io = PDBIO()
    pdb_io.set_structure(structure)
    pdb_buffer = io.StringIO()
    pdb_io.save(pdb_buffer)
    return pdb_buffer.getvalue()

# ========== MSA 缓存相关函数 ==========

def get_sequence_hash(sequence: str) -> str:
    """计算序列的MD5哈希值作为缓存键"""
    return hashlib.md5(sequence.encode('utf-8')).hexdigest()

def ensure_msa_cache_dir():
    """确保MSA缓存目录存在"""
    cache_dir = MSA_CACHE_CONFIG['cache_dir']
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def get_msa_cache_path(sequence: str) -> str:
    """获取序列对应的MSA缓存文件路径"""
    cache_dir = ensure_msa_cache_dir()
    seq_hash = get_sequence_hash(sequence)
    return os.path.join(cache_dir, f"msa_{seq_hash}.a3m")

def has_cached_msa(sequence: str) -> bool:
    """检查序列是否有有效的MSA缓存"""
    if not MSA_CACHE_CONFIG['enable_cache']:
        return False
    
    cache_path = get_msa_cache_path(sequence)
    if not os.path.exists(cache_path):
        return False
    
    # 检查缓存是否过期
    cache_age_days = (time.time() - os.path.getmtime(cache_path)) / (24 * 3600)
    if cache_age_days > MSA_CACHE_CONFIG['cache_expiry_days']:
        try:
            os.remove(cache_path)
        except:
            pass
        return False
    
    # 检查文件是否有效（非空且格式正确）
    try:
        with open(cache_path, 'r') as f:
            content = f.read().strip()
            if len(content) > 0 and content.startswith('>'):
                return True
    except:
        pass
    
    return False

def get_cached_msa_content(sequence: str) -> str:
    """获取缓存的MSA内容"""
    if not has_cached_msa(sequence):
        return None
    
    try:
        cache_path = get_msa_cache_path(sequence)
        with open(cache_path, 'r') as f:
            return f.read()
    except Exception as e:
        print(f"读取MSA缓存失败: {e}")
        return None

def cache_msa_content(sequence: str, msa_content: str) -> bool:
    """缓存MSA内容到文件"""
    if not MSA_CACHE_CONFIG['enable_cache']:
        return False
    
    try:
        cache_path = get_msa_cache_path(sequence)
        with open(cache_path, 'w') as f:
            f.write(msa_content)
        return True
    except Exception as e:
        print(f"缓存MSA失败: {e}")
        return False

def get_cache_stats() -> dict:
    """获取缓存统计信息"""
    cache_dir = MSA_CACHE_CONFIG['cache_dir']
    if not os.path.exists(cache_dir):
        return {
            'total_files': 0,
            'total_size_mb': 0,
            'oldest_file': None,
            'newest_file': None
        }
    
    cache_files = [f for f in os.listdir(cache_dir) if f.startswith('msa_') and f.endswith('.a3m')]
    total_size = 0
    oldest_time = float('inf')
    newest_time = 0
    
    for file in cache_files:
        file_path = os.path.join(cache_dir, file)
        try:
            file_size = os.path.getsize(file_path)
            file_time = os.path.getmtime(file_path)
            total_size += file_size
            oldest_time = min(oldest_time, file_time)
            newest_time = max(newest_time, file_time)
        except:
            continue
    
    return {
        'total_files': len(cache_files),
        'total_size_mb': total_size / (1024 * 1024),
        'oldest_file': datetime.fromtimestamp(oldest_time).strftime('%Y-%m-%d %H:%M:%S') if oldest_time != float('inf') else None,
        'newest_file': datetime.fromtimestamp(newest_time).strftime('%Y-%m-%d %H:%M:%S') if newest_time > 0 else None
    }

def get_smart_msa_default(components: list) -> bool:
    """
    智能决定新蛋白质组分的MSA默认值
    
    策略：
    1. 如果没有蛋白质组分，新组分默认不启用MSA
    2. 如果只有一个蛋白质组分且有缓存，新组分默认启用MSA（利用缓存优势）
    3. 如果只有一个蛋白质组分且无缓存，新组分默认不启用MSA（避免额外计算）
    4. 如果已有多个蛋白质组分，跟随第一个组分的MSA设置
    5. 这样可以优化用户体验，减少不必要的MSA计算
    """
    if not components:
        return False  # 第一个组分默认不启用MSA
    
    # 找到所有蛋白质组分
    protein_components = [comp for comp in components if comp.get('type') == 'protein']
    
    if not protein_components:
        return False  # 没有蛋白质组分时，新组分默认不启用MSA
    
    # 检查第一个蛋白质组分是否有有效序列和缓存
    first_protein = protein_components[0]
    first_sequence = first_protein.get('sequence', '').strip()
    
    if not first_sequence:
        return False  # 第一个蛋白质没有序列，新组分默认不启用MSA
    
    # 统计有序列的蛋白质组分数量
    proteins_with_sequence = [comp for comp in protein_components if comp.get('sequence', '').strip()]
    
    # 如果第一个蛋白质有缓存
    if has_cached_msa(first_sequence):
        # 如果只有第一个蛋白质有序列（还没有其他组分），新组分默认启用MSA
        if len(proteins_with_sequence) <= 1:
            return True
        # 如果已经有多个蛋白质组分，跟随第一个组分的MSA设置
        else:
            return first_protein.get('use_msa', True)
    
    # 第一个蛋白质没有缓存，新组分默认不启用MSA
    return False

def submit_job(yaml_content: str, use_msa: bool) -> str:
    """
    提交预测任务到后端 API。
    """
    files = {'yaml_file': ('input.yaml', yaml_content)}
    data = {'use_msa_server': str(use_msa).lower(), 'priority': 'high'}
    headers = {'X-API-Token': os.getenv('API_SECRET_TOKEN', 'your_default_api_token')}
    
    response = requests.post(f"{API_URL}/predict", files=files, data=data, headers=headers)
    response.raise_for_status()
    task_id = response.json()['task_id']
    
    return task_id

def get_status(task_id: str) -> dict:
    """
    查询指定 task_id 的 Celery 任务状态。
    """
    response = requests.get(f"{API_URL}/status/{task_id}")
    response.raise_for_status()
    return response.json()

def download_and_process_results(task_id: str) -> tuple[dict, bytes]:
    """
    下载并处理任务结果ZIP文件。
    """
    response = requests.get(f"{API_URL}/results/{task_id}", stream=True)
    response.raise_for_status()
    raw_zip_bytes = response.content
    zip_buffer = io.BytesIO(raw_zip_bytes)
    results = {}
    cif_candidate, confidence_candidate, affinity_candidate = None, None, None
    with zipfile.ZipFile(zip_buffer, 'r') as zf:
        all_files = zf.namelist()
        for filename in all_files:
            if filename.endswith((".cif", ".pdb")):
                if "_model_0.cif" in filename or "ranked_0.cif" in filename:
                    cif_candidate = filename
                elif "_unrelaxed_rank_001_alphafold2_ptm.pdb" in filename:
                    cif_candidate = filename
                elif cif_candidate is None:
                    cif_candidate = filename
            if "confidence" in filename and filename.endswith(".json"):
                confidence_candidate = filename
            if "affinity" in filename and filename.endswith(".json"):
                affinity_candidate = filename
        
        if cif_candidate:
            if cif_candidate.endswith(".cif"):
                results['cif'] = zf.read(cif_candidate).decode('utf-8')
            elif cif_candidate.endswith(".pdb"):
                results['cif'] = zf.read(cif_candidate).decode('utf-8')
            
        if confidence_candidate:
            results['confidence'] = json.loads(zf.read(confidence_candidate))
        if affinity_candidate:
            results['affinity'] = json.loads(zf.read(affinity_candidate))
            
    if 'cif' not in results or 'confidence' not in results:
        raise FileNotFoundError("未能从结果文件中找到预测的结构 (.cif/.pdb) 或置信度信息 (.json)。")
    return results, raw_zip_bytes

def generate_yaml_from_state():
    """
    Generates the YAML configuration string based on the current session state.
    确保所有蛋白质组分使用一致的MSA策略以避免Boltz的"混合MSA"错误。
    """
    if not st.session_state.get('components'):
        return None
        
    sequences_list = []
    chain_letters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    next_letter_idx = 0
    
    # 第一步：分析所有蛋白质组分的MSA情况
    protein_components = [comp for comp in st.session_state.components if comp['type'] == 'protein']
    
    # 检查MSA缓存情况
    msa_strategy = "mixed"  # none, cached, auto, mixed
    if protein_components:
        cached_count = 0
        enabled_count = 0
        total_proteins = len(protein_components)
        
        for comp in protein_components:
            if comp.get('use_msa', True):
                enabled_count += 1
                if has_cached_msa(comp['sequence']):
                    cached_count += 1
        
        # 决定MSA策略
        if enabled_count == 0:
            msa_strategy = "none"  # 所有蛋白质都禁用MSA
        elif cached_count == enabled_count and enabled_count == total_proteins:
            msa_strategy = "cached"  # 所有启用MSA的蛋白质都有缓存
        elif cached_count == 0 and enabled_count == total_proteins:
            msa_strategy = "auto"  # 所有蛋白质都启用MSA但无缓存
        else:
            # 混合情况：部分有缓存、部分无缓存、部分禁用MSA
            # 这种情况允许混合，因为empty MSA不会与cached/auto冲突
            msa_strategy = "mixed"
    
    for comp in st.session_state.components:
        num_copies = comp.get('num_copies', 1)
        current_ids = []
        for j in range(num_copies):
            if next_letter_idx + j < len(chain_letters):
                current_ids.append(chain_letters[next_letter_idx + j])
            else:
                st.warning(f"警告: 拷贝数过多，链ID可能重复或不足。请减少拷贝数或调整代码。")
                current_ids.append(f"UNK_{j}")
        next_letter_idx += num_copies
        
        component_dict = {'id': current_ids if len(current_ids) > 1 else current_ids[0]}

        if comp['type'] in ['protein', 'dna', 'rna']:
            component_dict['sequence'] = comp['sequence']
            if comp['type'] == 'protein' and comp.get('cyclic', False):
                component_dict['cyclic'] = True
            
            # MSA处理：基于统一的MSA策略
            if comp['type'] == 'protein':
                comp_use_msa = comp.get('use_msa', True)
                
                if msa_strategy == "none" or not comp_use_msa:
                    component_dict['msa'] = 'empty'
                elif msa_strategy == "cached":
                    # 所有蛋白质都使用缓存的MSA
                    sequence = comp['sequence']
                    component_dict['msa'] = get_msa_cache_path(sequence)
                elif msa_strategy == "auto":
                    # 所有蛋白质都使用自动生成的MSA（不设置msa字段）
                    pass  # 不设置msa字段，让系统自动生成
                elif msa_strategy == "mixed":
                    # 混合策略：避免混合custom和auto-generated MSA
                    # 策略：检查是否所有启用MSA的蛋白质都有缓存
                    # 如果有任何启用MSA的蛋白质没有缓存，则全部使用auto-generated
                    
                    # 检查所有启用MSA的蛋白质是否都有缓存
                    enabled_proteins_with_msa = [p for p in protein_components if p.get('use_msa', True)]
                    all_enabled_have_cache = all(
                        has_cached_msa(p['sequence']) for p in enabled_proteins_with_msa
                    ) if enabled_proteins_with_msa else True
                    
                    if not comp_use_msa:
                        component_dict['msa'] = 'empty'
                    else:
                        sequence = comp['sequence']
                        has_cache = has_cached_msa(sequence)
                        
                        if all_enabled_have_cache:
                            # 只有当所有启用MSA的蛋白质都有缓存时，才使用缓存
                            if has_cache:
                                component_dict['msa'] = get_msa_cache_path(sequence)
                            else:
                                # 这种情况理论上不应该发生，因为我们已经检查了all_enabled_have_cache
                                pass  # 不设置msa字段，让系统自动生成
                        else:
                            # 如果有任何启用MSA的蛋白质没有缓存，则全部使用auto-generated
                            # 不设置msa字段，让系统自动生成
                            pass
                    
        elif comp['type'] == 'ligand':
            # 对于ketcher输入，实际存储的是SMILES，所以统一使用smiles字段
            input_method = comp['input_method']
            if input_method == 'ketcher':
                component_dict['smiles'] = comp['sequence']
            else:
                component_dict[input_method] = comp['sequence']
            
        sequences_list.append({comp['type']: component_dict})
        
    if not sequences_list:
        return None
        
    final_yaml_dict = {'version': 1, 'sequences': sequences_list}
    
    if st.session_state.properties.get('affinity') and st.session_state.properties.get('binder'):
        final_yaml_dict['properties'] = [{'affinity': {'binder': st.session_state.properties['binder']}}]
    
    # 添加所有类型的约束
    if st.session_state.get('constraints'):
        constraints_list = []
        
        for constraint in st.session_state.constraints:
            constraint_type = constraint.get('type', 'contact')
            
            if constraint_type == 'contact':
                # Contact约束
                # 构建token1和token2 - 根据Boltz格式要求
                
                # 处理token1
                if constraint.get('token1_atom'):
                    token1 = [constraint['token1_chain'], constraint['token1_atom']]
                else:
                    # 检查链的类型来决定使用残基编号还是特殊处理
                    chain1_type = get_chain_type(st.session_state.components, constraint['token1_chain'])
                    if chain1_type == 'ligand':
                        # 对于配体，总是使用残基索引1（配体只有一个残基）
                        token1 = [constraint['token1_chain'], 1]
                    else:
                        # 对于蛋白质/DNA/RNA，使用残基编号（从1开始）
                        token1 = [constraint['token1_chain'], constraint['token1_residue']]
                    
                # 处理token2
                if constraint.get('token2_atom'):
                    token2 = [constraint['token2_chain'], constraint['token2_atom']]
                else:
                    # 检查链的类型来决定使用残基编号还是特殊处理
                    chain2_type = get_chain_type(st.session_state.components, constraint['token2_chain'])
                    if chain2_type == 'ligand':
                        # 对于配体，总是使用残基索引1（配体只有一个残基）
                        token2 = [constraint['token2_chain'], 1]
                    else:
                        # 对于蛋白质/DNA/RNA，使用残基编号（从1开始）
                        token2 = [constraint['token2_chain'], constraint['token2_residue']]
                        token2 = [constraint['token2_chain'], constraint['token2_residue']]
                
                constraint_dict = {
                    'contact': {
                        'token1': token1,
                        'token2': token2,
                        'max_distance': constraint['max_distance'],
                        'force': constraint.get('force', False)
                    }
                }
                
            elif constraint_type == 'bond':
                # Bond约束
                atom1 = [constraint['atom1_chain'], constraint['atom1_residue'], constraint['atom1_atom']]
                atom2 = [constraint['atom2_chain'], constraint['atom2_residue'], constraint['atom2_atom']]
                
                constraint_dict = {
                    'bond': {
                        'atom1': atom1,
                        'atom2': atom2
                    }
                }
            
            else:
                # 未知约束类型，跳过
                continue
                
            constraints_list.append(constraint_dict)
        
        if constraints_list:
            final_yaml_dict['constraints'] = constraints_list
        
    return yaml.dump(final_yaml_dict, sort_keys=False, indent=2, default_flow_style=False)

# ========== Designer 相关函数 ==========

def create_designer_template_yaml(target_protein_sequence: str, target_chain_id: str = "A") -> str:
    """创建 Designer 的模板 YAML 配置"""
    template_dict = {
        'version': 1,
        'sequences': [
            {
                'protein': {
                    'id': target_chain_id,
                    'sequence': target_protein_sequence,
                    'msa': 'empty'
                }
            }
        ]
    }
    return yaml.dump(template_dict, sort_keys=False, indent=2, default_flow_style=False)

def create_designer_complex_yaml(components: list, use_msa: bool = False, constraints: list = None) -> str:
    """为多组分复合物创建 Designer 的模板 YAML 配置
    当 use_msa=True 时，只对现有的目标蛋白质使用MSA，binder不使用MSA
    MSA策略：
    - 有缓存时：优先使用本地缓存的MSA文件
    - 无缓存时：使用MSA服务器(use_msa_server)自动生成MSA
    避免混合custom和auto-generated MSA以防止Boltz错误
    """
    sequences_list = []
    chain_counter = 0  # 用于自动分配链ID
    
    # 预先分析所有蛋白质组分的MSA情况，避免mixed MSA错误
    protein_components = [comp for comp in components if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
    
    # 检查MSA策略
    msa_strategy = "none"
    if use_msa and protein_components:
        cached_count = 0
        enabled_count = 0
        
        for comp in protein_components:
            if comp.get('use_msa', True):
                enabled_count += 1
                if has_cached_msa(comp['sequence']):
                    cached_count += 1
        
        if enabled_count == 0:
            msa_strategy = "none"
        elif cached_count > 0:
            # 有缓存的情况：优先使用缓存策略，避免混合
            msa_strategy = "cached"
        else:
            # 无缓存的情况：使用MSA服务器自动生成策略
            # 当序列找不到MSA缓存时，将通过use_msa_server参数启用MSA服务器
            msa_strategy = "auto"
    
    for comp in components:
        if not comp.get('sequence', '').strip():
            continue  # 跳过空序列的组分
            
        num_copies = comp.get('num_copies', 1)
        
        # 为每个拷贝创建独立的组分
        for copy_idx in range(num_copies):
            # 自动分配链ID (A, B, C, ...)
            chain_id = string.ascii_uppercase[chain_counter] if chain_counter < 26 else f"Z{chain_counter-25}"
            chain_counter += 1
            
            if comp['type'] == 'protein':
                # MSA处理：只对目标蛋白质使用MSA，binder蛋白质不使用MSA
                protein_dict = {
                    'id': chain_id,
                    'sequence': comp['sequence']
                }
                
                # 注意：这里不处理环肽选项，因为分子设计中的环肽是针对结合肽的，不是目标蛋白质
                # 环肽选项将在设计算法中处理
                
                # 分子设计逻辑：如果启用MSA，则只对现有的目标组分使用MSA
                # binder蛋白质（将要设计的）总是不使用MSA
                if use_msa:
                    # 对于目标蛋白质（现有组分），检查MSA设置
                    comp_use_msa = comp.get('use_msa', True)
                    
                    if not comp_use_msa:
                        protein_dict['msa'] = 'empty'
                    else:
                        sequence = comp['sequence']
                        
                        if msa_strategy == "cached":
                            # 缓存策略：只有当所有启用MSA的蛋白质都有缓存时才使用缓存策略
                            # 否则全部使用auto-generated策略
                            enabled_proteins_with_msa = [p for p in protein_components if p.get('use_msa', True)]
                            all_enabled_have_cache = all(
                                has_cached_msa(p['sequence']) for p in enabled_proteins_with_msa
                            ) if enabled_proteins_with_msa else True
                            
                            if all_enabled_have_cache and has_cached_msa(sequence):
                                protein_dict['msa'] = get_msa_cache_path(sequence)
                            else:
                                # 有蛋白质没有缓存，全部使用auto-generated
                                pass  # 不设置msa字段，让系统自动生成并缓存
                        elif msa_strategy == "auto":
                            # 自动生成策略：当序列找不到MSA缓存时，使用MSA服务器生成
                            # 设置use_msa_server标志，确保Boltz使用MSA服务器
                            pass  # 不设置msa字段，让系统使用MSA服务器自动生成
                        else:  # msa_strategy == "none"
                            protein_dict['msa'] = 'empty'
                else:
                    # 如果全局不启用MSA，所有蛋白质都设为empty
                    protein_dict['msa'] = 'empty'
                
                component_dict = {'protein': protein_dict}
            elif comp['type'] == 'dna':
                component_dict = {
                    'dna': {
                        'id': chain_id,
                        'sequence': comp['sequence']
                    }
                }
            elif comp['type'] == 'rna':
                component_dict = {
                    'rna': {
                        'id': chain_id,
                        'sequence': comp['sequence']
                    }
                }
            elif comp['type'] == 'ligand':
                input_method = comp.get('input_method', 'smiles')
                # 对于ketcher输入，实际存储的是SMILES，所以统一使用smiles字段
                actual_method = 'smiles' if input_method == 'ketcher' else input_method
                component_dict = {
                    'ligand': {
                        'id': chain_id,
                        actual_method: comp['sequence']
                    }
                }
            else:
                continue  # 跳过未知类型
                
            sequences_list.append(component_dict)
    
    if not sequences_list:
        raise ValueError("没有有效的组分序列")
        
    template_dict = {'version': 1, 'sequences': sequences_list}
    
    # 添加所有类型的约束
    if constraints:
        constraints_list = []
        
        for constraint in constraints:
            constraint_type = constraint.get('type', 'contact')
            
            if constraint_type == 'contact':
                # Contact约束 - 只到残基级别
                token1 = [constraint['token1_chain'], constraint['token1_residue']]
                token2 = [constraint['token2_chain'], constraint['token2_residue']]
                
                constraint_dict = {
                    'contact': {
                        'token1': token1,
                        'token2': token2,
                        'max_distance': constraint['max_distance'],
                        'force': constraint.get('force', False)
                    }
                }
                
            elif constraint_type == 'bond':
                # Bond约束 - 到原子级别
                atom1 = [constraint['atom1_chain'], constraint['atom1_residue'], constraint['atom1_atom']]
                atom2 = [constraint['atom2_chain'], constraint['atom2_residue'], constraint['atom2_atom']]
                
                constraint_dict = {
                    'bond': {
                        'atom1': atom1,
                        'atom2': atom2
                    }
                }
            
            else:
                # 未知约束类型，跳过
                continue
                
            constraints_list.append(constraint_dict)
        
        if constraints_list:
            template_dict['constraints'] = constraints_list
    
    return yaml.dump(template_dict, sort_keys=False, indent=2, default_flow_style=False)

def run_designer_workflow(params: dict, work_dir: str) -> str:
    """运行 Designer 工作流程（真实实现）"""
    try:
        # 创建工作目录
        os.makedirs(work_dir, exist_ok=True)
        
        # 尝试集成实际的 Designer 模块
        designer_script_path = os.path.join(os.getcwd(), 'designer', 'run_design.py')
        
        if os.path.exists(designer_script_path):
            # 计算设计链ID - 寻找下一个可用的链ID
            target_chain_id = params.get('target_chain_id', 'A')
            available_chains = string.ascii_uppercase
            used_chains = set()
            
            # 从模板YAML中解析已使用的链ID
            try:
                with open(params.get('template_path', ''), 'r') as f:
                    template_data = yaml.safe_load(f)
                    if 'sequences' in template_data:
                        for seq in template_data['sequences']:
                            for seq_type, seq_data in seq.items():
                                if 'id' in seq_data:
                                    used_chains.add(seq_data['id'])
            except Exception as e:
                print(f"Warning: Could not parse template YAML: {e}")
            
            # 找到下一个可用的链ID
            binder_chain_id = None
            for chain in available_chains:
                if chain not in used_chains:
                    binder_chain_id = chain
                    break
            
            if not binder_chain_id:
                binder_chain_id = "Z"  # 备用选项
            
            # 构建运行命令，直接传递参数
            cmd = [
                "python", "run_design.py",  # 相对于designer目录
                "--yaml_template", params.get('template_path', ''),
                "--binder_chain", binder_chain_id,  # 动态设计链ID
                "--binder_length", str(params.get('binder_length', 20)),
                "--iterations", str(params.get('generations', 5)),
                "--population_size", str(params.get('population_size', 10)),
                "--num_elites", str(params.get('elite_size', 3)),
                "--mutation_rate", str(params.get('mutation_rate', 0.3)),  # 新增：传递mutation_rate
                "--output_csv", os.path.join(work_dir, f"design_summary_{params.get('task_id', 'unknown')}.csv"),
                "--keep_temp_files"  # 保留临时文件以便下载结构
            ]
            
            # 添加增强功能参数
            if params.get('enable_enhanced', True):
                cmd.extend([
                    "--convergence-window", str(params.get('convergence_window', 5)),
                    "--convergence-threshold", str(params.get('convergence_threshold', 0.001)),
                    "--max-stagnation", str(params.get('max_stagnation', 3)),
                    "--initial-temperature", str(params.get('initial_temperature', 1.0)),
                    "--min-temperature", str(params.get('min_temperature', 0.1))
                ])
            else:
                cmd.append("--disable-enhanced")
            
            # 添加糖肽相关参数
            if params.get('design_type') == 'glycopeptide' and params.get('glycan_type'):
                cmd.extend([
                    "--glycan_ccd", params.get('glycan_type'),
                    "--glycosylation_site", str(params.get('glycosylation_site', 10))
                ])
            
            # 添加初始序列参数
            if params.get('use_initial_sequence') and params.get('initial_sequence'):
                # 处理初始序列长度匹配
                initial_seq = params.get('initial_sequence', '').upper()
                target_length = params.get('binder_length', 20)
                
                if len(initial_seq) < target_length:
                    # 序列太短，随机补全
                    amino_acids = "ACDEFGHIKLMNPQRSTVWY"
                    padding = ''.join(random.choices(amino_acids, k=target_length - len(initial_seq)))
                    initial_seq = initial_seq + padding
                elif len(initial_seq) > target_length:
                    # 序列太长，截取前面部分
                    initial_seq = initial_seq[:target_length]
                
                cmd.extend([
                    "--initial_binder_sequence", initial_seq
                ])
            
            # 添加服务器URL参数
            server_url = params.get('server_url', 'http://127.0.0.1:5000')
            cmd.extend(["--server_url", server_url])
            
            # 添加API令牌参数（如果有的话）
            api_token = os.environ.get('API_SECRET_TOKEN')
            if api_token:
                cmd.extend(["--api_token", api_token])
            
            # 添加MSA参数：当序列找不到MSA缓存时使用MSA服务器
            if params.get('use_msa', False):
                cmd.append("--use_msa_server")
            
            # 在后台运行设计任务
            # 先创建状态文件，表示任务已开始
            status_file = os.path.join(work_dir, 'status.json')
            initial_status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'starting',
                'start_time': datetime.now().isoformat(),
                'params': params,
                'process_id': None  # 先设为None，进程启动后更新
            }
            
            with open(status_file, 'w') as f:
                json.dump(initial_status_data, f, indent=2)
            
            # 创建日志文件
            log_file = os.path.join(work_dir, 'design.log')
            
            try:
                with open(log_file, 'w') as log:
                    log.write(f"设计任务开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    log.write(f"参数: {json.dumps(params, indent=2)}\n")
                    log.write(f"命令: {' '.join(cmd)}\n")
                    log.write("-" * 50 + "\n")
                    log.flush()  # 确保内容写入文件
                    
                    # 设置环境变量
                    env = os.environ.copy()
                    env['PYTHONPATH'] = os.path.join(os.getcwd(), "designer") + ":" + env.get('PYTHONPATH', '')
                    
                    # 启动异步进程
                    process = subprocess.Popen(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=os.path.join(os.getcwd(), "designer"),  # 切换到designer目录以便相对导入工作
                        env=env
                    )
                    
                    # 更新状态文件，添加进程ID
                    updated_status_data = {
                        'task_id': params.get('task_id', 'unknown'),
                        'status': 'running',
                        'process_id': process.pid,
                        'start_time': datetime.now().isoformat(),
                        'params': params
                    }
                    
                    with open(status_file, 'w') as f:
                        json.dump(updated_status_data, f, indent=2)
                    
                    return "running"
                    
            except Exception as process_error:
                # 如果进程启动失败，更新状态文件为失败
                error_status_data = {
                    'task_id': params.get('task_id', 'unknown'),
                    'status': 'failed',
                    'start_time': initial_status_data['start_time'],
                    'end_time': datetime.now().isoformat(),
                    'params': params,
                    'error': f"进程启动失败: {str(process_error)}"
                }
                
                with open(status_file, 'w') as f:
                    json.dump(error_status_data, f, indent=2)
                
                # 同时记录到日志文件中
                with open(log_file, 'a') as log:
                    log.write(f"\n❌ 进程启动失败: {str(process_error)}\n")
                
                return "failed"
        else:
            # Designer 脚本不存在，返回错误
            print(f"❌ Designer 脚本未找到: {designer_script_path}")
            
            # 创建错误状态文件
            status_file = os.path.join(work_dir, 'status.json')
            status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'failed',
                'start_time': datetime.now().isoformat(),
                'end_time': datetime.now().isoformat(),
                'params': params,
                'error': f"Designer script not found at {designer_script_path}"
            }
            
            with open(status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
            
            return "failed"
            
    except Exception as e:
        print(f"Error in run_designer_workflow: {e}")
        
        # 确保即使出错也创建状态文件
        try:
            status_file = os.path.join(work_dir, 'status.json')
            status_data = {
                'task_id': params.get('task_id', 'unknown'),
                'status': 'failed',
                'start_time': datetime.now().isoformat(),
                'end_time': datetime.now().isoformat(),
                'params': params,
                'error': f"Workflow execution error: {str(e)}"
            }
            
            with open(status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
        except Exception as status_error:
            print(f"Failed to create error status file: {status_error}")
        
        return "failed"


def submit_designer_job(
    template_yaml_content: str,
    design_type: str,
    binder_length: int,
    target_chain_id: str = "A",
    generations: int = 5,
    population_size: int = 10,
    elite_size: int = 3,
    mutation_rate: float = 0.3,
    glycan_type: str = None,
    glycosylation_site: int = None,
    # 增强功能参数
    convergence_window: int = 5,
    convergence_threshold: float = 0.001,
    max_stagnation: int = 3,
    initial_temperature: float = 1.0,
    min_temperature: float = 0.1,
    enable_enhanced: bool = True,
    # 新增初始序列参数
    use_initial_sequence: bool = False,
    initial_sequence: str = None,
    # 环状结合肽参数
    cyclic_binder: bool = False,
    # 新增MSA参数
    use_msa: bool = False
) -> dict:
    """提交 Designer 任务"""
    try:
        # 如果启用MSA，先预生成必要的MSA缓存
        if use_msa:
            # 解析模板YAML以提取需要MSA的蛋白质序列
            try:
                template_data = yaml.safe_load(template_yaml_content)
                target_protein_sequences = []
                
                if 'sequences' in template_data:
                    for seq_item in template_data['sequences']:
                        if 'protein' in seq_item:
                            protein_data = seq_item['protein']
                            sequence = protein_data.get('sequence', '').strip()
                            msa_setting = protein_data.get('msa', 'auto')  # 默认auto生成MSA
                            
                            # 只有当MSA设置不是'empty'时才计入需要MSA的蛋白质
                            # 注意：binder蛋白质在设计过程中会被动态添加，其MSA总是设置为'empty'
                            if sequence and msa_setting != 'empty':
                                target_protein_sequences.append(sequence)
                
                # 显示MSA信息（但不预生成，让Boltz在设计过程中自动处理）
                if target_protein_sequences:
                    cached_count = sum(1 for seq in target_protein_sequences if has_cached_msa(seq))
                    if cached_count > 0:
                        st.info(f"✅ 发现 {cached_count}/{len(target_protein_sequences)} 个目标蛋白质已有MSA缓存，将加速设计过程", icon="⚡")
                    else:
                        st.info(f"ℹ️ 检测到 {len(target_protein_sequences)} 个目标蛋白质需要MSA，Boltz将在设计过程中自动生成", icon="🧬")
                else:
                    st.info("ℹ️ 模板中无需MSA的目标蛋白质", icon="💡")
                    
            except Exception as e:
                st.warning(f"⚠️ 模板解析过程中出现错误: {e}，设计将继续进行", icon="⚠️")
        
        # 创建临时工作目录
        work_dir = tempfile.mkdtemp(prefix="boltz_designer_")
        template_path = os.path.join(work_dir, "template.yaml")
        
        # 保存模板文件
        with open(template_path, 'w') as f:
            f.write(template_yaml_content)
        
        # 构建设计参数
        design_params = {
            'template_path': template_path,
            'design_type': design_type,
            'binder_length': binder_length,
            'target_chain_id': target_chain_id,
            'generations': generations,
            'population_size': population_size,
            'elite_size': elite_size,
            'mutation_rate': mutation_rate,
            'work_dir': work_dir,
            # 增强功能参数
            'convergence_window': convergence_window,
            'convergence_threshold': convergence_threshold,
            'max_stagnation': max_stagnation,
            'initial_temperature': initial_temperature,
            'min_temperature': min_temperature,
            'enable_enhanced': enable_enhanced,
            # 初始序列参数
            'use_initial_sequence': use_initial_sequence,
            'initial_sequence': initial_sequence,
            # 环状结合肽参数
            'cyclic_binder': cyclic_binder,
            # MSA参数
            'use_msa': use_msa
        }
        
        if design_type == 'glycopeptide' and glycan_type:
            design_params['glycan_type'] = glycan_type
            design_params['glycosylation_site'] = glycosylation_site
        
        # 这里调用实际的 Designer 工作流程
        task_id = f"designer_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        design_params['task_id'] = task_id
        
        # 运行设计工作流程
        workflow_status = run_designer_workflow(design_params, work_dir)
        
        return {
            'success': True,
            'task_id': task_id,
            'work_dir': work_dir,
            'params': design_params,
            'initial_status': workflow_status
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def get_designer_status(task_id: str, work_dir: str = None) -> dict:
    """获取 Designer 任务状态（真实实现）"""
    try:
        # 如果没有提供工作目录，尝试找到它
        if not work_dir:
            # 在临时目录中搜索
            import tempfile
            temp_dir = tempfile.gettempdir()
            
            # 查找匹配的目录
            for item in os.listdir(temp_dir):
                if item.startswith('boltz_designer_'):
                    potential_dir = os.path.join(temp_dir, item)
                    status_file = os.path.join(potential_dir, 'status.json')
                    if os.path.exists(status_file):
                        try:
                            with open(status_file, 'r') as f:
                                status_data = json.load(f)
                                if status_data.get('task_id') == task_id:
                                    work_dir = potential_dir
                                    break
                        except:
                            continue
        
        if not work_dir:
            return {
                'task_id': task_id,
                'state': 'NOT_FOUND',
                'error': '未找到任务工作目录'
            }
        
        # 读取状态文件
        status_file = os.path.join(work_dir, 'status.json')
        
        if not os.path.exists(status_file):
            # 提供更详细的诊断信息
            work_dir_contents = []
            try:
                work_dir_contents = os.listdir(work_dir)
            except Exception as e:
                work_dir_contents = [f"Error listing directory: {e}"]
            
            # 检查是否有日志文件可以提供线索
            log_file = os.path.join(work_dir, 'design.log')
            log_info = "无日志文件"
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r') as f:
                        log_content = f.read()
                        # 提取最后几行或错误信息
                        log_lines = log_content.strip().split('\n')
                        if log_lines:
                            log_info = f"日志最后几行: {' | '.join(log_lines[-3:])}"
                except Exception as e:
                    log_info = f"读取日志失败: {e}"
            
            return {
                'task_id': task_id,
                'state': 'FAILED',
                'error': f'状态文件不存在。工作目录: {work_dir}, 目录内容: {work_dir_contents}, {log_info}'
            }
        
        with open(status_file, 'r') as f:
            status_data = json.load(f)
        
        current_status = status_data.get('status', 'unknown')
        
        # 检查进程是否还在运行（如果有进程ID）
        process_still_running = False
        
        if current_status == 'running':
            # 检查保存的进程ID是否仍在运行
            if 'process_id' in status_data:
                try:
                    if psutil and psutil.pid_exists(status_data['process_id']):
                        # 进一步验证这个PID确实是我们的run_design.py进程
                        proc = psutil.Process(status_data['process_id'])
                        cmdline = proc.cmdline()
                        if cmdline and 'run_design.py' in ' '.join(cmdline):
                            process_still_running = True
                        else:
                            # PID存在但不是我们的进程，可能被回收重用了
                            process_still_running = False
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                    # 进程不存在或无法访问
                    process_still_running = False
            
            # 如果进程已结束，检测完成状态
            if not process_still_running:
                # 首先检查是否有错误
                error_detected = False
                error_message = ""
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        with open(log_file, 'r') as f:
                            log_content = f.read()
                            # 检查常见的错误标识
                            error_indicators = [
                                'error: unrecognized arguments',
                                'error:',
                                'Error:',
                                'ERROR:',
                                'Traceback',
                                'usage:',  # 当参数错误时会显示用法
                                'FileNotFoundError',
                                'ModuleNotFoundError',
                                'ConnectionError'
                            ]
                            for indicator in error_indicators:
                                if indicator in log_content:
                                    error_detected = True
                                    # 提取错误信息的关键部分
                                    lines = log_content.split('\n')
                                    for i, line in enumerate(lines):
                                        if indicator in line:
                                            # 取该行及其后几行作为错误信息
                                            error_lines = lines[i:i+3]
                                            error_message = '\n'.join(error_lines).strip()
                                            break
                                    break
                except Exception:
                    pass
                
                if error_detected:
                    # 更新状态为失败
                    status_data['status'] = 'failed'
                    status_data['end_time'] = datetime.now().isoformat()
                    status_data['error'] = error_message
                    
                    with open(status_file, 'w') as f:
                        json.dump(status_data, f, indent=2)
                    
                    current_status = 'failed'
                else:
                    # 没有检测到错误，继续原来的完成检测逻辑
                    # 检查是否有CSV结果文件存在
                    csv_files = []
                try:
                    for filename in os.listdir(work_dir):
                        if filename.startswith('design_summary_') and filename.endswith('.csv'):
                            csv_path = os.path.join(work_dir, filename)
                            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                                csv_files.append(csv_path)
                except Exception:
                    pass
                
                # 检查日志文件是否显示完成
                log_completed = False
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        with open(log_file, 'r') as f:
                            log_content = f.read()
                            if any(indicator in log_content for indicator in [
                                'Design Run Finished', 
                                '设计任务完成', 
                                'Successfully created results package',
                                'Summary CSV successfully saved'
                            ]):
                                log_completed = True
                except Exception:
                    pass
                
                # 检查进度是否显示已完成所有代数
                progress_completed = False
                try:
                    log_file = os.path.join(work_dir, 'design.log')
                    if os.path.exists(log_file):
                        progress_info = parse_design_progress(log_file, status_data.get('params', {}))
                        current_gen = progress_info.get('current_generation', 0)
                        total_gen = progress_info.get('total_generations', 1)
                        best_sequences = progress_info.get('current_best_sequences', [])
                        
                        if current_gen > total_gen and total_gen > 0 and best_sequences:
                            progress_completed = True
                        elif current_gen == total_gen and total_gen > 0 and best_sequences:
                            # 只有在最后一代且有明确完成标记时才认为完成
                            try:
                                log_file = os.path.join(work_dir, 'design.log')
                                if os.path.exists(log_file):
                                    with open(log_file, 'r') as f:
                                        log_content = f.read()
                                        # 只检查非常明确的完成标记
                                        if any(indicator in log_content for indicator in [
                                            'Design Run Finished', 
                                            '设计任务完成', 
                                            'Successfully created results package',
                                            'All generations completed',
                                            f'Finished all {total_gen} generations'
                                        ]):
                                            progress_completed = True
                                        # 或者检查CSV文件的时间戳确认是最近完成的
                                        elif csv_files:
                                            for csv_file in csv_files:
                                                if os.path.exists(csv_file):
                                                    file_age = time.time() - os.path.getmtime(csv_file)
                                                    # 文件必须非常新（10秒内）且序列数量足够才认为刚完成
                                                    if file_age < 10:
                                                        # 额外检查：确保CSV中有足够的数据表明真正完成
                                                        try:
                                                            df = pd.read_csv(csv_file)
                                                            if len(df) >= total_gen:  # 至少有总代数的序列数
                                                                progress_completed = True
                                                                break
                                                        except:
                                                            pass
                            except Exception:
                                # 如果检查失败，不认为完成，继续等待
                                pass
                except Exception:
                    pass
                
                if log_completed or progress_completed:
                    status_data['status'] = 'completed'
                    status_data['end_time'] = datetime.now().isoformat()
                    if csv_files:
                        status_data['csv_files'] = csv_files
                    
                    with open(status_file, 'w') as f:
                        json.dump(status_data, f, indent=2)
                    
                    current_status = 'completed'
        
        # 构建返回状态
        result = {
            'task_id': task_id,
            'state': current_status.upper(),
            'start_time': status_data.get('start_time'),
            'work_dir': work_dir
        }
        
        # 添加进度信息
        if current_status == 'running':
            # 尝试从日志文件解析进度
            log_file = os.path.join(work_dir, 'design.log')
            if os.path.exists(log_file):
                result['progress'] = parse_design_progress(log_file, status_data.get('params', {}))
            else:
                # 如果没有日志文件，提供基础进度信息
                result['progress'] = {
                    'current_generation': 1,
                    'total_generations': status_data.get('params', {}).get('generations', 5),
                    'estimated_progress': 0.1,
                    'best_score': 0.0,
                    'status_message': '任务正在启动...',
                    'pending_tasks': 0,
                    'completed_tasks': 0,
                    'current_status': 'initializing'
                }
        elif current_status == 'completed':
            # 任务完成时也尝试获取最终进度
            log_file = os.path.join(work_dir, 'design.log')
            if os.path.exists(log_file):
                final_progress = parse_design_progress(log_file, status_data.get('params', {}))
                result['progress'] = final_progress
                result['progress']['estimated_progress'] = 1.0
                result['progress']['status_message'] = '设计任务已完成'
        elif current_status == 'failed':
            # 失败状态时提供错误信息
            result['error'] = status_data.get('error', '设计任务失败')
        
        # 添加结果摘要（如果已完成）
        if current_status == 'completed' and 'results_summary' in status_data:
            result['results_summary'] = status_data['results_summary']
        
        return result
        
    except Exception as e:
        return {
            'task_id': task_id,
            'state': 'ERROR',
            'error': str(e)
        }


def parse_design_progress(log_file: str, params: dict) -> dict:
    """从日志文件解析设计进度，并从CSV文件读取最佳序列"""
    try:
        with open(log_file, 'r') as f:
            content = f.read()
        
        lines = content.split('\n')
        current_generation = 0
        total_generations = params.get('generations', 5)
        best_score = 0.0
        current_status = "initializing"
        
        # 使用集合来跟踪唯一的任务ID
        pending_task_ids = set()
        completed_task_ids = set()
        current_best_sequences = []  # 从CSV文件读取的当前最佳序列列表
        
        # 分析日志内容
        for line in lines:
            line = line.strip()
            
            # 检测任务状态 - 提取任务ID避免重复计数
            if 'Polling task' in line:
                # 尝试提取任务ID
                task_id_match = re.search(r'task[_\s]*([a-f0-9\-]+)', line, re.IGNORECASE)
                task_id = task_id_match.group(1) if task_id_match else None
                
                if 'PENDING' in line and task_id:
                    pending_task_ids.add(task_id)
                    current_status = "waiting_for_prediction"
                elif 'SUCCESS' in line and task_id:
                    completed_task_ids.add(task_id)
                    # 如果任务完成了，从pending中移除
                    pending_task_ids.discard(task_id)
                    current_status = "processing_results"
            elif 'Generation' in line or 'generation' in line or '代演化' in line:
                # 提取世代信息 - 匹配多种格式
                gen_matches = re.findall(r'(?:Generation|第)\s*(\d+)', line, re.IGNORECASE)
                if gen_matches:
                    current_generation = max(current_generation, int(gen_matches[-1]))
                    current_status = "evolving"
                    
                # 匹配中文格式 "正在运行第 X/Y 代演化"
                gen_match = re.search(r'第\s*(\d+)/(\d+)\s*代演化', line)
                if gen_match:
                    current_generation = int(gen_match.group(1))
                    total_generations = int(gen_match.group(2))
                    current_status = "evolving"
                    
            elif 'Completed generation' in line or '完成第' in line or 'Generation.*complete' in line:
                # 确认某代已完成
                gen_matches = re.findall(r'(\d+)', line)
                if gen_matches:
                    current_generation = max(current_generation, int(gen_matches[-1]))
                    current_status = "evolving"
            
            # 提取评分信息 - 优化匹配模式
            if any(keyword in line.lower() for keyword in ['best score', '最佳评分', 'best:', 'top score', 'highest score']):
                # 匹配各种数值格式：整数、小数、科学记数法
                score_matches = re.findall(r'(\d+\.?\d*(?:[eE][+-]?\d+)?)', line)
                if score_matches:
                    try:
                        # 取最后一个匹配的数值作为评分
                        candidate_score = float(score_matches[-1])
                        # 合理性检查：评分通常在0-1之间，但也可能更大
                        if 0 <= candidate_score <= 10:  # 扩大合理范围
                            best_score = max(best_score, candidate_score)
                    except ValueError:
                        pass
                        
            # 匹配其他可能的评分格式
            score_patterns = [
                r'score[:\s]+(\d+\.?\d*)',  # "score: 0.85"
                r'评分[:\s]+(\d+\.?\d*)',    # "评分: 0.85"
                r'fitness[:\s]+(\d+\.?\d*)', # "fitness: 0.85"
                r'ipTM[:\s]+(\d+\.?\d*)',   # "ipTM: 0.85"
                r'pLDDT[:\s]+(\d+\.?\d*)'   # "pLDDT: 85.5"
            ]
            
            for pattern in score_patterns:
                matches = re.findall(pattern, line, re.IGNORECASE)
                if matches:
                    try:
                        candidate_score = float(matches[-1])
                        # 对pLDDT分数特殊处理（通常0-100），转换为0-1
                        if 'plddt' in pattern.lower() and candidate_score > 1:
                            candidate_score = candidate_score / 100.0
                        if 0 <= candidate_score <= 1:
                            best_score = max(best_score, candidate_score)
                    except ValueError:
                        pass
        
        # 从CSV文件读取当前最佳序列
        work_dir = os.path.dirname(log_file)
        csv_file_path = None
        csv_debug_info = {'work_dir': work_dir, 'files_found': [], 'error': None}
        
        # 尝试找到CSV文件
        try:
            files_in_dir = os.listdir(work_dir)
            csv_debug_info['files_found'] = [f for f in files_in_dir if f.endswith('.csv')]
            
            for filename in files_in_dir:
                if filename.startswith('design_summary_') and filename.endswith('.csv'):
                    csv_file_path = os.path.join(work_dir, filename)
                    break
        except Exception as e:
            csv_debug_info['error'] = f"List dir error: {str(e)}"
        
        if csv_file_path and os.path.exists(csv_file_path):
            try:
                import pandas as pd
                df = pd.read_csv(csv_file_path)
                csv_debug_info['csv_rows'] = len(df)
                csv_debug_info['csv_columns'] = list(df.columns)
                
                # 只有当CSV文件有数据且不为空时，才使用CSV中的评分
                if len(df) > 0 and not df.empty:
                    # 检查是否有有效的评分数据
                    valid_scores = df['composite_score'].dropna()
                    if len(valid_scores) > 0:
                        csv_best_score = valid_scores.max()
                        # 只有当CSV评分合理时才使用（避免使用默认的0或异常值）
                        if csv_best_score > 0 and csv_best_score <= 1.0:
                            # 如果当前代数与CSV数据匹配，优先使用CSV评分
                            csv_generations = df['generation'].dropna() if 'generation' in df.columns else []
                            if len(csv_generations) > 0 and current_generation > 0:
                                max_csv_generation = int(csv_generations.max())
                                # 只有当CSV中的代数与当前代数接近时才使用CSV评分
                                if abs(max_csv_generation - current_generation) <= 1:
                                    best_score = csv_best_score
                            elif current_generation == 0:  # 初始状态，可以使用CSV数据
                                best_score = csv_best_score
                
                # 读取前5个最佳序列
                for idx, row in df.head(5).iterrows():
                    sequence = row.get('sequence', '')
                    score = float(row.get('composite_score', '0'))
                    generation = int(row.get('generation', current_generation))
                    iptm = float(row.get('iptm', '0'))
                    plddt = float(row.get('binder_avg_plddt', '0'))
                    
                    if sequence and len(sequence) >= 8:  # 验证序列有效性
                        current_best_sequences.append({
                            'sequence': sequence,
                            'score': score,
                            'generation': generation,
                            'iptm': iptm,
                            'plddt': plddt
                        })
                        
            except Exception as e:
                # CSV读取失败，使用默认值
                csv_debug_info['read_error'] = str(e)
        else:
            # 没有CSV文件时，将评分重置为0
            best_score = 0.0
        
        # 计算最终的任务数
        pending_tasks = len(pending_task_ids)
        completed_tasks = len(completed_task_ids)
        
        # 计算进度
        if total_generations > 0:
            progress_ratio = min(current_generation / total_generations, 1.0)
        else:
            progress_ratio = 0.0
        
        # 根据任务状态调整进度显示
        if current_status == "waiting_for_prediction" and pending_tasks > 0:
            total_prediction_tasks = pending_tasks + completed_tasks
            status_msg = f"等待结构预测完成 ({completed_tasks}/{total_prediction_tasks} 个任务已完成)"
        elif current_status == "evolving":
            if current_generation > 0:
                status_msg = f"第 {current_generation}/{total_generations} 代演化"
            else:
                status_msg = "初始化演化算法"
        elif current_status == "processing_results":
            status_msg = "处理预测结果"
        else:
            status_msg = "初始化中"
        
        return {
            'current_generation': current_generation,
            'total_generations': total_generations,
            'best_score': best_score,
            'estimated_progress': progress_ratio,
            'status_message': status_msg,
            'pending_tasks': pending_tasks,
            'completed_tasks': completed_tasks,
            'current_status': current_status,
            'current_best_sequences': current_best_sequences,  # 从CSV读取
            'debug_info': {  # 添加调试信息
                'sequences_found': len(current_best_sequences),
                'log_lines_processed': len(lines),
                'generation_detected': current_generation > 0,
                'status_detected': current_status,
                'best_score_found': best_score > 0,
                'csv_file_found': csv_file_path is not None,
                'csv_file_path': csv_file_path,
                'csv_debug': csv_debug_info
            }
        }
        
    except Exception as e:
        total_gens = params.get('generations', 5)
        return {
            'current_generation': 0,
            'total_generations': total_gens,
            'best_score': 0.0,
            'estimated_progress': 0.0,
            'status_message': "初始化中",
            'pending_tasks': 0,
            'completed_tasks': 0,
            'current_status': 'initializing',
            'current_best_sequences': [],
            'debug_info': {
                'sequences_found': 0,
                'log_lines_processed': 0,
                'generation_detected': False,
                'status_detected': 'error',
                'error_message': str(e)
            },
            'error': str(e)
        }

def load_designer_results(task_id: str, work_dir: str) -> dict:
    """加载 Designer 结果（真实实现）"""
    try:
        # 查找可能的结果文件
        result_files = {
            'summary_csv': None,
            'best_sequences_json': None,
            'evolution_log': None
        }
        
        # 扫描工作目录和常见的结果目录
        search_dirs = [
            work_dir,
            os.path.join(work_dir, 'results'),
            '/tmp/boltz_designer',
            './designer/temp_design_*',
            f'./designer/temp_design_run_{task_id.split("_")[-1][:10]}*' if '_' in task_id else None
        ]
        
        # 移除 None 值
        search_dirs = [d for d in search_dirs if d is not None]
        
        found_results = []
        
        for search_dir in search_dirs:
            if '*' in search_dir:
                # 使用 glob 匹配模式
                import glob
                matching_dirs = glob.glob(search_dir)
                search_dirs.extend(matching_dirs)
                continue
                
            if not os.path.exists(search_dir):
                continue
                
            try:
                for root, dirs, files in os.walk(search_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        # 查找 CSV 汇总文件
                        if ('design_summary_' in file or 'design_run_summary' in file) and file.endswith('.csv'):
                            result_files['summary_csv'] = file_path
                            
                        # 查找最佳序列 JSON 文件
                        elif 'best_sequences' in file and file.endswith('.json'):
                            result_files['best_sequences_json'] = file_path
                            
                        # 查找演化日志文件
                        elif 'evolution' in file or 'log' in file:
                            result_files['evolution_log'] = file_path
                            
            except Exception as e:
                print(f"Error scanning directory {search_dir}: {e}")
                continue
        
        # 尝试从找到的文件中读取结果
        best_sequences = []
        evolution_history = {
            'generations': [],
            'best_scores': [],
            'avg_scores': []
        }
        
        # 读取 CSV 汇总文件
        if result_files['summary_csv'] and os.path.exists(result_files['summary_csv']):
            try:
                import pandas as pd
                df = pd.read_csv(result_files['summary_csv'])
                
                # 转换 DataFrame 为序列列表
                for idx, row in df.iterrows():
                    seq_data = {
                        'sequence': row.get('sequence', f'UNKNOWN_SEQ_{idx}'),
                        'score': float(row.get('composite_score', 0.0)) if pd.notna(row.get('composite_score')) else 0.0,
                        'iptm': float(row.get('iptm', 0.0)) if pd.notna(row.get('iptm')) else 0.0,
                        'plddt': float(row.get('binder_avg_plddt', 0.0)) if pd.notna(row.get('binder_avg_plddt')) else 0.0,
                        'generation': int(row.get('generation', 1)) if pd.notna(row.get('generation')) else 1,
                        'results_path': row.get('results_path', '') if pd.notna(row.get('results_path')) else ''
                    }
                    best_sequences.append(seq_data)
                    
                # 按评分排序
                best_sequences.sort(key=lambda x: x['score'], reverse=True)
                
                # 从数据中提取演化历史
                if len(best_sequences) > 0:
                    generations = sorted(list(set([seq['generation'] for seq in best_sequences])))
                    best_scores = []
                    avg_scores = []
                    
                    for gen in generations:
                        gen_scores = [seq['score'] for seq in best_sequences if seq['generation'] == gen]
                        if gen_scores:
                            best_scores.append(max(gen_scores))
                            avg_scores.append(sum(gen_scores) / len(gen_scores))
                        else:
                            best_scores.append(0.0)
                            avg_scores.append(0.0)
                    
                    evolution_history = {
                        'generations': generations,
                        'best_scores': best_scores,
                        'avg_scores': avg_scores
                    }
                
                print(f"✅ 成功从 {result_files['summary_csv']} 读取了 {len(best_sequences)} 个序列")
                
            except Exception as e:
                print(f"Error reading CSV file {result_files['summary_csv']}: {e}")
        
        # 读取 JSON 文件（如果存在）
        if result_files['best_sequences_json'] and os.path.exists(result_files['best_sequences_json']):
            try:
                with open(result_files['best_sequences_json'], 'r') as f:
                    json_data = json.load(f)
                    if 'best_sequences' in json_data:
                        best_sequences = json_data['best_sequences']
                    if 'evolution_history' in json_data:
                        evolution_history = json_data['evolution_history']
                        
                print(f"✅ 成功从 {result_files['best_sequences_json']} 读取了 JSON 数据")
                        
            except Exception as e:
                print(f"Error reading JSON file {result_files['best_sequences_json']}: {e}")
        
        # 如果没有找到真实数据，返回错误信息
        if not best_sequences:
            print(f"❌ 未找到真实设计结果文件。搜索的目录: {search_dirs}")
            print(f"📁 查找的文件类型: CSV汇总文件、JSON结果文件、演化日志")
            raise FileNotFoundError(f"No design results found in searched directories: {search_dirs}")
        
        return {
            'best_sequences': best_sequences,
            'evolution_history': evolution_history,
            'result_files': result_files,
            'search_info': {
                'searched_dirs': search_dirs,
                'found_files': {k: v for k, v in result_files.items() if v is not None}
            }
        }
        
    except Exception as e:
        print(f"Error in load_designer_results: {e}")
        # 返回错误信息而不是模拟数据
        raise Exception(f"Failed to load designer results: {str(e)}")

def validate_inputs(components):
    """验证用户输入是否完整且有效。"""
    if not components:
        return False, "请至少添加一个组分。"
    
    # 检查所有组分是否都有有效序列
    valid_components = 0
    for i, comp in enumerate(components):
        sequence = comp.get('sequence', '').strip()
        if not sequence:
            display_name = TYPE_TO_DISPLAY.get(comp.get('type', 'Unknown'), 'Unknown')
            return False, f"错误: 组分 {i+1} ({display_name}) 的序列不能为空。"
        
        # 验证小分子SMILES格式（ketcher也会生成SMILES）
        if comp.get('type') == 'ligand' and comp.get('input_method') in ['smiles', 'ketcher']:
            if sequence and not all(c in string.printable for c in sequence):
                return False, f"错误: 组分 {i+1} (小分子) 的 SMILES 字符串包含非法字符。"
        
        valid_components += 1
    
    # 至少需要一个有效组分（可以是任何类型，包括单独的小分子）
    if valid_components == 0:
        return False, "请至少输入一个有效的组分序列。"
            
    # 亲和力预测验证（只有在启用时才检查）
    if st.session_state.properties.get('affinity'):
        has_ligand_component_with_sequence = any(comp['type'] == 'ligand' and comp.get('sequence', '').strip() for comp in components)
        if not has_ligand_component_with_sequence:
            return False, "已选择计算亲和力，但未提供任何小分子序列。"
        if not st.session_state.properties.get('binder'):
            return False, "已选择计算亲和力，但未选择结合体（Binder）链ID。"
            
    return True, ""

def validate_designer_inputs(designer_components):
    """验证Designer输入是否完整且有效。"""
    if not designer_components:
        return False, "请至少添加一个组分。"
    
    # 检查是否至少有一个目标组分（蛋白质、DNA、RNA或小分子）
    # 支持两种设计模式：
    # 1. 正向设计：给定蛋白质/DNA/RNA，设计结合肽
    # 2. 反向设计：给定小分子，设计结合蛋白
    target_bio_components = [comp for comp in designer_components if comp['type'] in ['protein', 'dna', 'rna'] and comp.get('sequence', '').strip()]
    target_ligand_components = [comp for comp in designer_components if comp['type'] == 'ligand' and comp.get('sequence', '').strip()]
    
    # 至少需要一种目标组分
    if not target_bio_components and not target_ligand_components:
        return False, "请至少添加一个包含序列的蛋白质、DNA、RNA或小分子组分作为设计目标。"
    
    for i, comp in enumerate(designer_components):
        if comp.get('sequence', '').strip():  # 只验证非空序列
            comp_type = comp.get('type')
            sequence = comp.get('sequence', '').strip()
            
            if comp_type == 'protein':
                # 验证蛋白质序列只包含标准氨基酸字符
                valid_aa = set('ACDEFGHIKLMNPQRSTVWY')
                if not all(c.upper() in valid_aa for c in sequence):
                    return False, f"错误: 组分 {i+1} (蛋白质) 包含非标准氨基酸字符。请使用标准20种氨基酸。"
            
            elif comp_type == 'dna':
                # 验证DNA序列只包含ATGC
                valid_dna = set('ATGC')
                if not all(c.upper() in valid_dna for c in sequence):
                    return False, f"错误: 组分 {i+1} (DNA) 包含非法核苷酸。请只使用A、T、G、C。"
            
            elif comp_type == 'rna':
                # 验证RNA序列只包含AUGC
                valid_rna = set('AUGC')
                if not all(c.upper() in valid_rna for c in sequence):
                    return False, f"错误: 组分 {i+1} (RNA) 包含非法核苷酸。请只使用A、U、G、C。"
            
            elif comp_type == 'ligand' and comp.get('input_method') in ['smiles', 'ketcher']:
                # 验证SMILES字符串（ketcher也会生成SMILES）
                if not all(c in string.printable for c in sequence):
                    return False, f"错误: 组分 {i+1} (小分子) 的 SMILES 字符串包含非法字符。"
    
    return True, ""

# ========== Streamlit 应用程序 ==========

st.set_page_config(layout="centered", page_title="Boltz-WebUI", page_icon="🧬")

# 初始化 session state
if 'components' not in st.session_state: st.session_state.components = []
if 'constraints' not in st.session_state: st.session_state.constraints = []
if 'task_id' not in st.session_state: st.session_state.task_id = None
if 'results' not in st.session_state: st.session_state.results = None
if 'raw_zip' not in st.session_state: st.session_state.raw_zip = None
if 'error' not in st.session_state: st.session_state.error = None
if 'properties' not in st.session_state: st.session_state.properties = {'affinity': False, 'binder': None}
if 'use_msa_server' not in st.session_state: st.session_state.use_msa_server = False

# Designer 相关 session state
if 'designer_task_id' not in st.session_state: st.session_state.designer_task_id = None
if 'designer_work_dir' not in st.session_state: st.session_state.designer_work_dir = None
if 'designer_results' not in st.session_state: st.session_state.designer_results = None
if 'designer_error' not in st.session_state: st.session_state.designer_error = None
if 'designer_config' not in st.session_state: st.session_state.designer_config = {}

if not st.session_state.components:
    st.session_state.components.append({
        'id': str(uuid.uuid4()), 'type': 'protein', 'num_copies': 1, 'sequence': '', 'input_method': 'smiles', 'cyclic': False, 'use_msa': False
    })

# CSS 样式
st.markdown(f"""
<style>
    .stApp {{
        background-color: #FFFFFF;
        font-family: 'Segoe UI', 'Roboto', sans-serif;
    }}
    div.block-container {{
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1024px;
    }}
    h1 {{
        color: #0053D6;
        text-align: left;
        margin-bottom: 0.5rem;
    }}
    h3 {{
        color: #555555;
        text-align: left;
    }}
    h2, h3, h4 {{
        color: #333333;
        margin-top: 1.5rem;
        margin-bottom: 0.8rem;
    }}
    .stButton>button {{
        border-radius: 8px;
        padding: 0.6rem 1.2rem;
        font-size: 1rem;
        font-weight: 500;
    }}
    .stButton>button[kind="primary"] {{
        background-color: #007bff;
        color: white;
        border: none;
    }}
    .stButton>button[kind="primary"]:hover {{
        background-color: #0056b3;
    }}
    .stButton>button[kind="secondary"] {{
        background-color: #f0f2f6;
        color: #333333;
        border: 1px solid #ddd;
    }}
    .stButton>button[kind="secondary"]:hover {{
        background-color: #e0e0e0;
    }}
    .stButton>button[data-testid="baseButton-secondary"] {{
        border: none !important;
        background-color: transparent !important;
        color: #888 !important;
        padding: 0 !important;
        font-size: 1.2rem;
    }}
    .stButton>button[data-testid="baseButton-secondary"]:hover {{
        color: #ff4b4b !important;
    }}
    div[data-testid="stCheckbox"] {{
        display: flex;
        align-items: center;
        margin-top: 10px;
        margin-bottom: 10px;
    }}
    .stExpander {{
        border: 1px solid #e6e6e6;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        margin-bottom: 1.5rem;
    }}
    .stExpander>div>div[data-testid="stExpanderToggleIcon"] {{
        font-size: 1.5rem;
    }}
    .stCode {{
        background-color: #f8f8f8;
        border-left: 5px solid #007bff;
        padding: 10px;
        border-radius: 5px;
    }}
    .stAlert {{
        border-radius: 8px;
    }}
    hr {{
        border-top: 1px solid #eee;
        margin-top: 1.5rem;
        margin-bottom: 1.5rem;
    }}
    
    .loader {{
      border: 6px solid #f3f3f3;
      border-top: 6px solid #007bff;
      border-radius: 50%;
      width: 50px;
      height: 50px;
      animation: spin 1.5s linear infinite;
      margin: 20px auto;
    }}

    @keyframes spin {{
      0% {{ transform: rotate(0deg); }}
      100% {{ transform: rotate(360deg); }}
    }}
    
    /* 简洁标签页样式 */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 0;
        background: transparent;
        padding: 0;
        border-radius: 0;
        margin-bottom: 1.5rem;
        box-shadow: none;
        border-bottom: 2px solid #f1f5f9;
        justify-content: flex-start;
        width: auto;
        max-width: 300px;
    }}
    
    .stTabs [data-baseweb="tab"] {{
        height: 40px;
        background: transparent;
        border-radius: 0;
        color: #64748b;
        font-weight: 500;
        font-size: 14px;
        border: none;
        padding: 0 16px;
        transition: all 0.2s ease;
        position: relative;
        display: flex;
        align-items: center;
        justify-content: center;
        min-width: auto;
        border-bottom: 2px solid transparent;
    }}
    
    .stTabs [data-baseweb="tab"]:hover:not([aria-selected="true"]) {{
        color: #374151;
        background: #f8fafc;
    }}
    
    .stTabs [aria-selected="true"] {{
        background: transparent !important;
        color: #1e293b !important;
        border-bottom: 2px solid #3b82f6 !important;
        font-weight: 600 !important;
    }}
    
    /* 移除所有图标和额外装饰 */
    .stTabs [data-baseweb="tab"]::before,
    .stTabs [data-baseweb="tab"]::after {{
        display: none;
    }}
</style>
""", unsafe_allow_html=True)

st.title("🧬 Boltz-WebUI")
st.markdown("蛋白质-分子复合物结构预测与设计平台")

# 创建标签页
tab1, tab2 = st.tabs(["结构预测", "分子设计"])

# ========== 结构预测标签页 ==========
with tab1:
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
                # Reset cyclic for new type if changed from protein
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
                
                # 保存旧输入方式用于变化检测
                old_input_method = component.get('input_method', 'smiles')
                
                new_input_method = st.radio(
                    "小分子输入方式", method_options, key=f"ligand_type_{component['id']}",
                    index=current_method_index, disabled=is_running, horizontal=True,
                    help="选择通过SMILES字符串、PDB CCD代码或分子编辑器输入小分子。"
                )
                
                # 检测输入方式是否发生变化
                input_method_changed = new_input_method != old_input_method
                
                # 更新输入方式
                st.session_state.components[i]['input_method'] = new_input_method
                
                # 如果输入方式发生变化，清除序列内容并触发刷新
                if input_method_changed:
                    # 清除序列内容（不同输入方式的格式不同）
                    st.session_state.components[i]['sequence'] = ''
                    
                    # 显示输入方式变化的提示信息
                    method_display_names = {
                        "smiles": "SMILES 字符串",
                        "ccd": "PDB CCD 代码", 
                        "ketcher": "分子编辑器"
                    }
                    st.toast(f"输入方式已更新为 {method_display_names.get(new_input_method, new_input_method)}", icon="🔄")
                    
                    # 立即触发刷新以更新界面
                    st.rerun()
                
                num_copies = component.get('num_copies', 1)
                
                # 直接使用radio返回的值来显示对应的输入控件
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
                    initial_smiles = st.session_state.components[i].get('sequence', '')
                    
                    st.info("🎨 在下方 **Ketcher 编辑器** 中绘制分子，或直接粘贴 SMILES 字符串。**编辑完成后，请点击编辑器内部的 'Apply' 按钮，SMILES 字符串将自动更新。**", icon="💡")
                    
                    ketcher_current_smiles = st_ketcher(
                        value=initial_smiles,
                        key=f"ketcher_{component['id']}",
                        height=400
                    )
                    
                    # 更加严格的SMILES更新逻辑
                    if ketcher_current_smiles is not None:
                        # 清理空白字符
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
                        
                        # 显示 SMILES 基本信息
                        smiles_length = len(current_smiles_in_state)
                        atom_count = current_smiles_in_state.count('C') + current_smiles_in_state.count('N') + \
                                   current_smiles_in_state.count('O') + current_smiles_in_state.count('S')
                        st.caption(f"📊 长度: {smiles_length} 字符 | 主要原子数: ~{atom_count}")
                        
                        # 基本格式验证
                        if not all(c in string.printable for c in current_smiles_in_state):
                            st.warning("⚠️ SMILES 字符串包含非打印字符，可能导致预测失败。", icon="⚠️")
                        elif len(current_smiles_in_state.strip()) == 0:
                            st.warning("⚠️ SMILES 字符串为空。", icon="⚠️")
                        else:
                            st.success("SMILES 格式看起来正常", icon="✅")
                    else:
                        st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")
            else:
                placeholder_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('placeholder', '')
                help_text = TYPE_SPECIFIC_INFO.get(selected_type, {}).get('help', '')
                
                # 生成友好的中文标签
                num_copies = component.get('num_copies', 1)
                if selected_type == 'protein':
                    label = f"蛋白质序列 ({'单体' if num_copies == 1 else f'{num_copies}聚体'})"
                elif selected_type == 'dna':
                    label = f"DNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})"
                elif selected_type == 'rna':
                    label = f"RNA序列 ({'单链' if num_copies == 1 else f'{num_copies}链'})"
                else:
                    label = f"输入 {selected_type.capitalize()} 序列"
                
                # 保存旧序列用于变化检测
                old_sequence = component.get('sequence', '')
                
                new_sequence = st.text_area(
                    label, 
                    height=120, key=f"seq_{component['id']}",
                    value=component.get('sequence', ''),
                    placeholder=placeholder_text,
                    help=help_text,
                    disabled=is_running
                )
                
                # 检测序列是否发生变化
                sequence_changed = new_sequence != old_sequence
                
                # 更新序列到session state
                st.session_state.components[i]['sequence'] = new_sequence
                
                # 如果序列发生变化，进行必要的状态调整和刷新
                if sequence_changed:
                    # 对于蛋白质类型，进行智能MSA调整
                    if selected_type == 'protein':
                        # 当只有一个蛋白质组分时，基于缓存状态智能设置MSA
                        protein_components = [comp for comp in st.session_state.components if comp.get('type') == 'protein']
                        if len(protein_components) == 1:  # 只有当前这一个蛋白质组分
                            if new_sequence.strip():  # 有序列
                                # 根据缓存状态智能设置MSA
                                if has_cached_msa(new_sequence.strip()):
                                    st.session_state.components[i]['use_msa'] = True
                                else:
                                    st.session_state.components[i]['use_msa'] = False
                            else:  # 序列为空
                                st.session_state.components[i]['use_msa'] = False
                    
                    # 更激进的刷新策略：只要序列发生变化就刷新
                    # 这确保界面状态能及时更新
                    st.rerun()
                
                # Add cyclic peptide option and MSA settings for protein type
                if selected_type == 'protein':
                    # 使用最新的序列值（直接从session_state获取最新更新的值）
                    protein_sequence = st.session_state.components[i].get('sequence', '').strip()
                    
                    # 合并环肽选项和MSA选项到同一行
                    if protein_sequence:
                        # 有序列时：环肽选项 + MSA选项 + 缓存状态
                        protein_opts_cols = st.columns([1.5, 1.5, 1, 1])
                        
                        with protein_opts_cols[0]:
                            # 使用独立变量接收checkbox值，然后更新session_state
                            cyclic_value = st.checkbox(
                                "环肽 (Cyclic)",
                                value=st.session_state.components[i].get('cyclic', False),
                                key=f"cyclic_{component['id']}",
                                help="勾选此项表示该蛋白质序列是一个环状肽。对于环肽，模型将尝试生成闭合的环状结构。",
                                disabled=is_running
                            )
                            # 检测状态变化并更新
                            if cyclic_value != st.session_state.components[i].get('cyclic', False):
                                st.session_state.components[i]['cyclic'] = cyclic_value
                                st.rerun()
                        
                        with protein_opts_cols[1]:
                            # 使用独立变量接收checkbox值，然后更新session_state
                            msa_value = st.checkbox(
                                "启用 MSA",
                                value=st.session_state.components[i].get('use_msa', True),
                                key=f"msa_{component['id']}",
                                help="为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。",
                                disabled=is_running
                            )
                            # 检测状态变化并更新
                            if msa_value != st.session_state.components[i].get('use_msa', True):
                                st.session_state.components[i]['use_msa'] = msa_value
                                st.rerun()
                        
                        with protein_opts_cols[2]:
                            # 显示此组分的缓存状态 - 基于最新序列值
                            if has_cached_msa(protein_sequence):
                                st.markdown("🟢&nbsp;**已缓存**", unsafe_allow_html=True)
                            else:
                                st.markdown("🟡&nbsp;**未缓存**", unsafe_allow_html=True)
                        
                        with protein_opts_cols[3]:
                            # 显示缓存状态的详细信息 - 基于最新序列值
                            if has_cached_msa(protein_sequence):
                                st.markdown("⚡&nbsp;快速加载", unsafe_allow_html=True)
                            else:
                                st.markdown("🔄&nbsp;需要生成", unsafe_allow_html=True)
                    else:
                        # 无序列时：只显示环肽选项，MSA设置为默认值
                        cyclic_value = st.checkbox(
                            "环肽 (Cyclic Peptide)",
                            value=st.session_state.components[i].get('cyclic', False),
                            key=f"cyclic_{component['id']}",
                            help="勾选此项表示该蛋白质序列是一个环状肽。对于环肽，模型将尝试生成闭合的环状结构。",
                            disabled=is_running
                        )
                        # 使用中间变量检测状态变化
                        if cyclic_value != st.session_state.components[i].get('cyclic', False):
                            st.session_state.components[i]['cyclic'] = cyclic_value
                            st.rerun()
                        # 序列为空时，默认启用MSA但不显示缓存状态
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
            """添加新组分并智能设置MSA默认值"""
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
        
        # 创建两列布局
        col_global_left, col_global_right = st.columns(2)
        
        with col_global_left:
            # 显示MSA使用概览（基于统一策略）
            protein_components = [comp for comp in st.session_state.components 
                                if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
            
            if protein_components:
                # 确定统一的MSA策略
                cached_count = sum(1 for comp in protein_components 
                                 if comp.get('use_msa', True) and has_cached_msa(comp['sequence']))
                enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
                total_proteins = len(protein_components)
                
                # 应用统一策略逻辑
                if enabled_count == 0:
                    strategy = "none"
                    strategy_desc = "跳过MSA生成"
                elif cached_count == enabled_count and enabled_count == total_proteins:
                    strategy = "cached"  
                    strategy_desc = "使用缓存MSA"
                elif cached_count == 0 and enabled_count == total_proteins:
                    strategy = "auto"
                    strategy_desc = "自动生成MSA"
                else:
                    # 混合情况
                    strategy = "mixed"
                    strategy_desc = "混合MSA策略"
                
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
            # MSA缓存管理（与分子设计相同的逻辑）
            st.markdown("**MSA 缓存状态**")
            
            # 获取缓存统计信息（只显示，不提供清理功能）
            cache_stats = get_cache_stats()
            
            if cache_stats['total_files'] > 0:
                st.caption(f"📁 {cache_stats['total_files']} 个缓存文件 ({cache_stats['total_size_mb']:.1f} MB)")
                
                # 检查当前蛋白质组分的缓存状态
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
        
        has_ligand_component = any(comp['type'] == 'ligand' for comp in st.session_state.components)
        if has_ligand_component:
            affinity_value = st.checkbox(
                "🔬 计算结合亲和力 (Affinity)",
                value=st.session_state.properties.get('affinity', False),
                disabled=is_running,
                help="勾选后，模型将尝试预测小分子与大分子组分之间的结合亲和力。请确保至少输入了一个小分子组分。"
            )
            # 使用中间变量检测状态变化
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
                                    # 超出了可用的链ID范围
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

        # === 约束配置 ===
        st.markdown("---")
        st.subheader("🔗 分子约束 (可选)", anchor=False)
        st.markdown("设置分子结构约束，包括键约束、口袋约束和接触约束。")
        
        # 显示现有的约束
        constraint_id_to_delete = None
        for i, constraint in enumerate(st.session_state.constraints[:]):
            constraint_type = constraint.get('type', 'contact')
            
            # 根据约束类型显示不同的标题
            constraint_labels = {
                'bond': '🔗 键约束',
                'contact': '📍 接触约束'
            }
            
            with st.expander(f"{constraint_labels.get(constraint_type, '📍 约束')} {i+1}", expanded=True):
                col1, col2 = st.columns([5, 1])
                
                with col1:
                    # 约束类型选择
                    st.markdown("**约束类型**")
                    constraint_type = st.selectbox(
                        "选择约束类型",
                        options=['contact', 'bond'],
                        format_func=lambda x: {
                            'contact': '📍 Contact - 接触约束 (两个残基间距离)',
                            'bond': '🔗 Bond - 键约束 (两个原子间共价键)'
                        }[x],
                        index=['contact', 'bond'].index(constraint.get('type', 'contact')),
                        key=f"constraint_type_{i}",
                        disabled=is_running,
                        help="选择约束的类型：接触距离或共价键"
                    )
                    
                    # 检测约束类型变化并触发更新
                    if constraint_type != constraint.get('type', 'contact'):
                        constraint['type'] = constraint_type
                        # 清除不相关的配置
                        if constraint_type == 'bond':
                            # bond只需要atom1和atom2
                            constraint.pop('binder', None)
                            constraint.pop('contacts', None)
                        elif constraint_type == 'contact':
                            # contact需要token1和token2
                            constraint.pop('atom1_chain', None)
                            constraint.pop('atom1_residue', None)
                            constraint.pop('atom1_atom', None)
                            constraint.pop('atom2_chain', None)
                            constraint.pop('atom2_residue', None)
                            constraint.pop('atom2_atom', None)
                        st.rerun()
                    
                    # 获取可用链ID和描述
                    available_chains, chain_descriptions = get_available_chain_ids(st.session_state.components)
                    
                    st.markdown("---")
                    
                    # 根据约束类型显示不同的配置UI
                    if constraint_type == 'contact':
                        # Contact约束配置
                        render_contact_constraint_ui(constraint, f"constraint_{i}", available_chains, chain_descriptions, is_running)
                    elif constraint_type == 'bond':
                        # Bond约束配置
                        render_bond_constraint_ui(constraint, f"constraint_{i}", available_chains, chain_descriptions, is_running)
                
                with col2:
                    if st.button("🗑️", key=f"del_constraint_{i}", help="删除此约束", disabled=is_running):
                        constraint_id_to_delete = i
        
        # 删除约束
        if constraint_id_to_delete is not None:
            del st.session_state.constraints[constraint_id_to_delete]
            st.rerun()
        
        # 添加新约束按钮
        st.markdown("---")
        add_constraint_cols = st.columns(2)
        
        with add_constraint_cols[0]:
            if st.button("➕ 添加 Contact 约束", key="add_contact_constraint", disabled=is_running, help="添加接触距离约束"):
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
            if st.button("➕ 添加 Bond 约束", key="add_bond_constraint", disabled=is_running, help="添加共价键约束"):
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
            
            constraint_type_names = {'contact': 'Contact', 'bond': 'Bond'}
            type_summary = ', '.join([f"{count}个{constraint_type_names[ctype]}" 
                                    for ctype, count in constraint_types.items()])
            st.info(f"💡 已配置 {constraint_count} 个约束：{type_summary}")
        else:
            st.info("💡 暂无约束。可根据需要添加Contact或Bond约束。")

    is_valid, validation_message = validate_inputs(st.session_state.components)
    yaml_preview = generate_yaml_from_state() if is_valid else None

    # 添加 YAML 预览功能，帮助用户调试
    if yaml_preview and is_valid:
        with st.expander("📋 **预览生成的 YAML 配置**", expanded=False):
            st.markdown("以下是根据您的输入生成的 YAML 配置文件，将被发送给 Boltz 模型进行预测：")
            st.code(yaml_preview, language='yaml')
            
            # 特别提示 ketcher 转换
            has_ketcher = any(comp.get('type') == 'ligand' and comp.get('input_method') == 'ketcher' 
                            for comp in st.session_state.components)
            if has_ketcher:
                st.info("💡 **注意**: Ketcher 绘制的分子已自动转换为 `smiles` 字段，这是 Boltz 模型要求的格式。", icon="🔄")

    if st.button("🚀 提交预测任务", type="primary", disabled=(not is_valid or is_running), use_container_width=True):
        st.session_state.task_id = None
        st.session_state.results = None
        st.session_state.raw_zip = None
        st.session_state.error = None
        
        # 检查是否有蛋白质组分需要MSA
        protein_components = [comp for comp in st.session_state.components 
                            if comp['type'] == 'protein' and comp.get('sequence', '').strip()]
        
        # 智能检测MSA策略：如果YAML中已有MSA路径（缓存），则不使用MSA服务器
        use_msa_for_job = False
        if protein_components:
            yaml_data = yaml.safe_load(yaml_preview)
            has_msa_in_yaml = False
            
            # 检查YAML中是否已经包含MSA信息
            for sequence_item in yaml_data.get('sequences', []):
                if 'protein' in sequence_item:
                    protein_data = sequence_item['protein']
                    if protein_data.get('msa') and protein_data['msa'] != 'empty':
                        has_msa_in_yaml = True
                        break
            
            # 如果YAML中没有MSA信息，且有蛋白质启用了MSA，则使用MSA服务器
            if not has_msa_in_yaml:
                use_msa_for_job = any(comp.get('use_msa', True) for comp in protein_components)
        
        with st.spinner("⏳ 正在提交任务，请稍候..."):
            try:
                task_id = submit_job(
                    yaml_content=yaml_preview,
                    use_msa=use_msa_for_job
                )
                st.session_state.task_id = task_id
                
                # 显示MSA使用情况
                if use_msa_for_job:
                    msa_enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
                    st.toast(f"🎉 任务已提交！将为 {msa_enabled_count} 个蛋白质组分生成MSA", icon="✅")
                elif has_msa_in_yaml:
                    st.toast(f"🎉 任务已提交！使用缓存的MSA文件，预测将更快完成", icon="⚡")
                else:
                    st.toast(f"🎉 任务已提交！跳过MSA生成，预测将更快完成", icon="⚡")
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
                            
                            # 显示调试信息
                            with st.expander("🔍 **调试信息**", expanded=False):
                                st.markdown("**任务ID：**")
                                st.code(st.session_state.task_id)
                                
                                st.markdown("**提交的 YAML 配置：**")
                                if yaml_preview:
                                    st.code(yaml_preview, language='yaml')
                                
                                st.markdown("**完整错误信息：**")
                                st.json(st.session_state.error)
                                
                                # 特别检查是否是 ketcher 相关问题
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
            if st.button("🔄 重置并重新开始", type="secondary", use_container_width=True):
                for key in ['task_id', 'results', 'raw_zip', 'error', 'components', 'contacts', 'properties', 'use_msa_server']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        
        with col_reset[1]:
            if st.button("🔧 保留配置重新设计", type="primary", use_container_width=True):
                # 只清除任务状态，保留配置信息
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
                            st.checkbox("🔄 旋转模型", key='spin_model_vis', value=False, help="勾选后，模型将自动围绕Z轴旋转。")
                    
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
                <div style='display: flex; align-items: center;'><div style='width: 15px; height: 15px; background-color: #FF7D45; border-radius: 3px; margin-right: 5px;'></div><span><b>低</b> (&lt; 50)</span></div>
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("<b>全局结构质量指标</b>", unsafe_allow_html=True)
            cols_metrics = st.columns(2)
            cols_metrics[0].metric(
                "平均 pLDDT",
                f"{confidence_data.get('complex_plddt', 0):.2f}",
                help="预测的局部距离差异检验 (pLDDT) 是一个 0-100 范围内的单残基置信度得分，代表模型对局部结构预测的信心。这是整个复合物所有残基的平均 pLDDT 分数。值越高越好。"
            )
            cols_metrics[1].metric(
                "pTM",
                f"{confidence_data.get('ptm', 0):.4f}",
                help="预测的模板建模评分 (pTM) 是一个 0-1 范围内的分数，用于衡量预测结构与真实结构在全局拓扑结构上的相似性。pTM > 0.5 通常表示预测了正确的折叠方式。值越高越好。"
            )
            cols_metrics[0].metric(
                "ipTM",
                f"{confidence_data.get('iptm', 0):.4f}",
                help="界面预测模板建模评分 (ipTM) 是专门用于评估链间相互作用界面准确性的指标 (0-1)。ipTM > 0.85 通常表明对复合物的相互作用方式有很高的置信度。值越高越好。"
            )
            cols_metrics[1].metric(
                "PAE (Å)",
                f"{confidence_data.get('complex_pde', 0):.2f}",
                help="预测的对齐误差 (PAE) 表示残基对之间的预期位置误差（单位为埃 Å）。较低的值表示对不同结构域和链的相对位置和方向有更高的信心。这里显示的是整个复合物的平均误差。值越低越好。"
            )
            
            if affinity_data and st.session_state.properties.get('affinity'):
                st.markdown("<br><b>亲和力预测指标</b>", unsafe_allow_html=True)
                
                # 收集所有亲和力预测值 - 参考虚拟筛选中的处理方式
                affinity_values = []
                for key in ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        affinity_values.append(value)
                
                # 使用平均值作为最终的亲和力预测值
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
                    
                    # 根据是否有多个预测值来决定显示格式
                    if len(affinity_values) > 1:
                        # 计算IC50的标准差范围
                        ic50_std_lower = math.pow(10, log_ic50_in_uM - affinity_std)
                        ic50_std_upper = math.pow(10, log_ic50_in_uM + affinity_std)
                        
                        # 格式化IC50显示（带标准差）
                        if ic50_uM > 1000:
                            display_ic50_with_std = f"{ic50_uM/1000:.3f} ± {(ic50_std_upper-ic50_std_lower)/2000:.3f} mM"
                        elif ic50_uM > 1000000:
                            display_ic50_with_std = f"{ic50_uM/1000000:.3f} ± {(ic50_std_upper-ic50_std_lower)/2000000:.3f} M"
                        else:
                            display_ic50_with_std = f"{ic50_uM:.3f} ± {(ic50_std_upper-ic50_std_lower)/2:.3f} μM"
                            
                        st.metric("预测 IC50", display_ic50_with_std, help=f"预测的半数抑制浓度 (IC50)，基于 {len(affinity_values)} 个预测值的平均结果。数值越低表示预测的亲和力越强。")
                        # affinity_cols = st.columns(2)
                        # affinity_cols[0].metric("pIC50", f"{pIC50:.3f} ± {affinity_std:.3f}", help=f"pIC50 是 IC50 值的负对数，基于 {len(affinity_values)} 个预测值的平均结果。数值越高表示预测的亲和力越强。")
                        # affinity_cols[1].metric("ΔG (kcal/mol)", f"{delta_g:.3f} ± {affinity_std*1.364:.3f}", help=f"预测的吉布斯自由能 (ΔG)，基于 {len(affinity_values)} 个预测值的平均结果。负值越大，表明结合作用越强、越有利。")
                    else:
                        st.metric("预测 IC50", display_ic50, help="预测的半数抑制浓度 (IC50) 是指结合体（Binder）抑制其靶标 50% 所需的浓度。它是衡量效力的常用指标，数值越低表示预测的亲和力越强。")
                        # affinity_cols = st.columns(2)
                        # affinity_cols[0].metric("pIC50", f"{pIC50:.3f}", help="pIC50 是 IC50 值的负对数 (pIC50 = -log10(IC50 in M))。这个标度更便于比较，数值越高表示预测的亲和力越强。")
                        # affinity_cols[1].metric("ΔG (kcal/mol)", f"{delta_g:.3f}", help="预测的吉布斯自由能 (ΔG) 反映了结合事件的自发性，由 pIC50 计算得出。负值越大，表明结合作用越强、越有利。")
                    
                # 收集结合概率值 - 处理多个结合概率预测值
                binding_probabilities = []
                for key in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        binding_probabilities.append(value)
                
                # 使用平均的结合概率
                if binding_probabilities:
                    binder_prob = np.mean(binding_probabilities)
                    binding_prob_std = np.std(binding_probabilities) if len(binding_probabilities) > 1 else 0.0
                    
                    # 根据是否有多个预测值来决定显示格式
                    if len(binding_probabilities) > 1:
                        st.metric("结合概率", f"{binder_prob:.2%} ± {binding_prob_std:.2%}", help=f"模型预测结合体与其余组分形成稳定复合物的概率，基于 {len(binding_probabilities)} 个预测值的平均结果。百分比越高，表明模型对这是一个真实的结合事件越有信心。")
                    else:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测结合体与其余组分形成稳定复合物的概率。百分比越高，表明模型对这是一个真实的结合事件越有信心。")
                else:
                    # 如果没有收集到多个值，尝试获取单个值
                    binder_prob = affinity_data.get("affinity_probability_binary")
                    if binder_prob is not None:
                        st.metric("结合概率", f"{binder_prob:.2%}", help="模型预测结合体与其余组分形成稳定复合物的概率。百分比越高，表明模型对这是一个真实的结合事件越有信心。")
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
            
            cols_download = st.columns(2)
            with cols_download[0]:
                if cif_data:
                    st.download_button("📥 下载 PDB", export_to_pdb(cif_data), "predicted_structure.pdb", "chemical/x-pdb", use_container_width=True, help="下载预测结构的PDB格式文件。")
            with cols_download[1]:
                 if cif_data:
                    st.download_button("📄 下载 CIF", cif_data, "predicted_structure.cif", "chemical/x-cif", use_container_width=True, help="下载预测结构的CIF格式文件。")
            
            all_json_data = {"confidence": confidence_data, "affinity": affinity_data}
            st.download_button(
                label="📦 下载指标数据 (JSON)",
                data=json.dumps(all_json_data, indent=2),
                file_name="prediction_metrics.json",
                mime="application/json",
                use_container_width=True,
                help="下载包含pLDDT、pTM、ipTM、PAE以及亲和力预测结果的JSON文件。"
            )

# ========== 分子设计标签页 ==========
with tab2:
    st.markdown("### 🧪 从头分子设计")
    st.markdown("使用演化算法设计分子结合体，优化其与目标复合物的结合亲和力。")
    
    designer_is_running = (
        st.session_state.designer_task_id is not None and 
        st.session_state.designer_results is None and 
        st.session_state.designer_error is None
    )
    
    with st.expander("🎯 **步骤 1: 设置设计目标**", expanded=not designer_is_running and not st.session_state.designer_results):
        st.markdown("配置您的分子设计任务参数。")
        
        # 初始化 Designer 组分状态
        if 'designer_components' not in st.session_state:
            st.session_state.designer_components = [
                {'id': str(uuid.uuid4()), 'type': 'protein', 'sequence': '', 'num_copies': 1, 'use_msa': False}
            ]
        
        # 初始化约束状态
        if 'designer_constraints' not in st.session_state:
            st.session_state.designer_constraints = []
        
        # 组分管理
        designer_id_to_delete = None
        for i, component in enumerate(st.session_state.designer_components[:]):
            st.markdown(f"---")
            st.subheader(f"组分 {i+1}", anchor=False)
            
            cols_comp = st.columns([3, 1, 1])
            
            # 组分类型选择
            with cols_comp[0]:
                comp_type_options = ['protein', 'dna', 'rna', 'ligand']
                current_type = component.get('type', 'protein')
                current_type_index = comp_type_options.index(current_type) if current_type in comp_type_options else 0
                
                # 保存旧类型用于变化检测
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
                    key=f"designer_type_{component['id']}",
                    index=current_type_index,
                    disabled=designer_is_running,
                    help="选择此组分的分子类型：蛋白质、DNA、RNA或小分子配体。"
                )
                
                # 检测类型是否发生变化
                type_changed = new_type != old_type
                
                # 更新组分类型
                component['type'] = new_type
                
                # 如果类型发生变化，清除相关设置并触发刷新
                if type_changed:
                    # 清除序列内容（不同类型的序列格式不同）
                    component['sequence'] = ''
                    
                    # 清除类型特定的设置
                    if 'use_msa' in component:
                        del component['use_msa']
                    if 'cyclic' in component:
                        del component['cyclic']
                    if 'input_method' in component:
                        del component['input_method']
                    
                    # 根据新类型设置默认值
                    if new_type == 'protein':
                        component['use_msa'] = get_smart_msa_default(st.session_state.designer_components)
                    elif new_type == 'ligand':
                        component['input_method'] = 'smiles'
                    
                    # 显示类型变化的提示信息
                    type_display_names = {
                        "protein": "🧬 蛋白质/肽链",
                        "dna": "🧬 DNA",
                        "rna": "🧬 RNA", 
                        "ligand": "💊 辅酶/小分子"
                    }
                    st.toast(f"组分类型已更新为 {type_display_names.get(new_type, new_type)}", icon="🔄")
                    
                    # 立即触发刷新以更新界面
                    st.rerun()
            
            # 拷贝数设置
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
            
            # 删除按钮
            with cols_comp[2]:
                if len(st.session_state.designer_components) > 1:
                    if st.button("🗑️", key=f"designer_del_{component['id']}", help="删除此组分", disabled=designer_is_running):
                        designer_id_to_delete = component['id']
            
            # 显示预计分配的链ID
            num_copies = component.get('num_copies', 1)
            if num_copies > 1:
                st.caption(f"💡 此组分将创建 {num_copies} 个拷贝，自动分配链ID")
            
            # 序列输入
            if component['type'] == 'protein':
                # 保存旧序列用于变化检测
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
                
                # 检测序列是否发生变化
                sequence_changed = new_sequence != old_sequence
                
                # 更新序列到组分
                component['sequence'] = new_sequence
                
                # 如果序列发生变化，进行智能MSA调整和刷新
                if sequence_changed:
                    # 当只有一个蛋白质组分时，基于缓存状态智能设置MSA
                    protein_components = [comp for comp in st.session_state.designer_components if comp.get('type') == 'protein']
                    if len(protein_components) == 1:  # 只有当前这一个蛋白质组分
                        if new_sequence.strip():  # 有序列
                            # 根据缓存状态智能设置MSA
                            if has_cached_msa(new_sequence.strip()):
                                component['use_msa'] = True
                            else:
                                component['use_msa'] = False
                        else:  # 序列为空
                            component['use_msa'] = False
                    
                    # 这确保界面状态能及时更新
                    st.rerun()
                
                # MSA选项 - 使用最新的序列值
                designer_sequence = component.get('sequence', '').strip()
                if designer_sequence:
                    # 有序列时：只显示MSA选项
                    msa_value = st.checkbox(
                        "启用 MSA",
                        value=component.get('use_msa', True),
                        key=f"designer_msa_{component['id']}",
                        help="为此蛋白质组分生成多序列比对以提高预测精度。取消勾选可以跳过MSA生成，节省时间。",
                        disabled=designer_is_running
                    )
                    # 使用中间变量检测状态变化
                    if msa_value != component.get('use_msa', True):
                        component['use_msa'] = msa_value
                        # 显示MSA状态变化的提示
                        if msa_value:
                            st.toast("✅ 已启用 MSA 生成", icon="🧬")
                        else:
                            st.toast("❌ 已禁用 MSA 生成", icon="⚡")
                        st.rerun()
                else:
                    # 序列为空时，默认启用MSA但不显示缓存状态
                    component['use_msa'] = component.get('use_msa', True)
                    
                # 清除可能残留的环肽设置（因为在分子设计中，环肽是针对结合肽的，不是目标蛋白）
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
                # 保存旧输入方式用于变化检测
                old_input_method = component.get('input_method', 'smiles')
                
                new_input_method = st.radio(
                    "小分子输入方式",
                    ["smiles", "ccd", "ketcher"],
                    key=f"designer_method_{component['id']}",
                    horizontal=True,
                    disabled=designer_is_running,
                    help="选择通过SMILES字符串、PDB CCD代码或分子编辑器输入小分子。"
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        )
                
                # 检测输入方式是否发生变化
                input_method_changed = new_input_method != old_input_method
                
                # 更新输入方式
                component['input_method'] = new_input_method
                
                # 如果输入方式发生变化，清除序列内容并触发刷新
                if input_method_changed:
                    # 清除序列内容（不同输入方式的格式不同）
                    component['sequence'] = ''
                    
                    # 显示输入方式变化的提示信息
                    method_display_names = {
                        "smiles": "SMILES 字符串",
                        "ccd": "PDB CCD 代码", 
                        "ketcher": "分子编辑器"
                    }
                    st.toast(f"输入方式已更新为 {method_display_names.get(new_input_method, new_input_method)}", icon="🔄")
                    
                    # 立即触发刷新以更新界面
                    st.rerun()
                
                # 直接使用radio返回的值来显示对应的输入控件
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
                            st.toast("✅ SMILES 字符串已成功更新！", icon="🧪")
                        st.rerun()
                    
                    # 显示当前SMILES
                    current_smiles_display = st.session_state.designer_components[i].get('sequence', '')
                    if current_smiles_display:
                        st.caption("✨ 当前 SMILES 字符串:")
                        st.code(current_smiles_display, language='smiles')
                    else:
                        st.info("👆 请开始绘制或粘贴，SMILES 将会显示在这里。")
        
        # 删除组分
        if designer_id_to_delete:
            st.session_state.designer_components = [c for c in st.session_state.designer_components if c['id'] != designer_id_to_delete]
            st.rerun()
        
        # 添加组分按钮
        def add_new_designer_component():
            """添加新的设计组分并智能设置MSA默认值"""
            smart_msa_default = get_smart_msa_default(st.session_state.designer_components)
            st.session_state.designer_components.append({
                'id': str(uuid.uuid4()),
                'type': 'protein',
                'sequence': '',
                'num_copies': 1,
                'use_msa': smart_msa_default
            })
        
        if st.button("➕ 添加新组分", disabled=designer_is_running, help="添加新的蛋白质、DNA/RNA或小分子组分"):
            add_new_designer_component()
            st.rerun()
        
        # 后台计算目标链ID和结合肽链ID（不显示给用户）
        target_bio_chains = [comp for comp in st.session_state.designer_components if comp['type'] in ['protein', 'dna', 'rna'] and comp.get('sequence', '').strip()]
        target_ligand_chains = [comp for comp in st.session_state.designer_components if comp['type'] == 'ligand' and comp.get('sequence', '').strip()]
        
        if target_bio_chains or target_ligand_chains:
            # 计算总链数以确定结合肽的链ID
            total_chains = 0
            for comp in st.session_state.designer_components:
                if comp.get('sequence', '').strip():
                    total_chains += comp.get('num_copies', 1)
            
            # 结合肽链ID自动为下一个可用链ID
            binder_chain_id = string.ascii_uppercase[total_chains] if total_chains < 26 else f"Z{total_chains-25}"
            target_chain_id = 'A'  # 默认目标为第一个链
        else:
            target_chain_id = 'A'
            binder_chain_id = 'B'
        
        # === 分子约束配置 ===
        st.subheader("🔗 分子约束 (可选)", anchor=False)
        st.markdown("设置分子结构约束，包括键约束、口袋约束和接触约束。")
        
        # 显示现有的约束
        constraint_id_to_delete = None
        for i, constraint in enumerate(st.session_state.designer_constraints[:]):
            constraint_type = constraint.get('type', 'contact')
            
            # 根据约束类型显示不同的标题
            constraint_labels = {
                'bond': '🔗 键约束',
                'contact': '📍 接触约束'
            }
            
            with st.expander(f"{constraint_labels.get(constraint_type, '📍 约束')} {i+1}", expanded=True):
                col1, col2 = st.columns([5, 1])
                
                with col1:
                    # 约束类型选择
                    st.markdown("**约束类型**")
                    constraint_type = st.selectbox(
                        "选择约束类型",
                        options=['contact', 'bond'],
                        format_func=lambda x: {
                            'contact': '📍 Contact - 接触约束 (两个残基间距离)',
                            'bond': '🔗 Bond - 键约束 (两个原子间共价键)'
                        }[x],
                        index=['contact', 'bond'].index(constraint.get('type', 'contact')),
                        key=f"designer_constraint_type_{i}",
                        disabled=designer_is_running,
                        help="选择约束的类型：接触距离或共价键"
                    )
                    
                    # 检测约束类型变化并触发更新
                    if constraint_type != constraint.get('type', 'contact'):
                        constraint['type'] = constraint_type
                        # 清除不相关的配置
                        if constraint_type == 'bond':
                            constraint.pop('binder', None)
                            constraint.pop('contacts', None)
                        elif constraint_type == 'contact':
                            constraint.pop('atom1_chain', None)
                            constraint.pop('atom1_residue', None)
                            constraint.pop('atom1_atom', None)
                            constraint.pop('atom2_chain', None)
                            constraint.pop('atom2_residue', None)
                            constraint.pop('atom2_atom', None)
                        st.rerun()
                    
                    # 获取可用链ID和描述
                    available_chains, chain_descriptions = get_available_chain_ids(st.session_state.designer_components)
                    
                    st.markdown("---")
                    
                    # 根据约束类型显示不同的配置UI
                    if constraint_type == 'contact':
                        # Contact约束配置
                        render_contact_constraint_ui(constraint, f"designer_{i}", available_chains, chain_descriptions, designer_is_running)
                    elif constraint_type == 'bond':
                        # Bond约束配置
                        render_bond_constraint_ui(constraint, f"designer_{i}", available_chains, chain_descriptions, designer_is_running)
                
                with col2:
                    if st.button("🗑️", key=f"designer_del_constraint_{i}", help="删除此约束", disabled=designer_is_running):
                        constraint_id_to_delete = i
        
        # 删除约束
        if constraint_id_to_delete is not None:
            del st.session_state.designer_constraints[constraint_id_to_delete]
            st.rerun()
        
        # 添加新约束按钮
        st.markdown("---")
        add_constraint_cols = st.columns(2)
        
        with add_constraint_cols[0]:
            if st.button("➕ 添加 Contact 约束", key="add_designer_contact_constraint", disabled=designer_is_running, help="添加接触距离约束"):
                st.session_state.designer_constraints.append({
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
            if st.button("➕ 添加 Bond 约束", key="add_designer_bond_constraint", disabled=designer_is_running, help="添加共价键约束"):
                st.session_state.designer_constraints.append({
                    'type': 'bond',
                    'atom1_chain': 'A',
                    'atom1_residue': 1,
                    'atom1_atom': 'CA',
                    'atom2_chain': 'B',
                    'atom2_residue': 1,
                    'atom2_atom': 'CA'
                })
                st.rerun()
        
        if st.session_state.designer_constraints:
            constraint_count = len(st.session_state.designer_constraints)
            constraint_types = {}
            for c in st.session_state.designer_constraints:
                ctype = c.get('type', 'contact')
                constraint_types[ctype] = constraint_types.get(ctype, 0) + 1
            
            constraint_type_names = {'contact': 'Contact', 'bond': 'Bond'}
            type_summary = ', '.join([f"{count}个{constraint_type_names[ctype]}" 
                                    for ctype, count in constraint_types.items()])
            st.info(f"💡 已配置 {constraint_count} 个约束：{type_summary}")
        else:
            st.info("💡 暂无约束。可根据需要添加Contact或Bond约束。")
        
        st.markdown("---")
        
        # 设计类型选择
        st.subheader("设计参数", anchor=False)
        col1, col2, col3 = st.columns(3)
        
        with col1:
            design_type = st.selectbox(
                "设计类型",
                options=["peptide", "glycopeptide"],
                format_func=lambda x: "🧬 多肽设计" if x == "peptide" else "🍯 糖肽设计",
                help="选择是设计普通多肽还是含有糖基修饰的糖肽。",
                disabled=designer_is_running
            )
        
        with col2:
            binder_length = st.number_input(
                "结合肽长度",
                min_value=5,
                max_value=50,
                value=20,
                step=1,
                help="设计的结合肽的氨基酸残基数量。",
                disabled=designer_is_running
            )
        
        with col3:
            # 使用空字符串占位以保持对齐
            st.write("")  # 这会创建与其他列标签相同的垂直空间
            cyclic_binder = st.checkbox(
                "环状结合肽",
                value=False,
                help="勾选此项将设计的结合肽设计为环状肽，具有闭合的环状结构。",
                disabled=designer_is_running
            )
        
        # 初始序列设置
        st.subheader("🧬 初始序列设置", anchor=False)
        use_initial_sequence = st.checkbox(
            "使用初始序列作为演化起点",
            value=False,
            help="启用后可以提供一个初始序列作为演化算法的起点，而不是完全随机生成。",
            disabled=designer_is_running
        )
        
        initial_sequence = None
        if use_initial_sequence:
            initial_sequence = st.text_input(
                "初始序列",
                value="",
                placeholder="例如: MVSKGEELFTGVVPILVELD...",
                help=f"输入初始氨基酸序列。长度应该等于结合肽长度({binder_length})。如果长度不匹配，系统会自动调整。",
                disabled=designer_is_running
            )
            
            if initial_sequence:
                seq_len = len(initial_sequence)
                if seq_len != binder_length:
                    if seq_len < binder_length:
                        st.warning(f"⚠️ 初始序列长度({seq_len})小于目标长度({binder_length})，将随机补全缺失部分。")
                    else:
                        st.warning(f"⚠️ 初始序列长度({seq_len})大于目标长度({binder_length})，将截取前{binder_length}个氨基酸。")
                else:
                    st.success(f"✅ 初始序列长度({seq_len})与目标长度匹配。")
                
                # 显示序列预览
                st.code(initial_sequence, language="text")
            else:
                st.info("💡 请输入一个有效的氨基酸序列作为演化起点。")
        
        # 演化算法参数
        st.subheader("演化算法参数", anchor=False)
        
        # 优化模式选择 (新增)
        st.subheader("🚀 优化模式选择", anchor=False)
        optimization_mode = st.selectbox(
            "选择优化策略",
            options=["balanced", "stable", "aggressive", "conservative", "custom"],
            format_func=lambda x: {
                "balanced": "⚖️ 平衡模式 (推荐)",
                "stable": "🎯 平稳优化",
                "aggressive": "🔥 激进探索", 
                "conservative": "🛡️ 保守设计",
                "custom": "⚙️ 自定义配置"
            }[x],
            index=0,
            help="选择预设的优化策略或自定义配置。不同策略适用于不同的设计场景。",
            disabled=designer_is_running
        )
        
        # 显示模式说明
        mode_descriptions = {
            "balanced": "⚖️ **平衡模式**: 综合考虑探索性和收敛性，适用于大多数设计任务。",
            "stable": "🎯 **平稳优化**: 稳定收敛，减少分数波动，适用于需要可重复结果的场景。",
            "aggressive": "🔥 **激进探索**: 快速突破局部最优，适用于初始分数较低或需要大幅改进的场景。",
            "conservative": "🛡️ **保守设计**: 小步优化，适用于已有较好序列或对稳定性要求高的场景。",
            "custom": "⚙️ **自定义配置**: 手动调整所有参数，适用于高级用户。"
        }
        st.info(mode_descriptions[optimization_mode])
        
        col3, col4, col5 = st.columns(3)
        
        with col3:
            generations = st.number_input(
                "演化代数",
                min_value=2,
                max_value=20,
                value=8,
                step=1,
                help="演化算法的迭代次数。更多代数通常产生更好的结果，但需要更长时间。",
                disabled=designer_is_running
            )
        
        with col4:
            population_size = st.number_input(
                "种群大小",
                min_value=2,
                max_value=50,
                value=12,
                step=1,
                help="每一代中的候选序列数量。",
                disabled=designer_is_running
            )
        
        with col5:
            # 计算合理的精英保留数范围和默认值
            max_elite_size = min(10, max(1, population_size//2))  # 确保至少为1
            default_elite_size = max(1, min(max_elite_size, min(5, max(1, population_size//3))))  # 确保在有效范围内
            
            elite_size = st.number_input(
                "精英保留数",
                min_value=1,
                max_value=max_elite_size,
                value=default_elite_size,
                step=1,
                help="每一代中保留的最优个体数量。",
                disabled=designer_is_running
            )
        
        col6, col7 = st.columns(2)
        with col6:
            mutation_rate = st.slider(
                "突变率",
                min_value=0.1,
                max_value=0.8,
                value=0.3,
                step=0.05,
                help="每一代中发生突变的概率。",
                disabled=designer_is_running
            )
        
        # 高级参数配置
        if optimization_mode == "custom":
            st.subheader("🔧 高级参数配置", anchor=False)
            col_adv1, col_adv2, col_adv3 = st.columns(3)
            
            with col_adv1:
                convergence_window = st.number_input(
                    "收敛窗口",
                    min_value=3,
                    max_value=10,
                    value=5,
                    help="收敛检测的滑动窗口大小。较小值更敏感。",
                    disabled=designer_is_running
                )
                
                convergence_threshold = st.number_input(
                    "收敛阈值",
                    min_value=0.0001,
                    max_value=0.01,
                    value=0.001,
                    format="%.4f",
                    help="收敛检测的分数方差阈值。较小值更严格。",
                    disabled=designer_is_running
                )
            
            with col_adv2:
                max_stagnation = st.number_input(
                    "最大停滞周期",
                    min_value=1,
                    max_value=10,
                    value=3,
                    help="触发早停的最大停滞周期数。较小值更激进。",
                    disabled=designer_is_running
                )
                
                initial_temperature = st.number_input(
                    "初始温度",
                    min_value=0.1,
                    max_value=5.0,
                    value=1.0,
                    step=0.1,
                    help="自适应突变的初始温度。较高值更探索性。",
                    disabled=designer_is_running
                )
            
            with col_adv3:
                min_temperature = st.number_input(
                    "最小温度",
                    min_value=0.01,
                    max_value=1.0,
                    value=0.1,
                    step=0.01,
                    help="自适应突变的最小温度。较高值保持更多随机性。",
                    disabled=designer_is_running
                )
                
                enable_enhanced = st.checkbox(
                    "启用增强功能",
                    value=True,
                    help="启用自适应突变、Pareto优化等增强功能。",
                    disabled=designer_is_running
                )
        else:
            # 预设模式的参数映射
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
        
        # 糖肽特有参数
        if design_type == "glycopeptide":
            with col7:
                # 糖基类型选项和描述
                glycan_options = {
                    "NAG": "N-乙酰葡糖胺 (N-acetylglucosamine) - 最常见的N-连接糖基化起始糖",
                    "MAN": "甘露糖 (Mannose) - 常见的高甘露糖型糖链组分",
                    "GAL": "半乳糖 (Galactose) - 复合型糖链的末端糖",
                    "FUC": "岩藻糖 (Fucose) - 分支糖链，增加分子多样性",
                    "NAN": "神经氨酸 (Neuraminic acid/Sialic acid) - 带负电荷的末端糖",
                    "GLC": "葡萄糖 (Glucose) - 基础单糖，能量代谢相关",
                    "XYL": "木糖 (Xylose) - 植物糖蛋白常见糖基",
                    "GALNAC": "N-乙酰半乳糖胺 (N-acetylgalactosamine) - O-连接糖基化起始糖",
                    "GLCA": "葡萄糖醛酸 (Glucuronic acid) - 带负电荷，参与解毒代谢"
                }
                
                glycan_type = st.selectbox(
                    "糖基类型",
                    options=list(glycan_options.keys()),
                    format_func=lambda x: f"{glycan_options[x].split(' (')[0]} ({x})",
                    index=0,  # 默认选择 NAG
                    help="选择要在糖肽中使用的糖基类型。不同糖基具有不同的化学性质和生物学功能。",
                    disabled=designer_is_running
                )
                
                # 显示选中糖基的详细信息
                st.info(f"**{glycan_type}**: {glycan_options[glycan_type]}", icon="🍯")
            
            # 糖基化位点参数
            glycosylation_site = st.number_input(
                "糖基化位点",
                min_value=1,
                max_value=binder_length,
                value=min(10, binder_length),  # 默认位点10，但不超过肽长度
                step=1,
                help=f"肽链上用于连接糖基的氨基酸位置 (1-{binder_length})。",
                disabled=designer_is_running
            )
        else:
            glycan_type = None
            glycosylation_site = None
    
    # 验证输入
    designer_is_valid, validation_message = validate_designer_inputs(st.session_state.designer_components)
    
    # 添加糖肽参数验证
    if design_type == "glycopeptide":
        if not glycan_type:
            designer_is_valid = False
            validation_message = "糖肽设计模式需要选择糖基类型。"
        elif not glycosylation_site or glycosylation_site < 1 or glycosylation_site > binder_length:
            designer_is_valid = False
            validation_message = f"糖基化位点必须在 1 到 {binder_length} 范围内。"
    
    # 添加初始序列验证
    if use_initial_sequence:
        if not initial_sequence or not initial_sequence.strip():
            designer_is_valid = False
            validation_message = "启用初始序列时必须提供有效的氨基酸序列。"
        else:
            # 验证序列是否只包含标准氨基酸
            valid_amino_acids = set("ACDEFGHIKLMNPQRSTVWY")
            invalid_chars = set(initial_sequence.upper()) - valid_amino_acids
            if invalid_chars:
                designer_is_valid = False
                validation_message = f"初始序列包含无效字符: {', '.join(invalid_chars)}。请只使用标准的20种氨基酸字母。"
    
    # 添加MSA验证 - 检查是否有蛋白质组分启用了MSA
    protein_components_with_msa = [comp for comp in st.session_state.designer_components 
                                  if comp['type'] == 'protein' and comp.get('sequence', '').strip() and comp.get('use_msa', True)]
    
    # 提交设计任务
    if st.button("🚀 开始分子设计", type="primary", disabled=(not designer_is_valid or designer_is_running), use_container_width=True):
        st.session_state.designer_task_id = None
        st.session_state.designer_results = None
        st.session_state.designer_error = None
        
        with st.spinner("⏳ 正在启动设计任务，请稍候..."):
            try:
                # 检查是否有任何蛋白质组分启用了MSA
                any_msa_enabled = any(comp.get('use_msa', True) for comp in st.session_state.designer_components if comp['type'] == 'protein')
                
                # 创建复合物模板 YAML - 传递MSA参数和所有类型的约束
                template_yaml = create_designer_complex_yaml(
                    st.session_state.designer_components, 
                    use_msa=any_msa_enabled,
                    constraints=st.session_state.designer_constraints
                )
                
                # 提交设计任务
                result = submit_designer_job(
                    template_yaml_content=template_yaml,
                    design_type=design_type,
                    binder_length=binder_length,
                    target_chain_id=target_chain_id,
                    generations=generations,
                    population_size=population_size,
                    elite_size=elite_size,
                    mutation_rate=mutation_rate,
                    glycan_type=glycan_type,
                    glycosylation_site=glycosylation_site,
                    # 增强功能参数
                    convergence_window=convergence_window,
                    convergence_threshold=convergence_threshold,
                    max_stagnation=max_stagnation,
                    initial_temperature=initial_temperature,
                    min_temperature=min_temperature,
                    enable_enhanced=enable_enhanced,
                    # 新增初始序列参数
                    use_initial_sequence=use_initial_sequence,
                    initial_sequence=initial_sequence if use_initial_sequence else None,
                    # 环状结合肽参数
                    cyclic_binder=cyclic_binder,
                    # 传递是否有MSA启用的信息（用于日志记录）
                    use_msa=any_msa_enabled
                )
                
                if result['success']:
                    st.session_state.designer_task_id = result['task_id']
                    st.session_state.designer_work_dir = result['work_dir']
                    st.session_state.designer_config = result['params']
                    st.toast(f"🎉 设计任务已成功启动！任务ID: {result['task_id']}", icon="✅")
                    st.rerun()
                else:
                    st.error(f"❌ **任务启动失败**：{result['error']}")
                    st.session_state.designer_error = {"error_message": result['error'], "type": "Task Start Error"}
                    
            except Exception as e:
                st.error(f"❌ **任务启动失败：发生未知错误**。详情: {e}")
                st.session_state.designer_error = {"error_message": str(e), "type": "Client Error"}
    
    if not designer_is_valid and not designer_is_running:
        # 只有当用户确实有输入内容时才显示验证错误
        has_user_input = any(comp.get('sequence', '').strip() for comp in st.session_state.designer_components)
        if has_user_input:
            st.error(f"⚠️ **无法启动设计**: {validation_message}")
    
    # 显示设计进度和结果
    if st.session_state.designer_task_id and not st.session_state.designer_results:
        st.divider()
        
        # 标题和停止按钮在同一行
        col_title, col_stop = st.columns([3, 2])
        with col_title:
            st.header("🔄 **步骤 2: 设计进度监控**", anchor=False)
        with col_stop:
            # 创建更美观的停止按钮样式
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
                # 停止设计任务
                try:
                    # 首先尝试通过设计管理器优雅停止
                    try:
                        import sys
                        designer_path = os.path.join(os.path.dirname(__file__), 'designer')
                        if designer_path not in sys.path:
                            sys.path.append(designer_path)
                        from design_manager import design_manager
                        
                        graceful_stop_success = design_manager.stop_current_design()
                        if graceful_stop_success:
                            st.info("🔄 已发送优雅停止信号，等待任务完成当前迭代...")
                    except Exception as e:
                        st.warning(f"优雅停止失败，将使用强制终止: {e}")
                        graceful_stop_success = False
                    
                    work_dir = st.session_state.get('designer_work_dir', None)
                    if work_dir:
                        # 读取状态文件以获取进程ID
                        status_file = os.path.join(work_dir, 'status.json')
                        if os.path.exists(status_file):
                            with open(status_file, 'r') as f:
                                status_info = json.load(f)
                                process_id = status_info.get('process_id')
                                
                                if process_id and psutil:
                                    try:
                                        # 终止run_design.py进程
                                        if psutil.pid_exists(process_id):
                                            proc = psutil.Process(process_id)
                                            # 检查确实是我们的进程
                                            cmdline = proc.cmdline()
                                            if cmdline and 'run_design.py' in ' '.join(cmdline):
                                                proc.terminate()  # 优雅终止
                                                # 等待一段时间后强制终止
                                                try:
                                                    proc.wait(timeout=5)
                                                    st.toast("✅ 设计任务已成功停止", icon="🛑")
                                                except psutil.TimeoutExpired:
                                                    proc.kill()  # 强制终止
                                                    st.toast("⚠️ 设计任务已强制停止", icon="🛑")
                                                
                                                # 更新状态文件
                                                status_info['status'] = 'cancelled'
                                                status_info['end_time'] = datetime.now().isoformat()
                                                status_info['error'] = '用户手动停止'
                                                with open(status_file, 'w') as f:
                                                    json.dump(status_info, f, indent=2)
                                                
                                                # 清理session state
                                                st.session_state.designer_task_id = None
                                                st.session_state.designer_work_dir = None
                                                st.session_state.designer_results = None
                                                st.session_state.designer_error = {"error_message": "用户手动停止任务", "type": "User Cancelled"}
                                                
                                                st.rerun()
                                            else:
                                                st.error("❌ 无法确认进程身份，停止失败")
                                        else:
                                            st.warning("⚠️ 设计进程可能已经结束")
                                            # 清理session state
                                            st.session_state.designer_task_id = None
                                            st.session_state.designer_work_dir = None
                                            st.session_state.designer_results = None
                                            st.rerun()
                                    except Exception as e:
                                        st.error(f"❌ 停止进程时发生错误: {e}")
                                else:
                                    st.error("❌ 无法获取进程信息或psutil不可用")
                        else:
                            st.error("❌ 找不到任务状态文件")
                    else:
                        st.error("❌ 找不到任务工作目录")
                        
                except Exception as e:
                    st.error(f"❌ 停止任务时发生错误: {e}")
        
        if not st.session_state.designer_error:
            # 检查任务状态并处理错误
            try:
                work_dir = st.session_state.get('designer_work_dir', None)
                status_data = get_designer_status(st.session_state.designer_task_id, work_dir)
                
                # 验证状态数据
                if not status_data or 'state' not in status_data:
                    st.error("❌ 无法获取任务状态信息，任务可能已失败或被中断")
                    st.session_state.designer_error = {"error_message": "无法获取任务状态", "type": "Status Error"}
                elif status_data.get('error'):
                    st.error(f"❌ 任务执行错误: {status_data['error']}")
                    st.session_state.designer_error = {"error_message": status_data['error'], "type": "Task Error"}
                else:
                    # 状态检查成功，显示进度
                    current_state = status_data['state']
                    
                    if current_state in ['COMPLETED', 'SUCCESS']:
                        st.success("🎉 设计任务已完成！正在加载结果...")
                        try:
                            work_dir = st.session_state.get('designer_work_dir', '/tmp')
                            results = load_designer_results(st.session_state.designer_task_id, work_dir)
                            st.session_state.designer_results = results
                            st.toast("✅ 设计任务已完成！", icon="🎊")
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
                            
                            # 检查是否已完成所有代数且有结果
                            current_best_sequences = progress.get('current_best_sequences', [])
                            
                            # 更严格的完成检测逻辑：
                            # 1. 当前代数严格大于总代数（完全结束）
                            # 2. 或者当前代数等于总代数且有明确的完成证据
                            # 3. 或者run_design.py进程已经结束且有结果文件
                            task_completed = False
                            
                            if current_gen > total_gen and total_gen > 0:
                                task_completed = True
                            elif current_gen == total_gen and total_gen > 0 and current_best_sequences:
                                # 最后一代的情况，需要非常严格的验证
                                try:
                                    work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                    log_file = os.path.join(work_dir, 'design.log')
                                    if os.path.exists(log_file):
                                        with open(log_file, 'r') as f:
                                            log_content = f.read()
                                            # 检查是否有明确的最终完成标记
                                            if any(indicator in log_content for indicator in [
                                                'Design Run Finished', 
                                                '设计任务完成', 
                                                'Successfully created results package',
                                                'All generations completed',
                                                f'Finished all {total_gen} generations'
                                            ]):
                                                task_completed = True
                                            # 或者检查CSV文件是否最近被更新且包含足够数据
                                            else:
                                                csv_files = [f for f in os.listdir(work_dir) 
                                                           if f.startswith('design_summary_') and f.endswith('.csv')]
                                                for csv_file in csv_files:
                                                    csv_path = os.path.join(work_dir, csv_file)
                                                    if os.path.exists(csv_path):
                                                        file_age = time.time() - os.path.getmtime(csv_path)
                                                        if file_age < 15:  # 15秒内修改过
                                                            # 额外验证：检查CSV中的代数数据
                                                            try:
                                                                df = pd.read_csv(csv_path)
                                                                if len(df) > 0:
                                                                    max_gen_in_csv = df['generation'].max() if 'generation' in df.columns else 0
                                                                    # 确保CSV中确实包含了最后一代的数据
                                                                    if max_gen_in_csv >= total_gen:
                                                                        task_completed = True
                                                                        break
                                                            except:
                                                                pass
                                except Exception:
                                    # 如果检查失败，不认为完成
                                    pass
                            
                            # 额外检查：特定的run_design.py 进程是否还在运行
                            if not task_completed:
                                try:
                                    # 检查保存的进程ID是否仍在运行
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
                                                        # 进一步验证这个PID确实是我们的run_design.py进程
                                                        proc = psutil.Process(saved_pid)
                                                        cmdline = proc.cmdline()
                                                        if cmdline and 'run_design.py' in ' '.join(cmdline):
                                                            design_process_running = True
                                                except (psutil.NoSuchProcess, psutil.AccessDenied):
                                                    design_process_running = False
                                    
                                    # 如果run_design.py进程已经结束，且有结果文件，可能任务已完成
                                    if not design_process_running and current_best_sequences:
                                        # 检查是否有最近的结果文件
                                        csv_files = [f for f in os.listdir(work_dir) 
                                                   if f.startswith('design_summary_') and f.endswith('.csv')]
                                        for csv_file in csv_files:
                                            csv_path = os.path.join(work_dir, csv_file)
                                            if os.path.exists(csv_path):
                                                file_age = time.time() - os.path.getmtime(csv_path)
                                                if file_age < 30:  # 30秒内修改过
                                                    task_completed = True
                                                    break
                                except Exception:
                                    # 如果检查失败，继续使用原有的检测逻辑
                                    pass
                            
                            if task_completed:
                                st.success("🎉 设计任务已完成！正在加载最终结果...")
                                try:
                                    work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                    results = load_designer_results(st.session_state.designer_task_id, work_dir)
                                    st.session_state.designer_results = results
                                    st.toast("✅ 设计任务已完成！", icon="🎊")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"❌ 加载结果时发生错误: {e}")
                                    st.session_state.designer_error = {"error_message": str(e), "type": "Result Loading Error"}
                            else:
                                if current_gen > 0:
                                    # 正常的进度条显示逻辑
                                    gen_progress = min(current_gen / total_gen, 1.0)
                                    
                                    st.progress(gen_progress, text=f"演化进度: 第 {current_gen}/{total_gen} 代 | 当前最佳评分: {best_score:.3f}")
                                    
                                    # 如果当前代数等于总代数，显示最后一代进行中的状态
                                    if current_gen == total_gen:
                                        st.info("🧬 正在完成最后一代演化，请稍候...")
                                else:
                                    st.progress(0.0, text="准备开始演化...")
                                
                                st.info(f"🧬 {status_msg}")
                                
                                # # 可选的调试信息展示
                                # if debug_info and st.checkbox("显示调试信息", key="show_debug_evolving"):
                                #     with st.expander("🔧 调试信息", expanded=False):
                                #         st.json(debug_info)
                                
                                # 显示当前最佳序列
                                if current_best_sequences:
                                    # 显示代数时减1，因为实际演化是从第0代开始
                                    display_gen = max(1, current_gen)  # 确保显示至少为第1代
                                    with st.expander(f"🏆 当前最佳序列 (第 {display_gen} 代)", expanded=True):
                                        for i, seq_info in enumerate(current_best_sequences[:3]):
                                            rank = i + 1
                                            score = seq_info.get('score', 0)
                                            sequence = seq_info.get('sequence', '')
                                            iptm = seq_info.get('iptm', 0)
                                            plddt = seq_info.get('plddt', 0)
                                            # 使用CSV中的generation字段，如果没有则使用当前代数
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
                        
                        # 显示刷新倒计时
                        countdown_placeholder = st.empty()
                        for remaining in range(10, 0, -1):
                            countdown_placeholder.caption(f"🔄 将在 {remaining} 秒后自动刷新...")
                            time.sleep(1)
                        
                        st.rerun()
                    
                    else:
                        # 处理其他状态，包括可能的"未明确标记为完成但实际已完成"的情况
                        # 检查是否有完成的迹象
                        progress = status_data.get('progress', {})
                        current_gen = progress.get('current_generation', 0)
                        total_gen = progress.get('total_generations', 1)
                        csv_sequences = progress.get('current_best_sequences', [])
                        
                        # 最严格的完成检测：
                        # 1. 代数严格超过总代数（完全结束）
                        # 2. 或者代数等于总代数且有明确完成证据
                        # 3. 或者run_design.py进程已经结束且有结果文件
                        task_likely_completed = False
                        
                        if current_gen > total_gen and total_gen > 0:
                            task_likely_completed = True
                        elif current_gen == total_gen and total_gen > 0 and csv_sequences:
                            # 最后一代的情况，需要非常严格的验证
                            try:
                                work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                log_file = os.path.join(work_dir, 'design.log')
                                if os.path.exists(log_file):
                                    with open(log_file, 'r') as f:
                                        log_content = f.read()
                                        # 检查明确的完成标记
                                        if any(indicator in log_content for indicator in [
                                            'Design Run Finished', 
                                            '设计任务完成', 
                                            'Successfully created results package',
                                            'All generations completed',
                                            f'Finished all {total_gen} generations'
                                        ]):
                                            task_likely_completed = True
                                        else:
                                            # 检查CSV文件的新鲜度和数据完整性
                                            csv_files = [f for f in os.listdir(work_dir) 
                                                       if f.startswith('design_summary_') and f.endswith('.csv')]
                                            for csv_file in csv_files:
                                                csv_path = os.path.join(work_dir, csv_file)
                                                if os.path.exists(csv_path):
                                                    file_age = time.time() - os.path.getmtime(csv_path)
                                                    if file_age < 15:  # 15秒内修改过
                                                        # 验证CSV数据的完整性
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
                                # 检查失败时，不认为完成
                                pass
                        
                        # 额外检查：特定的run_design.py 进程是否还在运行
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
                                                    # 进一步验证这个PID确实是我们的run_design.py进程
                                                    proc = psutil.Process(saved_pid)
                                                    cmdline = proc.cmdline()
                                                    if cmdline and 'run_design.py' in ' '.join(cmdline):
                                                        design_process_running = True
                                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                                design_process_running = False
                                
                                # 如果run_design.py进程已经结束，且有结果文件，可能任务已完成
                                if not design_process_running and csv_sequences:
                                    # 检查是否有最近的结果文件
                                    csv_files = [f for f in os.listdir(work_dir) 
                                               if f.startswith('design_summary_') and f.endswith('.csv')]
                                    for csv_file in csv_files:
                                        csv_path = os.path.join(work_dir, csv_file)
                                        if os.path.exists(csv_path):
                                            file_age = time.time() - os.path.getmtime(csv_path)
                                            if file_age < 30:  # 30秒内修改过
                                                task_likely_completed = True
                                                break
                            except Exception:
                                # 如果检查失败，继续使用原有的检测逻辑
                                pass
                        
                        if task_likely_completed:
                            st.success("🎉 设计任务已完成！正在加载结果...")
                            try:
                                work_dir = st.session_state.get('designer_work_dir', '/tmp')
                                results = load_designer_results(st.session_state.designer_task_id, work_dir)
                                st.session_state.designer_results = results
                                st.toast("✅ 设计任务已完成！", icon="🎊")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ 加载结果时发生错误: {e}")
                                st.session_state.designer_error = {"error_message": str(e), "type": "Result Loading Error"}
                        else:
                            st.info(f"🕒 设计任务状态: {current_state}，正在检查完成状态...")
                            # 给用户更多信息
                            if current_gen > 0 and total_gen > 0:
                                st.caption(f"当前进度: 第 {current_gen}/{total_gen} 代")
                            if csv_sequences:
                                st.caption(f"已找到 {len(csv_sequences)} 个候选序列")
                            time.sleep(3)
                            st.rerun()
                        
            except Exception as e:
                st.error(f"❌ 获取任务状态时发生错误: {e}")
                st.session_state.designer_error = {"error_message": str(e), "type": "Status Check Error"}

        # 显示设计结果
    if st.session_state.designer_results:
        st.divider()
        st.header("🏆 **步骤 2: 设计结果展示**", anchor=False)
        
        results = st.session_state.designer_results
        best_sequences = results['best_sequences']
        evolution_history = results['evolution_history']
        
        # 结果统计摘要
        st.subheader("📊 设计统计摘要", anchor=False)
        
        # 应用阈值过滤
        score_threshold = 0.6
        high_quality_sequences = [seq for seq in best_sequences if seq.get('score', 0) >= score_threshold]
        top_sequences = high_quality_sequences[:10]  # Top 10
        
        col_stats = st.columns(4)
        col_stats[0].metric("总设计数", len(best_sequences))
        col_stats[1].metric("高质量设计", len(high_quality_sequences), help=f"评分 ≥ {score_threshold}")
        col_stats[2].metric("Top 10 选中", len(top_sequences))
        if best_sequences:
            col_stats[3].metric("最高评分", f"{max(seq.get('score', 0) for seq in best_sequences):.3f}")
        
        # 设置阈值控制
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
                    max_value=20,
                    value=10,
                    step=1,
                    help="最多显示多少个符合条件的设计"
                )
            
            # 重新过滤
            if custom_threshold != score_threshold:
                high_quality_sequences = [seq for seq in best_sequences if seq.get('score', 0) >= custom_threshold]
                top_sequences = high_quality_sequences[:max_display]
                
                # 更新统计
                col_stats[1].metric("高质量设计", len(high_quality_sequences), help=f"评分 ≥ {custom_threshold}")
                col_stats[2].metric(f"Top {max_display} 选中", len(top_sequences))
        
        # 最佳设计序列
        st.subheader("🥇 最佳设计序列", anchor=False)
        
        if not top_sequences:
            st.warning(f"😔 没有找到评分高于 {custom_threshold} 的设计序列。请尝试降低阈值或检查设计参数。")
        else:
            st.success(f"🎉 找到 {len(top_sequences)} 个高质量设计序列！")
            
            for i, seq_data in enumerate(top_sequences):
                rank = i + 1
                score = seq_data.get('score', 0)
                
                # 评分颜色编码
                if score >= 0.8:
                    score_color = "🟢"  # 绿色 - 优秀
                elif score >= 0.7:
                    score_color = "🟡"  # 黄色 - 良好
                elif score >= 0.6:
                    score_color = "🟠"  # 橙色 - 可接受
                else:
                    score_color = "🔴"  # 红色 - 较低
                
                with st.expander(
                    f"**第 {rank} 名** {score_color} 评分: {score:.3f}", 
                    expanded=(i < 3)  # 默认展开前3个
                ):
                    st.code(seq_data['sequence'], language="text")
                    
                    col_metrics = st.columns(4)
                    col_metrics[0].metric("综合评分", f"{score:.3f}")
                    col_metrics[1].metric("ipTM", f"{seq_data.get('iptm', 0):.3f}")
                    col_metrics[2].metric("pLDDT", f"{seq_data.get('plddt', 0):.3f}")
                    col_metrics[3].metric("发现代数", seq_data.get('generation', 'N/A'))
                    
                    # 下载结构文件
                    results_path = seq_data.get('results_path', '')
                    if results_path and os.path.exists(results_path):
                        # 查找CIF文件
                        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
                        if cif_files:
                            # 优先选择rank_1的文件，否则选择第一个
                            cif_file = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
                            cif_path = os.path.join(results_path, cif_file)
                            
                            try:
                                with open(cif_path, 'r') as f:
                                    cif_data = f.read()
                                
                                col_download = st.columns(2)
                                with col_download[0]:
                                    st.download_button(
                                        label="📄 下载 CIF",
                                        data=cif_data,
                                        file_name=f"rank_{rank}_designed_structure.cif",
                                        mime="chemical/x-cif",
                                        use_container_width=True,
                                        key=f"download_cif_{i}",
                                        help="下载该设计序列的3D结构文件 (CIF格式)"
                                    )
                                
                                with col_download[1]:
                                    # 查看相互作用按钮
                                    if st.button(
                                        "🔬 查看相互作用",
                                        use_container_width=True,
                                        key=f"view_interaction_{i}",
                                        help="在3D视图中查看该设计序列与目标的相互作用"
                                    ):
                                        # 使用session state来控制3D显示状态
                                        if f"show_3d_{i}" not in st.session_state:
                                            st.session_state[f"show_3d_{i}"] = False
                                        st.session_state[f"show_3d_{i}"] = not st.session_state.get(f"show_3d_{i}", False)
                                        st.rerun()
                                
                                # 3D结构显示区域 - 跨越整个宽度
                                if st.session_state.get(f"show_3d_{i}", False):
                                    st.markdown("---")
                                    st.markdown("**🔬 3D结构与相互作用**")
                                    
                                    try:
                                        # 读取结构并提取B因子信息
                                        structure = read_cif_from_string(cif_data)
                                        protein_bfactors = extract_protein_residue_bfactors(structure)
                                        
                                        # 使用AlphaFold颜色方案(pLDDT)显示结构
                                        view_html = visualize_structure_py3dmol(
                                            cif_content=cif_data,
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
                                        
                                        # 添加关闭按钮
                                        if st.button("❌ 关闭3D视图", key=f"close_3d_{i}", help="隐藏3D结构显示"):
                                            st.session_state[f"show_3d_{i}"] = False
                                            st.rerun()
                                        
                                    except Exception as e:
                                        st.error(f"❌ 3D结构显示失败: {str(e)}")
                                        st.exception(e)
                                        
                            except Exception as e:
                                st.caption(f"⚠️ 结构文件读取失败: {str(e)}")
                        else:
                            st.caption("⚠️ 未找到结构文件")
                    else:
                        st.caption("⚠️ 结构文件路径不可用")
        
        # 演化历史图表
        st.subheader("📈 演化历史", anchor=False)
        
        # 创建演化曲线数据
        chart_data = pd.DataFrame({
            '代数': evolution_history.get('generations', []),
            '最佳评分': evolution_history.get('best_scores', []),
            '平均评分': evolution_history.get('avg_scores', [])
        })
        
        if not chart_data.empty:
            # 使用Altair创建更精细的图表，动态调整Y轴范围
            try:
                import altair as alt
                
                # 计算合适的Y轴范围
                all_scores = []
                if '最佳评分' in chart_data.columns:
                    all_scores.extend(chart_data['最佳评分'].dropna().tolist())
                if '平均评分' in chart_data.columns:
                    all_scores.extend(chart_data['平均评分'].dropna().tolist())
                
                if all_scores:
                    min_score = min(all_scores)
                    max_score = max(all_scores)
                    score_range = max_score - min_score
                    
                    # 动态调整Y轴范围，给予一些边距
                    if score_range > 0:
                        y_min = max(0, min_score - score_range * 0.1)  # 下边距10%，但不低于0
                        y_max = min(1, max_score + score_range * 0.1)  # 上边距10%，但不高于1
                    else:
                        # 如果所有分数都相同，给一个小范围
                        y_min = max(0, min_score - 0.05)
                        y_max = min(1, max_score + 0.05)
                    
                    # 重新构建数据用于Altair
                    chart_data_melted = chart_data.melt(id_vars=['代数'], 
                                                       value_vars=['最佳评分', '平均评分'],
                                                       var_name='指标', value_name='评分')
                    
                    # 创建Altair图表
                    chart = alt.Chart(chart_data_melted).mark_line(point=True).encode(
                        x=alt.X('代数:O', title='演化代数'),
                        y=alt.Y('评分:Q', title='评分', scale=alt.Scale(domain=[y_min, y_max])),
                        color=alt.Color('指标:N', 
                                      scale=alt.Scale(range=['#1f77b4', '#ff7f0e']),  # 蓝色和橙色
                                      legend=alt.Legend(title="评分类型")),
                        tooltip=['代数:O', '指标:N', '评分:Q']
                    ).properties(
                        width=600,
                        height=300,
                        title="分子设计演化历史"
                    )
                    
                    st.altair_chart(chart, use_container_width=True)
                else:
                    # 如果没有有效数据，使用默认图表
                    st.line_chart(chart_data.set_index('代数'))
                    
            except ImportError:
                # 如果没有安装Altair，使用默认的line_chart但仍然有一些优化
                st.line_chart(chart_data.set_index('代数'))
        else:
            st.info("暂无演化历史数据可显示。")
        
        # 下载设计结果
        st.markdown("<b>📥 下载设计结果</b>", unsafe_allow_html=True)
        
        col_download = st.columns(2)
        
        # 1. CSV 下载
        with col_download[0]:
            if top_sequences:
                sequences_csv = pd.DataFrame(top_sequences)
                sequences_csv_str = sequences_csv.to_csv(index=False)
                
                st.download_button(
                    label="📊 Top序列 (CSV)",
                    data=sequences_csv_str,
                    file_name=f"top_designed_sequences_{st.session_state.designer_task_id}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    help=f"下载前 {len(top_sequences)} 个高质量设计序列"
                )
            else:
                st.button("📊 CSV下载", disabled=True, help="无符合条件的序列")
        
        # 2. JSON 下载
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
    
    # 显示错误信息
    if st.session_state.designer_error:
        st.error("ℹ️ 设计任务执行失败，详细信息如下：")
        st.json(st.session_state.designer_error)
        
        col_reset = st.columns(2)
        with col_reset[0]:
            if st.button("🔄 重置设计器", type="secondary", use_container_width=True):
                for key in ['designer_task_id', 'designer_results', 'designer_error', 'designer_config', 'designer_components', 'designer_constraints']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        
        with col_reset[1]:
            if st.button("🔧 保留配置重新设计", type="primary", use_container_width=True):
                # 只清除任务状态，保留组分配置和设计参数
                for key in ['designer_task_id', 'designer_results', 'designer_error']:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
