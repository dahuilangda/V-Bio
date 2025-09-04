
import streamlit as st
import string
import re
import io
import os
import time
import hashlib
import yaml
import py3Dmol
from datetime import datetime
from Bio.PDB import MMCIFParser, PDBIO
from Bio.PDB.Structure import Structure

from frontend.constants import (
    TYPE_TO_DISPLAY,
    AMINO_ACID_MAPPING, 
    AMINO_ACID_ATOMS, 
    DNA_BASE_ATOMS, 
    RNA_BASE_ATOMS, 
    COMMON_ATOMS,
    MSA_CACHE_CONFIG
)

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

def get_available_chain_ids_for_designer(components, binder_chain_id=None):
    """
    为设计器页面获取可用的链ID列表，包括BINDER_CHAIN占位符
    返回: (all_chain_ids, chain_descriptions)
    """
    # 获取现有组分的链ID
    existing_chain_ids, existing_descriptions = get_available_chain_ids(components)
    
    # 添加BINDER_CHAIN占位符
    all_chain_ids = existing_chain_ids + ['BINDER_CHAIN']
    all_descriptions = existing_descriptions.copy()
    
    # 为BINDER_CHAIN添加描述
    if binder_chain_id:
        all_descriptions['BINDER_CHAIN'] = f"🎯 设计中的结合肽 (将分配链 {binder_chain_id})"
    else:
        all_descriptions['BINDER_CHAIN'] = f"🎯 设计中的结合肽 (链ID待分配)"
    
    return all_chain_ids, all_descriptions

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

def read_cif_from_string(cif_content: str) -> Structure:
    """Parses a CIF string into a BioPython Structure object."""
    parser = MMCIFParser(QUIET=True)
    
    # Ensure the CIF content has the proper header
    cif_content = cif_content.strip()
    if not cif_content:
        raise ValueError("CIF content is empty")
        
    if not cif_content.startswith('data_'):
        # Add a proper mmCIF header if missing
        cif_content = f"data_structure\n#\n{cif_content}"
    
    # Verify essential mmCIF sections exist
    if '_atom_site' not in cif_content:
        raise ValueError("CIF content missing essential '_atom_site' section")
    
    try:
        file_like = io.StringIO(cif_content)
        structure = parser.get_structure('protein', file_like)
        return structure
    except Exception as e:
        raise ValueError(f"Failed to parse CIF content: {e}") from e

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

# ========== MSA Cache Functions ==========

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

def get_ligand_resnames_from_pdb(file_content: str) -> list[str]:
    """Extracts chain IDs from a PDB or CIF file content."""
    resnames = set()
    for line in file_content.split('\n'):
        if line.startswith('HETATM'):
            resname = line[17:20].strip()
            if resname:
                resnames.add(resname)
    return sorted(list(resnames))


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

def get_smart_msa_default(components: list) -> bool:

    """
    智能决定新蛋白质组分的MSA默认值
    """
    if not components:
        return False  # 第一个组分默认不启用MSA
    
    protein_components = [comp for comp in components if comp.get('type') == 'protein']
    
    if not protein_components:
        return False
    
    first_protein = protein_components[0]
    first_sequence = first_protein.get('sequence', '').strip()
    
    if not first_sequence:
        return False
    
    proteins_with_sequence = [comp for comp in protein_components if comp.get('sequence', '').strip()]
    
    if has_cached_msa(first_sequence):
        if len(proteins_with_sequence) <= 1:
            return True
        else:
            return first_protein.get('use_msa', True)
    
    return False

def generate_yaml_from_state():
    """
    Generates the YAML configuration string based on the current session state.
    """
    if not st.session_state.get('components'):
        return None
        
    sequences_list = []
    chain_letters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    next_letter_idx = 0
    
    protein_components = [comp for comp in st.session_state.components if comp['type'] == 'protein']
    
    msa_strategy = "mixed"
    if protein_components:
        cached_count = sum(1 for comp in protein_components if comp.get('use_msa', True) and has_cached_msa(comp['sequence']))
        enabled_count = sum(1 for comp in protein_components if comp.get('use_msa', True))
        total_proteins = len(protein_components)
        
        if enabled_count == 0:
            msa_strategy = "none"
        elif cached_count == enabled_count and enabled_count == total_proteins:
            msa_strategy = "cached"
        elif cached_count == 0 and enabled_count == total_proteins:
            msa_strategy = "auto"
        else:
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
            
            if comp['type'] == 'protein':
                comp_use_msa = comp.get('use_msa', True)
                
                if msa_strategy == "none" or not comp_use_msa:
                    component_dict['msa'] = 'empty'
                elif msa_strategy == "cached":
                    sequence = comp['sequence']
                    component_dict['msa'] = get_msa_cache_path(sequence)
                elif msa_strategy == "auto":
                    pass
                elif msa_strategy == "mixed":
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
                            if has_cache:
                                component_dict['msa'] = get_msa_cache_path(sequence)
                            else:
                                pass
                        else:
                            pass
                    
        elif comp['type'] == 'ligand':
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
    
    if st.session_state.get('constraints'):
        constraints_list = []
        
        for constraint in st.session_state.constraints:
            constraint_type = constraint.get('type', 'contact')
            
            if constraint_type == 'contact':
                if constraint.get('token1_atom'):
                    token1 = [constraint['token1_chain'], constraint['token1_atom']]
                else:
                    chain1_type = get_chain_type(st.session_state.components, constraint['token1_chain'])
                    if chain1_type == 'ligand':
                        # 根据官方文档，配体分子应使用原子名称而不是残基索引
                        # 对于简单的单原子配体如[Zn]，使用原子符号作为原子名称
                        ligand_smiles = None
                        if hasattr(st.session_state, 'components') and constraint['token1_chain'] in st.session_state.components:
                            component = st.session_state.components[constraint['token1_chain']]
                            ligand_smiles = component.get('smiles', '')
                        
                        # 对于简单的单原子配体，使用原子符号
                        if ligand_smiles and ligand_smiles.strip('[]').isalpha() and len(ligand_smiles.strip('[]')) <= 2:
                            atom_name = ligand_smiles.strip('[]')  # 如 [Zn] -> Zn
                            token1 = [constraint['token1_chain'], atom_name]
                        else:
                            # 对于复杂配体，使用残基索引1（根据文档）
                            token1 = [constraint['token1_chain'], 1]
                    else:
                        token1 = [constraint['token1_chain'], constraint['token1_residue']]
                    
                if constraint.get('token2_atom'):
                    token2 = [constraint['token2_chain'], constraint['token2_atom']]
                else:
                    chain2_type = get_chain_type(st.session_state.components, constraint['token2_chain'])
                    if chain2_type == 'ligand':
                        # 根据官方文档，配体分子应使用原子名称而不是残基索引
                        # 对于简单的单原子配体如[Zn]，使用原子符号作为原子名称
                        ligand_smiles = None
                        if hasattr(st.session_state, 'components') and constraint['token2_chain'] in st.session_state.components:
                            component = st.session_state.components[constraint['token2_chain']]
                            ligand_smiles = component.get('smiles', '')
                        
                        # 对于简单的单原子配体，使用原子符号
                        if ligand_smiles and ligand_smiles.strip('[]').isalpha() and len(ligand_smiles.strip('[]')) <= 2:
                            atom_name = ligand_smiles.strip('[]')  # 如 [Zn] -> Zn
                            token2 = [constraint['token2_chain'], atom_name]
                        else:
                            # 对于复杂配体，使用残基索引1（根据文档）
                            token2 = [constraint['token2_chain'], 1]
                    else:
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
                atom1 = [constraint['atom1_chain'], constraint['atom1_residue'], constraint['atom1_atom']]
                atom2 = [constraint['atom2_chain'], constraint['atom2_residue'], constraint['atom2_atom']]
                
                constraint_dict = {
                    'bond': {
                        'atom1': atom1,
                        'atom2': atom2
                    }
                }
                
            elif constraint_type == 'pocket':
                # 处理pocket约束
                binder = constraint.get('binder', 'BINDER_CHAIN')
                contacts = constraint.get('contacts', [])
                
                # 处理contacts中的配体链
                processed_contacts = []
                for contact in contacts:
                    if len(contact) >= 2:
                        chain_id, residue_or_atom = contact[0], contact[1]
                        chain_type = get_chain_type(st.session_state.components, chain_id)
                        
                        if chain_type == 'ligand':
                            # 对于配体，智能处理原子名称
                            ligand_smiles = None
                            if hasattr(st.session_state, 'components') and chain_id in st.session_state.components:
                                component = st.session_state.components[chain_id]
                                ligand_smiles = component.get('smiles', '')
                            
                            # 对于简单单原子配体，使用原子符号
                            if ligand_smiles and ligand_smiles.strip('[]').isalpha() and len(ligand_smiles.strip('[]')) <= 2:
                                atom_name = ligand_smiles.strip('[]')
                                processed_contacts.append([chain_id, atom_name])
                            else:
                                # 复杂配体使用残基索引1
                                processed_contacts.append([chain_id, 1])
                        else:
                            # 蛋白质/DNA/RNA使用残基索引
                            processed_contacts.append([chain_id, residue_or_atom])
                
                constraint_dict = {
                    'pocket': {
                        'binder': binder,
                        'contacts': processed_contacts,
                        'max_distance': constraint.get('max_distance', 6.0),
                        'force': constraint.get('force', False)
                    }
                }
            
            else:
                continue
                
            constraints_list.append(constraint_dict)
        
        if constraints_list:
            final_yaml_dict['constraints'] = constraints_list
        
    return yaml.dump(final_yaml_dict, sort_keys=False, indent=2, default_flow_style=False)

def validate_inputs(components):
    """验证用户输入是否完整且有效。"""
    if not components:
        return False, "请至少添加一个组分。"
    
    valid_components = 0
    for i, comp in enumerate(components):
        sequence = comp.get('sequence', '').strip()
        if not sequence:
            display_name = TYPE_TO_DISPLAY.get(comp.get('type', 'Unknown'), 'Unknown')
            return False, f"错误: 组分 {i+1} ({display_name}) 的序列不能为空。"
        
        if comp.get('type') == 'ligand' and comp.get('input_method') in ['smiles', 'ketcher']:
            if sequence and not all(c in string.printable for c in sequence):
                return False, f"错误: 组分 {i+1} (小分子) 的 SMILES 字符串包含非法字符。"
        
        valid_components += 1
    
    if valid_components == 0:
        return False, "请至少输入一个有效的组分序列。"
            
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
    
    target_bio_components = [comp for comp in designer_components if comp['type'] in ['protein', 'dna', 'rna'] and comp.get('sequence', '').strip()]
    target_ligand_components = [comp for comp in designer_components if comp['type'] == 'ligand' and comp.get('sequence', '').strip()]
    
    if not target_bio_components and not target_ligand_components:
        return False, "请至少添加一个包含序列的蛋白质、DNA、RNA或小分子组分作为设计目标。"
    
    for i, comp in enumerate(designer_components):
        if comp.get('sequence', '').strip():
            comp_type = comp.get('type')
            sequence = comp.get('sequence', '').strip()
            
            if comp_type == 'protein':
                valid_aa = set('ACDEFGHIKLMNPQRSTVWY')
                if not all(c.upper() in valid_aa for c in sequence):
                    return False, f"错误: 组分 {i+1} (蛋白质) 包含非标准氨基酸字符。请使用标准20种氨基酸。"
            
            elif comp_type == 'dna':
                valid_dna = set('ATGC')
                if not all(c.upper() in valid_dna for c in sequence):
                    return False, f"错误: 组分 {i+1} (DNA) 包含非法核苷酸。请只使用A、T、G、C。"
            
            elif comp_type == 'rna':
                valid_rna = set('AUGC')
                if not all(c.upper() in valid_rna for c in sequence):
                    return False, f"错误: 组分 {i+1} (RNA) 包含非法核苷酸。请只使用A、U、G、C。"
            
            elif comp_type == 'ligand' and comp.get('input_method') in ['smiles', 'ketcher']:
                if not all(c in string.printable for c in sequence):
                    return False, f"错误: 组分 {i+1} (小分子) 的 SMILES 字符串包含非法字符。"
    
    return True, ""
