#!/usr/bin/env python3
"""
小分子优化进化算法模块
"""

import random
import logging
from typing import List, Dict, Tuple, Optional
from rdkit import Chem
from rdkit.Chem import Descriptors
import numpy as np

logger = logging.getLogger(__name__)

class MolecularEvolutionEngine:
    """遗传算法"""
    
    def __init__(self, 
                 diversity_weight: float = 0.3,
                 fitness_weight: float = 0.7,
                 mutation_rate: float = 0.1,
                 crossover_rate: float = 0.3):
        """
        初始化分子进化引擎
        
        Args:
            diversity_weight: 多样性权重（防止过度收敛）
            fitness_weight: 适应性权重（优化目标函数）
            mutation_rate: 突变率
            crossover_rate: 交叉率
        """
        self.diversity_weight = diversity_weight
        self.fitness_weight = fitness_weight
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        
        logger.info(f"分子进化引擎初始化 - 多样性权重: {diversity_weight}, 适应性权重: {fitness_weight}")
    
    def select_parents_for_next_generation(self,
                                         candidates: List,
                                         population_size: int,
                                         elite_size: int) -> Tuple[List, List]:
        """
        为下一代选择父代
        
        Args:
            candidates: 当前候选化合物列表
            population_size: 种群大小
            elite_size: 精英个体数量
            
        Returns:
            (elite_compounds, diverse_compounds) 精英化合物和多样性化合物
        """
        if not candidates:
            return [], []
        
        # 按适应性排序
        sorted_candidates = sorted(candidates, 
                                 key=lambda x: x.scores.combined_score if x.scores else 0, 
                                 reverse=True)
        
        # 选择精英（最佳适应性）
        elite_candidates = sorted_candidates[:elite_size]
        
        # 为保持多样性，选择一些中等适应性但结构多样的化合物
        remaining_candidates = sorted_candidates[elite_size:]
        diverse_candidates = self._select_diverse_compounds(
            remaining_candidates, 
            population_size - elite_size
        )
        
        logger.info(f"选择 {len(elite_candidates)} 个精英化合物和 {len(diverse_candidates)} 个多样性化合物")
        
        return elite_candidates, diverse_candidates
    
    def _select_diverse_compounds(self, candidates: List, target_size: int) -> List:
        """基于结构多样性选择化合物"""
        if not candidates or target_size <= 0:
            return []
        
        if len(candidates) <= target_size:
            return candidates
        
        selected = []
        remaining = candidates.copy()
        
        # 首先随机选择一个作为种子
        if remaining:
            seed = random.choice(remaining)
            selected.append(seed)
            remaining.remove(seed)
        
        # 迭代选择与已选择化合物最不相似的化合物
        while len(selected) < target_size and remaining:
            best_candidate = None
            best_min_similarity = -1
            
            for candidate in remaining:
                # 计算与已选择化合物的最小相似性
                min_similarity = min(
                    self._calculate_tanimoto_similarity(candidate.smiles, selected_compound.smiles)
                    for selected_compound in selected
                )
                
                # 选择最小相似性最大的（即最多样的）化合物
                if min_similarity > best_min_similarity:
                    best_min_similarity = min_similarity
                    best_candidate = candidate
            
            if best_candidate:
                selected.append(best_candidate)
                remaining.remove(best_candidate)
            else:
                break
        
        return selected
    
    def _calculate_tanimoto_similarity(self, smiles1: str, smiles2: str) -> float:
        """计算两个SMILES的Tanimoto相似性"""
        try:
            from rdkit import Chem
            from rdkit.Chem import rdMolDescriptors
            from rdkit import DataStructs
            
            mol1 = Chem.MolFromSmiles(smiles1)
            mol2 = Chem.MolFromSmiles(smiles2)
            
            if not mol1 or not mol2:
                return 0.0
            
            fp1 = rdMolDescriptors.GetMorganFingerprintAsBitVect(mol1, 2, nBits=1024)
            fp2 = rdMolDescriptors.GetMorganFingerprintAsBitVect(mol2, 2, nBits=1024)
            
            return DataStructs.TanimotoSimilarity(fp1, fp2)
            
        except Exception as e:
            logger.warning(f"相似性计算失败: {e}")
            return 0.0
    
    def generate_next_generation_strategies(self,
                                          elite_compounds: List,
                                          diverse_compounds: List) -> List[Tuple[str, str, float]]:
        """
        生成下一代的优化策略
        
        Args:
            elite_compounds: 精英化合物
            diverse_compounds: 多样性化合物
            
        Returns:
            List of (compound_smiles, strategy, weight) 用于生成候选物
        """
        strategies = []
        
        # 对精英化合物使用保守的局部优化
        for compound in elite_compounds:
            strategies.extend([
                (compound.smiles, "fragment_replacement", 0.4),
                (compound.smiles, "functional_group_replacement", 0.3),
                (compound.smiles, "side_chain_modification", 0.3)
            ])
        
        # 对多样性化合物使用更激进的改变
        for compound in diverse_compounds:
            strategies.extend([
                (compound.smiles, "scaffold_hopping", 0.5),
                (compound.smiles, "ring_replacement", 0.3),
                (compound.smiles, "structural_elaboration", 0.2)
            ])
        
        # 添加一些交叉策略（如果有多个精英）
        if len(elite_compounds) >= 2:
            for i in range(min(3, len(elite_compounds) - 1)):
                strategies.append((
                    elite_compounds[i].smiles,
                    "hybrid_optimization",
                    0.2
                ))
        
        logger.info(f"生成 {len(strategies)} 个优化策略")
        return strategies
    
    def calculate_adaptive_parameters(self,
                                    generation: int,
                                    max_generations: int,
                                    convergence_rate: float) -> Dict[str, float]:
        """
        自适应调整进化参数
        
        Args:
            generation: 当前代数
            max_generations: 最大代数
            convergence_rate: 收敛率（0-1，表示种群的多样性）
            
        Returns:
            调整后的参数字典
        """
        progress = generation / max_generations if max_generations > 0 else 0
        
        # 早期更注重探索，后期更注重利用
        adaptive_diversity_weight = self.diversity_weight * (1 - progress * 0.7)
        adaptive_fitness_weight = self.fitness_weight * (1 + progress * 0.3)
        
        # 如果收敛过快，增加多样性权重
        if convergence_rate < 0.3:
            adaptive_diversity_weight *= 1.5
            adaptive_fitness_weight *= 0.8
        
        # 自适应突变率：收敛时增加突变
        adaptive_mutation_rate = self.mutation_rate
        if convergence_rate < 0.2:
            adaptive_mutation_rate *= 2.0
        elif convergence_rate > 0.8:
            adaptive_mutation_rate *= 0.5
        
        parameters = {
            'diversity_weight': adaptive_diversity_weight,
            'fitness_weight': adaptive_fitness_weight,
            'mutation_rate': adaptive_mutation_rate,
            'crossover_rate': self.crossover_rate,
            'exploration_bias': max(0.1, 0.8 - progress)  # 早期高探索偏向
        }
        
        logger.info(f"第{generation}代自适应参数: 多样性权重={adaptive_diversity_weight:.3f}, "
                   f"适应性权重={adaptive_fitness_weight:.3f}, 突变率={adaptive_mutation_rate:.3f}")
        
        return parameters
    
    def assess_population_diversity(self, candidates: List) -> float:
        """
        评估种群多样性
        
        Args:
            candidates: 候选化合物列表
            
        Returns:
            多样性分数 (0-1, 1表示最大多样性)
        """
        if len(candidates) < 2:
            return 1.0
        
        similarities = []
        
        # 计算所有化合物对的相似性
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                similarity = self._calculate_tanimoto_similarity(
                    candidates[i].smiles,
                    candidates[j].smiles
                )
                similarities.append(similarity)
        
        if not similarities:
            return 1.0
        
        # 多样性 = 1 - 平均相似性
        avg_similarity = np.mean(similarities)
        diversity = 1.0 - avg_similarity
        
        logger.debug(f"种群多样性评估: 平均相似性={avg_similarity:.3f}, 多样性分数={diversity:.3f}")
        
        return diversity
    
    def suggest_termination(self,
                          generation_results: List[List],
                          patience: int = 3) -> bool:
        """
        建议是否终止进化过程
        
        Args:
            generation_results: 每代的最佳结果列表
            patience: 容忍多少代没有显著改进
            
        Returns:
            是否建议终止
        """
        if len(generation_results) < patience + 1:
            return False
        
        # 检查最近几代的最佳分数是否有显著改进
        recent_scores = [
            max(gen_results, key=lambda x: x.scores.combined_score if x.scores else 0).scores.combined_score
            if gen_results and max(gen_results, key=lambda x: x.scores.combined_score if x.scores else 0).scores
            else 0
            for gen_results in generation_results[-patience-1:]
        ]
        
        if not recent_scores:
            return False
        
        # 计算改进幅度
        improvements = [recent_scores[i+1] - recent_scores[i] for i in range(len(recent_scores)-1)]
        avg_improvement = np.mean(improvements) if improvements else 0
        
        # 如果平均改进小于阈值，建议终止
        improvement_threshold = 0.01  # 1%改进阈值
        
        should_terminate = avg_improvement < improvement_threshold
        
        if should_terminate:
            logger.info(f"建议终止进化: 最近{patience}代平均改进{avg_improvement:.4f} < 阈值{improvement_threshold}")
        
        return should_terminate

class SmartOptimizationStrategies:
    """智能优化策略库"""
    
    @staticmethod
    def get_strategy_for_compound_type(smiles: str) -> str:
        """基于化合物类型选择最适合的优化策略"""
        try:
            mol = Chem.MolFromSmiles(smiles)
            if not mol:
                return "scaffold_hopping"
            
            # 分析分子特征
            mw = Descriptors.MolWt(mol)
            ring_count = Descriptors.RingCount(mol)
            aromatic_rings = Descriptors.NumAromaticRings(mol)
            rotatable_bonds = Descriptors.NumRotatableBonds(mol)
            
            # 基于分子特征选择策略
            if mw < 250:  # 小分子
                return "structural_elaboration"
            elif mw > 600:  # 大分子
                return "fragment_replacement"
            elif ring_count >= 3:  # 多环化合物
                return "side_chain_modification"
            elif aromatic_rings >= 2:  # 多芳环
                return "functional_group_replacement"
            elif rotatable_bonds <= 3:  # 刚性分子
                return "scaffold_hopping"
            else:  # 默认策略
                return "fragment_replacement"
                
        except Exception:
            return "scaffold_hopping"
    
    @staticmethod
    def get_complementary_strategies(primary_strategy: str) -> List[str]:
        """获取与主策略互补的策略"""
        strategy_groups = {
            "scaffold_hopping": ["fragment_replacement", "functional_group_replacement"],
            "fragment_replacement": ["side_chain_modification", "ring_replacement"],
            "functional_group_replacement": ["structural_elaboration", "scaffold_hopping"],
            "side_chain_modification": ["functional_group_replacement", "fragment_replacement"],
            "structural_elaboration": ["scaffold_hopping", "ring_replacement"],
            "ring_replacement": ["fragment_replacement", "structural_elaboration"],
            "hybrid_optimization": ["scaffold_hopping", "fragment_replacement"]
        }
        
        return strategy_groups.get(primary_strategy, ["scaffold_hopping", "fragment_replacement"])
