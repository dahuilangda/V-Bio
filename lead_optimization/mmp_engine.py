# /data/boltz_webui/lead_optimization/mmp_engine_simple.py

"""
简化的MMP引擎 - 只使用mmpdb命令行工具
"""

import os
import sqlite3
import logging
import subprocess
import tempfile
from typing import List, Dict, Any, Tuple, Optional, Set
import sys
import shutil
import importlib.util
from pathlib import Path
import warnings

# 禁用RDKit deprecation警告
from rdkit import RDLogger
RDLogger.DisableLog('rdApp.warning')
warnings.filterwarnings('ignore', message='.*please use MorganGenerator.*')

logger = logging.getLogger(__name__)

def _resolve_mmpdb_cmd() -> Optional[List[str]]:
    """Resolve mmpdb CLI command with a module fallback."""
    if shutil.which('mmpdb'):
        return ['mmpdb']
    if importlib.util.find_spec('mmpdb') is not None:
        return [sys.executable, '-m', 'mmpdb']
    return None

try:
    # 检查mmpdb命令行工具
    MMPDB_CMD = _resolve_mmpdb_cmd()
    HAS_MMPDB = MMPDB_CMD is not None
    if HAS_MMPDB:
        logger.info("已找到 mmpdb 命令行工具")
    else:
        logger.warning("mmpdb 命令行工具未找到")
    
    from rdkit import Chem
    from rdkit.Chem import Descriptors, Crippen, Lipinski
    from rdkit.Chem.Scaffolds import MurckoScaffold
    from rdkit.Chem import rdMolDescriptors, AllChem
    HAS_RDKIT = True
except ImportError:
    HAS_RDKIT = False
    HAS_MMPDB = False

from exceptions import MMPDatabaseError, InvalidCompoundError

logger = logging.getLogger(__name__)

class MMPEngine:
    """
    简化的MMP引擎 - 只使用mmpdb命令行工具
    """
    
    def __init__(self, config):
        if not HAS_RDKIT:
            raise MMPDatabaseError("RDKit is required but not installed")
        
        if not HAS_MMPDB:
            logger.warning("mmpdb命令行工具不可用")
            
        self.config = config
        self.db_path = getattr(config, 'database_path', None)
        
        # Validate database exists if path provided
        if self.db_path and not os.path.exists(self.db_path):
            logger.warning(f"MMP数据库未找到: {self.db_path}")
            self.db_path = None
        
        logger.info(f"MMP引擎已初始化，数据库: {self.db_path or '无数据库'}")
    
    def scaffold_hopping(self, 
                        target_smiles: str,
                        max_candidates: int = 100,
                        similarity_threshold: float = 0.3) -> List[Dict[str, Any]]:
        """
        使用mmpdb命令行工具进行骨架跃迁
        """
        if not HAS_MMPDB or not self.db_path:
            logger.warning("mmpdb不可用或无数据库，无法进行骨架跃迁")
            return []
        
        try:
            candidates = query_mmpdb_command_line(
                target_smiles, 
                self.db_path, 
                max_candidates
            )
            
            # 过滤相似度和同位素
            filtered_candidates = []
            for candidate in candidates:
                # 检查相似性阈值
                if candidate.get('similarity', 0) >= similarity_threshold:
                    # 过滤同位素标记的化合物
                    if not _contains_isotopes(candidate['smiles']):
                        filtered_candidates.append(candidate)
            
            logger.info(f"骨架跃迁生成了 {len(filtered_candidates)} 个候选物 (相似性>{similarity_threshold:.2f}, 无同位素)")
            return filtered_candidates
            
        except Exception as e:
            logger.error(f"骨架跃迁失败: {e}")
            return []
    
    def fragment_replacement(self, 
                           target_smiles: str,
                           max_candidates: int = 100,
                           similarity_threshold: float = 0.4) -> List[Dict[str, Any]]:
        """
        使用mmpdb命令行工具进行片段替换
        """
        # 片段替换和骨架跃迁使用相同的mmpdb功能，但使用更保守的相似性阈值
        return self.scaffold_hopping(target_smiles, max_candidates, similarity_threshold)


def query_mmpdb_command_line(target_smiles: str, 
                            database_path: str, 
                            max_results: int = 50) -> List[Dict[str, Any]]:
    """使用mmpdb命令行工具查询变换"""
    candidates = []
    
    try:
        cmd_base = _resolve_mmpdb_cmd()
        if not cmd_base:
            logger.warning("mmpdb命令行工具不可用")
            return []
        
        logger.info(f"使用mmpdb命令行查询 {target_smiles}")
        
        # 运行mmpdb transform命令
        cmd = cmd_base + [
            'transform',
            database_path,
            '--smiles', target_smiles,
            '--min-pairs', '1'
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0 and result.stdout:
            lines = result.stdout.strip().split('\n')
            logger.info(f"mmpdb返回了 {len(lines)} 行结果")
            
            for line_num, line in enumerate(lines):
                if line_num == 0:  # 跳过标题行 "ID      SMILES"
                    continue
                
                line = line.strip()
                if '\t' in line:
                    parts = line.split('\t')
                elif '      ' in line:  # 多个空格分隔
                    parts = line.split()
                else:
                    continue
                    
                if len(parts) >= 2:
                    try:
                        # mmpdb输出格式: ID    SMILES
                        mmp_id = parts[0].strip()
                        transformed_smiles = parts[1].strip()
                        
                        if transformed_smiles and transformed_smiles != target_smiles:
                            # 过滤掉同位素标记的化合物
                            if _contains_isotopes(transformed_smiles):
                                continue
                            
                            # 计算相似度
                            similarity = calculate_tanimoto_similarity(target_smiles, transformed_smiles)
                            
                            candidates.append({
                                'smiles': transformed_smiles,
                                'generation_method': 'mmpdb_transform',
                                'parent_smiles': target_smiles,
                                'transformation_description': f"mmpdb转换 (ID: {mmp_id})",
                                'transformation_rule': f"mmp_rule_{mmp_id}",
                                'similarity': similarity,
                                'properties': calculate_basic_properties(transformed_smiles)
                            })
                    except Exception as e:
                        logger.debug(f"解析mmpdb结果行失败: {e}")
                        continue
        else:
            logger.warning(f"mmpdb命令失败: {result.stderr}")
        
    except subprocess.TimeoutExpired:
        logger.warning("mmpdb命令超时")
    except Exception as e:
        logger.error(f"mmpdb命令行查询失败: {e}")
    
    logger.info(f"mmpdb生成了 {len(candidates)} 个候选化合物")
    return candidates


def calculate_tanimoto_similarity(smiles1: str, smiles2: str) -> float:
    """计算两个分子的Tanimoto相似度"""
    try:
        mol1 = Chem.MolFromSmiles(smiles1)
        mol2 = Chem.MolFromSmiles(smiles2)
        
        if mol1 and mol2:
            fp1 = AllChem.GetMorganFingerprintAsBitVect(mol1, 2)
            fp2 = AllChem.GetMorganFingerprintAsBitVect(mol2, 2)
            
            from rdkit import DataStructs
            return DataStructs.TanimotoSimilarity(fp1, fp2)
            
    except Exception:
        pass
    
    return 0.0


def _contains_isotopes(smiles: str) -> bool:
    """检查SMILES字符串是否包含同位素标记"""
    import re
    # 检查是否包含同位素标记，如 [11CH3], [2H], [13C] 等
    isotope_pattern = r'\[\d+[A-Z]'
    return bool(re.search(isotope_pattern, smiles))


def calculate_basic_properties(smiles: str) -> Dict[str, float]:
    """计算基本分子属性"""
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol:
            return {
                'molecular_weight': Descriptors.MolWt(mol),
                'logp': Crippen.MolLogP(mol),
                'hbd': Descriptors.NumHDonors(mol),
                'hba': Descriptors.NumHAcceptors(mol),
                'tpsa': Descriptors.TPSA(mol),
                'rotatable_bonds': Descriptors.NumRotatableBonds(mol),
                'aromatic_rings': Descriptors.NumAromaticRings(mol),
                'heavy_atoms': mol.GetNumHeavyAtoms()
            }
    except Exception:
        pass
    
    return {}


def generate_mmp_candidates_with_mmpdblib(target_smiles: str, 
                                        database_path: str = None,
                                        max_candidates: int = 100,
                                        min_similarity: float = 0.3) -> List[Dict[str, Any]]:
    """
    使用纯mmpdb命令行工具生成候选物
    """
    try:
        logger.info(f"使用mmpdb为 {target_smiles} 生成候选物")
        
        # 验证输入分子
        target_mol = Chem.MolFromSmiles(target_smiles)
        if not target_mol:
            logger.error(f"无效的目标SMILES: {target_smiles}")
            return []
        
        # 只使用mmpdb命令行工具
        if not HAS_MMPDB:
            logger.warning("mmpdb命令行工具不可用")
            return []
        
        if not database_path or not os.path.exists(database_path):
            logger.warning("数据库不存在")
            return []
        
        candidates = query_mmpdb_command_line(target_smiles, database_path, max_candidates)
        
        # 过滤相似度
        filtered_candidates = []
        for candidate in candidates:
            if candidate.get('similarity', 0) >= min_similarity:
                filtered_candidates.append(candidate)
        
        # 按相似度排序
        filtered_candidates.sort(key=lambda x: x.get('similarity', 0), reverse=True)
        
        logger.info(f"最终生成了 {len(filtered_candidates)} 个候选化合物")
        return filtered_candidates[:max_candidates]
        
    except Exception as e:
        logger.error(f"候选生成失败: {e}")
        return []


def create_mmp_database(compounds_file: str, output_db: str, max_heavy_atoms: int = 50):
    """
    使用mmpdb命令行工具创建数据库
    """
    try:
        if not HAS_MMPDB:
            raise MMPDatabaseError("mmpdb命令行工具不可用")
        
        fragment_cmd = [
            "mmpdb", "fragment", 
            "--smiles", compounds_file,
            "--output", f"{output_db}.fragments",
            "--max-heavyatoms", str(max_heavy_atoms)
        ]
        
        index_cmd = [
            "mmpdb", "index",
            f"{output_db}.fragments",
            "--output", output_db
        ]
        
        logger.info("开始构建MMP数据库...")
        
        # Fragment compounds
        result = subprocess.run(fragment_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise MMPDatabaseError(f"化合物片段化失败: {result.stderr}")
        
        # Build index
        result = subprocess.run(index_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise MMPDatabaseError(f"数据库索引构建失败: {result.stderr}")
        
        logger.info(f"MMP数据库构建成功: {output_db}")
        
        # Clean up fragments file
        fragments_file = f"{output_db}.fragments"
        if os.path.exists(fragments_file):
            os.remove(fragments_file)
            
    except Exception as e:
        raise MMPDatabaseError(f"MMP数据库创建失败: {e}")
