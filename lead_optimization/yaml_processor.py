#!/usr/bin/env python3
# /data/boltz_webui/lead_optimization/yaml_processor.py

"""
YAML配置处理器
参考virtual_screening的设计，让用户只需要提供目标蛋白信息，
程序自动补齐配体部分
"""

import yaml
import copy
import os
import tempfile
import logging
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

class YamlProcessor:
    """YAML配置处理器"""
    
    def __init__(self, temp_dir: Optional[str] = None):
        """
        初始化YAML处理器
        
        Args:
            temp_dir: 临时文件目录，如果为None则使用系统临时目录
        """
        if temp_dir:
            self.temp_dir = temp_dir
            os.makedirs(temp_dir, exist_ok=True)
        else:
            self.temp_dir = tempfile.mkdtemp(prefix="lead_opt_")
        
        logger.info(f"YAML处理器初始化，临时目录: {self.temp_dir}")
    
    def load_target_config(self, yaml_path: str) -> Dict[str, Any]:
        """
        加载目标蛋白配置文件
        
        Args:
            yaml_path: YAML文件路径
            
        Returns:
            解析后的配置字典
        """
        try:
            with open(yaml_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 验证配置文件基本结构
            if not self._validate_target_config(config):
                raise ValueError(f"目标配置文件格式不正确: {yaml_path}")
            
            logger.info(f"成功加载目标配置: {yaml_path}")
            return config
            
        except Exception as e:
            logger.error(f"加载目标配置失败: {e}")
            raise
    
    def _validate_target_config(self, config: Dict[str, Any]) -> bool:
        """
        验证目标配置文件格式
        
        Args:
            config: 配置字典
            
        Returns:
            是否有效
        """
        # 检查必需字段
        if 'sequences' not in config:
            logger.error("配置文件缺少sequences字段")
            return False
        
        sequences = config['sequences']
        if not isinstance(sequences, list) or len(sequences) == 0:
            logger.error("sequences字段必须是非空列表")
            return False
        
        # 检查是否包含蛋白质序列
        has_protein = any('protein' in seq for seq in sequences)
        if not has_protein:
            logger.error("配置文件中必须包含至少一个蛋白质序列")
            return False
        
        return True
    
    def create_compound_config(self, target_config_path: str, compound_smiles: str, 
                             compound_id: Optional[str] = None, use_msa_server: bool = True) -> str:
        """
        为单个化合物创建完整的配置文件
        
        Args:
            target_config_path: 目标蛋白配置文件路径
            compound_smiles: 化合物SMILES
            compound_id: 化合物ID，如果为None则自动生成
            use_msa_server: 是否使用MSA服务器
            
        Returns:
            生成的配置文件路径
        """
        try:
            # 加载目标配置
            target_config = self.load_target_config(target_config_path)
            
            # 创建化合物配置
            compound_config = self._create_single_compound_config(
                target_config, compound_smiles, compound_id, use_msa_server
            )
            
            # 生成临时配置文件
            if compound_id:
                config_filename = f"{compound_id}.yaml"
            else:
                config_filename = f"compound_{abs(hash(compound_smiles)) % 10000}.yaml"
            
            config_path = os.path.join(self.temp_dir, config_filename)
            
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(compound_config, f, default_flow_style=False, 
                         allow_unicode=True, indent=2)
            
            logger.debug(f"创建化合物配置文件: {config_path}, use_msa_server: {use_msa_server}")
            return config_path
            
        except Exception as e:
            logger.error(f"创建化合物配置失败: {e}")
            raise

    def is_denovo_config(self, config_path: str) -> bool:
        """
        检查配置文件是否为de novo设计模式
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            True如果是de novo模式
        """
        try:
            config = self.load_target_config(config_path)
            
            for seq in config.get('sequences', []):
                if 'ligand' in seq:
                    ligand = seq['ligand']
                    if ligand.get('smiles') == 'XXXX':
                        return True
            return False
            
        except Exception as e:
            logger.warning(f"检查de novo配置失败: {e}")
            return False

    def update_denovo_ligand(self, config_path: str, compound_smiles: str) -> str:
        """
        直接更新de novo配置文件中的配体SMILES
        
        Args:
            config_path: 配置文件路径
            compound_smiles: 新的SMILES字符串
            
        Returns:
            更新后的配置文件路径
        """
        try:
            config = self.load_target_config(config_path)
            
            # 查找并更新denovo配体
            updated = False
            for seq in config.get('sequences', []):
                if 'ligand' in seq:
                    ligand = seq['ligand']
                    if ligand.get('smiles') == 'XXXX':
                        ligand['smiles'] = compound_smiles
                        updated = True
                        logger.debug(f"更新de novo配体 {ligand['id']} 的SMILES为: {compound_smiles}")
                        break
            
            if not updated:
                raise ValueError("配置文件中未找到de novo配体")
            
            # 写回配置文件
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, 
                         allow_unicode=True, indent=2)
            
            logger.debug(f"成功更新de novo配置文件: {config_path}")
            return config_path
            
        except Exception as e:
            logger.error(f"更新de novo配置失败: {e}")
            raise
    
    def create_batch_configs(self, target_config_path: str, 
                           compounds: List[Tuple[str, Optional[str]]]) -> List[str]:
        """
        为多个化合物批量创建配置文件
        
        Args:
            target_config_path: 目标蛋白配置文件路径
            compounds: 化合物列表，每个元素为(smiles, compound_id)元组
            
        Returns:
            生成的配置文件路径列表
        """
        config_files = []
        target_config = self.load_target_config(target_config_path)
        
        logger.info(f"开始批量创建 {len(compounds)} 个化合物配置文件")
        
        for i, (smiles, compound_id) in enumerate(compounds):
            try:
                compound_config = self._create_single_compound_config(
                    target_config, smiles, compound_id or f"compound_{i+1:04d}", use_msa_server=True
                )
                
                config_filename = f"{compound_id or f'compound_{i+1:04d}'}.yaml"
                config_path = os.path.join(self.temp_dir, config_filename)
                
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.dump(compound_config, f, default_flow_style=False,
                             allow_unicode=True, indent=2)
                
                config_files.append(config_path)
                
            except Exception as e:
                logger.error(f"创建第 {i+1} 个化合物配置失败: {e}")
                continue
        
        logger.info(f"成功创建 {len(config_files)} 个化合物配置文件")
        return config_files
    
    def _create_single_compound_config(self, target_config: Dict[str, Any], 
                                     compound_smiles: str,
                                     compound_id: Optional[str] = None,
                                     use_msa_server: bool = True) -> Dict[str, Any]:
        """
        为单个化合物创建配置
        
        Args:
            target_config: 目标配置字典
            compound_smiles: 化合物SMILES
            compound_id: 化合物ID
            use_msa_server: 是否使用MSA服务器
            
        Returns:
            完整的配置字典
        """
        # 深拷贝目标配置
        compound_config = copy.deepcopy(target_config)
        
        # 确保有version字段
        if 'version' not in compound_config:
            compound_config['version'] = 1
        
        # # 添加MSA服务器配置
        # compound_config['use_msa_server'] = use_msa_server
        
        # 检查是否存在denovo配体（SMILES为"XXXX"）
        existing_denovo_ligand = None
        existing_denovo_ligand_id = None
        
        # 重新构建sequences列表
        new_sequences = []
        used_ids = set()
        
        # 保留所有蛋白质序列，处理配体序列
        for seq in compound_config.get('sequences', []):
            if 'protein' in seq:
                new_sequences.append(seq)
                used_ids.add(seq['protein']['id'])
            elif 'dna' in seq:
                new_sequences.append(seq)
                used_ids.add(seq['dna']['id'])
            elif 'rna' in seq:
                new_sequences.append(seq)
                used_ids.add(seq['rna']['id'])
            elif 'ligand' in seq:
                ligand = seq['ligand']
                used_ids.add(ligand['id'])
                
                # 检查是否是denovo配体（SMILES为"XXXX"）
                if ligand.get('smiles') == 'XXXX':
                    existing_denovo_ligand = ligand
                    existing_denovo_ligand_id = ligand['id']
                    logger.debug(f"检测到de novo配体定义: {ligand['id']}")
                else:
                    # 保留非denovo的配体（如果有的话）
                    new_sequences.append(seq)
        
        # 为化合物分配ID
        if existing_denovo_ligand_id:
            # 如果存在denovo配体，使用其ID
            ligand_id = existing_denovo_ligand_id
            logger.debug(f"使用已存在的de novo配体ID: {ligand_id}")
        elif compound_id:
            ligand_id = compound_id
        else:
            ligand_id = self._assign_ligand_id(used_ids)
        
        # 创建或更新配体序列
        ligand_sequence = {
            "ligand": {
                "id": ligand_id,
                "smiles": compound_smiles
            }
        }
        
        # 如果原来是denovo配体，保留其他属性（如ccd等）
        if existing_denovo_ligand:
            for key, value in existing_denovo_ligand.items():
                if key not in ['id', 'smiles']:  # 保留除了id和smiles之外的其他属性
                    ligand_sequence['ligand'][key] = value
        
        new_sequences.append(ligand_sequence)
        compound_config['sequences'] = new_sequences
        
        # 更新约束中的配体ID（如果存在）
        if 'constraints' in compound_config:
            compound_config['constraints'] = self._update_constraints_ligand_id(
                compound_config['constraints'], ligand_id
            )
        
        # 更新属性中的配体ID（如果存在）
        if 'properties' in compound_config:
            compound_config['properties'] = self._update_properties_ligand_id(
                compound_config['properties'], ligand_id
            )
        
        return compound_config
    
    def _assign_ligand_id(self, used_ids: set) -> str:
        """
        为配体分配唯一ID
        
        Args:
            used_ids: 已使用的ID集合
            
        Returns:
            新的配体ID
        """
        # 优先使用常见的配体ID
        preferred_ids = ['L', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
        
        for ligand_id in preferred_ids:
            if ligand_id not in used_ids:
                return ligand_id
        
        # 如果常见ID都被占用，使用数字后缀
        for i in range(1, 100):
            ligand_id = f"L{i}"
            if ligand_id not in used_ids:
                return ligand_id
        
        # 最后的备选方案
        return f"LIG_{len(used_ids)}"
    
    def _update_constraints_ligand_id(self, constraints: List[Dict], new_ligand_id: str) -> List[Dict]:
        """
        更新约束中的配体ID
        
        Args:
            constraints: 约束列表
            new_ligand_id: 新的配体ID
            
        Returns:
            更新后的约束列表
        """
        updated_constraints = []
        
        for constraint in constraints:
            constraint_copy = copy.deepcopy(constraint)
            
            if 'pocket' in constraint_copy:
                if 'binder' in constraint_copy['pocket']:
                    constraint_copy['pocket']['binder'] = new_ligand_id
            
            elif 'contact' in constraint_copy:
                # 更新contact约束中的配体引用
                if 'token1' in constraint_copy['contact']:
                    token1 = constraint_copy['contact']['token1']
                    if isinstance(token1, list) and len(token1) == 2:
                        # 假设配体token的格式为[chain_id, residue_index]
                        # 这里需要根据实际情况判断是否为配体
                        pass
                if 'token2' in constraint_copy['contact']:
                    token2 = constraint_copy['contact']['token2']
                    if isinstance(token2, list) and len(token2) == 2:
                        # 类似处理
                        pass
            
            elif 'bond' in constraint_copy:
                # 更新bond约束中的配体引用
                if 'atom1' in constraint_copy['bond']:
                    atom1 = constraint_copy['bond']['atom1']
                    if isinstance(atom1, list) and len(atom1) >= 2:
                        # 如果第一个元素看起来像配体ID，更新它
                        if atom1[0] in ['L', 'B'] or atom1[0].startswith('L'):
                            constraint_copy['bond']['atom1'][0] = new_ligand_id
                
                if 'atom2' in constraint_copy['bond']:
                    atom2 = constraint_copy['bond']['atom2']
                    if isinstance(atom2, list) and len(atom2) >= 2:
                        if atom2[0] in ['L', 'B'] or atom2[0].startswith('L'):
                            constraint_copy['bond']['atom2'][0] = new_ligand_id
            
            updated_constraints.append(constraint_copy)
        
        return updated_constraints
    
    def _update_properties_ligand_id(self, properties: List[Dict], new_ligand_id: str) -> List[Dict]:
        """
        更新属性中的配体ID
        
        Args:
            properties: 属性列表
            new_ligand_id: 新的配体ID
            
        Returns:
            更新后的属性列表
        """
        updated_properties = []
        
        for prop in properties:
            prop_copy = copy.deepcopy(prop)
            
            if 'affinity' in prop_copy:
                if 'binder' in prop_copy['affinity']:
                    prop_copy['affinity']['binder'] = new_ligand_id
            
            updated_properties.append(prop_copy)
        
        return updated_properties
    
    def cleanup(self):
        """清理临时文件"""
        try:
            import shutil
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.info(f"清理临时目录: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"清理临时目录失败: {e}")
    
    def __del__(self):
        """析构函数，自动清理"""
        self.cleanup()


def create_simple_target_template(output_path: str, 
                                protein_sequence: str,
                                protein_id: str = "A",
                                include_affinity: bool = True,
                                include_pocket_constraint: bool = False,
                                pocket_residues: Optional[List[int]] = None) -> str:
    """
    创建简单的目标蛋白模板文件
    
    Args:
        output_path: 输出文件路径
        protein_sequence: 蛋白质序列
        protein_id: 蛋白质ID
        include_affinity: 是否包含亲和力计算
        include_pocket_constraint: 是否包含口袋约束
        pocket_residues: 口袋残基位置列表
        
    Returns:
        创建的文件路径
    """
    config = {
        'version': 1,
        'sequences': [
            {
                'protein': {
                    'id': protein_id,
                    'sequence': protein_sequence
                }
            }
        ]
    }
    
    # 添加口袋约束
    if include_pocket_constraint and pocket_residues:
        config['constraints'] = [
            {
                'pocket': {
                    'binder': 'L',  # 将被自动替换
                    'contacts': [[protein_id, residue] for residue in pocket_residues],
                    'max_distance': 5.0,
                    'force': False
                }
            }
        ]
    
    # 添加亲和力计算
    if include_affinity:
        config['properties'] = [
            {
                'affinity': {
                    'binder': 'L'  # 将被自动替换
                }
            }
        ]
    
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, indent=2)
    
    logger.info(f"创建目标蛋白模板: {output_path}")
    return output_path
