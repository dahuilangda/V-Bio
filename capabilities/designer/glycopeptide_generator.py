# /V-Bio/designer/glycopeptide_generator.py

"""
Glycopeptide Generator - 糖肽修饰预处理器

- 通过脱水缩合将糖基连接到氨基酸（如Ser-O + MAN-C1-OH -> Ser-O-MAN + H2O）
- 生成正确的原子命名和属性
- 保存到Boltz CCD缓存供后续使用
"""


import os
import pickle
import logging
from pathlib import Path
from typing import Dict, Tuple, Optional
from rdkit import Chem
from rdkit.Chem import AllChem

Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

logger = logging.getLogger(__name__)

class GlycopeptideGenerator:
    """糖肽修饰生成器 - 专用于生成糖基化的非标准氨基酸"""
    
    def __init__(self, boltz_cache_dir: str = None):
        """
        初始化糖肽生成器
        
        Args:
            boltz_cache_dir: Boltz缓存目录路径，如果为None则自动查找
        """
        self.boltz_cache_dir = self._find_boltz_cache(boltz_cache_dir)
        self.ccd_path = self.boltz_cache_dir / 'ccd.pkl'
        self.ccd_cache = self._load_ccd_cache()
        
        # 糖基SMILES - 来自化学数据库的标准结构
        self.monosaccharide_smiles = {
            'MAN': 'OC[C@H]1O[C@H](O)[C@@H](O)[C@H](O)[C@@H]1O',  # α-D-甘露糖
            'NAG': 'CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@H](O[C@H]2[C@H](O)[C@@H](O)[C@H](O)[C@@H](CO)O2)[C@@H](CO)O1',  # N-乙酰葡糖胺
            'GAL': 'OC[C@H]1O[C@H](O)[C@@H](O)[C@@H](O)[C@H]1O',  # β-D-半乳糖
            'FUC': 'C[C@H]1O[C@H](O)[C@H](O)[C@H](O)[C@H]1O',  # α-L-岩藻糖
            'GLC': 'OC[C@H]1O[C@H](O)[C@@H](O)[C@H](O)[C@@H]1O',  # β-D-葡萄糖
            'XYL': 'O[C@H]1[C@@H](O)[C@H](O)[C@H](O)C1O',  # β-D-木糖
        }
        
        # 氨基酸SMILES - 标准L-氨基酸
        self.amino_acid_smiles = {
            'N': 'N[C@@H](CC(N)=O)C(O)=O',      # 天冬酰胺 (N-连接位点)
            'S': 'N[C@@H](CO)C(O)=O',           # 丝氨酸 (O-连接位点)
            'T': 'N[C@@H]([C@H](O)C)C(O)=O',    # 苏氨酸 (O-连接位点)
            'Y': 'N[C@@H](Cc1ccc(O)cc1)C(O)=O', # 酪氨酸 (O-连接位点，少见)
        }
        
        # 连接方式定义 - 糖基化位点的原子
        self.linkage_atoms = {
            'N': 'ND2',  # 天冬酰胺侧链胺基氮（N-连接）
            'S': 'OG',   # 丝氨酸羟基氧（O-连接）
            'T': 'OG1',  # 苏氨酸羟基氧（O-连接）
            'Y': 'OH',   # 酪氨酸酚羟基氧（O-连接）
        }
    
    def _find_boltz_cache(self, cache_dir: str = None) -> Path:
        """查找或创建Boltz缓存目录"""
        if cache_dir:
            return Path(cache_dir)
        
        # 尝试常见的缓存位置
        possible_paths = [
            Path.home() / '.boltz',
            Path.home() / '.cache' / 'boltz',
            Path('/tmp/boltz_cache'),
            Path('./boltz_cache'),
        ]
        
        for path in possible_paths:
            if path.exists() and (path / 'ccd.pkl').exists():
                logger.info(f"Found Boltz cache at: {path}")
                return path
        
        # 创建默认缓存目录
        default_path = Path.home() / '.boltz'
        default_path.mkdir(exist_ok=True)
        logger.info(f"Creating Boltz cache at: {default_path}")
        return default_path
    
    def _load_ccd_cache(self) -> Dict:
        """加载现有的CCD缓存"""
        try:
            if self.ccd_path.exists():
                with self.ccd_path.open("rb") as f:
                    cache = pickle.load(f)
                logger.info(f"Loaded CCD cache with {len(cache)} entries")
                return cache
            else:
                logger.info("CCD cache not found, starting with empty cache")
                return {}
        except Exception as e:
            logger.error(f"Failed to load CCD cache: {e}")
            return {}
    
    def _save_ccd_cache(self):
        """保存CCD缓存到磁盘"""
        try:
            self.ccd_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ccd_path.open("wb") as f:
                pickle.dump(self.ccd_cache, f)
            logger.info(f"Saved CCD cache with {len(self.ccd_cache)} entries to {self.ccd_path}")
        except Exception as e:
            logger.error(f"Failed to save CCD cache: {e}")
            raise
    
    def create_glycosylated_residue(
        self, 
        amino_acid: str, 
        glycan: str
    ) -> Tuple[Chem.Mol, str]:
        """
        创建糖基化的氨基酸残基
        
        参考Benjamin Fry的方法，构建完整的糖基化氨基酸分子
        
        Args:
            amino_acid: 氨基酸单字母代码 (N, S, T, Y)
            glycan: 糖基代码 (MAN, NAG, GAL等)
            
        Returns:
            (修饰后的分子, CCD代码)
        """
        if amino_acid not in self.amino_acid_smiles:
            raise ValueError(f"Unsupported amino acid: {amino_acid}")
        
        if glycan not in self.monosaccharide_smiles:
            raise ValueError(f"Unsupported glycan: {glycan}")
        
        # 创建CCD代码
        ccd_code = f"{glycan}{amino_acid}"
        
        # 创建糖基化分子（使用预构建的SMILES）
        glycosylated_mol = self._synthesize_glycopeptide_molecule(amino_acid, glycan)
        
        if glycosylated_mol is None:
            raise ValueError(f"Failed to synthesize {ccd_code}")
        
        # 去除氢原子进行原子属性设置
        mol_no_h = Chem.RemoveHs(glycosylated_mol)
        
        # 设置Boltz需要的原子属性
        self._set_boltz_atom_properties(mol_no_h, amino_acid)
        
        # 重新排序原子以匹配标准氨基酸顺序
        reordered_mol = self._reorder_atoms_canonical(mol_no_h, amino_acid)
        
        # 添加氢原子，保留重原子的属性
        final_mol = self._add_hydrogens_preserving_properties(reordered_mol)
        
        # 生成3D构象
        self._generate_conformation(final_mol)
        
        # 设置构象属性
        for conformer in final_mol.GetConformers():
            conformer.SetProp('name', 'Ideal')
        
        return final_mol, ccd_code
    
    def _add_hydrogens_preserving_properties(self, mol: Chem.Mol) -> Chem.Mol:
        """
        添加氢原子同时保留重原子的属性
        """
        try:
            # 保存重原子的属性
            heavy_atom_props = []
            for atom in mol.GetAtoms():
                props = {}
                for prop in ['name', 'alt_name', 'leaving_atom']:
                    if atom.HasProp(prop):
                        props[prop] = atom.GetProp(prop) if prop != 'leaving_atom' else atom.GetBoolProp(prop)
                heavy_atom_props.append(props)
            
            # 添加氢原子
            mol_with_h = Chem.AddHs(mol)
            
            # 恢复重原子的属性
            heavy_idx = 0
            for atom in mol_with_h.GetAtoms():
                if atom.GetSymbol() != 'H':
                    if heavy_idx < len(heavy_atom_props):
                        props = heavy_atom_props[heavy_idx]
                        for prop_name, prop_value in props.items():
                            if prop_name == 'leaving_atom':
                                atom.SetBoolProp(prop_name, prop_value)
                            else:
                                atom.SetProp(prop_name, prop_value)
                    heavy_idx += 1
                else:
                    # 为氢原子设置简单的名称
                    atom.SetProp('name', f'H{atom.GetIdx()}')
                    atom.SetProp('alt_name', f'H{atom.GetIdx()}')
                    atom.SetBoolProp('leaving_atom', False)
            
            return mol_with_h
            
        except Exception as e:
            logger.error(f"Failed to add hydrogens while preserving properties: {e}")
            return Chem.AddHs(mol)
    
    def _synthesize_glycopeptide_molecule(self, amino_acid: str, glycan: str) -> Chem.Mol:
        """
        合成糖肽分子 - 直接构建已经脱水缩合后的完整SMILES
        
        参考Benjamin Fry的方法，直接提供完整的糖基化氨基酸SMILES
        """
        try:
            # 直接构建已经糖基化的氨基酸SMILES
            glycopeptide_smiles = self._get_prebuilt_glycopeptide_smiles(amino_acid, glycan)
            
            if not glycopeptide_smiles:
                logger.error(f"No prebuilt SMILES available for {amino_acid}-{glycan}")
                return None
            
            # 从SMILES创建分子
            mol = Chem.MolFromSmiles(glycopeptide_smiles)
            
            if mol:
                try:
                    Chem.SanitizeMol(mol)
                    return mol
                except Exception as e:
                    logger.error(f"Failed to sanitize {amino_acid}-{glycan} molecule: {e}")
                    return None
            else:
                logger.error(f"Failed to create molecule from SMILES for {amino_acid}-{glycan}")
                return None
                
        except Exception as e:
            logger.error(f"Molecule synthesis failed for {amino_acid}-{glycan}: {e}")
            return None
    
    def _get_prebuilt_glycopeptide_smiles(self, amino_acid: str, glycan: str) -> str:
        """
        获取预构建的糖基化氨基酸SMILES
        
        这些SMILES是已经完成脱水缩合反应的最终产物
        """
        # 糖基化氨基酸的预构建SMILES字典
        glycopeptide_smiles = {
            # 丝氨酸糖基化 (O-连接通过CH2OH)
            ('S', 'MAN'): "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C(=O)O",
            ('S', 'NAG'): "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(=O)C)C(=O)O", 
            ('S', 'GAL'): "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C(=O)O",
            ('S', 'FUC'): "N[C@@H](CO[C@H]1O[C@@H](C)[C@H](O)[C@H](O)[C@H]1O)C(=O)O",
            ('S', 'GLC'): "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C(=O)O",
            ('S', 'XYL'): "N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H]1O)C(=O)O",
            
            # 苏氨酸糖基化 (O-连接通过CHOH)
            ('T', 'MAN'): "N[C@@H]([C@H](O[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C)C(=O)O",
            ('T', 'NAG'): "N[C@@H]([C@H](O[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(=O)C)C)C(=O)O",
            ('T', 'GAL'): "N[C@@H]([C@H](O[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C)C(=O)O",
            ('T', 'FUC'): "N[C@@H]([C@H](O[C@H]1O[C@@H](C)[C@H](O)[C@H](O)[C@H]1O)C)C(=O)O",
            ('T', 'GLC'): "N[C@@H]([C@H](O[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C)C(=O)O",
            ('T', 'XYL'): "N[C@@H]([C@H](O[C@H]1O[C@H](CO)[C@@H](O)[C@H]1O)C)C(=O)O",
            
            # 酪氨酸糖基化 (O-连接通过酚羟基)
            ('Y', 'MAN'): "N[C@@H](Cc1ccc(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]2O)cc1)C(=O)O",
            ('Y', 'NAG'): "N[C@@H](Cc1ccc(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]2NC(=O)C)cc1)C(=O)O",
            ('Y', 'GAL'): "N[C@@H](Cc1ccc(O[C@H]2O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]2O)cc1)C(=O)O",
            ('Y', 'FUC'): "N[C@@H](Cc1ccc(O[C@H]2O[C@@H](C)[C@H](O)[C@H](O)[C@H]2O)cc1)C(=O)O",
            ('Y', 'GLC'): "N[C@@H](Cc1ccc(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]2O)cc1)C(=O)O",
            ('Y', 'XYL'): "N[C@@H](Cc1ccc(O[C@H]2O[C@H](CO)[C@@H](O)[C@H]2O)cc1)C(=O)O",
            
            # 天冬酰胺糖基化 (N-连接通过酰胺氮)
            ('N', 'MAN'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H](O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O))C(=O)O",
            ('N', 'NAG'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H](O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(=O)C))C(=O)O",
            ('N', 'GAL'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@@H](O)[C@H](O[C@H](CO)[C@@H](O)[C@@H]1O))C(=O)O",
            ('N', 'FUC'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@H](O)[C@H](O[C@@H](C)[C@H](O)[C@H]1O))C(=O)O",
            ('N', 'GLC'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@H](O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O))C(=O)O",
            ('N', 'XYL'): "N[C@@H](CC(=O)N[C@H]1[C@H](O)[C@@H](O)[C@H](O[C@H](CO)[C@@H]1O))C(=O)O",
        }
        
        return glycopeptide_smiles.get((amino_acid, glycan))
    
    def _generate_conformation(self, mol: Chem.Mol):
        """生成分子的3D构象"""
        if mol is None:
            return
            
        try:
            # 确保分子被正确初始化
            if mol.GetNumConformers() == 0:
                # 如果分子没有氢原子，先添加
                if mol.GetNumAtoms() > 0:
                    # 检查是否需要sanitize
                    try:
                        Chem.SanitizeMol(mol)
                    except:
                        pass  # 可能已经sanitized
                    
                    # 生成构象
                    if AllChem.EmbedMolecule(mol) != -1:
                        try:
                            AllChem.UFFOptimizeMolecule(mol)
                        except:
                            logger.debug("UFF optimization failed, using embedded geometry")
                    
                    # 设置构象名称
                    if mol.GetNumConformers() > 0:
                        mol.GetConformer().SetProp('name', 'Ideal')
                
        except Exception as e:
            logger.debug(f"Conformation generation failed: {e}")
            # 如果所有方法都失败，创建一个空构象
            pass
    
    def _set_boltz_atom_properties(self, mol: Chem.Mol, reference_aa: str):
        """
        设置Boltz期望的原子属性
        
        参考Benjamin Fry的方法，使用子结构匹配来设置原子名称
        """
        if mol is None:
            return
            
        try:
            # 获取参考氨基酸分子
            reference_mol = self._get_reference_amino_acid_mol(reference_aa)
            if reference_mol is None:
                logger.warning(f"No reference molecule found for {reference_aa}")
                self._set_default_atom_properties(mol)
                return
            
            # 去除氢原子进行子结构匹配
            ref_no_h = Chem.RemoveHs(reference_mol)
            
            # 检查子结构匹配
            if mol.HasSubstructMatch(ref_no_h):
                match_indices = mol.GetSubstructMatch(ref_no_h)
                
                logger.debug(f"Substructure match found: {match_indices}")
                
                # 设置匹配的氨基酸原子的属性
                for ref_idx, mol_idx in enumerate(match_indices):
                    ref_atom = ref_no_h.GetAtoms()[ref_idx]
                    mol_atom = mol.GetAtoms()[mol_idx]
                    
                    if ref_atom.HasProp('name'):
                        name = ref_atom.GetProp('name')
                        mol_atom.SetProp('name', name)
                        mol_atom.SetProp('alt_name', name)
                        mol_atom.SetBoolProp('leaving_atom', name == 'OXT')
                        logger.debug(f"Set atom {mol_idx}: {mol_atom.GetSymbol()} -> {name}")
                
                # 设置未匹配的原子（糖基部分）的属性
                matched_indices = set(match_indices)
                for idx, atom in enumerate(mol.GetAtoms()):
                    if idx not in matched_indices:
                        name = f'{atom.GetSymbol()}{idx}'
                        atom.SetProp('name', name)
                        atom.SetProp('alt_name', name)
                        atom.SetBoolProp('leaving_atom', False)
                
                logger.debug(f"Successfully set atom properties using substructure match for {reference_aa}")
                    
            else:
                logger.warning(f"Substructure match failed for {reference_aa}")
                self._set_default_atom_properties(mol)
                
        except Exception as e:
            logger.error(f"Failed to set atom properties: {e}")
            self._set_default_atom_properties(mol)
    
    def _get_reference_amino_acid_mol(self, amino_acid: str) -> Optional[Chem.Mol]:
        """获取参考氨基酸分子，优先从缓存中获取"""
        aa_3letter = {'N': 'ASN', 'S': 'SER', 'T': 'THR', 'Y': 'TYR'}
        
        if amino_acid in aa_3letter:
            ccd_code = aa_3letter[amino_acid]
            if ccd_code in self.ccd_cache:
                return self.ccd_cache[ccd_code]
        
        # 如果缓存中没有，从SMILES创建
        if amino_acid in self.amino_acid_smiles:
            mol = Chem.MolFromSmiles(self.amino_acid_smiles[amino_acid])
            if mol:
                # 设置标准氨基酸原子名称
                self._set_standard_aa_atom_names(mol, amino_acid)
                return mol
        
        return None
    
    def _set_standard_aa_atom_names(self, mol: Chem.Mol, amino_acid: str):
        """为标准氨基酸设置原子名称"""
        standard_names = {
            'N': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'ND2', 'OXT'],
            'S': ['N', 'CA', 'C', 'O', 'CB', 'OG', 'OXT'],
            'T': ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2', 'OXT'],
            'Y': ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ', 'OH', 'OXT']
        }
        
        names = standard_names.get(amino_acid, [])
        for idx, atom in enumerate(mol.GetAtoms()):
            if atom.GetSymbol() == 'H':
                continue
            name = names[idx] if idx < len(names) else f'{atom.GetSymbol()}{idx}'
            atom.SetProp('name', name)
    
    def _set_default_atom_properties(self, mol: Chem.Mol):
        """设置默认的原子属性"""
        for idx, atom in enumerate(mol.GetAtoms()):
            if atom.GetSymbol() == 'H':
                continue
            name = f'{atom.GetSymbol()}{idx}'
            atom.SetProp('name', name)
            atom.SetProp('alt_name', name)
            atom.SetBoolProp('leaving_atom', False)
    
    def _reorder_atoms_canonical(self, mol: Chem.Mol, reference_aa: str) -> Chem.Mol:
        """
        重新排序原子以匹配标准氨基酸的顺序
        参考Benjamin Fry的方法
        """
        try:
            # 获取当前原子顺序
            curr_atom_order = {}
            for idx, atom in enumerate(mol.GetAtoms()):
                if atom.GetSymbol() != 'H' and atom.HasProp('name'):
                    curr_atom_order[atom.GetProp('name')] = idx
            
            # 获取目标原子顺序（标准氨基酸原子）
            target_atom_order = self._get_target_atom_order(reference_aa)
            
            # 构建重新映射顺序
            remapped_atom_order = {}
            offset_idx = len(target_atom_order)
            
            for atom_name in curr_atom_order:
                if atom_name in target_atom_order:
                    remapped_atom_order[atom_name] = target_atom_order[atom_name]
                else:
                    remapped_atom_order[atom_name] = offset_idx
                    offset_idx += 1
            
            # 移除氢原子并重新排序
            mol_no_h = Chem.RemoveHs(mol)
            
            # 构建重新排序的索引列表
            remap_order = {}
            for atom in mol_no_h.GetAtoms():
                if atom.HasProp('name'):
                    atom_name = atom.GetProp('name')
                    if atom_name in remapped_atom_order:
                        remap_order[atom_name] = (remapped_atom_order[atom_name], atom.GetIdx())
            
            if remap_order:
                remap_idx_list = [x[1] for x in sorted(remap_order.values())]
                reordered_mol = Chem.RenumberAtoms(mol_no_h, remap_idx_list)
                return reordered_mol
            else:
                return mol_no_h
                
        except Exception as e:
            logger.error(f"Atom reordering failed: {e}")
            return Chem.RemoveHs(mol)
    
    def _get_target_atom_order(self, amino_acid: str) -> Dict[str, int]:
        """获取标准氨基酸的原子顺序"""
        standard_orders = {
            'N': {'N': 0, 'CA': 1, 'C': 2, 'O': 3, 'CB': 4, 'CG': 5, 'OD1': 6, 'ND2': 7, 'OXT': 8},
            'S': {'N': 0, 'CA': 1, 'C': 2, 'O': 3, 'CB': 4, 'OG': 5, 'OXT': 6},
            'T': {'N': 0, 'CA': 1, 'C': 2, 'O': 3, 'CB': 4, 'OG1': 5, 'CG2': 6, 'OXT': 7},
            'Y': {'N': 0, 'CA': 1, 'C': 2, 'O': 3, 'CB': 4, 'CG': 5, 'CD1': 6, 'CD2': 7, 
                  'CE1': 8, 'CE2': 9, 'CZ': 10, 'OH': 11, 'OXT': 12}
        }
        return standard_orders.get(amino_acid, {})
    
    def generate_all_glycopeptides(self) -> Dict[str, Chem.Mol]:
        """生成所有支持的糖肽组合"""
        glycopeptides = {}
        
        for glycan in self.monosaccharide_smiles.keys():
            for amino_acid in self.amino_acid_smiles.keys():
                try:
                    mol, ccd_code = self.create_glycosylated_residue(amino_acid, glycan)
                    if mol is not None:  # 检查分子是否成功生成
                        glycopeptides[ccd_code] = mol
                        logger.info(f"Generated {ccd_code}: {amino_acid} modified with {glycan}")
                    else:
                        logger.error(f"Failed to generate {glycan}{amino_acid}: Molecule generation returned None")
                except Exception as e:
                    logger.error(f"Failed to generate {glycan}{amino_acid}: {e}")
        
        return glycopeptides
    
    def add_to_cache(self, glycopeptides: Dict[str, Chem.Mol] = None) -> int:
        """将糖肽添加到CCD缓存"""
        if glycopeptides is None:
            glycopeptides = self.generate_all_glycopeptides()
        
        initial_count = len(self.ccd_cache)
        updated_count = 0
        new_count = 0
        
        # 添加生成的分子到缓存
        for ccd_code, mol in glycopeptides.items():
            if mol is not None:
                if ccd_code in self.ccd_cache:
                    # 更新现有条目
                    self.ccd_cache[ccd_code] = mol
                    updated_count += 1
                    logger.debug(f"Updated existing entry: {ccd_code}")
                else:
                    # 添加新条目
                    self.ccd_cache[ccd_code] = mol
                    new_count += 1
                    logger.debug(f"Added new entry: {ccd_code}")
            else:
                logger.warning(f"Skipping {ccd_code}: molecule is None")
        
        # 保存到磁盘
        self._save_ccd_cache()
        
        logger.info(f"Cache operations: {new_count} new, {updated_count} updated")
        return new_count + updated_count
    
    def list_available_modifications(self) -> Dict[str, str]:
        """列出所有可用的糖肽修饰"""
        modifications = {}
        
        for glycan, glycan_smiles in self.monosaccharide_smiles.items():
            for aa, aa_smiles in self.amino_acid_smiles.items():
                ccd_code = f"{glycan}{aa}"
                description = f"{aa} modified with {glycan}"
                modifications[ccd_code] = description
        
        return modifications


def main():
    """主函数 - 生成糖肽修饰并保存到CCD缓存"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Glycopeptide Generator - Generate CCD modifications for Boltz')
    parser.add_argument('--cache-dir', type=str, help='Boltz cache directory')
    parser.add_argument('--generate-all', action='store_true', default=True,
                       help='Generate all supported glycopeptide modifications')
    parser.add_argument('--list-only', action='store_true', 
                       help='Only list available modifications without generating')
    parser.add_argument('--specific', nargs=2, metavar=('AA', 'GLYCAN'),
                       help='Generate specific modification (e.g., --specific S MAN)')
    
    args = parser.parse_args()
    
    # 设置日志
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    print("🧬 Glycopeptide Generator for Boltz CCD Cache")
    print("=" * 50)
    
    # 创建生成器
    generator = GlycopeptideGenerator(args.cache_dir)
    
    if args.list_only:
        print("\nAvailable glycopeptide modifications:")
        modifications = generator.list_available_modifications()
        for ccd_code, description in modifications.items():
            print(f"  {ccd_code:6} : {description}")
        return
    
    if args.specific:
        amino_acid, glycan = args.specific
        try:
            mol, ccd_code = generator.create_glycosylated_residue(amino_acid, glycan)
            generator.ccd_cache[ccd_code] = mol
            generator._save_ccd_cache()
            print(f"✅ Generated specific modification: {ccd_code}")
        except Exception as e:
            print(f"❌ Failed to generate {glycan}{amino_acid}: {e}")
        return
    
    if args.generate_all:
        print("\nGenerating all supported glycopeptide modifications...")
        try:
            glycopeptides_generated = generator.generate_all_glycopeptides()
            count = generator.add_to_cache(glycopeptides_generated)
            
            # 统计实际生成的数量
            successful_generations = len([mol for mol in glycopeptides_generated.values() if mol is not None])
            
            print(f"✅ Successfully generated {successful_generations} modifications")
            print(f"💾 Processed {count} cache operations (new + updated)")
            print(f"💾 Cache saved to: {generator.ccd_path}")
            
            print("\nGenerated modifications:")
            for ccd_code, description in generator.list_available_modifications().items():
                if ccd_code in glycopeptides_generated and glycopeptides_generated[ccd_code] is not None:
                    # 检查是否是新增还是更新
                    if ccd_code in generator.ccd_cache:
                        status = "✅"
                    else:
                        status = "🆕"
                else:
                    status = "❌"
                print(f"  {status} {ccd_code:6} : {description}")
                
        except Exception as e:
            print(f"❌ Failed to generate modifications: {e}")
            raise


if __name__ == '__main__':
    main()
