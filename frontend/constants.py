
import os
import string

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
