#!/usr/bin/env python3

"""
diversity_selector.py

智能多样性选择器，用于从MMPDB生成的大量候选化合物中
选择具有良好多样性和质量的子集，避免陷入局部最优
"""

import logging
import numpy as np
from typing import List, Dict, Any, Set, Tuple
from dataclasses import dataclass
import random

try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors
    from rdkit import DataStructs
    from rdkit.Chem.Scaffolds import MurckoScaffold
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False

logger = logging.getLogger(__name__)

@dataclass
class CompoundInfo:
    """候选化合物信息"""
    smiles: str
    similarity: float
    properties: Dict[str, float]
    fingerprint: Any = None
    scaffold: str = ""
    
    def __post_init__(self):
        if RDKIT_AVAILABLE:
            try:
                mol = Chem.MolFromSmiles(self.smiles)
                if mol:
                    # 计算分子指纹
                    self.fingerprint = rdMolDescriptors.GetMorganFingerprintAsBitVect(mol, 2, nBits=1024)
                    # 提取骨架
                    self.scaffold = MurckoScaffold.GetScaffoldForMol(mol)
                    if self.scaffold:
                        self.scaffold = Chem.MolToSmiles(self.scaffold)
            except Exception as e:
                logger.debug(f"计算化合物信息失败 {self.smiles}: {e}")

class DiversitySelector:
    """智能多样性选择器"""
    
    def __init__(self, 
                 diversity_weight: float = 0.3,
                 similarity_threshold: float = 0.5,
                 max_similarity_threshold: float = 0.9,
                 strategy: str = "tanimoto_diverse"):
        """
        初始化多样性选择器
        
        Args:
            diversity_weight: 多样性权重 (0-1)
            similarity_threshold: 最小相似性阈值
            max_similarity_threshold: 最大相似性阈值
            strategy: 选择策略
        """
        self.diversity_weight = diversity_weight
        self.similarity_threshold = similarity_threshold
        self.max_similarity_threshold = max_similarity_threshold
        self.strategy = strategy
        
        # 历史化合物记录，用于避免重复
        self.seen_compounds: Set[str] = set()
        self.seen_scaffolds: Set[str] = set()
        
        logger.info(f"多样性选择器初始化: 策略={strategy}, 多样性权重={diversity_weight}")
    
    def add_seen_compounds(self, compounds: List[str]):
        """添加已见过的化合物到历史记录"""
        for smiles in compounds:
            canonical_smiles = self._get_canonical_smiles(smiles)
            self.seen_compounds.add(canonical_smiles)
            
            # 也记录骨架
            scaffold = self._get_scaffold(smiles)
            if scaffold:
                self.seen_scaffolds.add(scaffold)
        
        logger.info(f"已记录 {len(self.seen_compounds)} 个历史化合物")
    
    def select_diverse_candidates(self, 
                                candidates: List[Dict[str, Any]], 
                                target_count: int,
                                parent_smiles: str = "") -> List[Dict[str, Any]]:
        """
        从候选化合物中选择多样性良好的子集
        
        Args:
            candidates: 候选化合物列表
            target_count: 目标选择数量
            parent_smiles: 父化合物SMILES
            
        Returns:
            选择的候选化合物列表
        """
        if not candidates:
            return []
        
        logger.info(f"从 {len(candidates)} 个候选中选择 {target_count} 个多样性化合物")
        
        # 过滤已见过的化合物
        unique_candidates = self._filter_seen_compounds(candidates)
        logger.info(f"过滤重复后剩余 {len(unique_candidates)} 个候选")
        
        if len(unique_candidates) <= target_count:
            return unique_candidates
        
        # 转换为CompoundInfo对象
        compound_infos = []
        for candidate in unique_candidates:
            try:
                info = CompoundInfo(
                    smiles=candidate['smiles'],
                    similarity=candidate.get('similarity', 0.0),
                    properties=candidate.get('properties', {})
                )
                
                # 过滤相似性范围
                if self.similarity_threshold <= info.similarity <= self.max_similarity_threshold:
                    compound_infos.append((info, candidate))
            except Exception as e:
                logger.debug(f"处理候选化合物失败: {e}")
                continue
        
        logger.info(f"相似性过滤后剩余 {len(compound_infos)} 个候选")
        
        if not compound_infos:
            # 如果没有符合相似性要求的，放宽要求
            logger.warning("没有符合相似性要求的候选，放宽限制")
            compound_infos = [(CompoundInfo(c['smiles'], c.get('similarity', 0.0), c.get('properties', {})), c) 
                             for c in unique_candidates]
        
        # 根据策略选择
        if self.strategy == "tanimoto_diverse":
            selected = self._select_by_tanimoto_diversity(compound_infos, target_count, parent_smiles)
        elif self.strategy == "scaffold_diverse":
            selected = self._select_by_scaffold_diversity(compound_infos, target_count)
        elif self.strategy == "property_diverse":
            selected = self._select_by_property_diversity(compound_infos, target_count)
        elif self.strategy == "hybrid":
            selected = self._select_by_hybrid_strategy(compound_infos, target_count, parent_smiles)
        else:
            # 默认使用Tanimoto多样性
            selected = self._select_by_tanimoto_diversity(compound_infos, target_count, parent_smiles)
        
        # 更新历史记录
        selected_smiles = [item[1]['smiles'] for item in selected]
        self.add_seen_compounds(selected_smiles)
        
        result = [item[1] for item in selected]
        logger.info(f"最终选择了 {len(result)} 个多样性候选化合物")
        
        return result
    
    def _filter_seen_compounds(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤已见过的化合物"""
        unique_candidates = []
        
        for candidate in candidates:
            smiles = candidate.get('smiles', '')
            canonical_smiles = self._get_canonical_smiles(smiles)
            
            if canonical_smiles not in self.seen_compounds:
                unique_candidates.append(candidate)
        
        return unique_candidates
    
    def _select_by_tanimoto_diversity(self, 
                                    compound_infos: List[Tuple[CompoundInfo, Dict]], 
                                    target_count: int,
                                    parent_smiles: str = "") -> List[Tuple[CompoundInfo, Dict]]:
        """基于Tanimoto距离的多样性选择"""
        if not RDKIT_AVAILABLE:
            # 随机选择作为备选
            return random.sample(compound_infos, min(target_count, len(compound_infos)))
        
        selected = []
        remaining = compound_infos.copy()
        
        # 首先选择与父化合物最相似但不完全相同的化合物
        if parent_smiles:
            parent_mol = Chem.MolFromSmiles(parent_smiles)
            if parent_mol:
                parent_fp = rdMolDescriptors.GetMorganFingerprintAsBitVect(parent_mol, 2, nBits=1024)
                
                # 按与父化合物的相似性排序，选择最相似的作为种子
                similarities_to_parent = []
                for info, candidate in remaining:
                    if info.fingerprint:
                        sim = DataStructs.TanimotoSimilarity(parent_fp, info.fingerprint)
                        similarities_to_parent.append((sim, info, candidate))
                
                if similarities_to_parent:
                    # 选择相似性适中的化合物作为起点
                    similarities_to_parent.sort(key=lambda x: abs(x[0] - 0.7))  # 目标相似性0.7
                    _, best_info, best_candidate = similarities_to_parent[0]
                    selected.append((best_info, best_candidate))
                    remaining = [(info, cand) for info, cand in remaining if cand['smiles'] != best_candidate['smiles']]
        
        # 如果没有选择到种子，随机选择一个
        if not selected and remaining:
            seed_idx = random.randint(0, len(remaining) - 1)
            selected.append(remaining.pop(seed_idx))
        
        # 迭代选择最多样化的化合物
        while len(selected) < target_count and remaining:
            best_candidate = None
            best_score = -1
            best_idx = -1
            
            for idx, (info, candidate) in enumerate(remaining):
                if not info.fingerprint:
                    continue
                
                # 计算与已选择化合物的最小相似性（多样性分数）
                min_similarity = float('inf')
                for selected_info, _ in selected:
                    if selected_info.fingerprint:
                        sim = DataStructs.TanimotoSimilarity(info.fingerprint, selected_info.fingerprint)
                        min_similarity = min(min_similarity, sim)
                
                # 综合分数：多样性 + 质量
                diversity_score = 1.0 - min_similarity if min_similarity != float('inf') else 1.0
                quality_score = info.similarity  # 与原始化合物的相似性作为质量分数
                
                combined_score = (self.diversity_weight * diversity_score + 
                                (1 - self.diversity_weight) * quality_score)
                
                if combined_score > best_score:
                    best_score = combined_score
                    best_candidate = (info, candidate)
                    best_idx = idx
            
            if best_candidate:
                selected.append(best_candidate)
                remaining.pop(best_idx)
            else:
                break
        
        return selected
    
    def _select_by_scaffold_diversity(self, 
                                    compound_infos: List[Tuple[CompoundInfo, Dict]], 
                                    target_count: int) -> List[Tuple[CompoundInfo, Dict]]:
        """基于骨架多样性的选择"""
        if not RDKIT_AVAILABLE:
            return random.sample(compound_infos, min(target_count, len(compound_infos)))
        
        # 按骨架分组
        scaffold_groups = {}
        for info, candidate in compound_infos:
            scaffold = info.scaffold
            if scaffold not in scaffold_groups:
                scaffold_groups[scaffold] = []
            scaffold_groups[scaffold].append((info, candidate))
        
        selected = []
        
        # 从每个骨架组中选择最佳化合物
        for scaffold, group in scaffold_groups.items():
            if len(selected) >= target_count:
                break
            
            # 按相似性排序，选择最佳的
            group.sort(key=lambda x: x[0].similarity, reverse=True)
            selected.append(group[0])
        
        # 如果还需要更多化合物，从剩余的选择
        while len(selected) < target_count and len(selected) < len(compound_infos):
            remaining = [item for item in compound_infos if item not in selected]
            if not remaining:
                break
            
            # 选择相似性最高的剩余化合物
            remaining.sort(key=lambda x: x[0].similarity, reverse=True)
            selected.append(remaining[0])
        
        return selected
    
    def _select_by_property_diversity(self, 
                                    compound_infos: List[Tuple[CompoundInfo, Dict]], 
                                    target_count: int) -> List[Tuple[CompoundInfo, Dict]]:
        """基于分子属性多样性的选择"""
        # 提取属性向量
        property_vectors = []
        valid_compounds = []
        
        for info, candidate in compound_infos:
            if info.properties:
                props = info.properties
                # 使用常见的药物样属性
                vector = [
                    props.get('molecular_weight', 0),
                    props.get('logp', 0),
                    props.get('hbd', 0),
                    props.get('hba', 0),
                    props.get('tpsa', 0),
                    props.get('rotatable_bonds', 0)
                ]
                property_vectors.append(vector)
                valid_compounds.append((info, candidate))
        
        if not property_vectors:
            return random.sample(compound_infos, min(target_count, len(compound_infos)))
        
        # 标准化属性向量
        property_matrix = np.array(property_vectors)
        if property_matrix.std(axis=0).sum() > 0:  # 避免除零
            property_matrix = (property_matrix - property_matrix.mean(axis=0)) / (property_matrix.std(axis=0) + 1e-8)
        
        selected = []
        remaining_indices = list(range(len(valid_compounds)))
        
        # 随机选择第一个
        if remaining_indices:
            first_idx = random.choice(remaining_indices)
            selected.append(valid_compounds[first_idx])
            remaining_indices.remove(first_idx)
        
        # 迭代选择与已选择化合物在属性空间中距离最远的
        while len(selected) < target_count and remaining_indices:
            best_idx = -1
            best_distance = -1
            
            for idx in remaining_indices:
                min_distance = float('inf')
                
                for selected_info, _ in selected:
                    selected_idx = valid_compounds.index((selected_info, _))
                    distance = np.linalg.norm(property_matrix[idx] - property_matrix[selected_idx])
                    min_distance = min(min_distance, distance)
                
                if min_distance > best_distance:
                    best_distance = min_distance
                    best_idx = idx
            
            if best_idx >= 0:
                selected.append(valid_compounds[best_idx])
                remaining_indices.remove(best_idx)
            else:
                break
        
        return selected
    
    def _select_by_hybrid_strategy(self, 
                                 compound_infos: List[Tuple[CompoundInfo, Dict]], 
                                 target_count: int,
                                 parent_smiles: str = "") -> List[Tuple[CompoundInfo, Dict]]:
        """混合策略选择"""
        # 分配不同策略的比例
        tanimoto_count = max(1, target_count // 2)
        scaffold_count = max(1, target_count // 3)
        property_count = target_count - tanimoto_count - scaffold_count
        
        selected = []
        used_compounds = set()
        
        # Tanimoto多样性选择
        tanimoto_selected = self._select_by_tanimoto_diversity(compound_infos, tanimoto_count, parent_smiles)
        for item in tanimoto_selected:
            if item[1]['smiles'] not in used_compounds:
                selected.append(item)
                used_compounds.add(item[1]['smiles'])
        
        # 骨架多样性选择（从剩余候选中）
        remaining_compounds = [(info, cand) for info, cand in compound_infos 
                             if cand['smiles'] not in used_compounds]
        
        if remaining_compounds and len(selected) < target_count:
            scaffold_selected = self._select_by_scaffold_diversity(remaining_compounds, scaffold_count)
            for item in scaffold_selected:
                if len(selected) >= target_count:
                    break
                selected.append(item)
                used_compounds.add(item[1]['smiles'])
        
        # 属性多样性选择（从剩余候选中）
        remaining_compounds = [(info, cand) for info, cand in compound_infos 
                             if cand['smiles'] not in used_compounds]
        
        if remaining_compounds and len(selected) < target_count:
            property_selected = self._select_by_property_diversity(remaining_compounds, property_count)
            for item in property_selected:
                if len(selected) >= target_count:
                    break
                selected.append(item)
                used_compounds.add(item[1]['smiles'])
        
        return selected
    
    def _get_canonical_smiles(self, smiles: str) -> str:
        """获取规范化SMILES"""
        if not RDKIT_AVAILABLE:
            return smiles
        
        try:
            mol = Chem.MolFromSmiles(smiles)
            if mol:
                return Chem.MolToSmiles(mol, canonical=True)
        except:
            pass
        
        return smiles
    
    def _get_scaffold(self, smiles: str) -> str:
        """获取分子骨架"""
        if not RDKIT_AVAILABLE:
            return ""
        
        try:
            mol = Chem.MolFromSmiles(smiles)
            if mol:
                scaffold_mol = MurckoScaffold.GetScaffoldForMol(mol)
                if scaffold_mol:
                    return Chem.MolToSmiles(scaffold_mol)
        except:
            pass
        
        return ""
    
    def get_diversity_statistics(self) -> Dict[str, Any]:
        """获取多样性统计信息"""
        return {
            'total_seen_compounds': len(self.seen_compounds),
            'unique_scaffolds': len(self.seen_scaffolds),
            'diversity_weight': self.diversity_weight,
            'similarity_range': f"{self.similarity_threshold} - {self.max_similarity_threshold}",
            'selection_strategy': self.strategy
        }
