"""蛋白质/糖肽设计的核心数据结构与工具函数。

包含氨基酸、单糖和糖基化位点常量，BLOSUM62 替换分数，以及序列生成、
突变和结构预测结果解析。所有函数均无状态。
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

logger = logging.getLogger(__name__)

# 核心常量

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"
AMINO_ACIDS_NO_CYS = "ARNDQEGHILKMFPSTWYV" # 用于禁用半胱氨酸的设计，避免生成额外的Cys

# 双环肽连接子
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
}

# 糖化学常量
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
    
    # 常见的高甘露糖型糖链组分
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
    
    # 兼容旧代号，同 NAN
    'SIA': {
        'atom': 'C2', 
        'type': ['O-linked'], 
        'name': '唾液酸', 
        'eng_name': 'Sialic acid',
        'smiles': 'CC(=O)N[C@H]1[C@@H](O)[C@H](O)[C@@H](O)[C@H](C(O)=O)[C@@H]1O',
        'molecular_weight': 309.27,
        'linkage_atoms': {'O-linked': 'OG'}
    },
    
    # 扩展糖基库，用于复杂糖基化模式
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
    """为双环肽生成随机序列：恰好三个Cys，其中一个固定在末端。"""
    if length < 3:
        raise ValueError("Sequence length must be at least 3 for bicyclic peptide design.")

    seq = list("".join(random.choice(AMINO_ACIDS_NO_CYS) for _ in range(length)))

    seq[-1] = 'C'

    available_indices = list(range(length - 1))  # 不包括末尾位置
    if len(available_indices) < 2:
        raise ValueError("Sequence length too short for bicyclic peptide (need at least 3 positions)")

    cys_pos1, cys_pos2 = random.sample(available_indices, 2)
    seq[cys_pos1] = 'C'
    seq[cys_pos2] = 'C'

    final_sequence = "".join(seq)

    cys_count = final_sequence.count('C')
    if cys_count != 3:
        raise ValueError(f"Generated bicyclic sequence has {cys_count} Cys, expected 3")
    
    return final_sequence


def generate_random_sequence(length: int, design_params: dict) -> str:
    """根据设计参数生成一个随机的氨基酸序列。"""
    design_type = design_params.get('design_type', 'linear')
    include_cysteine = design_params.get('include_cysteine', True)
    sequence_mask = design_params.get('sequence_mask')

    if include_cysteine:
        amino_acid_set = AMINO_ACIDS
    else:
        amino_acid_set = AMINO_ACIDS_NO_CYS

    if sequence_mask:
        mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
        if len(mask_clean) != length:
            raise ValueError(f"Sequence mask length ({len(mask_clean)}) must match binder length ({length})")

        seq = []
        for i, char in enumerate(mask_clean):
            if char == 'X':
                seq.append(random.choice(amino_acid_set))
            else:
                seq.append(char)
    else:
        seq = []

        if design_type == 'bicyclic':
            if length < 3:
                raise ValueError("Bicyclic peptide length must be at least 3.")
            # 双环肽需要Cys，即使用户禁用了半胱氨酸
            seq = list("".join(random.choice(AMINO_ACIDS_NO_CYS) for _ in range(length)))

            seq[-1] = 'C'

            cys_positions = design_params.get('cys_positions')
            if cys_positions and len(cys_positions) == 2:
                pos1, pos2 = cys_positions[0], cys_positions[1]
            else:
                available_indices = list(range(length - 1))
                pos1, pos2 = random.sample(available_indices, 2)

            seq[pos1] = 'C'
            seq[pos2] = 'C'

            return "".join(seq)

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

        else:
            seq = [random.choice(amino_acid_set) for _ in range(length)]

    return "".join(seq)



def mutate_sequence(
    sequence: str,
    mutation_rate: float = 0.1,
    plddt_scores: list = None,
    design_params: dict = None,
    position_selection_temp: float = 1.0,
    temperature: float = 1.0
) -> str:
    """
    对序列进行点突变，突变过程受pLDDT和BLOSUM62矩阵指导。
    根据 design_params 中的 design_type 适配不同的突变策略。
    """
    new_sequence = list(sequence)
    num_mutations = max(1, int(len(sequence) * mutation_rate))
    design_type = design_params.get('design_type', 'linear') if design_params else 'linear'
    sequence_mask = design_params.get('sequence_mask') if design_params else None

    # 步骤 1: 确定可突变的位置
    protected_indices = set()

    if sequence_mask:
        mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
        for i, char in enumerate(mask_clean):
            if char != 'X':  # 非X位置是固定位置，不能突变
                protected_indices.add(i)

    if design_type == 'glycopeptide':
        mod_site = design_params.get('modification_site') if design_params else None
        if mod_site is not None:
            protected_indices.add(mod_site)
    elif design_type == 'bicyclic':
        # 双环肽中，半胱氨酸数量必须恰好为3个
        cys_indices = {i for i, aa in enumerate(sequence) if aa == 'C'}

        if len(cys_indices) != 3:
            logger.warning(f"Bicyclic sequence has {len(cys_indices)} Cys, correcting to 3")
            try:
                corrected_seq = list(sequence)
                for i in range(len(corrected_seq)):
                    if corrected_seq[i] == 'C':
                        corrected_seq[i] = random.choice(AMINO_ACIDS_NO_CYS)

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

                    new_sequence[cys_to_move] = random.choice(AMINO_ACIDS_NO_CYS)
                    new_sequence[new_pos] = 'C'
                    logger.debug(f"Bicyclic mutation: Moved Cys from {cys_to_move+1} to {new_pos+1}")

                    sequence = "".join(new_sequence)
                    cys_indices = {i for i, aa in enumerate(sequence) if aa == 'C'}

        # 保护所有Cys位置不被常规突变
        protected_indices.update(cys_indices)

    available_indices = [i for i in range(len(sequence)) if i not in protected_indices]
    if not available_indices:
        logger.warning("No available positions to mutate after excluding protected sites. Returning current sequence.")
        return "".join(new_sequence)

    # 步骤 2: 选择突变位置 (pLDDT指导)
    k = min(num_mutations, len(available_indices))
    if plddt_scores and len(plddt_scores) == len(sequence):
        safe_temp = max(position_selection_temp, 1e-6)
        weights = np.array([(100.0 - plddt_scores[i]) for i in available_indices])

        probabilities = np.exp(weights / safe_temp)
        probabilities /= np.sum(probabilities)

        if np.isnan(probabilities).any(): # 概率计算出错则回退到随机选择
             positions_to_mutate = random.sample(available_indices, k=k)
        else:
             positions_to_mutate = np.random.choice(available_indices, size=k, replace=False, p=probabilities)
    else:
        if plddt_scores:
            logger.warning("pLDDT scores length mismatch. Falling back to random position selection.")
        positions_to_mutate = random.sample(available_indices, k=k)

    # 步骤 3: 选择替换的氨基酸 (BLOSUM62指导)
    include_cysteine = design_params.get('include_cysteine', True) if design_params else True

    for pos in positions_to_mutate:
        original_aa = new_sequence[pos]
        substitution_scores = BLOSUM62.get(original_aa, {})

        if design_type == 'bicyclic':
            # 双环肽不能突变为Cys（Cys位置由设计类型控制）
            possible_aas = [aa for aa in AMINO_ACIDS_NO_CYS if aa != original_aa]
        elif include_cysteine:
            possible_aas = [aa for aa in AMINO_ACIDS if aa != original_aa]
        else:
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


def _extract_chain_ids_from_summary(summary_data: dict) -> List[str]:
    if not isinstance(summary_data, dict):
        return []
    chain_ids = summary_data.get("chain_ids")
    if isinstance(chain_ids, list) and all(isinstance(c, str) for c in chain_ids):
        return chain_ids
    chains = summary_data.get("chains")
    if isinstance(chains, list):
        extracted = []
        for chain in chains:
            if isinstance(chain, dict):
                chain_id = chain.get("chain_id") or chain.get("id") or chain.get("name")
                if isinstance(chain_id, str):
                    extracted.append(chain_id)
        if extracted:
            return extracted
    return []

def _get_pair_iptm(
    chain_pair_iptm: Optional[List[List[float]]],
    chain_ids: List[str],
    chain_a: Optional[str],
    chain_b: Optional[str]
) -> Optional[float]:
    if not chain_pair_iptm or not chain_a or not chain_b or chain_a == chain_b:
        return None
    if chain_a in chain_ids and chain_b in chain_ids:
        idx_a = chain_ids.index(chain_a)
        idx_b = chain_ids.index(chain_b)
        try:
            value = chain_pair_iptm[idx_a][idx_b]
        except (IndexError, TypeError):
            value = None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            value = chain_pair_iptm[idx_b][idx_a]
        except (IndexError, TypeError):
            value = None
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _read_pair_chains_iptm_from_map(
    pair_map: Optional[dict],
    chain_a: Optional[str],
    chain_b: Optional[str],
    chain_order: Optional[List[str]] = None,
) -> Optional[float]:
    if not isinstance(pair_map, dict) or not chain_a or not chain_b or chain_a == chain_b:
        return None

    def _lookup_row(key_a, key_b) -> Optional[float]:
        row = pair_map.get(key_a)
        if not isinstance(row, dict):
            return None
        value = row.get(key_b)
        if isinstance(value, (int, float)):
            return float(value)
        return None

    direct = _lookup_row(chain_a, chain_b)
    if direct is not None:
        return direct
    reverse = _lookup_row(chain_b, chain_a)
    if reverse is not None:
        return reverse

    keys = [str(key).strip() for key in pair_map.keys()]
    if not keys or not all(token.isdigit() for token in keys):
        return None

    if not chain_order:
        return None

    normalized_order = [str(item).strip() for item in chain_order if str(item).strip()]
    if chain_a not in normalized_order or chain_b not in normalized_order:
        return None

    idx_a = normalized_order.index(chain_a)
    idx_b = normalized_order.index(chain_b)
    for key_a, key_b in (
        (str(idx_a), str(idx_b)),
        (str(idx_b), str(idx_a)),
        (idx_a, idx_b),
        (idx_b, idx_a),
    ):
        value = _lookup_row(key_a, key_b)
        if value is not None:
            return value
    return None


def _normalize_probability_metric(value: Optional[float]) -> Optional[float]:
    if not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    if normalized > 1.0 and normalized <= 100.0:
        normalized = normalized / 100.0
    if normalized < 0.0:
        return None
    return normalized


def resolve_preferred_interface_metric(metrics: Optional[dict]) -> Dict[str, Optional[float] | str]:
    payload = metrics if isinstance(metrics, dict) else {}
    ligand_ipsae_max = _normalize_probability_metric(payload.get("ligand_ipsae_max"))
    if ligand_ipsae_max is not None:
        return {
            "value": ligand_ipsae_max,
            "label": "IPSAE",
            "source": "ipsae",
            "kind": "ligand_ipsae_max",
        }

    ipsae_dom = _normalize_probability_metric(payload.get("ipsae_dom"))
    if ipsae_dom is not None:
        return {
            "value": ipsae_dom,
            "label": "IPSAE",
            "source": "ipsae",
            "kind": "ipsae_dom",
        }

    pair_iptm = _normalize_probability_metric(payload.get("pair_iptm"))
    if pair_iptm is not None:
        return {
            "value": pair_iptm,
            "label": "ipTM",
            "source": "iptm",
            "kind": "pair_iptm",
        }

    iptm = _normalize_probability_metric(payload.get("iptm"))
    if iptm is not None:
        return {
            "value": iptm,
            "label": "ipTM",
            "source": "iptm",
            "kind": "iptm",
        }

    return {
        "value": None,
        "label": "IPSAE",
        "source": "none",
        "kind": "none",
    }


def parse_confidence_metrics(
    results_path: str,
    binder_chain_id: str,
    target_chain_id: Optional[str] = None,
    chain_order: Optional[List[str]] = None,
    partner_chain_ids: Optional[List[str]] = None,
) -> dict:
    """从预测输出目录中解析关键置信度指标，并兼容 Boltz 与 AlphaFold3 后端。"""
    normalized_partner_chain_ids: List[str] = []
    for chain_id in partner_chain_ids or []:
        token = str(chain_id or "").strip()
        if not token or token == binder_chain_id or token in normalized_partner_chain_ids:
            continue
        normalized_partner_chain_ids.append(token)

    pair_chain_candidates = [binder_chain_id, *normalized_partner_chain_ids]
    metrics = {
        'iptm': 0.0,
        'pair_iptm': None,
        'pair_iptm_by_chain': {},
        'ipsae_dom': None,
        'ligand_ipsae_max': None,
        'interface_metric': None,
        'interface_metric_label': 'IPSAE',
        'interface_metric_source': 'none',
        'interface_metric_kind': 'none',
        'ptm': 0.0,
        'complex_plddt': 0.0,
        'binder_avg_plddt': 0.0,
        'plddts': [],
        'backend': 'boltz'
    }

    root_path = Path(results_path)

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

    def _extract_plddts_from_pdb(pdb_path: Path, chain_id: str) -> List[float]:
        try:
            lines = pdb_path.read_text().splitlines()
        except Exception as exc:
            logger.warning(f"Unable to read PDB file {pdb_path}: {exc}")
            return []

        plddts = []
        last_res_id = None
        for line in lines:
            if not line.startswith(("ATOM", "HETATM")):
                continue
            if len(line) < 66:
                continue
            chain_value = line[21].strip()
            if chain_value != chain_id:
                continue
            res_value = f"{line[22:26].strip()}:{line[26].strip()}"
            if res_value == last_res_id:
                continue
            try:
                plddts.append(float(line[60:66].strip()))
                last_res_id = res_value
            except ValueError:
                continue
        return plddts

    def _extract_plddts_from_structure(structure_path: Path, chain_id: str) -> List[float]:
        suffix = structure_path.suffix.lower()
        if suffix == ".pdb":
            return _extract_plddts_from_pdb(structure_path, chain_id)
        return _extract_plddts_from_cif(structure_path, chain_id)

    def _load_json_payload(json_path: Path):
        try:
            with json_path.open('r') as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Failed to load JSON payload from {json_path}: {exc}")
            return None

    def _find_numeric_value(payload, keys: List[str]) -> Optional[float]:
        normalized_keys = {str(key).strip().lower() for key in keys if str(key).strip()}
        stack = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    key_text = str(key).strip().lower()
                    if key_text in normalized_keys and isinstance(value, (int, float)):
                        return float(value)
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None

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

        # summary_confidences.json
        if summary_file and summary_file.exists():
            try:
                with summary_file.open('r') as f:
                    summary_data = json.load(f)
                ptm = summary_data.get("ptm")
                if isinstance(ptm, (int, float)):
                    metrics['ptm'] = ptm

                iptm = summary_data.get("iptm")
                if isinstance(iptm, (int, float)):
                    metrics['iptm'] = float(iptm)
                ipsae_dom = _normalize_probability_metric(summary_data.get("ipsae_dom"))
                if ipsae_dom is not None:
                    metrics['ipsae_dom'] = ipsae_dom
                ligand_ipsae_max = _normalize_probability_metric(summary_data.get("ligand_ipsae_max"))
                if ligand_ipsae_max is not None:
                    metrics['ligand_ipsae_max'] = ligand_ipsae_max
                chain_pair_iptm = summary_data.get("chain_pair_iptm")
                if isinstance(chain_pair_iptm, list):
                    chain_ids = _extract_chain_ids_from_summary(summary_data)
                    if not chain_ids and chain_order:
                        chain_ids = chain_order
                    pair_values_by_chain: Dict[str, float] = {}
                    for chain_id in pair_chain_candidates:
                        pair_value = _get_pair_iptm(
                            chain_pair_iptm,
                            chain_ids,
                            chain_id,
                            target_chain_id
                        )
                        if pair_value is not None:
                            pair_values_by_chain[chain_id] = float(pair_value)
                    if pair_values_by_chain:
                        metrics['pair_iptm_by_chain'] = pair_values_by_chain
                        preferred_pair = pair_values_by_chain.get(binder_chain_id)
                        if preferred_pair is None:
                            preferred_pair = next(iter(pair_values_by_chain.values()))
                        metrics['pair_iptm'] = float(preferred_pair)

                ranking_score = summary_data.get("ranking_score")
                if isinstance(ranking_score, (int, float)):
                    metrics['ranking_score'] = ranking_score

                fraction_disordered = summary_data.get("fraction_disordered")
                if isinstance(fraction_disordered, (int, float)):
                    metrics['fraction_disordered'] = fraction_disordered
            except Exception as exc:
                logger.warning(f"Failed to parse AF3 summary confidences from {summary_file}: {exc}")

        # confidences.json
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

        # CIF: binder pLDDT
        if model_cif_path and model_cif_path.exists():
            binder_plddts = _extract_plddts_from_cif(model_cif_path, binder_chain_id)
            if binder_plddts:
                metrics['plddts'] = binder_plddts
                metrics['binder_avg_plddt'] = float(np.mean(binder_plddts))

        return metrics

    protenix_dir = root_path / "protenix"
    if protenix_dir.is_dir():
        metrics['backend'] = 'protenix'
        output_root = protenix_dir / "output" if (protenix_dir / "output").is_dir() else protenix_dir

        ranking_file = _prefer_aggregate_file(list(output_root.glob("**/confidence_ranking.csv")))
        if ranking_file and ranking_file.exists():
            try:
                import csv

                with ranking_file.open('r', encoding='utf-8', newline='') as f:
                    first_row = next(csv.DictReader(f), None)
                if isinstance(first_row, dict):
                    for key, target_key in (
                        ("ptm", "ptm"),
                        ("iptm", "iptm"),
                        ("ranking_score", "ranking_score"),
                        ("plddt", "complex_plddt"),
                        ("complex_plddt", "complex_plddt"),
                    ):
                        raw_value = first_row.get(key)
                        if raw_value in (None, ""):
                            continue
                        try:
                            metrics[target_key] = float(raw_value)
                        except (TypeError, ValueError):
                            continue
            except Exception as exc:
                logger.warning(f"Failed to parse Protenix confidence ranking from {ranking_file}: {exc}")

        json_candidates = sorted(output_root.glob("**/*.json"))
        for json_path in json_candidates:
            payload = _load_json_payload(json_path)
            if payload is None:
                continue
            if not isinstance(metrics.get('ptm'), (int, float)) or float(metrics.get('ptm') or 0.0) == 0.0:
                ptm = _find_numeric_value(payload, ["ptm"])
                if ptm is not None:
                    metrics['ptm'] = ptm
            if not isinstance(metrics.get('iptm'), (int, float)) or float(metrics.get('iptm') or 0.0) == 0.0:
                iptm = _find_numeric_value(payload, ["iptm", "iptm_score", "ip_tm"])
                if iptm is not None:
                    metrics['iptm'] = iptm
            if metrics.get('ipsae_dom') is None:
                ipsae_dom = _normalize_probability_metric(_find_numeric_value(payload, ["ipsae_dom", "ipsaeDom"]))
                if ipsae_dom is not None:
                    metrics['ipsae_dom'] = ipsae_dom
            if metrics.get('ligand_ipsae_max') is None:
                ligand_ipsae_max = _normalize_probability_metric(
                    _find_numeric_value(payload, ["ligand_ipsae_max", "ligandIpsaeMax"])
                )
                if ligand_ipsae_max is not None:
                    metrics['ligand_ipsae_max'] = ligand_ipsae_max
            if 'ranking_score' not in metrics:
                ranking_score = _find_numeric_value(payload, ["ranking_score", "score"])
                if ranking_score is not None:
                    metrics['ranking_score'] = ranking_score
            if not isinstance(metrics.get('complex_plddt'), (int, float)) or float(metrics.get('complex_plddt') or 0.0) == 0.0:
                complex_plddt = _find_numeric_value(payload, ["complex_plddt", "plddt", "avg_plddt", "mean_plddt"])
                if complex_plddt is not None:
                    metrics['complex_plddt'] = complex_plddt

        structure_candidates = list(output_root.glob("**/*.cif")) + list(output_root.glob("**/*.pdb"))
        model_path = _prefer_aggregate_file(structure_candidates)
        if model_path and model_path.exists():
            binder_plddts = _extract_plddts_from_structure(model_path, binder_chain_id)
            if binder_plddts:
                metrics['plddts'] = binder_plddts
                metrics['binder_avg_plddt'] = float(np.mean(binder_plddts))
                if float(metrics.get('complex_plddt') or 0.0) <= 0.0:
                    metrics['complex_plddt'] = metrics['binder_avg_plddt']

        return metrics

    # Boltz 结果解析
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
            iptm_raw = data.get('iptm', 0.0)
            if isinstance(iptm_raw, (int, float)):
                metrics['iptm'] = float(iptm_raw)
            ipsae_dom = _normalize_probability_metric(data.get('ipsae_dom'))
            if ipsae_dom is not None:
                metrics['ipsae_dom'] = ipsae_dom
            ligand_ipsae_max = _normalize_probability_metric(data.get('ligand_ipsae_max'))
            if ligand_ipsae_max is not None:
                metrics['ligand_ipsae_max'] = ligand_ipsae_max
            pair_iptm = data.get('pair_chains_iptm', {})
            pair_values_by_chain: Dict[str, float] = {}
            if target_chain_id:
                for chain_id in pair_chain_candidates:
                    pair_value = _read_pair_chains_iptm_from_map(
                        pair_iptm,
                        chain_id,
                        target_chain_id,
                        chain_order,
                    )
                    if pair_value is not None:
                        pair_values_by_chain[chain_id] = float(pair_value)

            if pair_values_by_chain:
                metrics['pair_iptm_by_chain'] = pair_values_by_chain
                preferred_pair = pair_values_by_chain.get(binder_chain_id)
                if preferred_pair is None:
                    preferred_pair = next(iter(pair_values_by_chain.values()))
                metrics['pair_iptm'] = float(preferred_pair)
    except Exception as exc:
        logger.warning(f"Could not parse confidence metrics from JSON in {results_path}. Error: {exc}")

    try:
        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
        if cif_files:
            rank_1_cif = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
            cif_path = root_path / rank_1_cif
            binder_plddts = _extract_plddts_from_structure(cif_path, binder_chain_id)
            if binder_plddts:
                metrics['plddts'] = binder_plddts
                metrics['binder_avg_plddt'] = float(np.mean(binder_plddts))
    except Exception as exc:
        logger.warning(f"Error parsing pLDDTs from CIF file in {results_path}. Error: {exc}")

    preferred_interface_metric = resolve_preferred_interface_metric(metrics)
    metrics['interface_metric'] = preferred_interface_metric.get('value')
    metrics['interface_metric_label'] = preferred_interface_metric.get('label') or 'IPSAE'
    metrics['interface_metric_source'] = preferred_interface_metric.get('source') or 'none'
    metrics['interface_metric_kind'] = preferred_interface_metric.get('kind') or 'none'

    return metrics


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
            
        for i, aa in enumerate(sequence):
            self.position_preferences[i][aa] += score

        for length in range(2, min(6, len(sequence) + 1)):
            for i in range(len(sequence) - length + 1):
                motif = sequence[i:i+length]
                self.motif_patterns[motif] += score
        
        energy = -(score)  # 转换为能量值
        self.energy_landscape[sequence] = energy
    
    def select_mutation_strategy(self) -> str:
        """根据成功率选择突变策略"""
        total_success = sum(self.strategy_success_rates.values())
        if total_success > 0:
            adjusted_weights = {}
            for strategy, base_weight in self.strategy_weights.items():
                success_boost = self.strategy_success_rates.get(strategy, 0.0) / total_success
                adjusted_weights[strategy] = base_weight * (1.0 + success_boost)
        else:
            adjusted_weights = self.strategy_weights
        
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
        
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence

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
        
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
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
        
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence

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
        
        include_cysteine = design_params.get('include_cysteine', True) if design_params else True
        amino_acid_set = AMINO_ACIDS if include_cysteine else AMINO_ACIDS_NO_CYS
        sequence_mask = design_params.get('sequence_mask') if design_params else None
        
        new_sequence = list(sequence)
        
        available_positions = list(range(len(sequence)))
        if sequence_mask:
            mask_clean = sequence_mask.replace('-', '').replace('_', '').replace(' ', '').upper()
            available_positions = [i for i, char in enumerate(mask_clean) if char == 'X']
        
        if not available_positions:
            return sequence

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
        interface1 = resolve_preferred_interface_metric(solution1).get('value') or 0.0
        interface2 = resolve_preferred_interface_metric(solution2).get('value') or 0.0
        plddt1 = solution1.get('binder_avg_plddt', 0)
        plddt2 = solution2.get('binder_avg_plddt', 0)

        return (interface1 >= interface2 and plddt1 >= plddt2) and (interface1 > interface2 or plddt1 > plddt2)
    
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
        
        for obj_func in ['interface_metric', 'binder_avg_plddt']:
            if obj_func == 'interface_metric':
                solutions.sort(key=lambda x: resolve_preferred_interface_metric(x).get('value') or 0.0)
            else:
                solutions.sort(key=lambda x: x.get(obj_func, 0))
            
            if len(solutions) > 2:
                solutions[0]['crowding_distance'] = float('inf')
                solutions[-1]['crowding_distance'] = float('inf')
                
                if obj_func == 'interface_metric':
                    min_value = resolve_preferred_interface_metric(solutions[0]).get('value') or 0.0
                    max_value = resolve_preferred_interface_metric(solutions[-1]).get('value') or 0.0
                else:
                    min_value = solutions[0].get(obj_func, 0)
                    max_value = solutions[-1].get(obj_func, 0)
                obj_range = max_value - min_value
                if obj_range > 0:
                    for i in range(1, len(solutions) - 1):
                        if obj_func == 'interface_metric':
                            next_value = resolve_preferred_interface_metric(solutions[i + 1]).get('value') or 0.0
                            prev_value = resolve_preferred_interface_metric(solutions[i - 1]).get('value') or 0.0
                        else:
                            next_value = solutions[i + 1].get(obj_func, 0)
                            prev_value = solutions[i - 1].get(obj_func, 0)
                        distance = (next_value - prev_value) / obj_range
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

    aa_counts = Counter(sequence)
    total_length = len(sequence)

    hydrophobic = set('AILMFPWYV')
    features['hydrophobic_ratio'] = sum(aa_counts[aa] for aa in hydrophobic if aa in aa_counts) / total_length

    polar = set('NQST')
    features['polar_ratio'] = sum(aa_counts[aa] for aa in polar if aa in aa_counts) / total_length

    charged = set('DEKRHC')
    features['charged_ratio'] = sum(aa_counts[aa] for aa in charged if aa in aa_counts) / total_length

    aromatic = set('FWY')
    features['aromatic_ratio'] = sum(aa_counts[aa] for aa in aromatic if aa in aa_counts) / total_length

    small = set('AGS')
    features['small_ratio'] = sum(aa_counts[aa] for aa in small if aa in aa_counts) / total_length

    features['pro_ratio'] = aa_counts.get('P', 0) / total_length
    features['gly_ratio'] = aa_counts.get('G', 0) / total_length
    features['cys_ratio'] = aa_counts.get('C', 0) / total_length

    return features


def analyze_population_diversity(sequences: List[str]) -> Dict[str, float]:
    """分析群体多样性"""
    if len(sequences) < 2:
        return {'avg_pairwise_similarity': 0.0, 'position_entropy': 0.0}

    similarities = []
    for i in range(len(sequences)):
        for j in range(i + 1, len(sequences)):
            sim = calculate_sequence_similarity(sequences[i], sequences[j])
            similarities.append(sim)
    
    avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0

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
