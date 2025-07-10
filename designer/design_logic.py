# /Boltz-WebUI/designer/design_logic.py

import os
import yaml
import shutil
import time
import csv
import random
import concurrent.futures
from api_client import BoltzApiClient
from design_utils import generate_random_sequence, mutate_sequence, parse_confidence_metrics

class ProteinDesigner:
    """
    Manages a multi-lineage, gradient-free design optimization loop.
    """
    def __init__(self, base_yaml_path: str, client: BoltzApiClient):
        self.base_yaml_path = base_yaml_path
        self.client = client
        with open(base_yaml_path, 'r') as f:
            self.base_config = yaml.safe_load(f)
        
        self.work_dir = f"temp_design_run_{int(time.time())}"
        os.makedirs(self.work_dir, exist_ok=True)
        print(f"Temporary working directory created at: {os.path.abspath(self.work_dir)}")

    def _evaluate_one_candidate(self, candidate_sequence_info: tuple) -> tuple:
        generation_index, sequence, binder_chain_id = candidate_sequence_info
        print(f"  [Gen {generation_index}] Evaluating candidate...")
        candidate_yaml_path = self._create_candidate_yaml(sequence, binder_chain_id)
        task_id = self.client.submit_job(candidate_yaml_path)
        if not task_id: return (sequence, None, None)
        status = self.client.poll_status(task_id)
        if not status or status.get('state') != 'SUCCESS': return (sequence, None, None)
        results_path = self.client.download_and_unzip_results(task_id, self.work_dir)
        if not results_path: return (sequence, None, None)
        metrics = parse_confidence_metrics(results_path, binder_chain_id)
        score = metrics.get('iptm', 0.0)
        print(f"  [Gen {generation_index}] Candidate evaluated. Score (ipTM): {score:.4f}")
        return (sequence, metrics, results_path)

    def _write_summary_csv(self, all_results: list, output_csv_path: str, keep_temp_files: bool):
        if not all_results:
            print("Warning: No results were generated to save to CSV.")
            return
        all_results.sort(key=lambda x: x.get('score_iptm', 0.0), reverse=True)
        header = ['rank', 'generation', 'sequence', 'score_iptm', 'ptm', 'complex_plddt', 'binder_avg_plddt']
        if keep_temp_files:
            header.append('results_path')
        print(f"\nWriting {len(all_results)} total results to {os.path.abspath(output_csv_path)}...")
        try:
            with open(output_csv_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=header)
                writer.writeheader()
                for i, result_data in enumerate(all_results):
                    result_data['rank'] = i + 1
                    row_to_write = {key: result_data.get(key, 'N/A') for key in header}
                    writer.writerow(row_to_write)
            print("✅ Summary CSV successfully saved.")
        except IOError as e:
            print(f"Error: Could not write to CSV file at {output_csv_path}. Error: {e}")

    def run(
        self, 
        iterations: int, 
        population_size: int, 
        num_elites: int,
        binder_chain_id: str, 
        binder_length: int,
        output_csv_path: str,
        keep_temp_files: bool
    ):
        """
        Executes the main parallel design loop using multiple elite lineages.
        """
        print("--- Starting Multi-Lineage Protein Design Run ---")
        if num_elites > population_size:
            raise ValueError("`num_elites` must be smaller than `population_size`.")
        
        elite_population = [] # Will store a list of {'sequence': str, 'metrics': dict}
        all_results_data = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=population_size) as executor:
            for i in range(iterations):
                start_time = time.time()
                print(f"\n--- Generation {i+1}/{iterations} ---")

                # --- 1. Candidate Generation ---
                candidates_to_evaluate = []
                if not elite_population:
                    print(f"Seeding first generation with {population_size} random sequences...")
                    for _ in range(population_size):
                        random_seq = generate_random_sequence(binder_length)
                        candidates_to_evaluate.append((i + 1, random_seq, binder_chain_id))
                else:
                    print(f"Evolving {len(elite_population)} elite sequences...")
                    # Add the elites themselves to the evaluation pool
                    for elite in elite_population:
                        candidates_to_evaluate.append((i + 1, elite['sequence'], binder_chain_id))
                    
                    # Fill the rest of the population with mutations of random elites
                    num_mutants = population_size - len(elite_population)
                    for _ in range(num_mutants):
                        parent = random.choice(elite_population)
                        mutated_seq = mutate_sequence(
                            parent['sequence'],
                            mutation_rate=0.2,
                            plddt_scores=parent['metrics'].get('plddts', [])
                        )
                        candidates_to_evaluate.append((i + 1, mutated_seq, binder_chain_id))

                # --- 2. Evaluation ---
                results = list(executor.map(self._evaluate_one_candidate, candidates_to_evaluate))
                valid_results = [res for res in results if res[1] is not None]
                if not valid_results:
                    print("Warning: No candidates were successfully evaluated in this generation. Continuing with previous elites.")
                    continue

                # Log all valid results from this generation
                for seq, metrics, res_path in valid_results:
                    entry = {'generation': i + 1, 'sequence': seq, **metrics}
                    if keep_temp_files and res_path: entry['results_path'] = os.path.abspath(res_path)
                    all_results_data.append(entry)

                # --- 3. Selection ---
                # Create a combined pool of previous elites and new results
                candidate_pool = elite_population + [{'sequence': seq, 'metrics': met} for seq, met, _ in valid_results]
                
                # Filter for unique sequences, keeping only the best entry for each
                unique_candidates = {cand['sequence']: cand for cand in sorted(candidate_pool, key=lambda x: x['metrics'].get('iptm', 0.0))}
                
                # Sort the unique candidates by score to find the new best
                sorted_pool = sorted(unique_candidates.values(), key=lambda x: x['metrics'].get('iptm', 0.0), reverse=True)

                # Update the elite population for the next generation
                elite_population = sorted_pool[:num_elites]

                if elite_population:
                    best_score = elite_population[0]['metrics'].get('iptm', 0.0)
                    print(f"Generation {i+1} complete. Best score in population: {best_score:.4f}")

                end_time = time.time()
                print(f"--- Generation finished in {end_time - start_time:.2f} seconds ---")

        # --- Final Processing ---
        print("\n--- Design Run Finished ---")
        if all_results_data:
            final_best_score = max(r['iptm'] for r in all_results_data)
            final_best_seq = next(r['sequence'] for r in all_results_data if r['iptm'] == final_best_score)
            print(f"Final best sequence: {final_best_seq}")
            print(f"Final best score (ipTM): {final_best_score:.4f}")

        self._write_summary_csv(all_results_data, output_csv_path, keep_temp_files)

        if not keep_temp_files:
            print(f"Cleaning up temporary directory: {self.work_dir}")
            shutil.rmtree(self.work_dir)
        else:
            print(f"Temporary files are kept in: {os.path.abspath(self.work_dir)}")

    def _create_candidate_yaml(self, sequence: str, chain_id: str) -> str:
        config = self.base_config.copy()
        found = False
        for i, seq_block in enumerate(config.get('sequences', [])):
            protein_info = seq_block.get('protein', {})
            if (isinstance(p_id := protein_info.get('id'), list) and p_id[0] == chain_id) or \
               (isinstance(p_id, str) and p_id == chain_id):
                config['sequences'][i]['protein']['sequence'] = sequence
                found = True
                break
        if not found:
            raise ValueError(f"Could not find chain ID '{chain_id}' in the base YAML file.")
        yaml_path = os.path.join(self.work_dir, f"candidate_{os.getpid()}_{hash(sequence)}.yaml")
        with open(yaml_path, 'w') as f:
            yaml.dump(config, f)
        return yaml_path