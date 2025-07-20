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

# --- 初始化日志记录器 ---
# 在此模块中获取的日志记录器将继承在主程序入口处配置的设置
logger = logging.getLogger(__name__)

# --- 核心常量 ---

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"

# --- 糖化学常量 ---

# 定义常见单糖的PDB化学成分词典（CCD）代码及其关键属性。
# 'atom': 用于形成糖苷键的端基碳原子。
# 'type': 该单糖通常参与的糖基化类型（N-连锁或O-连锁）。
# 这个字典对于选择正确的氨基酸附着位点和构建约束至关重要。
MONOSACCHARIDES = {
    # 'type' 是一个列表，因为某些单糖可能参与多种连接类型
    'NAG': {'atom': 'C1', 'type': ['N-linked', 'O-linked']}, # N-连锁的核心，也是O-GlcNAc
    'GAL': {'atom': 'C1', 'type': ['N-linked', 'O-linked']}, # 在N-和O-连锁聚糖中都很常见
    'MAN': {'atom': 'C1', 'type': ['N-linked']},              # 主要与N-连锁核心结构相关
    'FUC': {'atom': 'C1', 'type': ['N-linked', 'O-linked']}, # 在两种类型上都存在常见的岩藻糖基化
    'GLC': {'atom': 'C1', 'type': ['N-linked', 'O-linked']}, # N-连锁前体的一部分
    'SIA': {'atom': 'C2', 'type': ['O-linked']},              # 在O-糖基化中通常连接到Ser/Thr
}

# 定义N-连锁和O-连锁糖基化的有效氨基酸残基及其附着原子。
GLYCOSYLATION_SITES = {
    'N-linked': {'N': 'ND2'}, # 天冬酰胺 (Asparagine)
    'O-linked': {
        'S': 'OG',  # 丝氨酸 (Serine)
        'T': 'OG1'  # 苏氨酸 (Threonine)
    }
}

# BLOSUM62替换矩阵。用于指导氨基酸突变，偏好替换为化学性质相似的氨基酸。
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
    """根据给定的聚糖CCD代码，返回其兼容的氨基酸残基列表。

    该函数通过查询MONOSACCHARIDES字典来确定聚糖的糖基化类型（N-连锁或O-连锁），
    然后返回相应的氨基酸。

    Args:
        glycan_ccd (str): 聚糖的3字母PDB CCD代码 (例如, 'MAN', 'NAG')。

    Returns:
        list: 与该聚糖兼容的有效氨基酸残基列表 (例如, ['N'] 或 ['S', 'T'])。

    Raises:
        KeyError: 如果提供的 glycan_ccd 在 MONOSACCHARIDES 中未定义。
        ValueError: 如果未找到与该聚糖关联的有效糖基化类型。
    """
    if not glycan_ccd or glycan_ccd not in MONOSACCHARIDES:
        raise KeyError(f"Glycan CCD '{glycan_ccd}' is not defined in MONOSACCHARIDES.")

    allowed_types = MONOSACCHARIDES[glycan_ccd]['type']
    valid_residues = []
    if 'N-linked' in allowed_types:
        valid_residues.extend(GLYCOSYLATION_SITES['N-linked'].keys())
    if 'O-linked' in allowed_types:
        valid_residues.extend(GLYCOSYLATION_SITES['O-linked'].keys())
    
    if not valid_residues:
        raise ValueError(f"No valid glycosylation types found for glycan '{glycan_ccd}'.")
        
    return list(set(valid_residues)) # 返回去重后的列表


def generate_random_sequence(length: int, glycosylation_site: int = None, glycan_ccd: str = None) -> str:
    """生成一个随机的氨基酸序列。

    如果指定了糖基化位点和聚糖类型，则会确保在该位置放置一个生物学上
    兼容的残基（例如，为N-连锁聚糖放置'N'，为O-连锁聚糖放置'S'或'T'）。

    Args:
        length (int): 序列的长度。
        glycosylation_site (int, optional): 糖基化位点（0-based索引）。Defaults to None.
        glycan_ccd (str, optional): 聚糖的CCD代码，用于确定兼容的残基。Defaults to None.

    Returns:
        str: 生成的氨基酸序列。
        
    Raises:
        ValueError: 如果 glycosylation_site 索引超出序列长度范围。
    """
    seq = list("".join(random.choice(AMINO_ACIDS) for _ in range(length)))
    if glycosylation_site is not None:
        if 0 <= glycosylation_site < length:
            if glycan_ccd:
                # 获取与指定聚糖兼容的残基
                valid_residues = get_valid_residues_for_glycan(glycan_ccd)
            else:
                # 如果未指定聚糖，则回退到任何有效的糖基化残基
                valid_residues = list(GLYCOSYLATION_SITES['N-linked'].keys()) + list(GLYCOSYLATION_SITES['O-linked'].keys())
            
            seq[glycosylation_site] = random.choice(valid_residues)
        else:
            raise ValueError("glycosylation_site index is out of bounds for the given sequence length.")
    return "".join(seq)


def mutate_sequence(
    sequence: str,
    mutation_rate: float = 0.1,
    plddt_scores: list = None,
    temperature: float = 1.0,
    glycosylation_site: int = None,
    glycan_ccd: str = None
) -> str:
    """对序列进行点突变，突变过程受pLDDT和BLOSUM62矩阵指导。

    - 突变位置优先选择pLDDT分数较低的区域。
    - 氨基酸的替换选择通过带温度的Softmax函数和BLOSUM62矩阵进行加权。
    - 指定的糖基化位点将被保护，确保其残基与指定的聚糖类型兼容。

    Args:
        sequence (str): 原始氨基酸序列。
        mutation_rate (float, optional): 序列中要突变的残基比例。Defaults to 0.1.
        plddt_scores (list, optional): 与序列对应的pLDDT分数列表。Defaults to None.
        temperature (float, optional): Softmax函数的温度因子。较高温度增加随机性。Defaults to 1.0.
        glycosylation_site (int, optional): 要保护的糖基化位点（0-based索引）。Defaults to None.
        glycan_ccd (str, optional): 聚糖的CCD代码，用于验证糖基化位点。Defaults to None.

    Returns:
        str: 突变后的新序列。
    """
    new_sequence = list(sequence)
    num_mutations = max(1, int(len(sequence) * mutation_rate))

    # --- 步骤 1: 选择突变位置 (pLDDT指导) ---
    available_indices = list(range(len(sequence)))
    if glycosylation_site is not None:
        # 防止糖基化位点被随机突变
        available_indices.pop(glycosylation_site)

    if not available_indices:
        logger.warning("No available positions to mutate after excluding the glycosylation site.")
        return sequence

    positions_to_mutate = []
    if plddt_scores and len(plddt_scores) == len(sequence):
        # 根据 (100 - pLDDT) 对位置进行加权，优先选择低置信度区域
        weights = [100.0 - plddt_scores[i] for i in available_indices]
        total_weight = sum(weights)
        if total_weight > 0:
            probabilities = [w / total_weight for w in weights]
            k = min(num_mutations, len(available_indices))
            positions_to_mutate = np.random.choice(available_indices, size=k, replace=False, p=probabilities)
        else:
            # 如果所有pLDDT都为100，则随机选择
            positions_to_mutate = random.sample(available_indices, k=min(num_mutations, len(available_indices)))
    else:
        if plddt_scores:
            logger.warning("pLDDT scores length mismatch or not provided. Falling back to random position selection.")
        # 如果没有提供pLDDT，则随机选择突变位置
        positions_to_mutate = random.sample(available_indices, k=min(num_mutations, len(available_indices)))

    # --- 步骤 2: 选择替换的氨基酸 (BLOSUM62指导) ---
    for pos in positions_to_mutate:
        original_aa = new_sequence[pos]
        substitution_scores = BLOSUM62.get(original_aa, {})

        possible_aas = [aa for aa in AMINO_ACIDS if aa != original_aa]
        if not possible_aas: continue

        scores = [substitution_scores.get(aa, 0) for aa in possible_aas]

        # 使用带温度的Softmax将分数转换为概率
        scores_array = np.array(scores) / temperature
        # 减去最大值以提高数值稳定性
        probabilities = np.exp(scores_array - np.max(scores_array))
        probabilities /= np.sum(probabilities)

        new_aa = np.random.choice(possible_aas, p=probabilities)
        new_sequence[pos] = new_aa
        
    # --- 步骤 3: 确保糖基化位点对于指定的聚糖仍然有效 ---
    if glycosylation_site is not None and glycan_ccd:
        valid_residues = get_valid_residues_for_glycan(glycan_ccd)
        if new_sequence[glycosylation_site] not in valid_residues:
              # 如果突变（或初始序列）导致了无效的位点，则进行修复
              logger.debug(f"Correcting glycosylation site {glycosylation_site} from "
                           f"'{new_sequence[glycosylation_site]}' to a valid residue for '{glycan_ccd}'.")
              new_sequence[glycosylation_site] = random.choice(valid_residues)

    return "".join(new_sequence)


def parse_confidence_metrics(results_path: str, binder_chain_id: str) -> dict:
    """从Boltz预测输出文件中解析关键的置信度指标（如ipTM, pLDDT）。

    此函数设计为健壮的，能够处理文件缺失或解析错误的情况。

    Args:
        results_path (str): 包含预测结果的目录路径。
        binder_chain_id (str): 要计算平均pLDDT的binder链的ID。

    Returns:
        dict: 包含解析出的指标的字典，如果解析失败则返回默认值。
              - 'iptm', 'ptm', 'complex_plddt', 'binder_avg_plddt', 'plddts'
    """
    metrics = {
        'iptm': 0.0, 'ptm': 0.0, 'complex_plddt': 0.0,
        'binder_avg_plddt': 0.0, 'plddts': []
    }
    # --- 解析 confidence.json 文件 ---
    try:
        json_path = next((os.path.join(results_path, f) for f in os.listdir(results_path) if f.startswith('confidence_') and f.endswith('.json')), None)
        if json_path:
            with open(json_path, 'r') as f: data = json.load(f)
            metrics.update({
                'ptm': data.get('ptm', 0.0),
                'complex_plddt': data.get('complex_plddt', 0.0)
            })
            # 优先使用针对特定链对的ipTM（如果存在）
            pair_iptm = data.get('pair_chains_iptm', {})
            chain_ids = list(pair_iptm.keys())
            if len(chain_ids) > 1:
                c1, c2 = chain_ids[0], chain_ids[1]
                metrics['iptm'] = pair_iptm.get(c1, {}).get(c2, 0.0)
            # 如果链对ipTM不存在或为零，则回退到全局ipTM
            if metrics['iptm'] == 0.0:
                metrics['iptm'] = data.get('iptm', 0.0)
    except Exception as e:
        logger.warning(f"Could not parse confidence metrics from JSON in {results_path}. Error: {e}")

    # --- 从CIF文件解析pLDDT分数 ---
    try:
        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
        if cif_files:
            # 优先选择rank_1模型，但如果找不到，则使用任何CIF文件
            rank_1_cif = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
            cif_path = os.path.join(results_path, rank_1_cif)
            with open(cif_path, 'r') as f: lines = f.readlines()

            header, atom_lines, in_loop = [], [], False
            for line in lines:
                s_line = line.strip()
                if s_line.startswith('_atom_site.'):
                    header.append(s_line)
                    in_loop = True
                elif in_loop and (s_line.startswith('ATOM') or s_line.startswith('HETATM')):
                    atom_lines.append(s_line)
                elif in_loop and not s_line:
                    in_loop = False # atom_site_loop块结束

            if header and atom_lines:
                h_map = {name: i for i, name in enumerate(header)}
                # 使用标准的 'label_asym_id'，并回退到 'auth_asym_id'
                chain_col = h_map.get('_atom_site.label_asym_id') or h_map.get('_atom_site.auth_asym_id')
                res_col, bfactor_col = h_map.get('_atom_site.label_seq_id'), h_map.get('_atom_site.B_iso_or_equiv')

                if all(c is not None for c in [chain_col, res_col, bfactor_col]):
                    plddts, last_res_id = [], None
                    for atom_line in atom_lines:
                        fields = atom_line.split()
                        if len(fields) > max(chain_col, res_col, bfactor_col):
                            chain_id = fields[chain_col]
                            res_id = fields[res_col]
                            # 只处理指定链上每个新残基的第一个原子
                            if chain_id == binder_chain_id and res_id != last_res_id:
                                try:
                                    plddts.append(float(fields[bfactor_col]))
                                    last_res_id = res_id
                                except (ValueError, IndexError):
                                    continue # 跳过格式不正确的行
                    if plddts:
                        metrics['plddts'] = plddts
                        metrics['binder_avg_plddt'] = np.mean(plddts)
    except Exception as e:
        logger.warning(f"Error parsing pLDDTs from CIF file in {results_path}. Error: {e}")

    return metrics