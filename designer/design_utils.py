# /Boltz-WebUI/designer/design_utils.py

"""
design_utils.py

该模块提供了蛋白质和糖肽设计所需的核心数据结构和辅助工具函数。
功能包括：
- 定义氨基酸、单糖和糖基化位点的常量。
- 提供基于BLOSUM62矩阵的氨基酸替换分数。
- 实现序列生成、突变和结构预测结果解析的功能。

所有函数均为无状态的，不依赖于外部类实例。
"""

import random
import os
import json
import logging
import numpy as np
import math
from pathlib import Path
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Optional

# --- 初始化日志记录器 ---
# 该模块的日志记录器将继承主入口点的配置
logger = logging.getLogger(__name__)

# --- 核心常量 ---

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"
AMINO_ACIDS_NO_CYS = "ARNDQEGHILKMFPSTWYV" # 用于禁用半胱氨酸的设计，避免生成额外的Cys

# --- 双环肽连接子 ---
BICYCLIC_LINKERS = {
    'SEZ': {
        'ccd': 'SEZ',
        'name': '1,3,5-三甲基苯',
        'eng_name': '1,3,5-trimethylbenzene',
        'attachment_atoms': ['CD', 'C1', 'C2']  # 连接到Cys的SG原子的配体原子
    },
    '29N': {
        'ccd': '29N',
        'name': '1-[3,5-di(propanoyl)-1,3,5-triazinan-1-yl]propan-1-one',
        'eng_name': '1-[3,5-di(propanoyl)-1,3,5-triazinan-1-yl]propan-1-one',
        'attachment_atoms': ['C16', 'C19', 'C25']  # 连接到Cys的SG原子的配体原子
    },
    'BS3': {
        'ccd': 'BS3',
        'name': '铋(III)三硫醇配合物',
        'eng_name': 'Bismuth(III) tris(thiolate) complex',
        'attachment_atoms': ['BI', 'BI', 'BI']  # 连接到Cys的SG原子的配体原子
    },
}

# --- 糖化学常量 ---
MONOSACCHARIDES = {
    # 最常见的N-连接糖基化起始糖
    'NAG': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': 'N-乙酰葡糖胺', 
        'eng_name': 'N-acetylglucosamine',
        'smiles': 'CC(=O)N[C@H]1[C@@H](CO)O[C@H](O[C@H]2[C@H](O)[C@@H](O)[C@H](O)[C@@H](CO)O2)[C@H](O)[C@H]1O',
        'molecular_weight': 221.21,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    # 常见的高甘露糖型糖链组分，C1位羟基氧原子在脱水缩合后成为连接Ser/Thr的桥梁氧原子
    'MAN': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': '甘露糖', 
        'eng_name': 'Mannose',
        'smiles': 'O[C@H]1[C@H](O)[C@@H](O)[C@@H](O)[C@H](O)[C@@H]1O',
        'molecular_weight': 180.16,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    # 复合型糖链的末端糖
    'GAL': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': '半乳糖', 
        'eng_name': 'Galactose',
        'smiles': 'O[C@H]1[C@H](O)[C@H](O)[C@@H](O)[C@H](O)[C@@H]1O',
        'molecular_weight': 180.16,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    # 分支糖链，增加分子多样性
    'FUC': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': '岩藻糖', 
        'eng_name': 'Fucose',
        'smiles': 'C[C@H]1O[C@H](O)[C@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 164.16,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    # 带负电荷的末端糖（神经氨酸/唾液酸）
    'NAN': {
        'atom': 'C2', 
        'type': ['O-linked'], 
        'name': '神经氨酸', 
        'eng_name': 'Neuraminic acid',
        'smiles': 'CC(=O)N[C@H]1[C@@H](O)[C@H](O)[C@@H](O[C@H]2[C@H](O)[C@@H](O)[C@H](O)[C@@H](CO)O2)[C@H](C(O)=O)[C@@H]1O',
        'molecular_weight': 309.27,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    # 额外的常用糖基
    'GLC': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': '葡萄糖', 
        'eng_name': 'Glucose',
        'smiles': 'O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H](O)[C@@H]1O',
        'molecular_weight': 180.16,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    'XYL': {
        'atom': 'C1', 
        'type': ['N-linked'], 
        'name': '木糖', 
        'eng_name': 'Xylose',
        'smiles': 'O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 150.13,
        'linkage_atoms': {'N-linked': 'ND2'}
    },
    
    'GLCNAC': {
        'atom': 'C1', 
        'type': ['N-linked', 'O-linked'], 
        'name': 'N-乙酰葡糖胺', 
        'eng_name': 'N-acetylglucosamine',
        'smiles': 'CC(=O)N[C@H]1[C@@H](CO)O[C@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 221.21,
        'linkage_atoms': {'N-linked': 'ND2', 'O-linked': 'OG'}
    },
    
    'GALNAC': {
        'atom': 'C1', 
        'type': ['O-linked'], 
        'name': 'N-乙酰半乳糖胺', 
        'eng_name': 'N-acetylgalactosamine',
        'smiles': 'CC(=O)N[C@H]1[C@@H](CO)O[C@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 221.21,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    'GLCA': {
        'atom': 'C1', 
        'type': ['O-linked'], 
        'name': '葡萄糖醛酸', 
        'eng_name': 'Glucuronic acid',
        'smiles': 'O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H](C(O)=O)[C@@H]1O',
        'molecular_weight': 194.14,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    # 历史兼容性保留（SIA是旧的神经氨酸代号）
    'SIA': {
        'atom': 'C2', 
        'type': ['O-linked'], 
        'name': '唾液酸', 
        'eng_name': 'Sialic acid',
        'smiles': 'CC(=O)N[C@H]1[C@@H](O)[C@H](O)[C@@H](O)[C@H](C(O)=O)[C@@H]1O',
        'molecular_weight': 309.27,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    # 扩展的糖基库 - 用于复杂糖基化模式
    'RHAB': {
        'atom': 'C1',
        'type': ['O-linked'],
        'name': '鼠李糖',
        'eng_name': 'Rhamnose',
        'smiles': 'C[C@H]1O[C@H](O)[C@@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 164.16,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    'ARA': {
        'atom': 'C1',
        'type': ['O-linked'],
        'name': '阿拉伯糖',
        'eng_name': 'Arabinose',
        'smiles': 'O[C@H]1[C@@H](O)[C@H](O)[C@H](O)[C@H]1O',
        'molecular_weight': 150.13,
        'linkage_atoms': {'O-linked': 'OG'}
    },
}

GLYCOSYLATION_SITES = {
    # N-连接糖基化：糖基C1位羟基与天冬酰胺侧链胺基脱氨基化形成糖苷键，通常在Asn-X-Ser/Thr基序中
    'N-linked': {
        'N': 'ND2'  # 天冬酰胺的侧链胺基氮原子
    },
    # O-连接糖基化：糖基C1羟基氧原子脱水缩合后成为连接氨基酸侧链的桥梁氧原子
    'O-linked': {
        'S': 'OG',    # 丝氨酸的羟基氧原子 (MAN-C1-O-Ser桥连)
        'T': 'OG1',   # 苏氨酸的羟基氧原子 (MAN-C1-O-Thr桥连)
        'Y': 'OH'     # 酪氨酸的酚羟基氧原子 (较少见但存在)
    },
    # C-连接糖基化：较少见，糖基直接与色氨酸吲哚环C原子连接
    'C-linked': {
        'W': 'CD1'    # 色氨酸吲哚环的C2位
    }
}

# BLOSUM62 替换矩阵
BLOSUM62 = {
    'A': {'A': 4, 'R': -1, 'N': -2, 'D': -2, 'C': 0, 'Q': -1, 'E': -1, 'G': 0, 'H': -2, 'I': -1, 'L': -1, 'K': -1, 'M': -1, 'F': -2, 'P': -1, 'S': 1, 'T': 0, 'W': -3, 'Y': -2, 'V': 0},
    'R': {'A': -1, 'R': 5, 'N': 0, 'D': -2, 'C': -3, 'Q': 1, 'E': 0, 'G': -2, 'H': 0, 'I': -3, 'L': -2, 'K': 2, 'M': -1, 'F': -3, 'P': -2, 'S': -1, 'T': -1, 'W': -3, 'Y': -2, 'V': -3},
    'N': {'A': -2, 'R': 0, 'N': 6, 'D': 1, 'C': -3, 'Q': 0, 'E': 0, 'G': 0, 'H': 1, 'I': -3, 'L': -3, 'K': 0, 'M': -2, 'F': -3, 'P': -2, 'S': 1, 'T': 0, 'W': -4, 'Y': -2, 'V': -3},
    'D': {'A': -2, 'R': -2, 'N': 1, 'D': 6, 'C': -3, 'Q': 0, 'E': 2, 'G': -1, 'H': -1, 'I': -3, 'L': -4, 'K': -1, 'M': -3, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -4, 'Y': -3, 'V': -3},
    'C': {'A': 0, 'R': -3, 'N': -3, 'D': -3, 'C': 9, 'Q': -3, 'E': -4, 'G': -3, 'H': -3, 'I': -1, 'L': -1, 'K': -3, 'M': -1, 'F': -2, 'P': -3, 'S': -1, 'T': -1, 'W': -2, 'Y': -2, 'V': -1},
    'Q': {'A': -1, 'R': 1, 'N': 0, 'D': 0, 'C': -3, 'Q': 5, 'E': 2, 'G': -2, 'H': 0, 'I': -3, 'L': -2, 'K': 1, 'M': 0, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -2, 'Y': -1, 'V': -2},
    'E': {'A': -1, 'R': 0, 'N': 0, 'D': 2, 'C': -4, 'Q': 2, 'E': 5, 'G': -2, 'H': 0, 'I': -3, 'L': -3, 'K': 1, 'M': -2, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -3, 'Y': -2, 'V': -2},
    'G': {'A': 0, 'R': -2, 'N': 0, 'D': -1, 'C': -3, 'Q': -2, 'E': -2, 'G': 6, 'H': -2, 'I': -4, 'L': -4, 'K': -2, 'M': -3, 'F': -3, 'P': -2, 'S': 0, 'T': -2, 'W': -2, 'Y': -3, 'V': -3},
    'H': {'A': -2, 'R': 0, 'N': 1, 'D': -1, 'C': -3, 'Q': 0, 'E': 0, 'G': -2, 'H': 8, 'I': -3, 'L': -3, 'K': -1, 'M': -2, 'F': -1, 'P': -2, 'S': -1, 'T': -2, 'W': -2, 'Y': 2, 'V': -3},
    'I': {'A': -1, 'R': -3, 'N': -3, 'D': -3, 'C': -1, 'Q': -3, 'E': -3, 'G': -4, 'H': -3, 'I': 4, 'L': 2, 'K': -3, 'M': 1, 'F': 0, 'P': -3, 'S': -2, 'T': -1, 'W': -3, 'Y': -1, 'V': 3},
    'L': {'A': -1, 'R': -2, 'N': -3, 'D': -4, 'C': -1, 'Q': -2, 'E': -3, 'G': -4, 'H': -3, 'I': 2, 'L': 4, 'K': -2, 'M': 2, 'F': 0, 'P': -3, 'S': -2, 'T': -1, 'W': -2, 'Y': -1, 'V': 1},
    'K': {'A': -1, 'R': 2, 'N': 0, 'D': -1, 'C': -3, 'Q': 1, 'E': 1, 'G': -2, 'H': -1, 'I': -3, 'L': -2, 'K': 5, 'M': -1, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -3, 'Y': -2, 'V': -2},
    'M': {'A': -1, 'R': -1, 'N': -2, 'D': -3, 'C': -1, 'Q': 0, 'E': -2, 'G': -3, 'H': -2, 'I': 1, 'L': 2, 'K': -1, 'M': 5, 'F': 0, 'P': -2, 'S': -1, 'T': -1, 'W': -1, 'Y': -1, 'V': 1},
    'F': {'A': -2, 'R': -3, 'N': -3, 'D': -3, 'C': -2, 'Q': -3, 'E': -3, 'G': -3, 'H': -1, 'I': 0, 'L': 0, 'K': -3, 'M': 0, 'F': 6, 'P': -4, 'S': -2, 'T': -2, 'W': 1, 'Y': 3, 'V': -1},
    'P': {'A': -1, 'R': -2, 'N': -2, 'D': -1, 'C': -3, 'Q': -1, 'E': -1, 'G': -2, 'H': -2, 'I': -3, 'L': -3, 'K': -1, 'M': -2, 'F': -4, 'P': 7, 'S': -1, 'T': -1, 'W': -4, 'Y': -3, 'V': -2},
    'S': {'A': 1, 'R': -1, 'N': 1, 'D': 0, 'C': -1, 'Q': 0, 'E': 0, 'G': 0, 'H': -1, 'I': -2, 'L': -2, 'K': 0, 'M': -1, 'F': -2, 'P': -1, 'S': 4, 'T': 1, 'W': -3, 'Y': -2, 'V': -2},
    'T': {'A': 0, 'R': -1, 'N': 0, 'D': -1, 'C': -1, 'Q': -1, 'E': -1, 'G': -2, 'H': -2, 'I': -1, 'L': -1, 'K': -1, 'M': -1, 'F': -2, 'P': -1, 'S': 1, 'T': 5, 'W': -2, 'Y': -2, 'V': 0},
    'W': {'A': -3, 'R': -3, 'N': -4, 'D': -4, 'C': -2, 'Q': -2, 'E': -3, 'G': -2, 'H': -2, 'I': -3, 'L': -2, 'K': -3, 'M': -1, 'F': 1, 'P': -4, 'S': -3, 'T': -2, 'W': 11, 'Y': 2, 'V': -3},
    'Y': {'A': -2, 'R': -2, 'N': -2, 'D': -3, 'C': -2, 'Q': -1, 'E': -2, 'G': -3, 'H': 2, 'I': -1, 'L': -1, 'K': -2, 'M': -1, 'F': 3, 'P': -3, 'S': -2, 'T': -2, 'W': 2, 'Y': 7, 'V': -1},
    'V': {'A': 0, 'R': -3, 'N': -3, 'D': -3, 'C': -1, 'Q': -2, 'E': -2, 'G': -3, 'H': -3, 'I': 3, 'L': 1, 'K': -2, 'M': 1, 'F': -1, 'P': -2, 'S': -2, 'T': 0, 'W': -3, 'Y': -1, 'V': 4}
}

def get_valid_residues_for_glycan(glycan_ccd: str) -> list:
    """根据给定的聚糖CCD代码，返回其兼容的氨基酸残基列表。"""
    if not glycan_ccd or glycan_ccd not in MONOSACCHARIDES:
        raise KeyError(f"Glycan CCD '{glycan_ccd}' is not defined in MONOSACCHARIDES.")
    allowed_types = MONOSACCHARIDES[glycan_ccd]['type']
    valid_residues = []
    if 'N-linked' in allowed_types:
        valid_residues.extend(GLYCOSYLATION_SITES['N-linked'].keys())
    if 'O-linked' in allowed_types:
        valid_residues.extend(GLYCOSYLATION_SITES['O-linked'].keys())
    if 'C-linked' in allowed_types:
        valid_residues.extend(GLYCOSYLATION_SITES['C-linked'].keys())
    if not valid_residues:
        raise ValueError(f"No valid glycosylation types found for glycan '{glycan_ccd}'.")
    return list(set(valid_residues))

def generate_random_bicyclic_sequence(length: int) -> str:
    """
    为双环肽设计生成一个随机序列，包含三个Cys，其中一个在末尾。
    确保序列中恰好包含3个半胱氨酸，不多不少。
    """
    if length < 3:
        raise ValueError("Sequence length must be at least 3 for bicyclic peptide design.")
    
    # 1. 生成不含Cys的随机序列
    seq = list("".join(random.choice(AMINO_ACIDS_NO_CYS) for _ in range(length)))
    
    # 2. 在末尾放置一个Cys（固定末端Cys）
    seq[-1] = 'C'
    
    # 3. 在其余位置随机选择两个不同位置放置Cys
    available_indices = list(range(length - 1))  # 不包括末尾位置
    if len(available_indices) < 2:
        raise ValueError("Sequence length too short for bicyclic peptide (need at least 3 positions)")
    
    cys_pos1, cys_pos2 = random.sample(available_indices, 2)
    seq[cys_pos1] = 'C'
    seq[cys_pos2] = 'C'
    
    final_sequence = "".join(seq)
    
    # 验证结果
    cys_count = final_sequence.count('C')
    if cys_count != 3:
        raise ValueError(f"Generated bicyclic sequence has {cys_count} Cys, expected 3")
    
    return final_sequence

# def generate_random_sequence(length: int, modification_site: int = None, glycan_modification: str = None) -> str:
#     """生成一个随机的氨基酸序列。"""
#     seq = list("".join(random.choice(AMINO_ACIDS) for _ in range(length)))
#     if modification_site is not None:
#         if 0 <= modification_site < length:
#             # 由于现在使用预生成的糖肽修饰（如MANS），我们不需要验证特定残基
#             # 而是使用任何适合糖基化的残基
#             valid_residues = (list(GLYCOSYLATION_SITES['N-linked'].keys()) + 
#                             list(GLYCOSYLATION_SITES['O-linked'].keys()) + 
#                             list(GLYCOSYLATION_SITES['C-linked'].keys()))
#             seq[modification_site] = random.choice(valid_residues)
#         else:
#             raise ValueError("modification_site index is out of bounds for the given sequence length.")
#     return "".join(seq)

def generate_random_sequence(length: int, design_params: dict) -> str:
    """根据设计参数生成一个随机的氨基酸序列。"""
    design_type = design_params.get('design_type', 'linear')
    include_cysteine = design_params.get('include_cysteine', True)
    sequence_mask = design_params.get('sequence_mask')
    
    # 选择氨基酸字符串
    if include_cysteine:
        amino_acid_set = AMINO_ACIDS
    else:
        amino_acid_set = AMINO_ACIDS_NO_CYS
    
    # 处理sequence_mask
    if sequence_mask:
        # 移除分隔符并转换为大写
        mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
        if len(mask_clean) != length:
            raise ValueError(f"Sequence mask length ({len(mask_clean)}) must match binder length ({length})")
        
        # 根据mask生成序列
        seq = []
        for i, char in enumerate(mask_clean):
            if char == 'X':
                seq.append(random.choice(amino_acid_set))
            else:
                seq.append(char)  # 固定氨基酸
    else:
        # 原有逻辑：根据设计类型生成序列
        seq = []
        
        # --- 双环肽的特殊生成逻辑 ---
        if design_type == 'bicyclic':
            if length < 3:
                raise ValueError("Bicyclic peptide length must be at least 3.")
            # 双环肽需要Cys，即使用户禁用了半胱氨酸
            seq = list("".join(random.choice(AMINO_ACIDS_NO_CYS) for _ in range(length)))
            
            # 固定最后一个位置为Cys
            seq[-1] = 'C'
            
            # 确定另外两个Cys的位置
            cys_positions = design_params.get('cys_positions')
            if cys_positions and len(cys_positions) == 2:
                pos1, pos2 = cys_positions[0], cys_positions[1]
            else:
                # 随机选择两个不与末端重复的位置
                available_indices = list(range(length - 1))
                pos1, pos2 = random.sample(available_indices, 2)
            
            seq[pos1] = 'C'
            seq[pos2] = 'C'
            
            return "".join(seq)

        # --- 糖肽的特殊生成逻辑 ---
        elif design_type == 'glycopeptide':
            seq = list("".join(random.choice(amino_acid_set) for _ in range(length)))
            modification_site = design_params.get('modification_site')
            if modification_site is not None:
                if 0 <= modification_site < length:
                    valid_residues = (list(GLYCOSYLATION_SITES['N-linked'].keys()) +
                                      list(GLYCOSYLATION_SITES['O-linked'].keys()) +
                                      list(GLYCOSYLATION_SITES['C-linked'].keys()))
                    seq[modification_site] = random.choice(valid_residues)
                else:
                    raise ValueError("modification_site index is out of bounds for the given sequence length.")
            return "".join(seq)

        # --- 默认线性多肽逻辑 ---
        else:
            seq = [random.choice(amino_acid_set) for _ in range(length)]
    
    return "".join(seq)


# def mutate_sequence(
#     sequence: str,
#     mutation_rate: float = 0.1,
#     plddt_scores: list = None,
#     temperature: float = 1.0,
#     modification_site: int = None,
#     glycan_modification: str = None,
#     position_selection_temp: float = 1.0
# ) -> str:
#     """
#     对序列进行点突变，突变过程受pLDDT和BLOSUM62矩阵指导。

#     - **突变位置选择**: 优先选择pLDDT分数较低（即模型预测的低置信度）的区域。
#       `position_selection_temp` 参数用于调节该选择压力：
#         - temp=1.0: 标准权重。
#         - temp>1.0: 降低pLDDT的影响，位置选择更趋于随机，增强探索性。
#         - temp<1.0: 增强pLDDT的影响，位置选择更集中于低分区域，增强利用性。
#     - **氨基酸替换选择**: 通过带温度的Softmax函数和BLOSUM62矩阵进行加权。
#     - **糖基化位点保护**: 指定的糖基化位点将被保护，确保其残基始终与指定的聚糖类型兼容。

#     Args:
#         sequence (str): 原始氨基酸序列。
#         mutation_rate (float): 序列中要突变的残基比例。
#         plddt_scores (list, optional): 与序列对应的pLDDT分数列表。
#         temperature (float): Softmax函数的温度因子，用于氨基酸替换选择。
#         modification_site (int, optional): 要保护的糖基化位点（0-based索引）。
#         glycan_modification (str, optional): 糖肽修饰的CCD代码，用于验证修饰位点。
#         position_selection_temp (float): 用于调节pLDDT指导位置选择的温度因子。

#     Returns:
#         str: 突变后的新序列。
#     """
#     new_sequence = list(sequence)
#     num_mutations = max(1, int(len(sequence) * mutation_rate))

#     # --- 步骤 1: 选择突变位置 (pLDDT指导) ---
#     available_indices = list(range(len(sequence)))
#     if modification_site is not None and modification_site in available_indices:
#         available_indices.remove(modification_site)

#     if not available_indices:
#         logger.warning("No available positions to mutate after excluding the glycosylation site. Returning original sequence.")
#         return sequence

#     positions_to_mutate = []
#     if plddt_scores and len(plddt_scores) == len(sequence):
#         # 根据 (100 - pLDDT) 对位置进行加权，优先选择低置信度区域
#         # 应用 position_selection_temp 来调整选择压力
#         safe_temp = max(position_selection_temp, 1e-6) # 避免除以零
#         weights = [(100.0 - plddt_scores[i]) / safe_temp for i in available_indices]
        
#         total_weight = sum(weights)
#         if total_weight > 0:
#             probabilities = [w / total_weight for w in weights]
#             k = min(num_mutations, len(available_indices))
#             positions_to_mutate = np.random.choice(available_indices, size=k, replace=False, p=probabilities)
#         else:
#             # 如果所有pLDDT都为100或权重因其他原因失效，则随机选择
#             logger.debug("All pLDDT scores are high; falling back to random position selection.")
#             positions_to_mutate = random.sample(available_indices, k=min(num_mutations, len(available_indices)))
#     else:
#         if plddt_scores:
#             logger.warning("pLDDT scores length mismatch or not provided. Falling back to random position selection.")
#         # 如果没有提供pLDDT，则随机选择突变位置
#         positions_to_mutate = random.sample(available_indices, k=min(num_mutations, len(available_indices)))

#     # --- 步骤 2: 选择替换的氨基酸 (BLOSUM62) ---
#     for pos in positions_to_mutate:
#         original_aa = new_sequence[pos]
#         substitution_scores = BLOSUM62.get(original_aa, {})
#         possible_aas = [aa for aa in AMINO_ACIDS if aa != original_aa]
#         if not possible_aas: continue

#         scores = [substitution_scores.get(aa, 0) for aa in possible_aas]
#         scores_array = np.array(scores) / temperature
#         probabilities = np.exp(scores_array - np.max(scores_array)) # Softmax
#         probabilities /= np.sum(probabilities)

#         new_aa = np.random.choice(possible_aas, p=probabilities)
#         new_sequence[pos] = new_aa
    
#     return "".join(new_sequence)

def mutate_sequence(
    sequence: str,
    mutation_rate: float = 0.1,
    plddt_scores: list = None,
    design_params: dict = None,
    position_selection_temp: float = 1.0,
    temperature: float = 1.0 # 兼容旧版调用
) -> str:
    """
    对序列进行点突变，突变过程受pLDDT和BLOSUM62矩阵指导。
    根据 design_params 中的 design_type 适配不同的突变策略。
    """
    new_sequence = list(sequence)
    num_mutations = max(1, int(len(sequence) * mutation_rate))
    design_type = design_params.get('design_type', 'linear') if design_params else 'linear'
    sequence_mask = design_params.get('sequence_mask') if design_params else None

    # --- 步骤 1: 确定可突变的位置 ---
    protected_indices = set()
    
    # 处理sequence_mask约束
    if sequence_mask:
        mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
        for i, char in enumerate(mask_clean):
            if char != 'X':  # 非X位置是固定位置，不能突变
                protected_indices.add(i)
    
    # 其他设计类型的保护位置
    if design_type == 'glycopeptide':
        mod_site = design_params.get('modification_site') if design_params else None
        if mod_site is not None:
            protected_indices.add(mod_site)
    elif design_type == 'bicyclic':
        # 双环肽中，半胱氨酸数量必须恰好为3个
        cys_indices = {i for i, aa in enumerate(sequence) if aa == 'C'}
        
        # 验证当前序列的Cys数量
        if len(cys_indices) != 3:
            logger.warning(f"Bicyclic sequence has {len(cys_indices)} Cys, correcting to 3")
            # 如果Cys数量不对，重新生成符合要求的序列
            try:
                corrected_seq = list(sequence)
                # 移除所有Cys
                for i in range(len(corrected_seq)):
                    if corrected_seq[i] == 'C':
                        corrected_seq[i] = random.choice(AMINO_ACIDS_NO_CYS)
                
                # 重新添加3个Cys
                corrected_seq[-1] = 'C'  # 末端Cys
                available_indices = list(range(len(corrected_seq) - 1))
                if len(available_indices) >= 2:
                    pos1, pos2 = random.sample(available_indices, 2)
                    corrected_seq[pos1] = 'C'
                    corrected_seq[pos2] = 'C'
                
                sequence = "".join(corrected_seq)
                new_sequence = list(sequence)
                cys_indices = {i for i, aa in enumerate(sequence) if aa == 'C'}
            except Exception as e:
                logger.error(f"Failed to correct bicyclic sequence: {e}")
        
        # 双环肽突变策略：以一定概率移动一个Cys的位置
        CYS_MOVE_PROBABILITY = 0.15  # 降低概率避免过度变化
        if random.random() < CYS_MOVE_PROBABILITY and len(cys_indices) == 3:
            variable_cys_indices = sorted(list(cys_indices - {len(sequence) - 1}))
            if len(variable_cys_indices) == 2:
                cys_to_move = random.choice(variable_cys_indices)
                
                available_swap_indices = [i for i in range(len(sequence) - 1) 
                                        if i not in cys_indices and i not in protected_indices]
                if available_swap_indices:
                    new_pos = random.choice(available_swap_indices)
                    
                    # 交换位置，并用随机氨基酸填充旧的Cys位置
                    new_sequence[cys_to_move] = random.choice(AMINO_ACIDS_NO_CYS)
                    new_sequence[new_pos] = 'C'
                    logger.debug(f"Bicyclic mutation: Moved Cys from {cys_to_move+1} to {new_pos+1}")
                    
                    # 更新序列和Cys位置
                    sequence = "".join(new_sequence)
                    cys_indices = {i for i, aa in enumerate(sequence) if aa == 'C'}

        # 保护所有Cys位置不被常规突变
        protected_indices.update(cys_indices)

    available_indices = [i for i in range(len(sequence)) if i not in protected_indices]
    if not available_indices:
        logger.warning("No available positions to mutate after excluding protected sites. Returning current sequence.")
        return "".join(new_sequence)
    
    # --- 步骤 2: 选择突变位置 (pLDDT指导) ---
    k = min(num_mutations, len(available_indices))
    if plddt_scores and len(plddt_scores) == len(sequence):
        safe_temp = max(position_selection_temp, 1e-6)
        weights = np.array([(100.0 - plddt_scores[i]) for i in available_indices])
        
        # 应用温度调整
        probabilities = np.exp(weights / safe_temp)
        probabilities /= np.sum(probabilities)

        if np.isnan(probabilities).any(): # 如果概率计算出错，回退到随机选择
             positions_to_mutate = random.sample(available_indices, k=k)
        else:
             positions_to_mutate = np.random.choice(available_indices, size=k, replace=False, p=probabilities)
    else:
        if plddt_scores:
            logger.warning("pLDDT scores length mismatch. Falling back to random position selection.")
        positions_to_mutate = random.sample(available_indices, k=k)

    # --- 步骤 3: 选择替换的氨基酸 (BLOSUM62指导) ---
    include_cysteine = design_params.get('include_cysteine', True) if design_params else True
    
    for pos in positions_to_mutate:
        original_aa = new_sequence[pos]
        substitution_scores = BLOSUM62.get(original_aa, {})
        
        # 选择可用的氨基酸集合
        if design_type == 'bicyclic':
            # 双环肽设计中，确保不会突变为Cys（Cys位置由设计类型控制）
            possible_aas = [aa for aa in AMINO_ACIDS_NO_CYS if aa != original_aa]
        elif include_cysteine:
            # 包含半胱氨酸
            possible_aas = [aa for aa in AMINO_ACIDS if aa != original_aa]
        else:
            # 不包含半胱氨酸
            possible_aas = [aa for aa in AMINO_ACIDS_NO_CYS if aa != original_aa]
            
        if not possible_aas: 
            continue

        scores = [substitution_scores.get(aa, 0) for aa in possible_aas]
        scores_array = np.array(scores) / temperature
        probabilities = np.exp(scores_array - np.max(scores_array)) # Softmax
        probabilities /= np.sum(probabilities)

        new_aa = np.random.choice(possible_aas, p=probabilities)
        new_sequence[pos] = new_aa
    
    return "".join(new_sequence)


def parse_confidence_metrics(results_path: str, binder_chain_id: str) -> dict:
    """从预测输出目录中解析关键置信度指标，并兼容 Boltz 与 AlphaFold3 后端。"""
    metrics = {
        'iptm': 0.0,
        'ptm': 0.0,
        'complex_plddt': 0.0,
        'binder_avg_plddt': 0.0,
        'plddts': [],
        'backend': 'boltz'
    }

    root_path = Path(results_path)

    # 读取亲和力数据（若存在）
    affinity_path = root_path / "affinity_data.json"
    if affinity_path.exists():
        try:
            with affinity_path.open('r') as f:
                metrics['affinity'] = json.load(f)
        except Exception as exc:
            logger.warning(f"Failed to load affinity data from {affinity_path}: {exc}")

    def _extract_plddts_from_cif(cif_path: Path, chain_id: str) -> List[float]:
        """解析指定链的pLDDT值。"""
        try:
            lines = cif_path.read_text().splitlines()
        except Exception as exc:
            logger.warning(f"Unable to read CIF file {cif_path}: {exc}")
            return []

        header, atom_lines, in_loop = [], [], False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('_atom_site.'):
                header.append(stripped)
                in_loop = True
            elif in_loop and (stripped.startswith('ATOM') or stripped.startswith('HETATM')):
                atom_lines.append(stripped)
            elif in_loop and not stripped:
                in_loop = False

        if not header or not atom_lines:
            return []

        header_map = {name: idx for idx, name in enumerate(header)}
        chain_col = header_map.get('_atom_site.label_asym_id') or header_map.get('_atom_site.auth_asym_id')
        res_col = header_map.get('_atom_site.label_seq_id')
        bfactor_col = header_map.get('_atom_site.B_iso_or_equiv')

        if None in (chain_col, res_col, bfactor_col):
            return []

        plddts = []
        last_res_id = None
        for atom_line in atom_lines:
            fields = atom_line.split()
            if len(fields) <= max(chain_col, res_col, bfactor_col):
                continue
            chain_value = fields[chain_col]
            res_value = fields[res_col]
            if chain_value == chain_id and res_value != last_res_id:
                try:
                    plddts.append(float(fields[bfactor_col]))
                    last_res_id = res_value
                except (ValueError, IndexError):
                    continue
        return plddts

    def _prefer_aggregate_file(candidates: List[Path]) -> Optional[Path]:
        """优先选择不包含 seed- 的聚合文件。"""
        for candidate in sorted(candidates):
            if "seed-" not in candidate.as_posix():
                return candidate
        return sorted(candidates)[0] if candidates else None

    af3_dir = root_path / "af3"
    if af3_dir.is_dir():
        metrics['backend'] = 'alphafold3'
        output_root = af3_dir / "output"

        summary_file = None
        confidences_file = None
        model_cif_path = None

        if output_root.is_dir():
            summary_candidates = list(output_root.glob("**/*summary_confidences.json"))
            summary_file = _prefer_aggregate_file(summary_candidates)

            confidences_candidates = list(output_root.glob("**/confidences.json"))
            confidences_file = _prefer_aggregate_file(confidences_candidates)

            model_candidates = list(output_root.glob("**/*model.cif"))
            model_cif_path = _prefer_aggregate_file(model_candidates)

        # 解析 summary_confidences.json
        if summary_file and summary_file.exists():
            try:
                with summary_file.open('r') as f:
                    summary_data = json.load(f)
                ptm = summary_data.get("ptm")
                if isinstance(ptm, (int, float)):
                    metrics['ptm'] = ptm

                iptm = summary_data.get("iptm")
                if not isinstance(iptm, (int, float)) or iptm == 0.0:
                    chain_pair_iptm = summary_data.get("chain_pair_iptm")
                    if (
                        isinstance(chain_pair_iptm, list)
                        and chain_pair_iptm
                        and isinstance(chain_pair_iptm[0], list)
                        and chain_pair_iptm[0]
                        and isinstance(chain_pair_iptm[0][0], (int, float))
                    ):
                        iptm = chain_pair_iptm[0][0]
                if isinstance(iptm, (int, float)):
                    metrics['iptm'] = iptm

                ranking_score = summary_data.get("ranking_score")
                if isinstance(ranking_score, (int, float)):
                    metrics['ranking_score'] = ranking_score

                fraction_disordered = summary_data.get("fraction_disordered")
                if isinstance(fraction_disordered, (int, float)):
                    metrics['fraction_disordered'] = fraction_disordered
            except Exception as exc:
                logger.warning(f"Failed to parse AF3 summary confidences from {summary_file}: {exc}")

        # 解析 confidences.json
        if confidences_file and confidences_file.exists():
            try:
                with confidences_file.open('r') as f:
                    conf_data = json.load(f)

                atom_plddts = conf_data.get("atom_plddts") or []
                if atom_plddts:
                    metrics['complex_plddt'] = float(sum(atom_plddts) / len(atom_plddts))

                pae_matrix = conf_data.get("pae") or []
                flattened_pae = [
                    value
                    for row in pae_matrix
                    if isinstance(row, list)
                    for value in row
                    if isinstance(value, (int, float))
                ]
                if flattened_pae:
                    metrics['complex_pde'] = float(sum(flattened_pae) / len(flattened_pae))
            except Exception as exc:
                logger.warning(f"Failed to parse AF3 confidences from {confidences_file}: {exc}")

        # 解析 CIF 获取 binder pLDDT
        if model_cif_path and model_cif_path.exists():
            binder_plddts = _extract_plddts_from_cif(model_cif_path, binder_chain_id)
            if binder_plddts:
                metrics['plddts'] = binder_plddts
                metrics['binder_avg_plddt'] = float(np.mean(binder_plddts))

        return metrics

    # --- Boltz 默认结果解析 ---
    try:
        json_path = next(
            (
                root_path / f
                for f in os.listdir(results_path)
                if f.startswith('confidence_') and f.endswith('.json')
            ),
            None,
        )
        if json_path and json_path.exists():
            with json_path.open('r') as f:
                data = json.load(f)
            metrics.update({
                'ptm': data.get('ptm', 0.0),
                'complex_plddt': data.get('complex_plddt', 0.0)
            })
            metrics['iptm'] = data.get('iptm', 0.0)

            if metrics['iptm'] == 0.0:
                pair_iptm = data.get('pair_chains_iptm', {})
                for c1, c2_dict in pair_iptm.items():
                    for c2, iptm_val in (c2_dict or {}).items():
                        if c1 != c2 and isinstance(iptm_val, (int, float)) and iptm_val > 0:
                            metrics['iptm'] = max(metrics['iptm'], iptm_val)
    except Exception as exc:
        logger.warning(f"Could not parse confidence metrics from JSON in {results_path}. Error: {exc}")

    try:
        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
        if cif_files:
            rank_1_cif = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
            cif_path = root_path / rank_1_cif
            binder_plddts = _extract_plddts_from_cif(cif_path, binder_chain_id)
            if binder_plddts:
                metrics['plddts'] = binder_plddts
                metrics['binder_avg_plddt'] = float(np.mean(binder_plddts))
    except Exception as exc:
        logger.warning(f"Error parsing pLDDTs from CIF file in {results_path}. Error: {exc}")

    return metrics


# === 增强版功能类 ===

class AdvancedMutationEngine:
    """高级自适应突变引擎"""
    
    def __init__(self):
        self.motif_patterns = defaultdict(float)
        self.position_preferences = defaultdict(lambda: defaultdict(float))
        self.energy_landscape = {}
        self.mutation_history = defaultdict(list)
        self.strategy_success_rates = defaultdict(float)
        
        # 突变策略权重
        self.strategy_weights = {
            'conservative': 0.3,
            'aggressive': 0.2,
            'motif_guided': 0.2,
            'energy_guided': 0.2,
            'diversity_driven': 0.1
        }
        
        # 预定义有益motif
        self.beneficial_motifs = {
            'RGD': 0.8, 'YIGSR': 0.7, 'PHSRN': 0.6, 'DGEA': 0.6,
            'KRG': 0.5, 'KRSR': 0.6, 'GP': 0.4, 'WW': 0.4, 'FF': 0.4
        }
    
    def learn_from_sequence(self, sequence: str, score: float):
        """从序列结果中学习模式"""
        if score < 0.5:
            return
            
        # 学习位置偏好
        for i, aa in enumerate(sequence):
            self.position_preferences[i][aa] += score
        
        # 学习motif模式
        for length in range(2, min(6, len(sequence) + 1)):
            for i in range(len(sequence) - length + 1):
                motif = sequence[i:i+length]
                self.motif_patterns[motif] += score
        
        # 更新能量景观
        energy = -(score)  # 转换为能量值
        self.energy_landscape[sequence] = energy
    
    def select_mutation_strategy(self) -> str:
        """根据成功率选择突变策略"""
        # 调整权重
        total_success = sum(self.strategy_success_rates.values())
        if total_success > 0:
            adjusted_weights = {}
            for strategy, base_weight in self.strategy_weights.items():
                success_boost = self.strategy_success_rates.get(strategy, 0.0) / total_success
                adjusted_weights[strategy] = base_weight * (1.0 + success_boost)
        else:
            adjusted_weights = self.strategy_weights
        
        # 随机选择
        total_weight = sum(adjusted_weights.values())
        r = random.uniform(0, total_weight)
        cumulative = 0
        
        for strategy, weight in adjusted_weights.items():
            cumulative += weight
            if r <= cumulative:
                return strategy
        
        return 'conservative'
    
    def conservative_mutation(self, sequence: str, plddt_scores: List[float] = None, 
                            num_mutations: int = None, design_params: dict = None) -> str:
        """保守突变：偏向BLOSUM62高分替换"""
        if num_mutations is None:
            num_mutations = max(1, len(sequence) // 8)
        
        # 获取包含半胱氨酸的设置
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
        # 确定可突变的位置
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence
        
        # 选择突变位置
        if plddt_scores and len(plddt_scores) == len(sequence):
            available_weights = [(100 - plddt_scores[i]) ** 2 for i in available_positions]
            total_weight = sum(available_weights)
            if total_weight > 0:
                positions = []
                for _ in range(min(num_mutations, len(available_positions))):
                    r = random.random() * total_weight
                    cumsum = 0
                    for j, i in enumerate(available_positions):
                        cumsum += available_weights[j]
                        if r <= cumsum:
                            positions.append(i)
                            break
                positions = list(set(positions))
            else:
                positions = random.sample(available_positions, min(num_mutations, len(available_positions)))
        else:
            positions = random.sample(available_positions, min(num_mutations, len(available_positions)))
        
        for pos in positions:
            original_aa = sequence[pos]
            # 只考虑允许的氨基酸类型
            candidates = [(aa, score) for aa, score in BLOSUM62.get(original_aa, {}).items()
                         if aa != original_aa and score > 0 and aa in amino_acid_set]
            
            if candidates:
                weights = [score for _, score in candidates]
                total_weight = sum(weights)
                probs = [w / total_weight for w in weights]
                r = random.random()
                cumsum = 0
                chosen_aa = candidates[0][0]
                for (aa, _), prob in zip(candidates, probs):
                    cumsum += prob
                    if r <= cumsum:
                        chosen_aa = aa
                        break
                new_sequence[pos] = chosen_aa
            else:
                # 使用位置偏好（仅限允许的氨基酸）
                if pos in self.position_preferences:
                    pos_prefs = self.position_preferences[pos]
                    if pos_prefs:
                        valid_prefs = {aa: score for aa, score in pos_prefs.items() if aa in amino_acid_set}
                        if valid_prefs:
                            best_aa = max(valid_prefs.items(), key=lambda x: x[1])[0]
                            if best_aa != original_aa:
                                new_sequence[pos] = best_aa
        
        return ''.join(new_sequence)
    
    def aggressive_mutation(self, sequence: str, num_mutations: int = None, design_params: dict = None) -> str:
        """激进突变：大范围探索"""
        if num_mutations is None:
            num_mutations = max(2, len(sequence) // 4)
        
        # 获取包含半胱氨酸的设置
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
        # 确定可突变的位置
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence
            
        positions = random.sample(available_positions, min(num_mutations, len(available_positions)))
        
        for pos in positions:
            current_aa = sequence[pos]
            new_aa = random.choice([aa for aa in amino_acid_set if aa != current_aa])
            new_sequence[pos] = new_aa
        
        return ''.join(new_sequence)
    
    def motif_guided_mutation(self, sequence: str, design_params: dict = None) -> str:
        """motif导引突变"""
        new_sequence = list(sequence)
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        # 确定可突变的位置
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence
        
        # 选择有益motif
        all_motifs = {**self.beneficial_motifs, **dict(self.motif_patterns)}
        beneficial_motifs = [motif for motif, score in all_motifs.items() if score >= 0.3]
        
        if beneficial_motifs:
            motif = random.choice(beneficial_motifs)
            if len(motif) <= len(available_positions):
                # 尝试在可用位置中插入motif
                max_start = max(0, len(sequence) - len(motif))
                attempts = 0
                while attempts < 10:
                    start_pos = random.randint(0, max_start)
                    # 检查motif位置是否都可变
                    motif_positions = list(range(start_pos, start_pos + len(motif)))
                    if all(pos in available_positions for pos in motif_positions):
                        for i, aa in enumerate(motif):
                            new_sequence[start_pos + i] = aa
                        break
                    attempts += 1
        
        # 额外保守突变（仅在可变位置）
        remaining_pos = [i for i in available_positions if new_sequence[i] == sequence[i]]
        if remaining_pos:
            num_additional = min(2, len(remaining_pos))
            additional_positions = random.sample(remaining_pos, num_additional)
            
            for pos in additional_positions:
                original_aa = sequence[pos]
                candidates = [(aa, score) for aa, score in BLOSUM62.get(original_aa, {}).items()
                             if score > 0 and aa != original_aa]
                if candidates:
                    chosen_aa = random.choice(candidates)[0]
                    new_sequence[pos] = chosen_aa
        
        return ''.join(new_sequence)
    
    def energy_guided_mutation(self, sequence: str, temperature: float = 1.0, design_params: dict = None) -> str:
        """能量导引突变"""
        new_sequence = list(sequence)
        num_mutations = max(1, len(sequence) // 6)
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        # 确定可突变的位置
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence
        
        # 获取低保守性位置（在可变位置中）
        variable_positions = []
        for i in available_positions:
            if i in self.position_preferences:
                total_weight = sum(self.position_preferences[i].values())
                current_weight = self.position_preferences[i].get(sequence[i], 0)
                conservation = current_weight / total_weight if total_weight > 0 else 0
                if conservation < 0.3:
                    variable_positions.append(i)
        
        if len(variable_positions) >= num_mutations:
            positions = random.sample(variable_positions, num_mutations)
        else:
            positions = random.sample(available_positions, min(num_mutations, len(available_positions)))
        
        for pos in positions:
            current_aa = sequence[pos]
            if pos in self.position_preferences:
                pos_prefs = self.position_preferences[pos]
                candidates = [(aa, weight) for aa, weight in pos_prefs.items() if aa != current_aa]
                if candidates:
                    # 温度调整
                    adj_candidates = [(aa, weight ** (1.0 / temperature)) for aa, weight in candidates]
                    total_weight = sum(weight for _, weight in adj_candidates)
                    if total_weight > 0:
                        r = random.random() * total_weight
                        cumsum = 0
                        for aa, weight in adj_candidates:
                            cumsum += weight
                            if r <= cumsum:
                                new_sequence[pos] = aa
                                break
        
        return ''.join(new_sequence)
    
    def diversity_driven_mutation(self, sequence: str, elite_sequences: List[str], design_params: dict = None) -> str:
        """多样性驱动突变"""
        if not elite_sequences:
            return self.aggressive_mutation(sequence, design_params=design_params)
        
        # 获取包含半胱氨酸的设置
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
        # 确定可突变的位置
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence
        
        # 计算相似性
        similarities = []
        for elite_seq in elite_sequences:
            if len(elite_seq) == len(sequence):
                sim = sum(a == b for a, b in zip(sequence, elite_seq)) / len(sequence)
                similarities.append(sim)
        
        avg_similarity = sum(similarities) / len(similarities) if similarities else 0.5
        
        # 根据相似性调整突变强度
        if avg_similarity > 0.8:
            num_mutations = max(3, len(available_positions) // 3)
        else:
            num_mutations = max(1, len(available_positions) // 6)
        
        num_mutations = min(num_mutations, len(available_positions))
        
        # 选择差异小的位置进行突变（在可变位置中）
        position_differences = []
        for i in available_positions:
            diff_count = sum(1 for elite_seq in elite_sequences 
                           if len(elite_seq) > i and elite_seq[i] != sequence[i])
            position_differences.append((i, diff_count))
        
        position_differences.sort(key=lambda x: x[1])
        positions_to_mutate = [pos for pos, _ in position_differences[:num_mutations]]
        
        for pos in positions_to_mutate:
            current_aa = sequence[pos]
            # 选择在精英群体中该位置出现频率低的氨基酸
            elite_aas_at_pos = [elite_seq[pos] for elite_seq in elite_sequences 
                               if len(elite_seq) > pos]
            aa_counts = Counter(elite_aas_at_pos)
            
            rare_aas = [aa for aa in amino_acid_set 
                       if aa != current_aa and aa_counts.get(aa, 0) <= 1]
            
            if rare_aas:
                new_sequence[pos] = random.choice(rare_aas)
            else:
                new_sequence[pos] = random.choice([aa for aa in amino_acid_set if aa != current_aa])
        
        return ''.join(new_sequence)
    
    def adaptive_mutate(self, sequence: str, parent_metrics: Dict = None, 
                       elite_sequences: List[str] = None, 
                       temperature: float = 1.0, design_params: dict = None) -> Tuple[str, str]:
        """执行自适应突变"""
        strategy = self.select_mutation_strategy()
        
        if strategy == 'conservative':
            plddt_scores = parent_metrics.get('plddts', []) if parent_metrics else []
            mutated_sequence = self.conservative_mutation(sequence, plddt_scores, design_params=design_params)
        elif strategy == 'aggressive':
            mutated_sequence = self.aggressive_mutation(sequence, design_params=design_params)
        elif strategy == 'motif_guided':
            mutated_sequence = self.motif_guided_mutation(sequence, design_params=design_params)
        elif strategy == 'energy_guided':
            mutated_sequence = self.energy_guided_mutation(sequence, temperature, design_params=design_params)
        elif strategy == 'diversity_driven':
            mutated_sequence = self.diversity_driven_mutation(sequence, elite_sequences or [], design_params=design_params)
        else:
            mutated_sequence = self.conservative_mutation(sequence, design_params=design_params)
        
        return mutated_sequence, strategy
    
    def update_strategy_success(self, strategy: str, improvement: float):
        """更新策略成功率"""
        self.strategy_success_rates[strategy] = (
            0.9 * self.strategy_success_rates[strategy] + 0.1 * max(0, improvement)
        )


class ParetoOptimizer:
    """Pareto多目标优化器"""
    
    def __init__(self):
        self.pareto_front = []
    
    def dominates(self, solution1: Dict, solution2: Dict) -> bool:
        """检查solution1是否支配solution2"""
        iptm1, plddt1 = solution1.get('iptm', 0), solution1.get('binder_avg_plddt', 0)
        iptm2, plddt2 = solution2.get('iptm', 0), solution2.get('binder_avg_plddt', 0)
        
        return (iptm1 >= iptm2 and plddt1 >= plddt2) and (iptm1 > iptm2 or plddt1 > plddt2)
    
    def update_pareto_front(self, new_solutions: List[Dict]):
        """更新Pareto前沿"""
        all_solutions = self.pareto_front + new_solutions
        pareto_front = []
        
        for solution in all_solutions:
            is_dominated = False
            for other in all_solutions:
                if other != solution and self.dominates(other, solution):
                    is_dominated = True
                    break
            if not is_dominated:
                pareto_front.append(solution)
        
        self.pareto_front = pareto_front
    
    def get_diverse_elites(self, num_elites: int) -> List[Dict]:
        """从Pareto前沿选择多样化精英"""
        if not self.pareto_front:
            return []
        
        if len(self.pareto_front) <= num_elites:
            return self.pareto_front
        
        # 简单的多样性选择：基于目标值分布
        solutions = self.pareto_front.copy()
        
        # 计算拥挤距离
        for solution in solutions:
            solution['crowding_distance'] = 0
        
        for obj_func in ['iptm', 'binder_avg_plddt']:
            solutions.sort(key=lambda x: x.get(obj_func, 0))
            
            if len(solutions) > 2:
                solutions[0]['crowding_distance'] = float('inf')
                solutions[-1]['crowding_distance'] = float('inf')
                
                obj_range = solutions[-1].get(obj_func, 0) - solutions[0].get(obj_func, 0)
                if obj_range > 0:
                    for i in range(1, len(solutions) - 1):
                        distance = (solutions[i+1].get(obj_func, 0) - solutions[i-1].get(obj_func, 0)) / obj_range
                        solutions[i]['crowding_distance'] += distance
        
        solutions.sort(key=lambda x: x.get('crowding_distance', 0), reverse=True)
        return solutions[:num_elites]


def calculate_sequence_similarity(seq1: str, seq2: str) -> float:
    """计算两个序列的相似性"""
    if len(seq1) != len(seq2):
        return 0.0
    
    matches = sum(a == b for a, b in zip(seq1, seq2))
    return matches / len(seq1)


def extract_sequence_features(sequence: str) -> Dict[str, float]:
    """提取序列的生化特征"""
    features = {}
    
    # 氨基酸组成
    aa_counts = Counter(sequence)
    total_length = len(sequence)
    
    # 疏水性氨基酸比例
    hydrophobic = set('AILMFPWYV')
    features['hydrophobic_ratio'] = sum(aa_counts[aa] for aa in hydrophobic if aa in aa_counts) / total_length
    
    # 极性氨基酸比例
    polar = set('NQST')
    features['polar_ratio'] = sum(aa_counts[aa] for aa in polar if aa in aa_counts) / total_length
    
    # 带电氨基酸比例
    charged = set('DEKRHC')
    features['charged_ratio'] = sum(aa_counts[aa] for aa in charged if aa in aa_counts) / total_length
    
    # 芳香性氨基酸比例
    aromatic = set('FWY')
    features['aromatic_ratio'] = sum(aa_counts[aa] for aa in aromatic if aa in aa_counts) / total_length
    
    # 小氨基酸比例
    small = set('AGS')
    features['small_ratio'] = sum(aa_counts[aa] for aa in small if aa in aa_counts) / total_length
    
    # Pro含量（结构柔性）
    features['pro_ratio'] = aa_counts.get('P', 0) / total_length
    
    # Gly含量（结构柔性）
    features['gly_ratio'] = aa_counts.get('G', 0) / total_length
    
    # Cys含量（二硫键潜力）
    features['cys_ratio'] = aa_counts.get('C', 0) / total_length
    
    return features


def analyze_population_diversity(sequences: List[str]) -> Dict[str, float]:
    """分析群体多样性"""
    if len(sequences) < 2:
        return {'avg_pairwise_similarity': 0.0, 'position_entropy': 0.0}
    
    # 平均成对相似性
    similarities = []
    for i in range(len(sequences)):
        for j in range(i + 1, len(sequences)):
            sim = calculate_sequence_similarity(sequences[i], sequences[j])
            similarities.append(sim)
    
    avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0
    
    # 位置熵
    position_entropies = []
    seq_length = len(sequences[0]) if sequences else 0
    
    for pos in range(seq_length):
        aa_counts = Counter(seq[pos] for seq in sequences if len(seq) > pos)
        total = sum(aa_counts.values())
        
        if total > 0:
            entropy = -sum((count / total) * math.log2(count / total) 
                          for count in aa_counts.values() if count > 0)
            position_entropies.append(entropy)
    
    avg_entropy = sum(position_entropies) / len(position_entropies) if position_entropies else 0.0
    
    return {
        'avg_pairwise_similarity': avg_similarity,
        'position_entropy': avg_entropy,
        'num_unique_sequences': len(set(sequences)),
        'diversity_index': 1.0 - avg_similarity  # 多样性指数
    }


# === 糖肽修饰相关辅助函数 ===

def get_glycopeptide_ccd_code(glycan_code: str, amino_acid: str) -> str:
    """生成糖肽的CCD代码"""
    return f"{glycan_code}_{amino_acid}"


def validate_glycosylation_compatibility(amino_acid: str, glycan_code: str) -> bool:
    """验证氨基酸与糖基的兼容性"""
    if glycan_code not in MONOSACCHARIDES:
        return False
    
    glycan_info = MONOSACCHARIDES[glycan_code]
    allowed_types = glycan_info.get('type', [])
    
    # 检查氨基酸是否支持任何允许的糖基化类型
    for linkage_type in allowed_types:
        if linkage_type in GLYCOSYLATION_SITES:
            if amino_acid in GLYCOSYLATION_SITES[linkage_type]:
                return True
    
    return False


def get_optimal_linkage_type(amino_acid: str, glycan_code: str) -> str:
    """获取氨基酸和糖基的最佳连接类型"""
    if not validate_glycosylation_compatibility(amino_acid, glycan_code):
        raise ValueError(f"Incompatible combination: {amino_acid} with {glycan_code}")
    
    glycan_info = MONOSACCHARIDES[glycan_code]
    allowed_types = glycan_info.get('type', [])
    
    # 优先级：N-linked > O-linked > C-linked
    priority = ['N-linked', 'O-linked', 'C-linked']
    
    for linkage_type in priority:
        if linkage_type in allowed_types and linkage_type in GLYCOSYLATION_SITES:
            if amino_acid in GLYCOSYLATION_SITES[linkage_type]:
                return linkage_type
    
    # 如果没有找到优先的类型，返回第一个可用的
    for linkage_type in allowed_types:
        if linkage_type in GLYCOSYLATION_SITES:
            if amino_acid in GLYCOSYLATION_SITES[linkage_type]:
                return linkage_type
    
    raise ValueError(f"No compatible linkage type found for {amino_acid} and {glycan_code}")


def calculate_glycopeptide_properties(glycan_code: str) -> Dict[str, float]:
    """计算糖肽的理化性质"""
    if glycan_code not in MONOSACCHARIDES:
        return {}
    
    glycan_info = MONOSACCHARIDES[glycan_code]
    
    properties = {
        'molecular_weight': glycan_info.get('molecular_weight', 0.0),
        'hydrophilic_contribution': 0.8,  # 糖基通常是亲水的
        'flexibility_increase': 0.6,      # 糖基增加分子柔性
        'surface_exposure': 0.9,          # 糖基通常暴露在表面
    }
    
    # 根据糖基类型调整性质
    if 'NAN' in glycan_code or 'SIA' in glycan_code:
        properties['charge'] = -1.0  # 唾液酸带负电荷
    else:
        properties['charge'] = 0.0
    
    if 'FUC' in glycan_code:
        properties['steric_hindrance'] = 0.7  # 岩藻糖空间位阻较大
    else:
        properties['steric_hindrance'] = 0.3
    
    return properties


def suggest_glycosylation_sites(sequence: str, target_properties: Dict = None) -> List[Dict]:
    """建议蛋白质序列的糖基化位点"""
    suggestions = []
    
    # 默认目标属性
    if target_properties is None:
        target_properties = {
            'increase_solubility': True,
            'increase_stability': True,
            'avoid_active_sites': True,
        }
    
    for i, aa in enumerate(sequence):
        position = i + 1
        
        # 检查该氨基酸是否可以糖基化
        compatible_glycans = []
        for glycan_code, glycan_info in MONOSACCHARIDES.items():
            if validate_glycosylation_compatibility(aa, glycan_code):
                compatible_glycans.append({
                    'glycan_code': glycan_code,
                    'glycan_name': glycan_info.get('name', ''),
                    'linkage_type': get_optimal_linkage_type(aa, glycan_code),
                    'properties': calculate_glycopeptide_properties(glycan_code)
                })
        
        if compatible_glycans:
            # 根据目标属性排序
            if target_properties.get('increase_solubility', False):
                compatible_glycans.sort(
                    key=lambda x: x['properties'].get('hydrophilic_contribution', 0),
                    reverse=True
                )
            
            suggestions.append({
                'position': position,
                'amino_acid': aa,
                'compatible_glycans': compatible_glycans
            })
    
    return suggestions


def generate_glycosylation_patterns(
    sequence: str, 
    max_sites: int = 3,
    pattern_type: str = 'balanced'
) -> List[List[Dict]]:
    """
    生成糖基化模式
    
    Args:
        sequence: 蛋白质序列
        max_sites: 最大糖基化位点数
        pattern_type: 模式类型 ('balanced', 'high_density', 'terminal_focused')
    
    Returns:
        糖基化模式列表，每个模式是位点配置的列表
    """
    from itertools import combinations
    
    # 获取所有可能的糖基化位点
    potential_sites = suggest_glycosylation_sites(sequence)
    
    if not potential_sites:
        return []
    
    patterns = []
    
    # 根据模式类型调整策略
    if pattern_type == 'balanced':
        # 均匀分布的糖基化位点
        step = max(1, len(potential_sites) // max_sites)
        selected_sites = potential_sites[::step][:max_sites]
        
        for site in selected_sites:
            # 为每个位点选择最佳糖基
            best_glycan = site['compatible_glycans'][0] if site['compatible_glycans'] else None
            if best_glycan:
                patterns.append([{
                    'position': site['position'],
                    'amino_acid': site['amino_acid'],
                    'glycan': best_glycan['glycan_code']
                }])
    
    elif pattern_type == 'high_density':
        # 高密度糖基化
        for num_sites in range(1, min(max_sites + 1, len(potential_sites) + 1)):
            for site_combination in combinations(potential_sites[:max_sites*2], num_sites):
                pattern = []
                for site in site_combination:
                    best_glycan = site['compatible_glycans'][0] if site['compatible_glycans'] else None
                    if best_glycan:
                        pattern.append({
                            'position': site['position'],
                            'amino_acid': site['amino_acid'],
                            'glycan': best_glycan['glycan_code']
                        })
                if pattern:
                    patterns.append(pattern)
    
    elif pattern_type == 'terminal_focused':
        # 关注N端和C端的糖基化
        n_terminal_sites = [s for s in potential_sites if s['position'] <= len(sequence) // 3]
        c_terminal_sites = [s for s in potential_sites if s['position'] >= 2 * len(sequence) // 3]
        
        for sites_group in [n_terminal_sites, c_terminal_sites]:
            for site in sites_group[:max_sites//2 + 1]:
                best_glycan = site['compatible_glycans'][0] if site['compatible_glycans'] else None
                if best_glycan:
                    patterns.append([{
                        'position': site['position'],
                        'amino_acid': site['amino_acid'],
                        'glycan': best_glycan['glycan_code']
                    }])
    
    # 限制返回的模式数量
    return patterns[:20]  # 最多返回20种模式


def create_modification_based_yaml(
    sequence: str,
    glycosylation_pattern: List[Dict],
    chain_id: str = 'A',
    msa_type: str = 'empty'
) -> str:
    """
    创建基于modifications的Boltz YAML配置
    
    Args:
        sequence: 蛋白质序列
        glycosylation_pattern: 糖基化模式
        chain_id: 链ID
        msa_type: MSA类型
    
    Returns:
        YAML配置字符串
    """
    import yaml
    
    # 构建modifications列表
    modifications = []
    for site in glycosylation_pattern:
        position = site['position']
        glycan = site['glycan']
        amino_acid = site.get('amino_acid', sequence[position-1] if position <= len(sequence) else 'N')
        
        ccd_code = get_glycopeptide_ccd_code(glycan, amino_acid)
        modifications.append({
            'position': position,
            'ccd': ccd_code
        })
    
    # 构建YAML配置
    config = {
        'version': 1,
        'sequences': [{
            'protein': {
                'id': [chain_id],
                'sequence': sequence,
                'msa': msa_type,
                'modifications': modifications
            }
        }]
    }
    
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def compare_glycosylation_strategies(sequence: str) -> Dict[str, List[Dict]]:
    """比较不同的糖基化策略"""
    strategies = {}
    
    # 生成不同类型的糖基化模式
    pattern_types = ['balanced', 'high_density', 'terminal_focused']
    
    for pattern_type in pattern_types:
        patterns = generate_glycosylation_patterns(
            sequence, 
            max_sites=3, 
            pattern_type=pattern_type
        )
        strategies[pattern_type] = patterns
    
    return strategies
