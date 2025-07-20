# /Boltz-WebUI/designer/design_logic.py

"""
design_logic.py

该模块定义了`Designer`类，它是蛋白质和糖肽设计过程的核心控制器。
它实现了基于演化策略的梯度自由优化循环：
1.  **生成 (Generation)**: 基于精英群体创建新一代候选序列。
2.  **评估 (Evaluation)**: 并行提交候选者到Boltz-WebUI API进行结构预测和打分。
3.  **选择 (Selection)**: 根据一个复合评分函数（结合了ipTM和pLDDT）选择新的精英群体。

该类管理工作目录、与API客户端的交互以及整个设计流程的状态。
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
import numpy as np

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
    """
    def __init__(self, base_yaml_path: str, client: BoltzApiClient):
        """
        初始化Designer实例。

        Args:
            base_yaml_path (str): 定义系统静态部分（如受体）的模板YAML文件路径。
            client (BoltzApiClient): 用于与Boltz-WebUI API通信的客户端实例。
        """
        self.base_yaml_path = base_yaml_path
        self.client = client
        with open(base_yaml_path, 'r') as f:
            self.base_config = yaml.safe_load(f)
        
        self.work_dir = f"temp_design_run_{int(time.time())}"
        os.makedirs(self.work_dir, exist_ok=True)
        logger.info(f"Temporary working directory created at: {os.path.abspath(self.work_dir)}")

    def _evaluate_one_candidate(self, candidate_info: tuple) -> tuple:
        """
        提交单个候选者进行评估，轮询结果，并解析指标。

        这是一个内部方法，设计为在并发执行器中运行。

        Args:
            candidate_info (tuple): 包含 (generation_index, sequence, 
                                      binder_chain_id, design_params) 的元组。

        Returns:
            tuple: 包含 (sequence, metrics, results_path) 的元组。如果失败，
                   metrics 和 results_path 将为 None。
        """
        generation_index, sequence, binder_chain_id, design_params = candidate_info
        logger.info(f"[Gen {generation_index}] Evaluating candidate sequence: {sequence[:10]}...")
        
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
        logger.info(f"Candidate '{sequence[:10]}...' evaluated. ipTM: {iptm_score:.4f}, pLDDT: {plddt_score:.2f}")
        return (sequence, metrics, results_path)

    def _write_summary_csv(self, all_results: list, output_csv_path: str, keep_temp_files: bool):
        """
        将所有已评估候选者的摘要写入CSV文件，按复合分数排名。

        Args:
            all_results (list): 包含所有结果数据的字典列表。
            output_csv_path (str): 输出CSV文件的路径。
            keep_temp_files (bool): 是否在摘要中包含临时文件路径。
        """
        if not all_results:
            logger.warning("No results were generated to save to CSV.")
            return

        # 按新的复合分数排序
        all_results.sort(key=lambda x: x.get('composite_score', 0.0), reverse=True)
        # 在表头中添加复合分数
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
                    # 格式化浮点数输出，提高可读性
                    for key in ['composite_score', 'iptm', 'ptm', 'complex_plddt', 'binder_avg_plddt']:
                        if key in result_data and isinstance(result_data[key], float):
                            result_data[key] = f"{result_data[key]:.4f}"
                    row_to_write = {key: result_data.get(key, 'N/A') for key in header}
                    writer.writerow(row_to_write)
            logger.info(f"Summary CSV successfully saved to {output_csv_path}")
        except IOError as e:
            logger.error(f"Could not write to CSV file at {output_csv_path}. Reason: {e}")

    def run(
        self,
        iterations: int,
        population_size: int,
        num_elites: int,
        binder_chain_id: str,
        binder_length: int,
        glycan_ccd: str,
        glycan_chain_id: str,
        glycosylation_site: int, # 0-based index
        output_csv_path: str,
        keep_temp_files: bool,
        weight_iptm: float,
        weight_plddt: float
    ):
        """
        执行使用演化策略的主要并行设计循环。

        Args:
            (省略了其他参数的文档以保持简洁)
            ...
            weight_iptm (float): 复合评分中ipTM分数的权重。
            weight_plddt (float): 复合评分中binder平均pLDDT分数的权重。
        """
        logger.info("--- Starting Multi-Lineage Design Run ---")
        logger.info(f"Scoring weights -> ipTM: {weight_iptm}, pLDDT: {weight_plddt}")
        if num_elites >= population_size:
            raise ValueError("`num_elites` must be less than `population_size`.")
        
        design_params = {
            'is_glycopeptide': bool(glycan_ccd),
            'glycan_ccd': glycan_ccd,
            'glycan_chain_id': glycan_chain_id,
            'glycosylation_site': glycosylation_site,
        }
        if design_params['is_glycopeptide']:
             logger.info(f"Glycopeptide design mode activated for glycan '{glycan_ccd}' on chain {glycan_chain_id} at site {glycosylation_site + 1}.")
             if glycosylation_site is None:
                 raise ValueError("`glycosylation_site` must be provided for glycopeptide design.")

        # 定义复合分数计算函数
        def calculate_composite_score(metrics: dict) -> float:
            """根据给定的权重计算复合分数。"""
            iptm = metrics.get('iptm', 0.0)
            # 将 pLDDT (0-100) 归一化到 (0-1) 范围
            avg_plddt_normalized = metrics.get('binder_avg_plddt', 0.0) / 100.0
            score = (weight_iptm * iptm) + (weight_plddt * avg_plddt_normalized)
            return score

        elite_population = []
        all_results_data = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=population_size) as executor:
            for i in range(iterations):
                start_time = time.time()
                logger.info(f"--- Generation {i+1}/{iterations} ---")

                # --- 1. 候选者生成 ---
                candidates_to_evaluate = []
                if not elite_population:
                    logger.info(f"Seeding first generation with {population_size} random sequences...")
                    for _ in range(population_size):
                        random_seq = generate_random_sequence(
                            binder_length, 
                            glycosylation_site, 
                            glycan_ccd=design_params.get('glycan_ccd')
                        )
                        candidates_to_evaluate.append((i + 1, random_seq, binder_chain_id, design_params))
                else:
                    logger.info(f"Evolving {len(elite_population)} elite sequences...")
                    for elite in elite_population:
                        candidates_to_evaluate.append((i + 1, elite['sequence'], binder_chain_id, design_params))
                    
                    num_mutants = population_size - len(elite_population)
                    for _ in range(num_mutants):
                        parent = random.choice(elite_population)
                        mutated_seq = mutate_sequence(
                            parent['sequence'],
                            mutation_rate=0.1,
                            plddt_scores=parent['metrics'].get('plddts', []),
                            glycosylation_site=glycosylation_site,
                            glycan_ccd=design_params.get('glycan_ccd')
                        )
                        candidates_to_evaluate.append((i + 1, mutated_seq, binder_chain_id, design_params))

                # --- 2. 评估 ---
                results = list(executor.map(self._evaluate_one_candidate, candidates_to_evaluate))
                valid_results = [res for res in results if res[1] is not None]

                if not valid_results:
                    logger.warning("No candidates were successfully evaluated in this generation. Continuing with previous elites.")
                    if not elite_population:
                        logger.critical("First generation failed completely. Stopping run.")
                        break
                    continue
                
                # --- 为每个有效结果计算并添加复合分数 ---
                for seq, metrics, res_path in valid_results:
                    metrics['composite_score'] = calculate_composite_score(metrics)
                    entry = {'generation': i + 1, 'sequence': seq, **metrics}
                    if keep_temp_files and res_path: entry['results_path'] = os.path.abspath(res_path)
                    all_results_data.append(entry)

                # --- 3. 选择 (精英主义)，现在基于复合分数 ---
                candidate_pool = elite_population + [{'sequence': seq, 'metrics': met} for seq, met, _ in valid_results]
                unique_candidates = {cand['sequence']: cand for cand in sorted(candidate_pool, key=lambda x: x['metrics'].get('composite_score', 0.0))}
                
                # 按新的复合分数对整个池进行排序
                sorted_pool = sorted(unique_candidates.values(), key=lambda x: x['metrics'].get('composite_score', 0.0), reverse=True)
                
                elite_population = sorted_pool[:num_elites]

                if elite_population:
                    best_elite = elite_population[0]
                    best_score = best_elite['metrics'].get('composite_score', 0.0)
                    best_iptm = best_elite['metrics'].get('iptm', 0.0)
                    best_plddt = best_elite['metrics'].get('binder_avg_plddt', 0.0)
                    logger.info(
                        f"Generation {i+1} complete. Best score: {best_score:.4f} "
                        f"(ipTM: {best_iptm:.4f}, pLDDT: {best_plddt:.2f})"
                    )
                
                end_time = time.time()
                logger.info(f"--- Generation finished in {end_time - start_time:.2f} seconds ---")

        # --- 最终处理 ---
        logger.info("\n--- Design Run Finished ---")
        if all_results_data:
            # 按复合分数找到最终最佳条目
            final_best_entry = max(all_results_data, key=lambda r: r['composite_score'])
            final_best_score = final_best_entry['composite_score']
            logger.info(f"Final best sequence: {final_best_entry['sequence']}")
            logger.info(f"Final best composite score: {final_best_score:.4f} "
                        f"(ipTM: {final_best_entry.get('iptm', 0.0):.4f}, "
                        f"pLDDT: {final_best_entry.get('binder_avg_plddt', 0.0):.2f})")
        
        self._write_summary_csv(all_results_data, output_csv_path, keep_temp_files)

        if not keep_temp_files:
            logger.info(f"Cleaning up temporary directory: {self.work_dir}")
            shutil.rmtree(self.work_dir)
        else:
            logger.info(f"Temporary files are kept in: {os.path.abspath(self.work_dir)}")

    def _create_candidate_yaml(self, sequence: str, chain_id: str, design_params: dict) -> str:
        """为候选序列动态创建Boltz YAML配置文件。

        此版本使用深拷贝来防止候选者之间的状态泄漏，并验证糖基化位点的有效性。

        Args:
            sequence (str): 要评估的候选序列。
            chain_id (str): 肽链的ID。
            design_params (dict): 包含糖肽设计参数的字典。

        Returns:
            str: 生成的YAML文件的路径。
        """
        config = copy.deepcopy(self.base_config)
        
        if 'sequences' not in config or not isinstance(config.get('sequences'), list):
            config['sequences'] = []

        # 更新或添加肽链序列
        found_chain = False
        for i, seq_block in enumerate(config['sequences']):
            if 'protein' in seq_block and seq_block.get('protein', {}).get('id') == chain_id:
                config['sequences'][i]['protein']['sequence'] = sequence
                found_chain = True
                break
        
        if not found_chain:
            config['sequences'].append({
                'protein': {'id': chain_id, 'sequence': sequence, 'msa': 'empty'}
            })

        # 处理糖肽配置
        if design_params.get('is_glycopeptide'):
            site_idx = design_params['glycosylation_site'] # 0-based
            glycan_ccd = design_params['glycan_ccd']
            residue_at_site = sequence[site_idx]
            
            # --- 验证: 检查残基是否与聚糖类型兼容 ---
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
                # 这个检查理论上是冗余的，但作为安全措施保留
                raise ValueError(f"Could not find attachment atom for residue '{residue_at_site}'.")

            config['sequences'].append({
                'ligand': {'id': design_params['glycan_chain_id'], 'ccd': glycan_ccd}
            })

            if 'constraints' not in config or not isinstance(config.get('constraints'), list):
                config['constraints'] = []
            
            config['constraints'].append({
                'bond': {
                    'atom1': [chain_id, site_idx + 1, attachment_atom], # YAML是1-indexed
                    'atom2': [design_params['glycan_chain_id'], 1, MONOSACCHARIDES[glycan_ccd]['atom']]
                }
            })

        # --- 写入YAML文件 ---
        # 使用进程ID和哈希值确保文件名唯一，避免并发冲突
        yaml_path = os.path.join(self.work_dir, f"candidate_{os.getpid()}_{hash(sequence)}.yaml")
        with open(yaml_path, 'w') as f:
            yaml.dump(config, f, sort_keys=False)
        return yaml_path