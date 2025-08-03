#!/usr/bin/env python3

"""
affinity_calculator.py

小分子-蛋白质亲和力计算模块
"""

import os
import json
import yaml
import logging
import math
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

class AffinityCalculator:
    """亲和力计算器"""
    
    def __init__(self, api_client):
        self.api_client = api_client
    
    def calculate_affinity(self, protein_sequence: str, ligand_sequence: str, 
                          ligand_type: str = "small_molecule") -> Optional[Dict[str, Any]]:
        """
        计算小分子与蛋白质的结合亲和力
        
        Args:
            protein_sequence: 蛋白质序列
            ligand_sequence: 配体序列（SMILES或氨基酸序列）
            ligand_type: 配体类型 ("small_molecule" 或 "peptide")
            
        Returns:
            亲和力计算结果字典，包含亲和力值和相关信息
        """
        try:
            # 构建YAML配置
            yaml_config = self._build_affinity_yaml(protein_sequence, ligand_sequence, ligand_type)
            if not yaml_config:
                return None
            
            # 提交亲和力计算任务
            task_id = self._submit_affinity_job(yaml_config)
            if not task_id:
                return None
            
            # 等待任务完成并获取结果
            affinity_result = self._get_affinity_result(task_id)
            return affinity_result
            
        except Exception as e:
            logger.error(f"计算亲和力时发生错误: {e}")
            return None
    
    def _build_affinity_yaml(self, protein_sequence: str, ligand_sequence: str, 
                           ligand_type: str) -> Optional[str]:
        """构建亲和力计算的YAML配置"""
        try:
            # 基本序列配置
            sequences_list = []
            
            # 添加蛋白质组分
            protein_config = {
                'protein': {
                    'id': 'A',
                    'sequence': protein_sequence
                }
            }
            sequences_list.append(protein_config)
            
            # 添加配体组分
            if ligand_type == "small_molecule":
                ligand_config = {
                    'ligand': {
                        'id': 'B', 
                        'smiles': ligand_sequence  # 使用SMILES字段，与用户提供的格式一致
                    }
                }
            else:  # peptide
                ligand_config = {
                    'protein': {
                        'id': 'B',
                        'sequence': ligand_sequence
                    }
                }
            
            sequences_list.append(ligand_config)
            
            # 构建完整的YAML字典
            yaml_dict = {
                'version': 1,
                'sequences': sequences_list,
                'properties': [
                    {
                        'affinity': {
                            'binder': 'B'  # 配体链作为结合体
                        }
                    }
                ]
            }
            
            # 转换为YAML字符串
            yaml_content = yaml.dump(yaml_dict, default_flow_style=False, allow_unicode=True)
            return yaml_content
            
        except Exception as e:
            logger.error(f"构建亲和力YAML配置失败: {e}")
            return None
    
    def _submit_affinity_job(self, yaml_content: str) -> Optional[str]:
        """提交亲和力计算任务"""
        try:
            # 创建临时YAML文件
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(yaml_content)
                yaml_path = f.name
            
            # 提交任务
            task_id = self.api_client.submit_screening_job(
                yaml_path=yaml_path,
                job_name="affinity_calculation",
                use_msa_server=False
            )
            
            # 清理临时文件
            os.unlink(yaml_path)
            
            return task_id
            
        except Exception as e:
            logger.error(f"提交亲和力计算任务失败: {e}")
            return None
    
    def _get_affinity_result(self, task_id: str, timeout: int = 300) -> Optional[Dict[str, Any]]:
        """获取亲和力计算结果"""
        try:
            import time
            import tempfile
            
            start_time = time.time()
            
            # 等待任务完成
            while time.time() - start_time < timeout:
                status_result = self.api_client.poll_status(task_id, poll_interval=10, max_wait_time=int(timeout - (time.time() - start_time)))
                if status_result and status_result.get("status") == "completed":
                    break
                elif status_result and status_result.get("status") == "failed":
                    logger.error(f"亲和力计算任务失败: {task_id}")
                    return None
                elif not status_result:
                    logger.warning(f"无法获取任务状态: {task_id}")
                    return None
                
                time.sleep(10)  # 等待10秒后重新检查
            
            if time.time() - start_time >= timeout:
                logger.error(f"亲和力计算任务超时: {task_id}")
                return None
            
            # 下载结果
            with tempfile.TemporaryDirectory() as temp_dir:
                if self.api_client.download_results(task_id, temp_dir):
                    # 解析亲和力结果
                    affinity_data = self._parse_affinity_results(temp_dir)
                    return affinity_data
                else:
                    logger.error(f"下载亲和力计算结果失败: {task_id}")
                    return None
            
        except Exception as e:
            logger.error(f"获取亲和力结果时发生错误: {e}")
            return None
    
    def _parse_affinity_results(self, result_dir: str) -> Optional[Dict[str, Any]]:
        """解析亲和力计算结果"""
        try:
            affinity_file = None
            
            # 查找亲和力结果文件
            for file_name in os.listdir(result_dir):
                if "affinity" in file_name.lower() and file_name.endswith(".json"):
                    affinity_file = os.path.join(result_dir, file_name)
                    break
            
            if not affinity_file:
                logger.warning("未找到亲和力结果文件")
                return None
            
            # 读取并解析亲和力数据
            with open(affinity_file, 'r') as f:
                affinity_data = json.load(f)
            
            # 提取关键亲和力信息
            result = {
                "affinity_score": affinity_data.get("affinity", affinity_data.get("affinity_pred_value", 0.0)),
                "binding_energy": affinity_data.get("binding_energy", 0.0),
                "raw_data": affinity_data
            }
            
            # 计算IC50值（基于affinity_pred_value）
            affinity_pred_value = affinity_data.get("affinity_pred_value", 0.0)
            if affinity_pred_value > 0:
                # 使用通用的pIC50 = -log10(IC50_M)公式
                # 假设affinity_pred_value是以μM为单位的IC50值
                ic50_uM = affinity_pred_value
                pIC50 = -math.log10(ic50_uM * 1e-6) if ic50_uM > 0 else 0.0
                
                result.update({
                    "ic50_uM": ic50_uM,
                    "pIC50": pIC50,
                    "binding_probability": affinity_data.get("affinity_probability_binary", 0.0),
                    "delta_g_kcal_mol": -1.364 * pIC50 if pIC50 > 0 else 0.0  # 近似转换
                })
            
            logger.info(f"成功解析亲和力结果: affinity={result['affinity_score']}, IC50={result.get('ic50_uM', 'N/A')} μM")
            return result
            
        except Exception as e:
            logger.error(f"解析亲和力结果失败: {e}")
            return None


class SmallMoleculeAffinityEnhancer:
    """小分子亲和力增强器 - 为虚拟筛选结果添加亲和力计算"""
    
    def __init__(self, api_client, target_protein_sequence: str):
        self.affinity_calculator = AffinityCalculator(api_client)
        self.target_protein_sequence = target_protein_sequence
    
    def enhance_screening_results(self, screening_results: List, 
                                enable_affinity: bool = True) -> List:
        """
        为筛选结果添加亲和力信息
        
        Args:
            screening_results: 筛选结果列表
            enable_affinity: 是否启用亲和力计算
            
        Returns:
            增强后的筛选结果列表
        """
        if not enable_affinity:
            return screening_results
        
        enhanced_results = []
        
        for i, result in enumerate(screening_results):
            try:
                logger.info(f"正在计算第 {i+1}/{len(screening_results)} 个分子的亲和力...")
                
                # 只为小分子计算亲和力
                if result.mol_type == "small_molecule":
                    affinity_data = self.affinity_calculator.calculate_affinity(
                        protein_sequence=self.target_protein_sequence,
                        ligand_sequence=result.sequence,
                        ligand_type="small_molecule"
                    )
                    
                    if affinity_data:
                        # 将亲和力信息添加到结果中
                        if not result.properties:
                            result.properties = {}
                        
                        result.properties.update({
                            "affinity_score": affinity_data.get("affinity_score", 0.0),
                            "binding_energy": affinity_data.get("binding_energy", 0.0),
                            "ic50_uM": affinity_data.get("ic50_uM"),
                            "pIC50": affinity_data.get("pIC50"),
                            "binding_probability": affinity_data.get("binding_probability"),
                            "delta_g_kcal_mol": affinity_data.get("delta_g_kcal_mol"),
                            "has_affinity_data": True
                        })
                        
                        # 更新综合评分（包含亲和力）
                        result.combined_score = self._calculate_enhanced_score(result, affinity_data)
                        
                        logger.info(f"分子 {result.molecule_id} 亲和力计算完成: "
                                  f"affinity={affinity_data.get('affinity_score', 0.0):.3f}, "
                                  f"IC50={affinity_data.get('ic50_uM', 'N/A')} μM")
                    else:
                        logger.warning(f"分子 {result.molecule_id} 亲和力计算失败")
                        if not result.properties:
                            result.properties = {}
                        result.properties["has_affinity_data"] = False
                
                enhanced_results.append(result)
                
            except Exception as e:
                logger.error(f"处理分子 {result.molecule_id} 时发生错误: {e}")
                enhanced_results.append(result)
        
        # 重新排序结果
        enhanced_results.sort(key=lambda x: x.combined_score, reverse=True)
        for i, result in enumerate(enhanced_results):
            result.rank = i + 1
        
        logger.info(f"亲和力增强完成，处理了 {len(enhanced_results)} 个结果")
        return enhanced_results
    
    def _calculate_enhanced_score(self, result, affinity_data: Dict[str, Any]) -> float:
        """计算包含亲和力的增强评分"""
        try:
            # 原始综合评分
            original_score = result.combined_score
            
            # 亲和力评分（归一化到0-1范围）
            affinity_score = affinity_data.get("affinity_score", 0.0)
            normalized_affinity = max(0, min(1, affinity_score / 10.0))  # 假设最大亲和力为10
            
            # 权重配置
            weights = {
                "original": 0.7,     # 原始评分权重
                "affinity": 0.3      # 亲和力评分权重
            }
            
            # 计算增强评分
            enhanced_score = (weights["original"] * original_score + 
                            weights["affinity"] * normalized_affinity)
            
            return enhanced_score
            
        except Exception as e:
            logger.error(f"计算增强评分失败: {e}")
            return result.combined_score  # 返回原始评分
