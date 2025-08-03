# /Boltz-WebUI/virtual_screening/molecule_library.py

"""
molecule_library.py

该模块定义了分子库管理和预处理的相关类：
1. MoleculeLibrary: 通用分子库基类
2. PeptideLibrary: 多肽库管理
3. SmallMoleculeLibrary: 小分子库管理
4. LibraryProcessor: 分子库预处理工具
"""

import os
import csv
import json
import yaml
import logging
from typing import List, Dict, Any, Iterator, Optional, Tuple
from abc import ABC, abstractmethod
from dataclasses import dataclass
import pandas as pd

# 尝试导入RDKit，如果未安装则跳过小分子相关功能
try:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, Crippen, Lipinski
    from rdkit.Chem.rdMolDescriptors import CalcMolFormula
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False
    logging.warning("RDKit未安装，小分子处理功能将受限")

logger = logging.getLogger(__name__)

@dataclass
class Molecule:
    """分子基本信息数据类"""
    id: str
    name: str = ""
    sequence: str = ""  # 对于多肽是氨基酸序列，对于小分子是SMILES
    mol_type: str = "unknown"  # peptide, small_molecule
    molecular_weight: float = 0.0
    properties: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.properties is None:
            self.properties = {}

class MoleculeLibrary(ABC):
    """分子库管理基类"""
    
    def __init__(self, library_path: str, mol_type: str):
        self.library_path = library_path
        self.mol_type = mol_type
        self.molecules: List[Molecule] = []
        self.metadata: Dict[str, Any] = {}
        self._loaded = False
    
    @abstractmethod
    def load_library(self) -> bool:
        """加载分子库"""
        pass
    
    @abstractmethod
    def validate_molecule(self, molecule: Molecule) -> bool:
        """验证分子有效性"""
        pass
    
    def get_molecule(self, index: int) -> Optional[Molecule]:
        """获取指定索引的分子"""
        if 0 <= index < len(self.molecules):
            return self.molecules[index]
        return None
    
    def get_molecules_by_id(self, mol_ids: List[str]) -> List[Molecule]:
        """根据ID列表获取分子"""
        id_to_mol = {mol.id: mol for mol in self.molecules}
        return [id_to_mol[mol_id] for mol_id in mol_ids if mol_id in id_to_mol]
    
    def filter_molecules(self, filter_func) -> List[Molecule]:
        """根据过滤函数筛选分子"""
        return [mol for mol in self.molecules if filter_func(mol)]
    
    def get_library_stats(self) -> Dict[str, Any]:
        """获取分子库统计信息"""
        stats = {
            "total_molecules": len(self.molecules),
            "molecule_type": self.mol_type,
            "library_path": self.library_path
        }
        
        if self.molecules:
            mw_values = [mol.molecular_weight for mol in self.molecules if mol.molecular_weight > 0]
            if mw_values:
                stats.update({
                    "avg_molecular_weight": sum(mw_values) / len(mw_values),
                    "min_molecular_weight": min(mw_values),
                    "max_molecular_weight": max(mw_values)
                })
        
        return stats
    
    def save_filtered_library(self, output_path: str, molecules: List[Molecule]) -> bool:
        """保存过滤后的分子库"""
        try:
            with open(output_path, 'w') as f:
                if self.mol_type == "peptide":
                    for mol in molecules:
                        f.write(f">{mol.id}\n{mol.sequence}\n")
                elif self.mol_type == "small_molecule":
                    f.write("molecule_id,smiles,name,molecular_weight\n")
                    for mol in molecules:
                        f.write(f"{mol.id},{mol.sequence},{mol.name},{mol.molecular_weight}\n")
            logger.info(f"过滤后的分子库已保存到: {output_path}")
            return True
        except Exception as e:
            logger.error(f"保存分子库失败: {e}")
            return False
    
    def __len__(self) -> int:
        return len(self.molecules)
    
    def __iter__(self) -> Iterator[Molecule]:
        return iter(self.molecules)

class PeptideLibrary(MoleculeLibrary):
    """多肽库管理类"""
    
    def __init__(self, library_path: str):
        super().__init__(library_path, "peptide")
        self.valid_amino_acids = set("ACDEFGHIKLMNPQRSTVWY")
    
    def load_library(self) -> bool:
        """从FASTA文件加载多肽库"""
        try:
            self.molecules = []
            current_id = None
            current_sequence = ""
            
            with open(self.library_path, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    if line.startswith('>'):
                        # 保存前一个多肽
                        if current_id and current_sequence:
                            mol = Molecule(
                                id=current_id,
                                sequence=current_sequence,
                                mol_type="peptide",
                                molecular_weight=self._calculate_peptide_mw(current_sequence)
                            )
                            if self.validate_molecule(mol):
                                self.molecules.append(mol)
                        
                        # 开始新多肽
                        current_id = line[1:].split()[0]  # 取第一个空格前的部分作为ID
                        current_sequence = ""
                    else:
                        current_sequence += line.upper()
                
                # 处理最后一个多肽
                if current_id and current_sequence:
                    mol = Molecule(
                        id=current_id,
                        sequence=current_sequence,
                        mol_type="peptide",
                        molecular_weight=self._calculate_peptide_mw(current_sequence)
                    )
                    if self.validate_molecule(mol):
                        self.molecules.append(mol)
            
            self._loaded = True
            logger.info(f"成功加载多肽库: {len(self.molecules)} 个多肽")
            return True
            
        except Exception as e:
            logger.error(f"加载多肽库失败: {e}")
            return False
    
    def validate_molecule(self, molecule: Molecule) -> bool:
        """验证多肽有效性"""
        if not molecule.sequence:
            logger.warning(f"多肽 {molecule.id} 序列为空")
            return False
        
        # 检查是否包含无效氨基酸
        invalid_chars = set(molecule.sequence) - self.valid_amino_acids
        if invalid_chars:
            logger.warning(f"多肽 {molecule.id} 包含无效氨基酸: {invalid_chars}")
            return False
        
        # 检查长度限制
        if len(molecule.sequence) < 3:
            logger.warning(f"多肽 {molecule.id} 序列过短")
            return False
        
        if len(molecule.sequence) > 1000:
            logger.warning(f"多肽 {molecule.id} 序列过长")
            return False
        
        return True
    
    def _calculate_peptide_mw(self, sequence: str) -> float:
        """计算多肽分子量"""
        # 氨基酸分子量表 (去除水分子)
        aa_weights = {
            'A': 71.04, 'R': 156.11, 'N': 114.04, 'D': 115.03, 'C': 103.01,
            'E': 129.04, 'Q': 128.06, 'G': 57.02, 'H': 137.06, 'I': 113.08,
            'L': 113.08, 'K': 128.09, 'M': 131.04, 'F': 147.07, 'P': 97.05,
            'S': 87.03, 'T': 101.05, 'W': 186.08, 'Y': 163.06, 'V': 99.07
        }
        
        total_weight = 18.015  # 水分子重量
        for aa in sequence:
            total_weight += aa_weights.get(aa, 0)
        
        return round(total_weight, 2)
    
    def get_peptide_properties(self, peptide: Molecule) -> Dict[str, Any]:
        """计算多肽的各种性质"""
        sequence = peptide.sequence
        properties = {
            "length": len(sequence),
            "molecular_weight": peptide.molecular_weight,
            "hydrophobic_residues": sum(1 for aa in sequence if aa in "AILMFWYV"),
            "charged_residues": sum(1 for aa in sequence if aa in "DEKR"),
            "polar_residues": sum(1 for aa in sequence if aa in "NQSTYC"),
            "aromatic_residues": sum(1 for aa in sequence if aa in "FWY")
        }
        
        # 计算净电荷 (简化计算，pH=7)
        positive_charge = sum(1 for aa in sequence if aa in "KR")
        negative_charge = sum(1 for aa in sequence if aa in "DE")
        properties["net_charge"] = positive_charge - negative_charge
        
        # 计算疏水性指数 (Kyte-Doolittle)
        hydrophobicity_scale = {
            'A': 1.8, 'R': -4.5, 'N': -3.5, 'D': -3.5, 'C': 2.5,
            'E': -3.5, 'Q': -3.5, 'G': -0.4, 'H': -3.2, 'I': 4.5,
            'L': 3.8, 'K': -3.9, 'M': 1.9, 'F': 2.8, 'P': -1.6,
            'S': -0.8, 'T': -0.7, 'W': -0.9, 'Y': -1.3, 'V': 4.2
        }
        hydrophobicity = sum(hydrophobicity_scale.get(aa, 0) for aa in sequence) / len(sequence)
        properties["hydrophobicity"] = round(hydrophobicity, 3)
        
        return properties

class SmallMoleculeLibrary(MoleculeLibrary):
    """小分子库管理类"""
    
    def __init__(self, library_path: str):
        super().__init__(library_path, "small_molecule")
        if not RDKIT_AVAILABLE:
            raise ImportError("小分子库需要安装RDKit: pip install rdkit-pypi")
    
    def load_library(self) -> bool:
        """从SDF或CSV文件加载小分子库"""
        try:
            self.molecules = []
            
            if self.library_path.endswith('.sdf'):
                self._load_from_sdf()
            elif self.library_path.endswith('.csv'):
                self._load_from_csv()
            elif self.library_path.endswith('.smi') or self.library_path.endswith('.smiles'):
                self._load_from_smiles()
            else:
                raise ValueError(f"不支持的文件格式: {self.library_path}")
            
            self._loaded = True
            logger.info(f"成功加载小分子库: {len(self.molecules)} 个化合物")
            return True
            
        except Exception as e:
            logger.error(f"加载小分子库失败: {e}")
            return False
    
    def _load_from_sdf(self):
        """从SDF文件加载"""
        suppl = Chem.SDMolSupplier(self.library_path)
        for i, mol in enumerate(suppl):
            if mol is not None:
                mol_id = mol.GetProp('_Name') if mol.HasProp('_Name') else f"mol_{i+1:04d}"
                smiles = Chem.MolToSmiles(mol)
                
                molecule = Molecule(
                    id=mol_id,
                    sequence=smiles,
                    mol_type="small_molecule",
                    molecular_weight=Descriptors.MolWt(mol)
                )
                
                # 添加额外属性
                molecule.properties = self._calculate_mol_properties(mol)
                
                if self.validate_molecule(molecule):
                    self.molecules.append(molecule)
    
    def _load_from_csv(self):
        """从CSV文件加载 (需要包含smiles列，molecule_id列可选)"""
        df = pd.read_csv(self.library_path)
        
        # 检查必需的列
        required_cols = ['smiles']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            raise ValueError(f"CSV文件缺少必需的列: {missing_cols}")
        
        for i, row in df.iterrows():
            # 如果有molecule_id列就使用，否则自动生成
            if 'molecule_id' in df.columns:
                mol_id = str(row['molecule_id'])
            else:
                mol_id = f"mol_{i+1:04d}"
                
            smiles = str(row['smiles'])
            
            # 验证SMILES
            mol = Chem.MolFromSmiles(smiles)
            if mol is not None:
                molecule = Molecule(
                    id=mol_id,
                    name=str(row.get('name', '')),
                    sequence=smiles,
                    mol_type="small_molecule",
                    molecular_weight=Descriptors.MolWt(mol)
                )
                
                # 添加CSV中的其他属性
                excluded_cols = ['molecule_id', 'smiles', 'name']
                molecule.properties = {col: row[col] for col in df.columns 
                                    if col not in excluded_cols}
                molecule.properties.update(self._calculate_mol_properties(mol))
                
                if self.validate_molecule(molecule):
                    self.molecules.append(molecule)
    
    def _load_from_smiles(self):
        """从SMILES文件加载"""
        with open(self.library_path, 'r') as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('\t')
                if len(parts) >= 1:
                    smiles = parts[0]
                    mol_id = parts[1] if len(parts) > 1 else f"mol_{i+1:04d}"
                    
                    mol = Chem.MolFromSmiles(smiles)
                    if mol is not None:
                        molecule = Molecule(
                            id=mol_id,
                            sequence=smiles,
                            mol_type="small_molecule",
                            molecular_weight=Descriptors.MolWt(mol)
                        )
                        molecule.properties = self._calculate_mol_properties(mol)
                        
                        if self.validate_molecule(molecule):
                            self.molecules.append(molecule)
    
    def validate_molecule(self, molecule: Molecule) -> bool:
        """验证小分子有效性"""
        if not molecule.sequence:
            logger.warning(f"化合物 {molecule.id} SMILES为空")
            return False
        
        # 验证SMILES格式
        mol = Chem.MolFromSmiles(molecule.sequence)
        if mol is None:
            logger.warning(f"化合物 {molecule.id} SMILES格式无效")
            return False
        
        # 检查分子量限制
        if molecule.molecular_weight > 2000:
            logger.warning(f"化合物 {molecule.id} 分子量过大")
            return False
        
        return True
    
    def _calculate_mol_properties(self, mol) -> Dict[str, Any]:
        """计算小分子的各种性质"""
        properties = {
            "molecular_formula": CalcMolFormula(mol),
            "logp": round(Crippen.MolLogP(mol), 3),
            "hbd": Lipinski.NumHDonors(mol),
            "hba": Lipinski.NumHAcceptors(mol),
            "rotatable_bonds": Descriptors.NumRotatableBonds(mol),
            "tpsa": round(Descriptors.TPSA(mol), 2),
            "heavy_atoms": mol.GetNumHeavyAtoms(),
            "aromatic_rings": Descriptors.NumAromaticRings(mol)
        }
        
        # Lipinski规则检查
        mw = Descriptors.MolWt(mol)
        logp = properties["logp"]
        hbd = properties["hbd"]
        hba = properties["hba"]
        
        lipinski_violations = 0
        if mw > 500: lipinski_violations += 1
        if logp > 5: lipinski_violations += 1
        if hbd > 5: lipinski_violations += 1
        if hba > 10: lipinski_violations += 1
        
        properties["lipinski_violations"] = lipinski_violations
        properties["drug_like"] = lipinski_violations <= 1
        
        return properties
    
    def filter_by_lipinski(self) -> List[Molecule]:
        """根据Lipinski规则过滤化合物"""
        return self.filter_molecules(
            lambda mol: mol.properties.get("lipinski_violations", 5) <= 1
        )
    
    def filter_by_molecular_weight(self, min_mw: float = 0, max_mw: float = 1000) -> List[Molecule]:
        """根据分子量过滤化合物"""
        return self.filter_molecules(
            lambda mol: min_mw <= mol.molecular_weight <= max_mw
        )

class LibraryProcessor:
    """分子库预处理工具"""
    
    @staticmethod
    def create_library(library_path: str, mol_type: str = None) -> MoleculeLibrary:
        """创建适当的分子库实例"""
        if mol_type is None:
            # 根据文件扩展名自动判断类型
            ext = os.path.splitext(library_path)[1].lower()
            if ext in ['.fasta', '.fa', '.fas']:
                mol_type = "peptide"
            elif ext in ['.sdf', '.mol', '.csv', '.smi', '.smiles']:
                mol_type = "small_molecule"
            else:
                raise ValueError(f"无法从文件扩展名 {ext} 推断分子类型，请明确指定 mol_type")
        
        if mol_type == "peptide":
            return PeptideLibrary(library_path)
        elif mol_type == "small_molecule":
            return SmallMoleculeLibrary(library_path)
        else:
            raise ValueError(f"不支持的分子类型: {mol_type}")
    
    @staticmethod
    def merge_libraries(libraries: List[MoleculeLibrary], output_path: str) -> bool:
        """合并多个分子库"""
        try:
            all_molecules = []
            mol_types = set()
            
            for lib in libraries:
                all_molecules.extend(lib.molecules)
                mol_types.add(lib.mol_type)
            
            if len(mol_types) > 1:
                raise ValueError("无法合并不同类型的分子库")
            
            mol_type = mol_types.pop()
            
            # 去重 (基于ID)
            unique_molecules = {}
            for mol in all_molecules:
                if mol.id not in unique_molecules:
                    unique_molecules[mol.id] = mol
            
            # 保存合并后的库
            with open(output_path, 'w') as f:
                if mol_type == "peptide":
                    for mol in unique_molecules.values():
                        f.write(f">{mol.id}\n{mol.sequence}\n")
                elif mol_type == "small_molecule":
                    f.write("molecule_id,smiles,name,molecular_weight\n")
                    for mol in unique_molecules.values():
                        f.write(f"{mol.id},{mol.sequence},{mol.name},{mol.molecular_weight}\n")
            
            logger.info(f"成功合并 {len(libraries)} 个分子库，共 {len(unique_molecules)} 个分子")
            return True
            
        except Exception as e:
            logger.error(f"合并分子库失败: {e}")
            return False
    
    @staticmethod
    def split_library(library: MoleculeLibrary, chunk_size: int, output_dir: str) -> List[str]:
        """将大型分子库分割为小块"""
        try:
            os.makedirs(output_dir, exist_ok=True)
            output_files = []
            
            for i in range(0, len(library.molecules), chunk_size):
                chunk = library.molecules[i:i + chunk_size]
                chunk_file = os.path.join(output_dir, f"chunk_{i//chunk_size + 1:04d}.{library.mol_type}")
                
                with open(chunk_file, 'w') as f:
                    if library.mol_type == "peptide":
                        for mol in chunk:
                            f.write(f">{mol.id}\n{mol.sequence}\n")
                    elif library.mol_type == "small_molecule":
                        f.write("molecule_id,smiles,name,molecular_weight\n")
                        for mol in chunk:
                            f.write(f"{mol.id},{mol.sequence},{mol.name},{mol.molecular_weight}\n")
                
                output_files.append(chunk_file)
            
            logger.info(f"分子库已分割为 {len(output_files)} 个文件，每个包含最多 {chunk_size} 个分子")
            return output_files
            
        except Exception as e:
            logger.error(f"分割分子库失败: {e}")
            return []
