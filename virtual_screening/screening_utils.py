# /Boltz-WebUI/virtual_screening/screening_utils.py

"""
screening_utils.py

虚拟筛选的实用工具函数和类：
1. 数据格式转换
2. 结果后处理
3. 库管理工具
4. 配置管理
"""

import os
import csv
import json
import yaml
import logging
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Tuple, Union
from pathlib import Path
import tempfile
import shutil

# 尝试导入RDKit
try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors
    from rdkit.Chem.Fingerprints import FingerprintMols
    from rdkit.DataStructs import TanimotoSimilarity
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False

from molecule_library import Molecule, MoleculeLibrary
from screening_engine import ScreeningResult

logger = logging.getLogger(__name__)

class FormatConverter:
    """格式转换工具"""
    
    @staticmethod
    def smiles_to_sdf(smiles_file: str, output_file: str, id_column: str = "molecule_id", 
                     smiles_column: str = "smiles") -> bool:
        """将SMILES文件转换为SDF格式"""
        if not RDKIT_AVAILABLE:
            logger.error("格式转换需要RDKit库")
            return False
        
        try:
            # 读取SMILES文件
            df = pd.read_csv(smiles_file)
            
            if id_column not in df.columns or smiles_column not in df.columns:
                logger.error(f"SMILES文件缺少必需的列: {id_column}, {smiles_column}")
                return False
            
            # 创建SDF写入器
            writer = Chem.SDWriter(output_file)
            
            converted_count = 0
            for _, row in df.iterrows():
                mol_id = str(row[id_column])
                smiles = str(row[smiles_column])
                
                mol = Chem.MolFromSmiles(smiles)
                if mol is not None:
                    mol.SetProp("_Name", mol_id)
                    # 添加其他属性
                    for col in df.columns:
                        if col not in [id_column, smiles_column]:
                            mol.SetProp(col, str(row[col]))
                    
                    writer.write(mol)
                    converted_count += 1
                else:
                    logger.warning(f"无效的SMILES: {mol_id} - {smiles}")
            
            writer.close()
            logger.info(f"成功转换 {converted_count} 个分子到SDF格式")
            return True
            
        except Exception as e:
            logger.error(f"SMILES到SDF转换失败: {e}")
            return False
    
    @staticmethod
    def fasta_to_csv(fasta_file: str, output_file: str) -> bool:
        """将FASTA格式转换为CSV格式"""
        try:
            sequences = []
            current_id = None
            current_seq = ""
            
            with open(fasta_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('>'):
                        if current_id and current_seq:
                            sequences.append({
                                'peptide_id': current_id,
                                'sequence': current_seq,
                                'length': len(current_seq)
                            })
                        current_id = line[1:].split()[0]
                        current_seq = ""
                    else:
                        current_seq += line.upper()
                
                # 处理最后一个序列
                if current_id and current_seq:
                    sequences.append({
                        'peptide_id': current_id,
                        'sequence': current_seq,
                        'length': len(current_seq)
                    })
            
            # 保存为CSV
            df = pd.DataFrame(sequences)
            df.to_csv(output_file, index=False)
            
            logger.info(f"成功转换 {len(sequences)} 个多肽序列到CSV格式")
            return True
            
        except Exception as e:
            logger.error(f"FASTA到CSV转换失败: {e}")
            return False
    
    @staticmethod
    def results_to_sdf(results: List[ScreeningResult], output_file: str) -> bool:
        """将小分子筛选结果导出为SDF格式"""
        if not RDKIT_AVAILABLE:
            logger.error("导出SDF需要RDKit库")
            return False
        
        try:
            writer = Chem.SDWriter(output_file)
            exported_count = 0
            
            for result in results:
                if result.mol_type == "small_molecule":
                    mol = Chem.MolFromSmiles(result.sequence)
                    if mol is not None:
                        # 设置分子名称
                        mol.SetProp("_Name", result.molecule_id)
                        
                        # 添加筛选结果属性
                        mol.SetProp("rank", str(result.rank))
                        mol.SetProp("combined_score", f"{result.combined_score:.4f}")
                        mol.SetProp("binding_score", f"{result.binding_score:.4f}")
                        mol.SetProp("structural_score", f"{result.structural_score:.4f}")
                        mol.SetProp("confidence_score", f"{result.confidence_score:.4f}")
                        
                        # 添加其他属性
                        if result.properties:
                            for key, value in result.properties.items():
                                mol.SetProp(str(key), str(value))
                        
                        writer.write(mol)
                        exported_count += 1
            
            writer.close()
            logger.info(f"成功导出 {exported_count} 个小分子结果到SDF格式")
            return True
            
        except Exception as e:
            logger.error(f"结果导出SDF失败: {e}")
            return False

class SimilarityAnalyzer:
    """分子相似性分析工具"""
    
    def __init__(self):
        if not RDKIT_AVAILABLE:
            raise ImportError("相似性分析需要RDKit库")
    
    def calculate_tanimoto_similarity(self, smiles1: str, smiles2: str) -> float:
        """计算两个分子的Tanimoto相似性"""
        try:
            mol1 = Chem.MolFromSmiles(smiles1)
            mol2 = Chem.MolFromSmiles(smiles2)
            
            if mol1 is None or mol2 is None:
                return 0.0
            
            fp1 = FingerprintMols.FingerprintMol(mol1)
            fp2 = FingerprintMols.FingerprintMol(mol2)
            
            return TanimotoSimilarity(fp1, fp2)
            
        except Exception as e:
            logger.warning(f"相似性计算失败: {e}")
            return 0.0
    
    def find_similar_molecules(self, query_smiles: str, molecule_library: List[Molecule], 
                             threshold: float = 0.7) -> List[Tuple[Molecule, float]]:
        """在分子库中查找相似分子"""
        similar_molecules = []
        
        for mol in molecule_library:
            if mol.mol_type == "small_molecule":
                similarity = self.calculate_tanimoto_similarity(query_smiles, mol.sequence)
                if similarity >= threshold:
                    similar_molecules.append((mol, similarity))
        
        # 按相似性排序
        similar_molecules.sort(key=lambda x: x[1], reverse=True)
        return similar_molecules
    
    def cluster_molecules(self, molecules: List[Molecule], threshold: float = 0.8) -> List[List[Molecule]]:
        """基于相似性对分子进行聚类"""
        # 简单的层次聚类实现
        clusters = []
        unassigned = molecules.copy()
        
        while unassigned:
            # 选择第一个分子作为聚类中心
            center = unassigned.pop(0)
            cluster = [center]
            
            # 查找相似分子
            to_remove = []
            for i, mol in enumerate(unassigned):
                if mol.mol_type == "small_molecule" and center.mol_type == "small_molecule":
                    similarity = self.calculate_tanimoto_similarity(center.sequence, mol.sequence)
                    if similarity >= threshold:
                        cluster.append(mol)
                        to_remove.append(i)
            
            # 移除已分配的分子
            for i in reversed(to_remove):
                unassigned.pop(i)
            
            clusters.append(cluster)
        
        return clusters

class LibraryFilter:
    """分子库过滤工具"""
    
    @staticmethod
    def filter_by_drug_likeness(molecules: List[Molecule]) -> List[Molecule]:
        """根据药物类似性过滤分子"""
        if not RDKIT_AVAILABLE:
            logger.warning("药物类似性过滤需要RDKit库")
            return molecules
        
        filtered = []
        for mol in molecules:
            if mol.mol_type == "small_molecule":
                try:
                    rdkit_mol = Chem.MolFromSmiles(mol.sequence)
                    if rdkit_mol is not None:
                        # Lipinski规则检查
                        mw = Descriptors.MolWt(rdkit_mol)
                        logp = Descriptors.MolLogP(rdkit_mol)
                        hbd = Descriptors.NumHDonors(rdkit_mol)
                        hba = Descriptors.NumHAcceptors(rdkit_mol)
                        
                        violations = 0
                        if mw > 500: violations += 1
                        if logp > 5: violations += 1
                        if hbd > 5: violations += 1
                        if hba > 10: violations += 1
                        
                        if violations <= 1:  # 允许一个违反
                            filtered.append(mol)
                except Exception as e:
                    logger.warning(f"药物类似性检查失败 {mol.id}: {e}")
            else:
                # 多肽直接通过
                filtered.append(mol)
        
        logger.info(f"药物类似性过滤: {len(molecules)} -> {len(filtered)}")
        return filtered
    
    @staticmethod
    def filter_by_molecular_weight(molecules: List[Molecule], min_mw: float = 100, 
                                 max_mw: float = 1000) -> List[Molecule]:
        """根据分子量过滤分子"""
        filtered = []
        for mol in molecules:
            if min_mw <= mol.molecular_weight <= max_mw:
                filtered.append(mol)
        
        logger.info(f"分子量过滤 ({min_mw}-{max_mw}): {len(molecules)} -> {len(filtered)}")
        return filtered
    
    @staticmethod
    def filter_by_sequence_length(molecules: List[Molecule], min_length: int = 5, 
                                max_length: int = 50) -> List[Molecule]:
        """根据序列长度过滤分子"""
        filtered = []
        for mol in molecules:
            seq_length = len(mol.sequence)
            if min_length <= seq_length <= max_length:
                filtered.append(mol)
        
        logger.info(f"序列长度过滤 ({min_length}-{max_length}): {len(molecules)} -> {len(filtered)}")
        return filtered
    
    @staticmethod
    def remove_duplicates(molecules: List[Molecule]) -> List[Molecule]:
        """移除重复分子"""
        seen_sequences = set()
        unique_molecules = []
        
        for mol in molecules:
            if mol.sequence not in seen_sequences:
                seen_sequences.add(mol.sequence)
                unique_molecules.append(mol)
        
        logger.info(f"去重: {len(molecules)} -> {len(unique_molecules)}")
        return unique_molecules

class ResultPostProcessor:
    """结果后处理器"""
    
    @staticmethod
    def merge_results(result_files: List[str], output_file: str) -> bool:
        """合并多个结果文件"""
        try:
            all_results = []
            
            for file_path in result_files:
                df = pd.read_csv(file_path)
                all_results.append(df)
            
            # 合并所有结果
            merged_df = pd.concat(all_results, ignore_index=True)
            
            # 重新排序
            merged_df = merged_df.sort_values('combined_score', ascending=False)
            merged_df['rank'] = range(1, len(merged_df) + 1)
            
            # 保存合并结果
            merged_df.to_csv(output_file, index=False)
            
            logger.info(f"成功合并 {len(result_files)} 个结果文件，共 {len(merged_df)} 个结果")
            return True
            
        except Exception as e:
            logger.error(f"合并结果失败: {e}")
            return False
    
    @staticmethod
    def filter_results_by_score(input_file: str, output_file: str, 
                              min_score: float, score_column: str = "combined_score") -> bool:
        """根据评分过滤结果"""
        try:
            df = pd.read_csv(input_file)
            
            if score_column not in df.columns:
                logger.error(f"结果文件中没有找到评分列: {score_column}")
                return False
            
            # 过滤结果
            filtered_df = df[df[score_column] >= min_score]
            
            # 重新排名
            filtered_df = filtered_df.sort_values(score_column, ascending=False)
            filtered_df['rank'] = range(1, len(filtered_df) + 1)
            
            # 保存过滤结果
            filtered_df.to_csv(output_file, index=False)
            
            logger.info(f"评分过滤 (>= {min_score}): {len(df)} -> {len(filtered_df)}")
            return True
            
        except Exception as e:
            logger.error(f"结果过滤失败: {e}")
            return False
    
    @staticmethod
    def extract_top_molecules(input_file: str, output_file: str, top_n: int = 100) -> bool:
        """提取顶部分子"""
        try:
            df = pd.read_csv(input_file)
            
            # 取前N个结果
            top_df = df.head(top_n)
            
            # 保存结果
            top_df.to_csv(output_file, index=False)
            
            logger.info(f"提取顶部 {min(top_n, len(df))} 个分子")
            return True
            
        except Exception as e:
            logger.error(f"提取顶部分子失败: {e}")
            return False

class ConfigManager:
    """配置管理器"""
    
    @staticmethod
    def load_config(config_file: str) -> Dict[str, Any]:
        """从文件加载配置"""
        try:
            with open(config_file, 'r') as f:
                if config_file.endswith('.yaml') or config_file.endswith('.yml'):
                    config = yaml.safe_load(f)
                elif config_file.endswith('.json'):
                    config = json.load(f)
                else:
                    raise ValueError(f"不支持的配置文件格式: {config_file}")
            
            logger.info(f"成功加载配置文件: {config_file}")
            return config
            
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return {}
    
    @staticmethod
    def save_config(config: Dict[str, Any], output_file: str) -> bool:
        """保存配置到文件"""
        try:
            with open(output_file, 'w') as f:
                if output_file.endswith('.yaml') or output_file.endswith('.yml'):
                    yaml.dump(config, f, default_flow_style=False, indent=2)
                elif output_file.endswith('.json'):
                    json.dump(config, f, indent=2, ensure_ascii=False)
                else:
                    raise ValueError(f"不支持的配置文件格式: {output_file}")
            
            logger.info(f"配置已保存到: {output_file}")
            return True
            
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False
    
    @staticmethod
    def create_default_config() -> Dict[str, Any]:
        """创建默认配置"""
        return {
            "screening_parameters": {
                "batch_size": 50,
                "max_workers": 4,
                "timeout": 300,
                "retry_attempts": 3,
                "use_msa_server": False,
                "priority": "normal"
            },
            "scoring_weights": {
                "binding_affinity": 0.6,
                "structural_stability": 0.2,
                "confidence": 0.2
            },
            "filters": {
                "min_binding_score": 0.0,
                "max_molecular_weight": 1000,
                "min_molecular_weight": 100,
                "drug_likeness": True
            },
            "output_settings": {
                "top_n": 100,
                "save_structures": True,
                "generate_plots": True,
                "export_sdf": False
            }
        }

class BatchProcessor:
    """批量处理工具"""
    
    def __init__(self, temp_dir: str = None):
        self.temp_dir = temp_dir or tempfile.mkdtemp(prefix="screening_batch_")
        os.makedirs(self.temp_dir, exist_ok=True)
    
    def split_large_library(self, library_path: str, chunk_size: int = 1000) -> List[str]:
        """分割大型分子库"""
        try:
            from molecule_library import LibraryProcessor
            
            # 创建分子库
            library = LibraryProcessor.create_library(library_path)
            if not library.load_library():
                logger.error("加载分子库失败")
                return []
            
            # 分割库
            chunk_files = LibraryProcessor.split_library(library, chunk_size, self.temp_dir)
            return chunk_files
            
        except Exception as e:
            logger.error(f"分割分子库失败: {e}")
            return []
    
    def process_chunks_parallel(self, chunk_files: List[str], process_func, max_workers: int = 4):
        """并行处理分块"""
        import concurrent.futures
        
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_chunk = {executor.submit(process_func, chunk): chunk for chunk in chunk_files}
            
            for future in concurrent.futures.as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                try:
                    result = future.result()
                    results.append(result)
                    logger.info(f"分块处理完成: {chunk}")
                except Exception as e:
                    logger.error(f"分块处理失败 {chunk}: {e}")
        
        return results
    
    def cleanup(self):
        """清理临时文件"""
        try:
            shutil.rmtree(self.temp_dir)
            logger.info(f"清理临时目录: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"清理临时目录失败: {e}")

def validate_library_format(library_path: str) -> Tuple[bool, str]:
    """验证分子库文件格式"""
    if not os.path.exists(library_path):
        return False, "文件不存在"
    
    ext = Path(library_path).suffix.lower()
    
    try:
        if ext in ['.fasta', '.fa', '.fas']:
            # 验证FASTA格式
            with open(library_path, 'r') as f:
                lines = f.readlines()
                if not any(line.startswith('>') for line in lines):
                    return False, "FASTA文件格式错误：缺少序列标识符"
        
        elif ext in ['.csv']:
            # 验证CSV格式
            df = pd.read_csv(library_path, nrows=5)
            if df.empty:
                return False, "CSV文件为空"
            
            # 检查必需的列
            required_cols = ['molecule_id', 'smiles'] if 'smiles' in df.columns else ['peptide_id', 'sequence']
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                return False, f"CSV文件缺少必需的列: {missing_cols}"
        
        elif ext in ['.sdf', '.mol']:
            if RDKIT_AVAILABLE:
                # 验证SDF格式
                suppl = Chem.SDMolSupplier(library_path)
                mol_count = 0
                for mol in suppl:
                    if mol is not None:
                        mol_count += 1
                    if mol_count >= 5:  # 只检查前5个分子
                        break
                if mol_count == 0:
                    return False, "SDF文件中没有有效的分子"
        
        elif ext in ['.smi', '.smiles']:
            # 验证SMILES格式
            with open(library_path, 'r') as f:
                lines = f.readlines()[:5]  # 只检查前5行
                for line in lines:
                    if line.strip() and not line.startswith('#'):
                        smiles = line.strip().split()[0]
                        if RDKIT_AVAILABLE:
                            mol = Chem.MolFromSmiles(smiles)
                            if mol is None:
                                return False, f"无效的SMILES: {smiles}"
        
        else:
            return False, f"不支持的文件格式: {ext}"
        
        return True, "格式验证通过"
        
    except Exception as e:
        return False, f"文件读取错误: {e}"

def estimate_screening_time(num_molecules: int, batch_size: int = 50, 
                          avg_prediction_time: float = 60) -> Dict[str, float]:
    """估算筛选时间"""
    num_batches = (num_molecules + batch_size - 1) // batch_size
    
    # 估算时间（秒）
    estimated_time = num_batches * avg_prediction_time
    
    return {
        "molecules": num_molecules,
        "batches": num_batches,
        "estimated_time_seconds": estimated_time,
        "estimated_time_minutes": estimated_time / 60,
        "estimated_time_hours": estimated_time / 3600
    }
