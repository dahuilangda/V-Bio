# /Boltz-WebUI/designer/design_logic.py

"""
design_logic.py

该模块定义了`Designer`类，它是蛋白质和糖肽设计过程的核心控制器。
它实现了基于演化策略的梯度自由优化循环：
1.  **生成 (Generation)**: 基于精英群体创建新一代候选序列。
2.  **评估 (Evaluation)**: 并行提交候选者到Boltz-WebUI API进行结构预测和打分。
3.  **选择 (Selection)**: 根据一个复合评分函数（结合了ipTM和pLDDT）选择新的精英群体。

该类管理工作目录、与API客户端的交互以及整个设计流程的状态。
新增了自适应超参数调整机制，以应对过早收敛问题。
"""

import os
import yaml
import shutil
import time
import csv
import random
import copy
import logging
import concurrent.futures

# 本地模块导入
from api_client import BoltzApiClient
from design_utils import (
    generate_random_sequence,
    mutate_sequence,
    parse_confidence_metrics,
    MONOSACCHARIDES,
    GLYCOSYLATION_SITES,
    get_valid_residues_for_glycan
)

logger = logging.getLogger(__name__)


class Designer:
    """
    管理蛋白质和糖肽的多谱系、梯度自由设计优化循环。
    集成了自适应机制以动态调整探索强度，防止过早收敛。
    """
    def __init__(self, base_yaml_path: str, client: BoltzApiClient):
        """初始化Designer实例。"""
        self.base_yaml_path = base_yaml_path
        self.client = client
        with open(base_yaml_path, 'r') as f:
            self.base_config = yaml.safe_load(f)
        
        self.work_dir = f"temp_design_run_{int(time.time())}"
        os.makedirs(self.work_dir, exist_ok=True)
        
        self.evaluated_sequences = set()
        
        # --- 初始化自适应超参数 ---
        self.hparams = {
            'mutation_rate': 0.1,         # 初始突变率
            'pos_select_temp': 1.0,       # 初始pLDDT位置选择温度
        }
        # 定义超参数的边界和调整因子，以提供专业且可控的调整
        self.hparam_configs = {
            'mutation_rate': {'base': 0.1, 'max': 0.5, 'decay': 0.95, 'increase': 1.2},
            'pos_select_temp': {'base': 1.0, 'max': 10.0, 'decay': 0.9, 'increase': 1.5},
        }
        
        logger.info(f"Temporary working directory created at: {os.path.abspath(self.work_dir)}")

    def _evaluate_one_candidate(self, candidate_info: tuple) -> tuple:
        """提交单个候选者进行评估，轮询结果，并解析指标。"""
        generation_index, sequence, binder_chain_id, design_params = candidate_info
        logger.info(f"[Gen {generation_index}] Evaluating new unique candidate: {sequence[:20]}...")
        
        try:
            candidate_yaml_path = self._create_candidate_yaml(sequence, binder_chain_id, design_params)
        except (ValueError, KeyError) as e:
            logger.error(f"Failed to create YAML for sequence '{sequence}'. Skipping. Reason: {e}")
            return (sequence, None, None)

        task_id = self.client.submit_job(candidate_yaml_path)
        if not task_id:
            return (sequence, None, None)

        status = self.client.poll_status(task_id)
        if not status or status.get('state') != 'SUCCESS':
            logger.warning(f"Job {task_id} for sequence '{sequence}' failed or did not complete successfully. Status: {status.get('state')}")
            return (sequence, None, None)

        results_path = self.client.download_and_unzip_results(task_id, self.work_dir)
        if not results_path:
            return (sequence, None, None)

        metrics = parse_confidence_metrics(results_path, binder_chain_id)
        iptm_score = metrics.get('iptm', 0.0)
        plddt_score = metrics.get('binder_avg_plddt', 0.0)
        logger.info(f"Candidate '{sequence[:20]}...' evaluated. ipTM: {iptm_score:.4f}, pLDDT: {plddt_score:.2f}")
        return (sequence, metrics, results_path)
    
    def _write_summary_csv(self, all_results: list, output_csv_path: str, keep_temp_files: bool):
        """将所有已评估候选者的摘要写入CSV文件，按复合分数排名。"""
        if not all_results:
            logger.warning("No results were generated to save to CSV.")
            return
        all_results.sort(key=lambda x: x.get('composite_score', 0.0), reverse=True)
        header = ['rank', 'generation', 'sequence', 'composite_score', 'iptm', 'binder_avg_plddt', 'ptm', 'complex_plddt']
        if keep_temp_files:
            header.append('results_path')
        logger.info(f"Writing {len(all_results)} total results to {os.path.abspath(output_csv_path)}...")
        try:
            with open(output_csv_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=header)
                writer.writeheader()
                for i, result_data in enumerate(all_results):
                    result_data['rank'] = i + 1
                    for key in ['composite_score', 'iptm', 'ptm', 'complex_plddt', 'binder_avg_plddt']:
                        if key in result_data and isinstance(result_data[key], float):
                            result_data[key] = f"{result_data[key]:.4f}"
                    row_to_write = {key: result_data.get(key, 'N/A') for key in header}
                    writer.writerow(row_to_write)
            logger.info(f"Summary CSV successfully saved to {output_csv_path}")
        except IOError as e:
            logger.error(f"Could not write to CSV file at {output_csv_path}. Reason: {e}")
            
    def _update_adaptive_hparams(self, attempts: int, population_size: int):
        """根据种群多样性指标（生成尝试次数）动态调整超参数。"""
        if population_size == 0: return

        avg_attempts = attempts / population_size
        DIVERSITY_PRESSURE_THRESHOLD = 7.5  # 当平均尝试次数超过此阈值，认为多样性不足

        if avg_attempts > DIVERSITY_PRESSURE_THRESHOLD:
            # --- 探索压力不足，增强探索 ---
            old_rate = self.hparams['mutation_rate']
            old_temp = self.hparams['pos_select_temp']
            
            # 提高突变率
            cfg = self.hparam_configs['mutation_rate']
            self.hparams['mutation_rate'] = min(old_rate * cfg['increase'], cfg['max'])
            
            # 提高pLDDT选择温度，降低其影响
            cfg_temp = self.hparam_configs['pos_select_temp']
            self.hparams['pos_select_temp'] = min(old_temp * cfg_temp['increase'], cfg_temp['max'])
            
            logger.warning(
                f"Low diversity detected (avg attempts: {avg_attempts:.1f}). Increasing exploration pressure. "
                f"Mutation rate: {old_rate:.3f} -> {self.hparams['mutation_rate']:.3f}, "
                f"pLDDT Temp: {old_temp:.2f} -> {self.hparams['pos_select_temp']:.2f}."
            )
        else:
            # --- 多样性充足，逐渐恢复基准值，加强利用 ---
            is_decaying = False
            # 衰减突变率
            cfg = self.hparam_configs['mutation_rate']
            if self.hparams['mutation_rate'] > cfg['base']:
                self.hparams['mutation_rate'] = max(self.hparams['mutation_rate'] * cfg['decay'], cfg['base'])
                is_decaying = True

            # 衰减pLDDT选择温度
            cfg_temp = self.hparam_configs['pos_select_temp']
            if self.hparams['pos_select_temp'] > cfg_temp['base']:
                self.hparams['pos_select_temp'] = max(self.hparams['pos_select_temp'] * cfg_temp['decay'], cfg_temp['base'])
                is_decaying = True
            
            if is_decaying:
                 logger.info(
                    f"Sufficient diversity (avg attempts: {avg_attempts:.1f}). "
                    f"Decaying exploration pressure towards baseline. "
                    f"Current mutation rate: {self.hparams['mutation_rate']:.3f}, "
                    f"pLDDT Temp: {self.hparams['pos_select_temp']:.2f}."
                )

    def run(self, iterations: int, **kwargs):
        """
        执行使用演化策略的主要并行设计循环。
        **kwargs 包含了所有其他的运行参数。
        """
        # 从 kwargs 中解包参数，提供默认值
        population_size = kwargs.get('population_size', 8)
        num_elites = kwargs.get('num_elites', 2)
        binder_chain_id = kwargs['binder_chain_id']
        binder_length = kwargs['binder_length']
        initial_binder_sequence = kwargs.get('initial_binder_sequence')
        glycan_ccd = kwargs.get('glycan_ccd')
        glycan_chain_id = kwargs.get('glycan_chain_id', 'C')
        glycosylation_site = kwargs.get('glycosylation_site')
        output_csv_path = kwargs.get('output_csv_path', f"design_summary_{int(time.time())}.csv")
        keep_temp_files = kwargs.get('keep_temp_files', False)
        weight_iptm = kwargs.get('weight_iptm', 0.7)
        weight_plddt = kwargs.get('weight_plddt', 0.3)

        logger.info("--- Starting Multi-Lineage Design Run with Adaptive Hyperparameters ---")
        logger.info(f"Scoring weights -> ipTM: {weight_iptm}, pLDDT: {weight_plddt}")
        if num_elites >= population_size:
            raise ValueError("`num_elites` must be less than `population_size`.")
        
        design_params = {
            'is_glycopeptide': bool(glycan_ccd), 'glycan_ccd': glycan_ccd,
            'glycan_chain_id': glycan_chain_id, 'glycosylation_site': glycosylation_site
        }

        def calculate_composite_score(metrics: dict) -> float:
            iptm = metrics.get('iptm', 0.0)
            avg_plddt_normalized = metrics.get('binder_avg_plddt', 0.0) / 100.0
            return (weight_iptm * iptm) + (weight_plddt * avg_plddt_normalized)

        elite_population = []
        all_results_data = []

        if initial_binder_sequence and initial_binder_sequence not in self.evaluated_sequences:
            self.evaluated_sequences.add(initial_binder_sequence)
            _, metrics, res_path = self._evaluate_one_candidate((0, initial_binder_sequence, binder_chain_id, design_params))
            if metrics:
                metrics['composite_score'] = calculate_composite_score(metrics)
                entry = {'generation': 0, 'sequence': initial_binder_sequence, **metrics}
                if keep_temp_files and res_path: entry['results_path'] = os.path.abspath(res_path)
                all_results_data.append(entry)
                elite_population = [{'sequence': initial_binder_sequence, 'metrics': metrics}]

        with concurrent.futures.ThreadPoolExecutor(max_workers=population_size) as executor:
            for i in range(iterations):
                start_time = time.time()
                logger.info(f"--- Generation {i+1}/{iterations} ---")

                # --- 1. 候选者生成 ---
                candidates_to_evaluate = []
                max_attempts = population_size * 25
                attempts = 0

                while len(candidates_to_evaluate) < population_size and attempts < max_attempts:
                    new_seq = None
                    if not elite_population:
                        if not candidates_to_evaluate:
                            logger.info(f"Seeding generation with {population_size} new random sequences...")
                        new_seq = generate_random_sequence(binder_length, glycosylation_site, design_params.get('glycan_ccd'))
                    else:
                        if not candidates_to_evaluate:
                            logger.info(f"Evolving {len(elite_population)} elites to create {population_size} new candidates...")
                        parent = random.choice(elite_population)
                        new_seq = mutate_sequence(
                            parent['sequence'],
                            mutation_rate=self.hparams['mutation_rate'],
                            plddt_scores=parent['metrics'].get('plddts', []),
                            glycosylation_site=glycosylation_site,
                            glycan_ccd=design_params.get('glycan_ccd'),
                            position_selection_temp=self.hparams['pos_select_temp']
                        )
                    
                    if new_seq and new_seq not in self.evaluated_sequences:
                        self.evaluated_sequences.add(new_seq)
                        candidates_to_evaluate.append((i + 1, new_seq, binder_chain_id, design_params))
                    attempts += 1
                
                if not candidates_to_evaluate:
                    logger.critical(f"Could not generate any new unique candidates after {max_attempts} attempts. This may indicate extreme premature convergence. Stopping run.")
                    break

                # --- 2. 评估 ---
                results = list(executor.map(self._evaluate_one_candidate, candidates_to_evaluate))
                valid_results = [res for res in results if res[1] is not None]

                if not valid_results and not elite_population:
                     logger.critical("First generation failed completely. Stopping run.")
                     break
                
                for seq, metrics, res_path in valid_results:
                    metrics['composite_score'] = calculate_composite_score(metrics)
                    entry = {'generation': i + 1, 'sequence': seq, **metrics}
                    if keep_temp_files and res_path: entry['results_path'] = os.path.abspath(res_path)
                    all_results_data.append(entry)
                
                all_results_data.sort(key=lambda x: x.get('composite_score', 0.0), reverse=True)
                new_elites = []
                seen_elite_seqs = set()
                for result in all_results_data:
                    if len(new_elites) >= num_elites: break
                    sequence = result.get('sequence')
                    if sequence not in seen_elite_seqs:
                        elite_entry = {
                            'sequence': sequence,
                            'metrics': result 
                        }
                        new_elites.append(elite_entry)
                        seen_elite_seqs.add(sequence)
                elite_population = new_elites
                
                # --- 3. 动态调整超参数 ---
                self._update_adaptive_hparams(attempts, len(candidates_to_evaluate))
                
                # 日志记录
                if elite_population:
                    best_elite = elite_population[0]
                    logger.info(
                        f"Generation {i+1} complete. Best score so far: {best_elite['metrics'].get('composite_score', 0.0):.4f} "
                        f"(ipTM: {best_elite['metrics'].get('iptm', 0.0):.4f}, pLDDT: {best_elite['metrics'].get('binder_avg_plddt', 0.0):.2f})"
                    )
                
                end_time = time.time()
                logger.info(f"--- Generation finished in {end_time - start_time:.2f} seconds ---")

        # --- 最终处理 ---
        logger.info("\n--- Design Run Finished ---")
        if all_results_data:
            final_best_entry = max(all_results_data, key=lambda r: r['composite_score'])
            logger.info(f"Final best sequence: {final_best_entry['sequence']}")
            logger.info(f"Final best composite score: {final_best_entry['composite_score']:.4f} "
                        f"(ipTM: {final_best_entry.get('iptm', 0.0):.4f}, "
                        f"pLDDT: {final_best_entry.get('binder_avg_plddt', 0.0):.2f})")
        self._write_summary_csv(all_results_data, output_csv_path, keep_temp_files)
        if not keep_temp_files:
            logger.info(f"Cleaning up temporary directory: {self.work_dir}")
            shutil.rmtree(self.work_dir)
        else:
            logger.info(f"Temporary files are kept in: {os.path.abspath(self.work_dir)}")

    def _create_candidate_yaml(self, sequence: str, chain_id: str, design_params: dict) -> str:
        """为候选序列动态创建Boltz YAML配置文件。"""
        config = copy.deepcopy(self.base_config)
        if 'sequences' not in config or not isinstance(config.get('sequences'), list):
            config['sequences'] = []

        found_chain = False
        for i, seq_block in enumerate(config['sequences']):
            if 'protein' in seq_block and seq_block.get('protein', {}).get('id') == chain_id:
                config['sequences'][i]['protein']['sequence'] = sequence
                found_chain = True
                break
        
        if not found_chain:
            config['sequences'].append({'protein': {'id': chain_id, 'sequence': sequence, 'msa': 'empty'}})

        if design_params.get('is_glycopeptide'):
            site_idx = design_params['glycosylation_site']
            glycan_ccd = design_params['glycan_ccd']
            residue_at_site = sequence[site_idx]
            
            valid_residues = get_valid_residues_for_glycan(glycan_ccd)
            if residue_at_site not in valid_residues:
                raise ValueError(
                    f"Residue '{residue_at_site}' at site {site_idx + 1} is not a valid attachment point "
                    f"for the '{glycan_ccd}' glycan (requires one of: {valid_residues})."
                )

            attachment_atom = None
            if residue_at_site in GLYCOSYLATION_SITES['N-linked']:
                attachment_atom = GLYCOSYLATION_SITES['N-linked'][residue_at_site]
            elif residue_at_site in GLYCOSYLATION_SITES['O-linked']:
                attachment_atom = GLYCOSYLATION_SITES['O-linked'][residue_at_site]

            if not attachment_atom:
                raise ValueError(f"Could not find attachment atom for residue '{residue_at_site}'.")

            config['sequences'].append({'ligand': {'id': design_params['glycan_chain_id'], 'ccd': glycan_ccd}})

            if 'constraints' not in config or not isinstance(config.get('constraints'), list):
                config['constraints'] = []
            
            config['constraints'].append({
                'bond': {
                    'atom1': [chain_id, site_idx + 1, attachment_atom],
                    'atom2': [design_params['glycan_chain_id'], 1, MONOSACCHARIDES[glycan_ccd]['atom']]
                }
            })

        yaml_path = os.path.join(self.work_dir, f"candidate_{os.getpid()}_{hash(sequence)}.yaml")
        with open(yaml_path, 'w') as f:
            yaml.dump(config, f, sort_keys=False)
        return yaml_path