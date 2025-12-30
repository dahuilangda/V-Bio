# /data/boltz_webui/lead_optimization/optimization_engine_new.py

"""
Advanced optimization engine for drug discovery
Uses MMPDB and Boltz-WebUI to optimize lead compounds
"""

import os
import json
import time
import string
import logging
import yaml
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict

from rdkit import Chem
from rdkit.Chem import rdMolDescriptors

from config import OptimizationConfig
from api_client import BoltzOptimizationClient
from mmp_engine import MMPEngine
from scoring_system import MultiObjectiveScoring, CompoundScore
from molecular_evolution import MolecularEvolutionEngine
from diversity_selector import DiversitySelector
from exceptions import OptimizationError, InvalidCompoundError

logger = logging.getLogger(__name__)

@dataclass
class OptimizationResult:
    """Results from optimization run"""
    original_compound: str
    strategy: str
    candidates: List[Dict[str, Any]]
    scores: List[CompoundScore]
    top_candidates: List[Dict[str, Any]]
    execution_time: float
    statistics: Dict[str, Any] = None
    total_candidates: int = 0
    success_rate: float = 0.0
    
    def __post_init__(self):
        if self.statistics is None:
            self.statistics = {}

@dataclass
class OptimizationCandidate:
    """Individual optimization candidate"""
    smiles: str
    compound_id: str
    mmp_transformation: str
    generation_method: str = "mmpdb"
    transformation_rule: str = ""
    parent_smiles: str = ""
    similarity: float = 0.0
    prediction_results: Dict[str, Any] = None
    scores: CompoundScore = None

class OptimizationEngine:
    """
    Lead optimization engine using MMPDB + Boltz-WebUI
    Similar architecture to virtual_screening for consistency
    """
    
    def __init__(self, config: OptimizationConfig):
        self.config = config
        
        # Initialize components
        self.boltz_client = BoltzOptimizationClient(config.boltz_api)
        self.mmp_engine = MMPEngine(config.mmp_database)
        self.scoring_system = MultiObjectiveScoring(config.scoring_weights, config.filters)
        self.diversity_selector = DiversitySelector()
        
        # Results storage
        self.optimization_results: List[CompoundScore] = []
        
        # Global candidate counter for unique IDs across generations
        self.candidate_counter = 0
        
        # Setup directories
        config.setup_directories()
        
        logger.info("Lead optimization engine initialized")
    
    def optimize_compound(self, 
                         compound_smiles: str,
                         target_protein_yaml: str,
                         strategy: str = "scaffold_hopping",
                         max_candidates: int = 50,
                         output_dir: Optional[str] = None,
                         iterations: int = 1,
                         batch_size: int = 4,
                         top_k_per_iteration: int = 5,
                         diversity_weight: float = 0.3,
                         similarity_threshold: float = 0.5,
                         max_similarity_threshold: float = 0.9,
                         diversity_selection_strategy: str = "tanimoto_diverse",
                         max_chiral_centers: int = None,
                         core_smarts: Optional[str] = None,
                         exclude_smarts: Optional[str] = None,
                         rgroup_smarts: Optional[str] = None,
                         variable_smarts: Optional[str] = None,
                         variable_const_smarts: Optional[str] = None) -> OptimizationResult:
        """
        Optimize a single compound using MMPDB + Boltz-WebUI with iterative evolution
        
        Args:
            compound_smiles: Input compound SMILES
            target_protein_yaml: Target protein configuration
            strategy: Optimization strategy
            max_candidates: Maximum number of candidates to generate per iteration
            output_dir: Output directory for results
            iterations: Number of optimization iterations (genetic evolution)
            batch_size: Number of compounds to submit to Boltz simultaneously (GPU limit)
            top_k_per_iteration: Top compounds to use as seeds for next iteration
            
        Returns:
            OptimizationResult object
        """
        start_time = time.time()
        
        try:
            # Validate input compound
            if not self._is_valid_smiles(compound_smiles):
                raise InvalidCompoundError(f"Invalid compound SMILES: {compound_smiles}")
            
            # Setup output directory
            if not output_dir:
                output_dir = os.path.join(self.config.temp_dir, f"optimization_{int(time.time())}")
            os.makedirs(output_dir, exist_ok=True)
            
            logger.info("🚀 Starting iterative optimization")
            logger.info(f"Input compound: {compound_smiles[:50]}...")
            logger.info(f"Strategy: {strategy}")
            logger.info(f"Iterations: {iterations}")
            logger.info(f"Max candidates per iteration: {max_candidates}")
            logger.info(f"Batch size: {batch_size}")
            logger.info(f"Top-K per iteration: {top_k_per_iteration}")
            if core_smarts:
                logger.info(f"核心片段限制: {core_smarts}")
            if exclude_smarts:
                logger.info(f"排除片段限制: {exclude_smarts}")
            if rgroup_smarts:
                logger.info(f"R-group 片段限制: {rgroup_smarts}")
            if variable_smarts:
                logger.info(f"严格可变片段: {variable_smarts}")
            if variable_const_smarts:
                logger.info(f"可变片段常量约束: {variable_const_smarts}")
            
            # Initialize batch evaluator and evolution engine
            from .batch_evaluation import BatchEvaluator
            from .molecular_evolution import MolecularEvolutionEngine
            
            batch_evaluator = BatchEvaluator(self.boltz_client, self.scoring_system, batch_size)
            evolution_engine = MolecularEvolutionEngine()

            core_queries = self._parse_smarts_queries(core_smarts)
            exclude_queries = self._parse_smarts_queries(exclude_smarts)
            rgroup_queries = self._parse_smarts_queries(rgroup_smarts)
            variable_fragments = [p.strip() for p in (variable_smarts or "").split(";;") if p.strip()]
            variable_const_queries = self._parse_rgroup_smarts_queries(variable_const_smarts)
            if core_smarts and not core_queries:
                raise InvalidCompoundError(f"Invalid core SMARTS/SMILES: {core_smarts}")
            if exclude_smarts and not exclude_queries:
                raise InvalidCompoundError(f"Invalid exclude SMARTS/SMILES: {exclude_smarts}")
            if rgroup_smarts and not rgroup_queries:
                raise InvalidCompoundError(f"Invalid R-group SMARTS/SMILES: {rgroup_smarts}")
            if variable_smarts and not variable_fragments:
                raise InvalidCompoundError(f"Invalid variable fragments: {variable_smarts}")
            if variable_const_smarts and not variable_const_queries:
                raise InvalidCompoundError(f"Invalid variable const SMARTS/SMILES: {variable_const_smarts}")

            rule_level_queries = variable_fragments if variable_fragments else []
            variable_queries = []
            variable_excludes = []
            if variable_fragments:
                for fragment in variable_fragments:
                    required_query, exclude_query = self._build_variable_query(compound_smiles, fragment)
                    if required_query is None:
                        raise InvalidCompoundError(f"Invalid variable fragment: {fragment}")
                    variable_queries.append(required_query)
                    if exclude_query is not None:
                        variable_excludes.append(exclude_query)
            if variable_const_queries:
                variable_queries.extend(variable_const_queries)
            
            # Track all evaluated candidates across iterations
            all_evaluated_candidates = []
            generation_results = []  # Track results per generation
            current_seeds = [compound_smiles]  # Start with original compound
            
            # Iterative optimization loop with evolution
            for iteration in range(iterations):
                logger.info(f"🧬 === 进化第 {iteration + 1}/{iterations} 代 ===")
                
                # Get adaptive parameters for this generation
                convergence_rate = evolution_engine.assess_population_diversity(all_evaluated_candidates) if all_evaluated_candidates else 1.0
                adaptive_params = evolution_engine.calculate_adaptive_parameters(iteration, iterations, convergence_rate)
                
                # Generate candidates using evolution-guided strategies
                iteration_candidates = []
                
                if iteration == 0:
                    # First generation: explore around original compound
                    logger.info(f"🌱 第一代：从原始化合物生成候选")
                    candidates = self._generate_candidates_with_mmpdb(
                        compound_smiles, strategy, max_candidates, iteration, iterations,
                        diversity_weight=diversity_weight, similarity_threshold=similarity_threshold,
                        max_chiral_centers=max_chiral_centers, reference_smiles=compound_smiles,
                        rule_query_smarts=rule_level_queries
                    )
                    if candidates:
                        iteration_candidates.extend(candidates)
                else:
                    # Subsequent generations: use evolution engine
                    logger.info(f"🧬 第{iteration + 1}代：使用进化策略")
                    
                    if len(all_evaluated_candidates) >= top_k_per_iteration:
                        # Select parents using evolution engine
                        elite_size = max(1, top_k_per_iteration // 2)
                        diversity_size = max(1, top_k_per_iteration - elite_size)
                        
                        elite_compounds, diverse_compounds = evolution_engine.select_parents_for_next_generation(
                            all_evaluated_candidates, top_k_per_iteration, elite_size
                        )
                        
                        # Generate next generation strategies
                        strategies = evolution_engine.generate_next_generation_strategies(elite_compounds, diverse_compounds)
                        
                        # Generate candidates based on evolution strategies
                        for parent_smiles, evo_strategy, weight in strategies[:max_candidates]:
                            logger.info(f"  从 {parent_smiles[:30]}... 使用策略 {evo_strategy}")
                            candidates = self._generate_candidates_with_mmpdb(
                                parent_smiles, evo_strategy, max(1, int(max_candidates * weight)), 
                                iteration, iterations, diversity_weight=diversity_weight, 
                                similarity_threshold=similarity_threshold,
                                max_chiral_centers=max_chiral_centers, reference_smiles=compound_smiles,
                                rule_query_smarts=rule_level_queries
                            )
                            if candidates:
                                iteration_candidates.extend(candidates[:int(max_candidates * weight)])
                    else:
                        # Not enough candidates yet, continue with current seeds
                        for seed_compound in current_seeds:
                            candidates = self._generate_candidates_with_mmpdb(
                                seed_compound, strategy, max_candidates // len(current_seeds), 
                                iteration, iterations, diversity_weight=diversity_weight, 
                                similarity_threshold=similarity_threshold,
                                max_chiral_centers=max_chiral_centers, reference_smiles=compound_smiles,
                                rule_query_smarts=rule_level_queries
                            )
                            if candidates:
                                iteration_candidates.extend(candidates)
                
                if not iteration_candidates:
                    logger.warning(f"第 {iteration + 1} 代没有生成候选化合物，停止进化")
                    break
                
                # Remove duplicates and previously evaluated compounds
                # Use normalized SMILES for more accurate duplicate detection
                seen_smiles = set()
                for c in all_evaluated_candidates:
                    try:
                        from rdkit import Chem
                        mol = Chem.MolFromSmiles(c.smiles)
                        if mol:
                            canonical_smiles = Chem.MolToSmiles(mol, canonical=True)
                            seen_smiles.add(canonical_smiles)
                        else:
                            seen_smiles.add(c.smiles)  # Fallback
                    except:
                        seen_smiles.add(c.smiles)  # Fallback
                
                unique_candidates = []
                for c in iteration_candidates:
                    try:
                        from rdkit import Chem
                        mol = Chem.MolFromSmiles(c.smiles)
                        if mol:
                            canonical_smiles = Chem.MolToSmiles(mol, canonical=True)
                            if canonical_smiles not in seen_smiles:
                                unique_candidates.append(c)
                                seen_smiles.add(canonical_smiles)  # Add to prevent internal duplicates
                        else:
                            # Invalid SMILES, skip
                            logger.warning(f"跳过无效的 SMILES: {c.smiles}")
                    except Exception as e:
                        logger.warning(f"处理 SMILES {c.smiles} 时出错: {e}")
                
                logger.info(f"第 {iteration + 1} 代总共生成 {len(iteration_candidates)} 个候选，去重后剩余 {len(unique_candidates)} 个")

                if core_queries or rgroup_queries or exclude_queries or variable_queries or variable_excludes:
                    before_filter = len(unique_candidates)
                    unique_candidates = self._filter_candidates_by_queries(
                        unique_candidates,
                        required_queries=[q for q in (core_queries + rgroup_queries + variable_queries) if q is not None],
                        exclude_queries=[q for q in (exclude_queries + variable_excludes) if q is not None]
                    )
                    logger.info(f"子结构过滤后剩余 {len(unique_candidates)}/{before_filter} 个候选")
                
                # Update progress hint so UI doesn't overestimate expected candidates
                self._write_progress_hint(output_dir, len(all_evaluated_candidates), len(unique_candidates))
                
                if not unique_candidates:
                    logger.warning(f"第 {iteration + 1} 代没有新的候选化合物，停止进化")
                    # 如果第二代开始没有候选，可能是过度保守，让我们查看原因
                    if iteration > 0:
                        logger.info(f"调试信息: 第 {iteration + 1} 代生成的原始候选:")
                        for i, c in enumerate(iteration_candidates[:5]):  # 只显示前5个
                            logger.info(f"  {i+1}. {c.smiles}")
                        logger.info(f"已评估的候选SMILES数量: {len(seen_smiles)}")
                    break
                
                # Evaluate candidates using batch processing
                evaluated_candidates = batch_evaluator.evaluate_candidates_batch(
                    unique_candidates,
                    target_protein_yaml,
                    output_dir,
                    compound_smiles
                )
                
                all_evaluated_candidates.extend(evaluated_candidates)
                generation_results.append(evaluated_candidates.copy())
                
                logger.info(f"✅ 第 {iteration + 1} 代完成，成功评估 {len(evaluated_candidates)} 个候选化合物")
                
                # Check for early termination using evolution engine
                if iteration > 0 and evolution_engine.suggest_termination(generation_results, patience=2):
                    logger.info("🏁 进化提前收敛，停止优化")
                    break
                
                # Select seeds for next generation (if not last iteration)
                if iteration < iterations - 1 and evaluated_candidates:
                    # Use evolution engine for better selection
                    if len(all_evaluated_candidates) >= top_k_per_iteration:
                        elite_compounds, diverse_compounds = evolution_engine.select_parents_for_next_generation(
                            all_evaluated_candidates, top_k_per_iteration, top_k_per_iteration // 2
                        )
                        selected_compounds = elite_compounds + diverse_compounds
                    else:
                        # Fallback to simple top-k selection
                        sorted_candidates = sorted(all_evaluated_candidates, 
                                                key=lambda x: x.scores.combined_score if x.scores else 0, 
                                                reverse=True)
                        selected_compounds = sorted_candidates[:top_k_per_iteration]
                    
                    current_seeds = [c.smiles for c in selected_compounds]
                    
                    logger.info(f"🎯 选择第 {iteration + 2} 代的 {len(current_seeds)} 个种子:")
                    for i, candidate in enumerate(selected_compounds):
                        score = candidate.scores.combined_score if candidate.scores else 0
                        logger.info(f"  {i+1}. {candidate.compound_id} - 分数: {score:.4f}")
                        
                    # Log population diversity
                    diversity = evolution_engine.assess_population_diversity(all_evaluated_candidates)
                    logger.info(f"📊 当前种群多样性: {diversity:.3f}")
            
            # Generate final results
            logger.info(f"🎉 优化完成！总共评估了 {len(all_evaluated_candidates)} 个候选化合物")
            
            if not all_evaluated_candidates:
                logger.warning("No candidates were successfully evaluated")
                return self._create_empty_result(compound_smiles, strategy, start_time)
            
            # Sort all candidates by score
            sorted_candidates = sorted(all_evaluated_candidates, 
                                    key=lambda x: x.scores.combined_score if x.scores else 0, 
                                    reverse=True)
            
            # Create optimization result
            execution_time = time.time() - start_time
            result = OptimizationResult(
                original_compound=compound_smiles,
                strategy=strategy,
                candidates=[self._candidate_to_dict(c) for c in sorted_candidates],
                scores=[c.scores for c in sorted_candidates if c.scores],
                top_candidates=[self._candidate_to_dict(c) for c in sorted_candidates[:10]],
                execution_time=execution_time,
                total_candidates=len(sorted_candidates),
                success_rate=len([c for c in all_evaluated_candidates if c.scores]) / len(all_evaluated_candidates) if all_evaluated_candidates else 0
            )
            
            # Save summary (no final CSV needed - real-time CSV already exists)
            self._save_optimization_summary(result, output_dir)
            
            logger.info(f"Optimization completed in {execution_time:.2f}s")
            logger.info(f"Success rate: {result.success_rate:.1%}")
            return result
            
        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            raise OptimizationError(f"Optimization failed: {e}")
    
    def _generate_candidates_with_mmpdb(self, 
                                       compound_smiles: str, 
                                       strategy: str, 
                                       max_candidates: int,
                                       iteration: int = 0,
                                       max_iterations: int = 1,
                                       diversity_weight: float = 0.3,
                                       similarity_threshold: float = 0.4,
                                       max_chiral_centers: int = None,
                                       reference_smiles: str = None,
                                       rule_query_smarts: Optional[List[str]] = None) -> List[OptimizationCandidate]:
        """Generate candidate compounds using MMPDB with intelligent diversity selection"""
        try:
            # Calculate reference chiral centers if not provided
            if max_chiral_centers is None and reference_smiles:
                max_chiral_centers = self._count_chiral_centers(reference_smiles)
                logger.info(f"参考化合物手性中心数量: {max_chiral_centers}")
            elif max_chiral_centers is None:
                max_chiral_centers = self._count_chiral_centers(compound_smiles)
                logger.info(f"原始化合物手性中心数量: {max_chiral_centers}")
            
            # Calculate adaptive similarity threshold for progressive convergence
            # Early iterations: lower similarity (more diverse)
            # Later iterations: higher similarity (more conservative)
            progress = iteration / max_iterations if max_iterations > 1 else 0
            base_similarity = 0.2  # Start with more diversity
            target_similarity = similarity_threshold  # Use provided threshold as target
            adaptive_similarity_threshold = base_similarity + (target_similarity - base_similarity) * progress
            
            logger.info(f"Generating candidates with MMPDB, strategy: {strategy}")
            logger.info(f"迭代 {iteration+1}/{max_iterations}, 相似性阈值: {adaptive_similarity_threshold:.3f}")
            
            # Generate raw candidates (more than needed for selection)
            raw_candidate_count = max_candidates * 4  # Generate 4x more for better diversity selection
            
            mmp_results = []
            if rule_query_smarts:
                source_smiles = reference_smiles or compound_smiles
                mmp_results = self.mmp_engine.generate_with_rule_queries(
                    source_smiles,
                    rule_query_smarts,
                    raw_candidate_count
                )
                if not mmp_results:
                    logger.warning("规则级查询未生成候选，回退到常规策略生成")
            if not mmp_results:
                if strategy == "scaffold_hopping":
                    mmp_results = self.mmp_engine.scaffold_hopping(compound_smiles, raw_candidate_count, adaptive_similarity_threshold)
                elif strategy == "fragment_replacement":
                    mmp_results = self.mmp_engine.fragment_replacement(compound_smiles, raw_candidate_count, adaptive_similarity_threshold)
                else:
                    # Default to scaffold hopping with adaptive threshold
                    mmp_results = self.mmp_engine.scaffold_hopping(compound_smiles, raw_candidate_count, adaptive_similarity_threshold)
            
            logger.info(f"MMPDB generated {len(mmp_results)} raw candidates")
            
            if not mmp_results:
                return []
            
            # Convert MMPDB results to OptimizationCandidate objects
            all_candidates = []
            for result in mmp_results:
                # Check chiral center constraint
                if max_chiral_centers is not None:
                    chiral_count = self._count_chiral_centers(result['smiles'])
                    if chiral_count > max_chiral_centers:
                        logger.debug(f"跳过化合物 {result['smiles'][:50]}... (手性中心数量: {chiral_count} > {max_chiral_centers})")
                        continue
                
                self.candidate_counter += 1
                candidate = OptimizationCandidate(
                    smiles=result['smiles'],
                    compound_id=f"cand_{self.candidate_counter:04d}",
                    mmp_transformation=result.get('transformation_rule', result.get('transformation_description', 'mmpdb_transform')),
                    generation_method=result.get('generation_method', 'mmpdb'),
                    transformation_rule=result.get('transformation_rule', result.get('transformation_description', 'mmpdb_transform')),
                    parent_smiles=result.get('parent_smiles', compound_smiles),
                    similarity=result.get('similarity', 0.0)
                )
                all_candidates.append(candidate)
            
            # Use DiversitySelector for intelligent candidate selection
            if len(all_candidates) > max_candidates:
                # Determine selection strategy based on iteration progress
                if progress < 0.3:
                    # Early stage: prioritize diversity
                    selection_strategy = 'scaffold_diverse'
                    current_diversity_weight = max(0.7, diversity_weight + 0.2)
                elif progress < 0.7:
                    # Middle stage: hybrid approach
                    selection_strategy = 'hybrid'
                    current_diversity_weight = diversity_weight
                else:
                    # Late stage: balance similarity and diversity
                    selection_strategy = 'tanimoto_diverse'
                    current_diversity_weight = max(0.1, diversity_weight - 0.2)
                
                logger.info(f"Using {selection_strategy} selection with diversity weight {current_diversity_weight:.2f}")
                
                # Extract SMILES for diversity selection
                candidate_smiles = [candidate.smiles for candidate in all_candidates]
                
                # Use diversity selector to pick best candidates
                candidate_dicts = [{'smiles': candidate.smiles, 'similarity': candidate.similarity} 
                                 for candidate in all_candidates]
                
                selected_candidates_list = self.diversity_selector.select_diverse_candidates(
                    candidates=candidate_dicts,
                    target_count=max_candidates,
                    parent_smiles=compound_smiles
                )
                
                # Extract selected indices by matching SMILES
                selected_indices = []
                for selected_dict in selected_candidates_list:
                    for i, candidate in enumerate(all_candidates):
                        if candidate.smiles == selected_dict['smiles']:
                            selected_indices.append(i)
                            break
                
                selected_candidates = [all_candidates[i] for i in selected_indices]
                logger.info(f"Selected {len(selected_candidates)} diverse candidates using {selection_strategy}")
                
            else:
                selected_candidates = all_candidates
                logger.info(f"Using all {len(selected_candidates)} candidates (below max threshold)")
            
            logger.info(f"Final candidate set: {len(selected_candidates)} compounds")
            return selected_candidates
            
        except Exception as e:
            logger.error(f"MMPDB candidate generation failed: {e}")
            return []
    
    def _count_chiral_centers(self, smiles: str) -> int:
        """Count the number of chiral centers in a molecule"""
        try:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                return 0
            
            # Find chiral centers
            chiral_centers = Chem.FindMolChiralCenters(mol, includeUnassigned=True)
            return len(chiral_centers)
            
        except Exception as e:
            logger.debug(f"Failed to count chiral centers for {smiles}: {e}")
            return 0
    
    def _evaluate_candidates_with_boltz(self, 
                                       candidates: List[OptimizationCandidate],
                                       target_protein_yaml: str,
                                       output_dir: str,
                                       original_compound: str = None,
                                       batch_size: int = 4) -> List[OptimizationCandidate]:
        """Evaluate candidates using Boltz-WebUI with batch processing and real-time CSV updates"""
        evaluated_candidates = []
        
        # Read target protein configuration
        with open(target_protein_yaml, 'r') as f:
            target_config = yaml.safe_load(f)
        
        # Create temp directory for configs
        temp_config_dir = os.path.join(output_dir, "temp_configs")
        os.makedirs(temp_config_dir, exist_ok=True)
        
        # Set current output dir for debug YAML saving
        self._current_output_dir = output_dir
        
        # Initialize real-time CSV file only if it doesn't exist
        csv_file = os.path.join(output_dir, "optimization_results.csv")
        if not os.path.exists(csv_file):
            self._initialize_csv_file(csv_file)
            logger.info(f"Initialized new CSV file: {csv_file}")
        else:
            logger.info(f"Using existing CSV file: {csv_file}")
        
        logger.info(f"Evaluating {len(candidates)} candidates with Boltz-WebUI")
        logger.info(f"Real-time results will be saved to: {csv_file}")
        
        for i, candidate in enumerate(candidates):
            try:
                logger.info(f"Processing candidate {i+1}/{len(candidates)}: {candidate.compound_id}")
                
                # Create config for this candidate
                config_yaml = self._create_candidate_config_yaml(
                    candidate, target_config
                )
                
                if not config_yaml:
                    logger.warning(f"Failed to create config for {candidate.compound_id}")
                    self._write_csv_row(csv_file, candidate, original_compound, status="config_failed")
                    continue
                
                # Submit to Boltz-WebUI
                task_id = self.boltz_client.submit_optimization_job(
                    yaml_content=config_yaml,
                    job_name=f"opt_{candidate.compound_id}",
                    compound_smiles=candidate.smiles,
                    backend=self.config.boltz_api.backend
                )
                
                if not task_id:
                    logger.warning(f"Failed to submit {candidate.compound_id}")
                    self._write_csv_row(csv_file, candidate, original_compound, status="submit_failed")
                    continue
                
                logger.info(f"Submitted {candidate.compound_id} as task {task_id}")
                
                # Wait for completion
                result = self.boltz_client.poll_job_status(task_id)
                
                if result and result.get('status') == 'completed':
                    # Download results
                    result_dir = os.path.join(output_dir, "results", candidate.compound_id)
                    try:
                        result_files = self.boltz_client.download_results(task_id, result_dir)
                        if result_files:  # Check if download was successful
                            # Parse prediction results
                            prediction_results = self._parse_prediction_results(result_dir)
                            candidate.prediction_results = prediction_results
                            
                            # Score the candidate immediately
                            try:
                                score = self.scoring_system.score_compound(
                                    smiles=candidate.smiles,
                                    boltz_results=candidate.prediction_results,
                                    reference_smiles=original_compound
                                )
                                candidate.scores = score
                                
                                # Write to CSV immediately
                                self._write_csv_row(csv_file, candidate, original_compound, 
                                                  status="completed", score=score, task_id=task_id)
                                
                                evaluated_candidates.append(candidate)
                                logger.info(f"✅ {candidate.compound_id} completed - Score: {score.combined_score:.4f}")
                                
                            except Exception as e:
                                logger.error(f"Scoring failed for {candidate.compound_id}: {e}")
                                self._write_csv_row(csv_file, candidate, original_compound, 
                                                  status="scoring_failed", task_id=task_id)
                        else:
                            logger.warning(f"Failed to download results for {candidate.compound_id}")
                            self._write_csv_row(csv_file, candidate, original_compound, 
                                              status="download_failed", task_id=task_id)
                    except Exception as e:
                        logger.warning(f"Error downloading results for {candidate.compound_id}: {e}")
                        self._write_csv_row(csv_file, candidate, original_compound, 
                                          status="download_error", task_id=task_id)
                else:
                    logger.warning(f"Task {task_id} failed or timed out")
                    failure_reason = result.get('error', 'unknown_error') if result else 'timeout'
                    self._write_csv_row(csv_file, candidate, original_compound, 
                                      status=f"task_failed_{failure_reason}", task_id=task_id)
                    
            except Exception as e:
                logger.error(f"Error evaluating {candidate.compound_id}: {e}")
                self._write_csv_row(csv_file, candidate, original_compound, status="error")
        
        logger.info(f"Successfully evaluated {len(evaluated_candidates)} candidates")
        logger.info(f"Real-time results saved in: {csv_file}")
        return evaluated_candidates
    
    def _create_candidate_config_yaml(self, 
                                      candidate: OptimizationCandidate,
                                      target_config: Dict) -> Optional[str]:
        """Create YAML config content for candidate"""
        try:
            import copy
            config = copy.deepcopy(target_config)

            def _get_chain_id_by_index(index: int) -> str:
                if index < 26:
                    return string.ascii_uppercase[index]
                return f"Z{index-25}"

            def _get_next_chain_id(used_ids: set) -> str:
                idx = 0
                while True:
                    chain_id = _get_chain_id_by_index(idx)
                    if chain_id not in used_ids:
                        return chain_id
                    idx += 1
            
            # Add the candidate compound as a ligand
            used_ids = set()
            for entry in config.get('sequences', []):
                if isinstance(entry, dict):
                    key = next(iter(entry.keys()), None)
                    if key and isinstance(entry.get(key), dict):
                        seq_id = entry[key].get('id')
                        if seq_id:
                            used_ids.add(seq_id)

            ligand_id = _get_next_chain_id(used_ids)
            ligand_entry = {
                "ligand": {
                    "id": ligand_id,
                    "smiles": candidate.smiles
                }
            }
            
            # Add to sequences
            if 'sequences' not in config:
                config['sequences'] = []
            config['sequences'].append(ligand_entry)
            
            # Clear any existing affinity properties and add only our candidate
            # This ensures we only have ONE affinity ligand (Boltz requirement)
            config['properties'] = [{
                "affinity": {
                    "binder": ligand_id
                }
            }]
            
            # Convert to YAML string
            import yaml
            yaml_content = yaml.dump(config, default_flow_style=False, allow_unicode=True)
            
            # Save YAML for debugging (use output_dir parameter from _evaluate_candidates_with_boltz)
            if hasattr(self, '_current_output_dir'):
                debug_yaml_path = os.path.join(self._current_output_dir, "temp_configs", f"{candidate.compound_id}_config.yaml")
                os.makedirs(os.path.dirname(debug_yaml_path), exist_ok=True)
                with open(debug_yaml_path, 'w', encoding='utf-8') as f:
                    f.write(yaml_content)
                logger.debug(f"Saved debug YAML to {debug_yaml_path}")
            
            return yaml_content
            
        except Exception as e:
            logger.error(f"Failed to create config for {candidate.compound_id}: {e}")
            return None
    
    def _initialize_csv_file(self, csv_file: str):
        """Initialize CSV file with headers"""
        import csv
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp',
                'compound_id', 
                'original_smiles',
                'optimized_smiles',
                'mmp_transformation',
                'status',
                'task_id',
                'combined_score',
                'binding_affinity',
                'drug_likeness', 
                'synthetic_accessibility',
                'novelty',
                'stability',
                'plddt',
                'iptm',
                'binding_probability',
                'ic50_um',
                'molecular_weight',
                'logp',
                'lipinski_violations',
                'qed_score'
            ])
    
    def _write_csv_row(self, csv_file: str, candidate: OptimizationCandidate, 
                       original_compound: str = None, status: str = "processing", 
                       score: 'CompoundScore' = None, task_id: str = None):
        """Write a candidate result to CSV file immediately"""
        import csv
        import time
        
        try:
            with open(csv_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # Prepare row data
                row = [
                    time.strftime('%Y-%m-%d %H:%M:%S'),
                    candidate.compound_id,
                    original_compound or '',
                    candidate.smiles,
                    candidate.mmp_transformation,
                    status,
                    task_id or '',
                ]
                
                if score:
                    row.extend([
                        f"{score.combined_score:.4f}",
                        f"{score.binding_affinity:.4f}",
                        f"{score.drug_likeness:.4f}",
                        f"{score.synthetic_accessibility:.4f}", 
                        f"{score.novelty:.4f}",
                        f"{score.stability:.4f}",
                        f"{score.plddt:.4f}",
                        f"{score.iptm:.4f}",
                        f"{score.binding_probability:.4f}",
                        f"{score.ic50_um:.4f}",
                        f"{score.properties.get('molecular_weight', 0):.2f}",
                        f"{score.properties.get('logp', 0):.2f}",
                        score.lipinski_violations,
                        f"{score.qed_score:.4f}"
                    ])
                else:
                    # Empty values for failed/incomplete candidates
                    row.extend([''] * 14)
                
                writer.writerow(row)
                
            logger.debug(f"Written CSV row for {candidate.compound_id}: {status}")
            
        except Exception as e:
            logger.error(f"Failed to write CSV row for {candidate.compound_id}: {e}")
    
    def _parse_prediction_results(self, result_dir: str) -> Dict[str, Any]:
        """Parse Boltz prediction results"""
        results = {}
        
        # Parse affinity data
        affinity_file = os.path.join(result_dir, "affinity_data.json")
        if os.path.exists(affinity_file):
            try:
                with open(affinity_file, 'r') as f:
                    affinity_data = json.load(f)
                
                # Extract key affinity metrics
                results['affinity_pred_value'] = affinity_data.get('affinity_pred_value', 0.0)
                results['binding_probability'] = affinity_data.get('affinity_probability_binary', 0.0)
                results['ic50_um'] = self._convert_affinity_to_ic50(affinity_data.get('affinity_pred_value', 0.0))
                
                logger.debug(f"Parsed affinity data: pred_value={results['affinity_pred_value']:.4f}, prob={results['binding_probability']:.4f}")
                
            except Exception as e:
                logger.warning(f"Failed to parse affinity_data.json: {e}")
        
        # Parse confidence data (contains pLDDT and ipTM)
        confidence_file = os.path.join(result_dir, "confidence_data_model_0.json")
        if os.path.exists(confidence_file):
            try:
                with open(confidence_file, 'r') as f:
                    confidence_data = json.load(f)
                
                # Extract key confidence metrics
                results['plddt'] = confidence_data.get('complex_plddt', 0.0)
                results['iptm'] = confidence_data.get('iptm', 0.0)
                results['ptm'] = confidence_data.get('ptm', 0.0)
                results['confidence_score'] = confidence_data.get('confidence_score', 0.0)
                
                logger.debug(f"Parsed confidence data: pLDDT={results['plddt']:.4f}, ipTM={results['iptm']:.4f}")
                
            except Exception as e:
                logger.warning(f"Failed to parse confidence_data_model_0.json: {e}")
        
        # Look for any additional files (backward compatibility)
        for filename in ['confidence.json', 'prediction_info.json']:
            file_path = os.path.join(result_dir, filename)
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r') as f:
                        additional_data = json.load(f)
                    results.update(additional_data)
                except Exception as e:
                    logger.warning(f"Failed to parse {filename}: {e}")
        
        logger.info(f"Parsed prediction results with {len(results)} metrics")
        return results
    
    def _convert_affinity_to_ic50(self, affinity_value: float) -> float:
        """Convert affinity prediction to IC50 (rough estimation)"""
        try:
            # This is a rough conversion - you may need to adjust based on your model
            # Higher affinity values typically correspond to lower IC50 values
            if affinity_value > 0:
                # Simple exponential conversion (adjust as needed)
                ic50_um = max(0.001, 1000.0 * (1.0 - affinity_value))
            else:
                ic50_um = 1000.0  # High IC50 for negative affinity
            return ic50_um
        except:
            return 0.0
    
    def _score_candidates(self, 
                         candidates: List[OptimizationCandidate],
                         reference_smiles: str) -> List[CompoundScore]:
        """Score all candidates"""
        scores = []
        
        for candidate in candidates:
            try:
                score = self.scoring_system.score_compound(
                    smiles=candidate.smiles,
                    boltz_results=candidate.prediction_results,
                    reference_smiles=reference_smiles
                )
                scores.append(score)
                candidate.scores = score
                
            except Exception as e:
                logger.error(f"Scoring failed for {candidate.compound_id}: {e}")
        
        logger.info(f"Scored {len(scores)} candidates")
        return scores
    
    def _prepare_top_candidates(self, 
                               ranked_scores: List[CompoundScore],
                               candidates: List[OptimizationCandidate]) -> List[Dict[str, Any]]:
        """Prepare top candidates for output"""
        top_candidates = []
        candidate_map = {c.smiles: c for c in candidates}
        
        for score in ranked_scores:
            candidate = candidate_map.get(score.smiles)
            if candidate:
                top_candidates.append({
                    'smiles': score.smiles,
                    'compound_id': candidate.compound_id,
                    'mmp_transformation': candidate.mmp_transformation,
                    'generation_method': candidate.generation_method,
                    'transformation_rule': candidate.transformation_rule,
                    'combined_score': score.combined_score,
                    'binding_affinity': score.binding_affinity,
                    'binding_probability': getattr(score, 'binding_probability', 0.0),
                    'ic50_um': getattr(score, 'ic50_um', 0.0),
                    'drug_likeness': score.drug_likeness,
                    'synthetic_accessibility': score.synthetic_accessibility,
                    'molecular_weight': score.properties.get('molecular_weight', 0.0) if score.properties else 0.0,
                    'logp': score.properties.get('logp', 0.0) if score.properties else 0.0,
                    'plddt': getattr(score, 'plddt', 0.0),
                    'iptm': getattr(score, 'iptm', 0.0),
                    'properties': score.properties,
                    'boltz_metrics': getattr(score, 'boltz_metrics', {})
                })
        
        return top_candidates
    
    def _calculate_statistics(self, scores: List[CompoundScore]) -> Dict[str, Any]:
        """Calculate optimization statistics"""
        if not scores:
            return {}
        
        combined_scores = [s.combined_score for s in scores]
        
        return {
            'total_candidates': len(scores),
            'mean_score': sum(combined_scores) / len(combined_scores),
            'max_score': max(combined_scores),
            'min_score': min(combined_scores),
            'success_rate': len([s for s in scores if s.combined_score > 0.5]) / len(scores)
        }
    
    def _create_empty_result(self, compound_smiles: str, strategy: str, start_time: float) -> OptimizationResult:
        """Create empty result when no candidates are generated"""
        return OptimizationResult(
            original_compound=compound_smiles,
            strategy=strategy,
            candidates=[],
            scores=[],
            top_candidates=[],
            execution_time=time.time() - start_time,
            statistics={'total_candidates': 0},
            total_candidates=0,
            success_rate=0.0
        )
    
    def _is_valid_smiles(self, smiles: str) -> bool:
        """Validate SMILES string"""
        try:
            mol = Chem.MolFromSmiles(smiles)
            return mol is not None
        except:
            return False

    def _parse_smarts_query(self, query_text: Optional[str]) -> Optional[Chem.Mol]:
        if not query_text:
            return None
        query_text = query_text.strip()
        if not query_text:
            return None
        query_mol = Chem.MolFromSmarts(query_text)
        if query_mol is None:
            query_mol = Chem.MolFromSmiles(query_text)
        return query_mol

    def _parse_smarts_queries(self, query_text: Optional[str]) -> List[Chem.Mol]:
        queries = []
        if not query_text:
            return queries
        parts = [p.strip() for p in str(query_text).split(";;") if p.strip()]
        for part in parts:
            query = self._parse_smarts_query(part)
            if query is None:
                return []
            queries.append(query)
        return queries

    def _parse_rgroup_smarts_queries(self, query_text: Optional[str]) -> List[Chem.Mol]:
        queries = []
        if not query_text:
            return queries
        parts = [p.strip() for p in str(query_text).split(";;") if p.strip()]
        for part in parts:
            subparts = [s for s in part.split(".") if s]
            for sub in subparts:
                query = None
                if "*" in sub:
                    try:
                        from mmpdblib import rgroup2smarts
                        mol = Chem.MolFromSmiles(sub)
                        if mol:
                            smarts = rgroup2smarts.rgroup_mol_to_smarts(mol)
                            query = Chem.MolFromSmarts(smarts)
                    except Exception:
                        query = None
                if query is None:
                    query = self._parse_smarts_query(sub)
                if query is None:
                    return []
                queries.append(query)
        return queries

    def _strip_dummy_atoms(self, mol: Chem.Mol) -> Chem.Mol:
        editable = Chem.EditableMol(mol)
        dummy_indices = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
        for idx in sorted(dummy_indices, reverse=True):
            editable.RemoveAtom(idx)
        cleaned = editable.GetMol()
        try:
            Chem.SanitizeMol(cleaned)
        except Exception:
            return mol
        return cleaned

    def _build_variable_query(self, compound_smiles: str, fragment_smiles: str) -> Tuple[Optional[Chem.Mol], Optional[Chem.Mol]]:
        try:
            compound = Chem.MolFromSmiles(compound_smiles)
            if compound is None:
                return None, None
            fragment = Chem.MolFromSmiles(fragment_smiles)
            if fragment is None:
                fragment = Chem.MolFromSmarts(fragment_smiles)
            if fragment is None:
                return None, None
            fragment = self._strip_dummy_atoms(fragment)

            replacement = Chem.MolFromSmiles("*")
            replaced = Chem.ReplaceSubstructs(compound, fragment, replacement, replaceAll=False)
            if not replaced:
                return None, None
            scaffold = replaced[0]
            try:
                Chem.SanitizeMol(scaffold)
            except Exception:
                pass
            scaffold_smiles = Chem.MolToSmiles(scaffold, canonical=True)
            scaffold_query = Chem.MolFromSmarts(scaffold_smiles)
            if scaffold_query is None:
                scaffold_query = Chem.MolFromSmiles(scaffold_smiles)

            exclude_query = Chem.MolFromSmarts(Chem.MolToSmiles(fragment, canonical=True))
            if exclude_query is None:
                exclude_query = Chem.MolFromSmiles(Chem.MolToSmiles(fragment, canonical=True))

            return scaffold_query, exclude_query
        except Exception:
            return None, None

    def _filter_candidates_by_queries(self,
                                      candidates: List[OptimizationCandidate],
                                      required_queries: List[Chem.Mol],
                                      exclude_queries: List[Chem.Mol]) -> List[OptimizationCandidate]:
        filtered = []
        for candidate in candidates:
            mol = Chem.MolFromSmiles(candidate.smiles)
            if not mol:
                continue
            if any(query and not mol.HasSubstructMatch(query) for query in required_queries):
                continue
            if any(query and mol.HasSubstructMatch(query) for query in exclude_queries):
                continue
            filtered.append(candidate)
        return filtered

    def _write_progress_hint(self, output_dir: Optional[str], processed: int, upcoming: int) -> None:
        if not output_dir:
            return
        try:
            path = os.path.join(output_dir, "optimization_progress.json")
            payload = {
                "processed_candidates": int(processed),
                "expected_candidates": int(processed + max(0, upcoming)),
                "updated_at": time.time()
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
        except Exception:
            pass

    def batch_optimize(self, 
                      compounds_file: str,
                      target_protein_yaml: str,
                      strategy: str = "scaffold_hopping",
                      max_candidates: int = 50,
                      output_dir: Optional[str] = None,
                      core_smarts: Optional[str] = None,
                      exclude_smarts: Optional[str] = None,
                      rgroup_smarts: Optional[str] = None,
                      variable_smarts: Optional[str] = None,
                      variable_const_smarts: Optional[str] = None) -> Dict[str, OptimizationResult]:
        """
        Batch optimization of multiple compounds
        """
        results = {}
        
        # Setup output directory
        if not output_dir:
            output_dir = os.path.join(self.config.temp_dir, f"batch_optimization_{int(time.time())}")
        os.makedirs(output_dir, exist_ok=True)
        
        # Read compounds file
        compounds = self._read_compounds_file(compounds_file)
        logger.info(f"Starting batch optimization for {len(compounds)} compounds")
        
        for i, (compound_id, smiles) in enumerate(compounds):
            logger.info(f"Optimizing compound {i+1}/{len(compounds)}: {compound_id}")
            
            compound_output_dir = os.path.join(output_dir, f"compound_{compound_id}")
            
            try:
                result = self.optimize_compound(
                    compound_smiles=smiles,
                    target_protein_yaml=target_protein_yaml,
                    strategy=strategy,
                    max_candidates=max_candidates,
                    output_dir=compound_output_dir,
                    core_smarts=core_smarts,
                    exclude_smarts=exclude_smarts,
                    rgroup_smarts=rgroup_smarts,
                    variable_smarts=variable_smarts,
                    variable_const_smarts=variable_const_smarts
                )
                results[compound_id] = result
                logger.info(f"Compound {compound_id} optimization completed")
                
            except Exception as e:
                logger.error(f"Compound {compound_id} optimization failed: {e}")
                results[compound_id] = self._create_empty_result(smiles, strategy, time.time())
        
        logger.info(f"Batch optimization completed: {len(results)} compounds processed")
        return results
    
    def _read_compounds_file(self, compounds_file: str) -> List[Tuple[str, str]]:
        """Read compounds from file (CSV or text)"""
        compounds = []
        
        if compounds_file.endswith('.csv'):
            import pandas as pd
            df = pd.read_csv(compounds_file)
            for _, row in df.iterrows():
                compound_id = str(row.get('id', f'compound_{len(compounds)+1}'))
                smiles = str(row['smiles']).strip()
                compounds.append((compound_id, smiles))
        else:
            # Text file with SMILES
            with open(compounds_file, 'r') as f:
                for i, line in enumerate(f):
                    smiles = line.strip()
                    if smiles and not smiles.startswith('#'):
                        compounds.append((f'compound_{i+1}', smiles))
        
        return compounds
    
    def _candidate_to_dict(self, candidate):
        """Convert a candidate object to dictionary for JSON serialization"""
        result = {
            'compound_id': candidate.compound_id,
            'smiles': candidate.smiles,
            'combined_score': candidate.scores.combined_score if candidate.scores else 0.0,
            'mmp_transformation': candidate.mmp_transformation,
            'generation_method': getattr(candidate, 'generation_method', 'mmpdb'),
            'transformation_rule': getattr(candidate, 'transformation_rule', candidate.mmp_transformation),
            'parent_smiles': getattr(candidate, 'parent_smiles', ''),
            'similarity': getattr(candidate, 'similarity', 0.0),
            'status': getattr(candidate, 'status', 'completed')
        }
        
        if candidate.scores:
            result.update({
                'binding_affinity': candidate.scores.binding_affinity,
                'drug_likeness': candidate.scores.drug_likeness,
                'synthetic_accessibility': candidate.scores.synthetic_accessibility,
                'novelty': candidate.scores.novelty,
                'stability': candidate.scores.stability,
                'plddt': candidate.scores.plddt,
                'iptm': candidate.scores.iptm,
                'binding_probability': candidate.scores.binding_probability,
                'ic50_um': candidate.scores.ic50_um,
                'molecular_weight': candidate.scores.molecular_weight,
                'logp': candidate.scores.logp,
                'lipinski_violations': candidate.scores.lipinski_violations,
                'qed_score': candidate.scores.qed_score
            })
        
        return result
    
    def _save_optimization_summary(self, result, output_dir: str):
        """Save optimization summary to JSON file"""
        import json
        
        summary = {
            'original_compound': result.original_compound,
            'strategy': result.strategy,
            'total_candidates': result.total_candidates,
            'successful_evaluations': len([c for c in result.candidates if c.get('status') == 'completed']),
            'success_rate': result.success_rate,
            'execution_time': result.execution_time,
            'best_candidate': result.top_candidates[0] if result.top_candidates else None,
            'top_candidates': result.top_candidates[:5],  # Top 5 candidates
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'parameters': {
                'strategy': result.strategy,
                'total_generated': result.total_candidates
            }
        }
        
        summary_file = os.path.join(output_dir, 'optimization_summary.json')
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Optimization summary saved to: {summary_file}")
