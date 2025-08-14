# /data/boltz_webui/lead_optimization/scoring_system.py

"""
Multi-objective scoring system for compound optimization
"""

import numpy as np
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass

from rdkit import Chem
from rdkit.Chem import Descriptors, Crippen, Lipinski, QED
from rdkit.Chem import rdMolDescriptors
from rdkit.Contrib.SA_Score import sascorer

from exceptions import ScoringError
from config import ScoringWeights, FilterCriteria

logger = logging.getLogger(__name__)

@dataclass
class CompoundScore:
    """Compound scoring result"""
    smiles: str
    binding_affinity: float = 0.0
    selectivity: float = 0.0
    drug_likeness: float = 0.0
    synthetic_accessibility: float = 0.0
    novelty: float = 0.0
    stability: float = 0.0
    combined_score: float = 0.0
    
    # Detailed scores
    binding_probability: float = 0.0
    ic50_um: float = 0.0
    iptm: float = 0.0
    plddt: float = 0.0
    
    # Drug-likeness components
    lipinski_violations: int = 0
    qed_score: float = 0.0
    sa_score: float = 0.0
    molecular_weight: float = 0.0
    logp: float = 0.0
    
    # Properties
    properties: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.properties is None:
            self.properties = {}

class MultiObjectiveScoring:
    """
    Multi-objective scoring system for compound optimization
    """
    
    def __init__(self, weights: ScoringWeights, filters: FilterCriteria):
        self.weights = weights.normalize()
        self.filters = filters
        
        logger.info("Multi-objective scoring system initialized")
    
    def score_compound(self, 
                      smiles: str,
                      boltz_results: Optional[Dict[str, Any]] = None,
                      reference_smiles: Optional[str] = None) -> CompoundScore:
        """
        Score a single compound using multiple objectives
        
        Args:
            smiles: Compound SMILES string
            boltz_results: Boltz-WebUI prediction results
            reference_smiles: Reference compound for novelty calculation
            
        Returns:
            CompoundScore object
        """
        try:
            mol = Chem.MolFromSmiles(smiles)
            if not mol:
                raise ScoringError(f"Invalid SMILES: {smiles}")
            
            score = CompoundScore(smiles=smiles)
            
            # Calculate molecular properties
            properties = self._calculate_properties(mol)
            score.properties = properties
            
            # Extract detailed Boltz results
            if boltz_results:
                # Map Boltz result keys to our attributes
                score.binding_probability = boltz_results.get('affinity_probability_binary', 0.0)
                score.plddt = boltz_results.get('complex_plddt', 0.0)
                score.iptm = boltz_results.get('iptm', 0.0)
                
                # Convert affinity prediction to IC50 (approximation)
                affinity_pred = boltz_results.get('affinity_pred_value', 0.0)
                if affinity_pred > 0:
                    # Simple conversion: higher affinity_pred -> lower IC50
                    score.ic50_um = max(0.001, 10.0 / (1.0 + affinity_pred))
                else:
                    score.ic50_um = 100.0  # High IC50 for low affinity
            
            # Binding affinity score
            score.binding_affinity = self._score_binding_affinity(boltz_results)
            
            # Selectivity score  
            score.selectivity = self._score_selectivity(boltz_results)
            
            # Drug-likeness score
            score.drug_likeness = self._score_drug_likeness(mol, properties)
            
            # Synthetic accessibility score
            score.synthetic_accessibility = self._score_synthetic_accessibility(mol)
            
            # Novelty score
            score.novelty = self._score_novelty(smiles, reference_smiles)
            
            # Stability score (from Boltz results)
            score.stability = self._score_stability(boltz_results)
            
            # Set drug-likeness components and molecular properties
            score.molecular_weight = properties.get('molecular_weight', 0.0)
            score.logp = properties.get('logp', 0.0)
            score.lipinski_violations = self._count_lipinski_violations(properties)
            score.qed_score = self._calculate_qed(mol)
            score.sa_score = properties.get('sa_score', 0.0)
            
            # Calculate combined score
            score.combined_score = self._calculate_combined_score(score)
            
            return score
            
        except Exception as e:
            logger.error(f"Scoring failed for {smiles}: {e}")
            # Return zero score for failed compounds
            return CompoundScore(smiles=smiles)
    
    def _calculate_properties(self, mol: Chem.Mol) -> Dict[str, float]:
        """Calculate molecular properties"""
        try:
            properties = {
                'molecular_weight': Descriptors.MolWt(mol),
                'logp': Descriptors.MolLogP(mol),
                'hbd': Descriptors.NumHDonors(mol),
                'hba': Descriptors.NumHAcceptors(mol),
                'rotatable_bonds': Descriptors.NumRotatableBonds(mol),
                'psa': Descriptors.TPSA(mol),
                'aromatic_rings': Descriptors.NumAromaticRings(mol),
                'heavy_atoms': mol.GetNumHeavyAtoms(),
                'formal_charge': Chem.rdmolops.GetFormalCharge(mol)
            }
            
            return properties
            
        except Exception as e:
            logger.warning(f"Property calculation failed: {e}")
            return {}
    
    def _count_lipinski_violations(self, properties: Dict[str, float]) -> int:
        """Count Lipinski Rule of Five violations"""
        violations = 0
        
        mw = properties.get('molecular_weight', 0)
        logp = properties.get('logp', 0)
        hbd = properties.get('hbd', 0)
        hba = properties.get('hba', 0)
        
        if mw > 500:
            violations += 1
        if logp > 5:
            violations += 1
        if hbd > 5:
            violations += 1
        if hba > 10:
            violations += 1
            
        return violations
    
    def _calculate_qed(self, mol: Chem.Mol) -> float:
        """Calculate Quantitative Estimate of Drug-likeness (QED)"""
        try:
            from rdkit.Chem import QED
            return QED.qed(mol)
        except Exception as e:
            logger.warning(f"QED calculation failed: {e}")
            return 0.0
    
    def _score_binding_affinity(self, boltz_results: Optional[Dict[str, Any]]) -> float:
        """Score binding affinity from Boltz results"""
        if not boltz_results:
            return 0.0
        
        try:
            # Try different possible keys for affinity
            affinity_keys = ['binding_probability', 'affinity', 'ic50_uM', 'delta_g_kcal_mol']
            
            for key in affinity_keys:
                if key in boltz_results:
                    value = float(boltz_results[key])
                    
                    if key == 'binding_probability':
                        return value  # Already 0-1
                    elif key == 'ic50_uM':
                        # Convert IC50 to score (lower is better)
                        if value <= 0:
                            return 0.0
                        return max(0.0, 1.0 - np.log10(value) / 6.0)  # Assume max IC50 ~1M
                    elif key == 'delta_g_kcal_mol':
                        # Convert ΔG to score (more negative is better)
                        return max(0.0, min(1.0, (-value) / 15.0))  # Assume -15 kcal/mol is excellent
                    else:
                        return min(1.0, max(0.0, value))  # Clamp to 0-1
            
            # Fallback: use iPTM as proxy for binding quality
            iptm = boltz_results.get('iptm', 0.0)
            return float(iptm) / 100.0 if iptm > 1 else float(iptm)
            
        except Exception as e:
            logger.warning(f"Binding affinity scoring failed: {e}")
            return 0.0
    
    def _score_selectivity(self, boltz_results: Optional[Dict[str, Any]]) -> float:
        """Score selectivity (placeholder - could be enhanced with off-target predictions)"""
        if not boltz_results:
            return 0.5  # Neutral score
        
        # For now, return a baseline score
        # In a real implementation, this would involve off-target predictions
        return 0.7
    
    def _score_drug_likeness(self, mol: Chem.Mol, properties: Dict[str, float]) -> float:
        """Score drug-likeness using Lipinski's Rule of Five and QED"""
        try:
            # Lipinski violations
            violations = 0
            
            mw = properties.get('molecular_weight', 0)
            logp = properties.get('logp', 0)
            hbd = properties.get('hbd', 0)
            hba = properties.get('hba', 0)
            
            if mw > 500:
                violations += 1
            if logp > 5:
                violations += 1
            if hbd > 5:
                violations += 1
            if hba > 10:
                violations += 1
            
            # Calculate QED score
            qed_score = QED.qed(mol)
            
            # Combine Lipinski and QED
            lipinski_score = max(0.0, (4 - violations) / 4.0)
            drug_likeness_score = (lipinski_score + qed_score) / 2.0
            
            return drug_likeness_score
            
        except Exception as e:
            logger.warning(f"Drug-likeness scoring failed: {e}")
            return 0.0
    
    def _score_synthetic_accessibility(self, mol: Chem.Mol) -> float:
        """Score synthetic accessibility using SA_Score"""
        try:
            sa_score = sascorer.calculateScore(mol)
            # SA_Score ranges from 1 (easy) to 10 (difficult)
            # Convert to 0-1 scale where 1 is most accessible
            normalized_score = max(0.0, (10 - sa_score) / 9.0)
            return normalized_score
            
        except Exception as e:
            logger.warning(f"SA scoring failed: {e}")
            return 0.5  # Neutral score
    
    def _score_novelty(self, smiles: str, reference_smiles: Optional[str]) -> float:
        """Score novelty compared to reference compound"""
        if not reference_smiles:
            return 0.7  # Neutral score
        
        try:
            from rdkit.Chem import DataStructs
            from rdkit.Chem.rdMolDescriptors import GetMorganFingerprintAsBitVect
            
            mol1 = Chem.MolFromSmiles(smiles)
            mol2 = Chem.MolFromSmiles(reference_smiles)
            
            if not mol1 or not mol2:
                return 0.5
            
            # Calculate Tanimoto similarity
            fp1 = GetMorganFingerprintAsBitVect(mol1, 2)
            fp2 = GetMorganFingerprintAsBitVect(mol2, 2)
            similarity = DataStructs.TanimotoSimilarity(fp1, fp2)
            
            # Novelty is inverse of similarity, but we want some similarity
            # Optimal range might be 0.3-0.7 similarity
            if 0.3 <= similarity <= 0.7:
                novelty = 1.0 - abs(similarity - 0.5) / 0.2
            else:
                novelty = max(0.0, 1.0 - similarity)
            
            return novelty
            
        except Exception as e:
            logger.warning(f"Novelty scoring failed: {e}")
            return 0.5
    
    def _score_stability(self, boltz_results: Optional[Dict[str, Any]]) -> float:
        """Score structural stability from Boltz results"""
        if not boltz_results:
            return 0.5
        
        try:
            # Use pLDDT as stability measure
            plddt = boltz_results.get('plddt', boltz_results.get('complex_iplddt', 0.0))
            
            if plddt > 1:  # Assume percentage
                return plddt / 100.0
            else:
                return float(plddt)
            
        except Exception as e:
            logger.warning(f"Stability scoring failed: {e}")
            return 0.5
    
    def _calculate_combined_score(self, score: CompoundScore) -> float:
        """Calculate weighted combined score"""
        try:
            combined = (
                self.weights.binding_affinity * score.binding_affinity +
                self.weights.selectivity * score.selectivity +
                self.weights.drug_likeness * score.drug_likeness +
                self.weights.synthetic_accessibility * score.synthetic_accessibility +
                self.weights.novelty * score.novelty +
                self.weights.stability * score.stability
            )
            
            return max(0.0, min(1.0, combined))
            
        except Exception as e:
            logger.error(f"Combined score calculation failed: {e}")
            return 0.0
    
    def rank_compounds(self, scores: List[CompoundScore]) -> List[CompoundScore]:
        """Rank compounds by combined score"""
        return sorted(scores, key=lambda x: x.combined_score, reverse=True)
    
    def apply_filters(self, scores: List[CompoundScore]) -> List[CompoundScore]:
        """Apply filtering criteria"""
        if not self.filters:
            return scores
        
        filtered_scores = []
        
        for score in scores:
            # Apply filters based on criteria
            if self._passes_filters(score):
                filtered_scores.append(score)
        
        logger.info(f"Filtered {len(scores)} compounds to {len(filtered_scores)}")
        return filtered_scores
    
    def _passes_filters(self, score: CompoundScore) -> bool:
        """Check if compound passes all filters"""
        try:
            props = score.properties
            
            # Molecular weight filter
            if self.filters.max_molecular_weight:
                mw = props.get('molecular_weight', 0)
                if mw > self.filters.max_molecular_weight:
                    return False
            
            # LogP filter
            if self.filters.max_logp:
                logp = props.get('logp', 0)
                if logp > self.filters.max_logp:
                    return False
            
            # Combined score filter
            if self.filters.min_combined_score:
                if score.combined_score < self.filters.min_combined_score:
                    return False
            
            # Binding affinity filter
            if self.filters.min_binding_affinity:
                if score.binding_affinity < self.filters.min_binding_affinity:
                    return False
            
            return True
            
        except Exception as e:
            logger.warning(f"Filter check failed: {e}")
            return False
