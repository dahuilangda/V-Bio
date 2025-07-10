# /Boltz-WebUI/designer/design_logic.py

import os
import yaml
import shutil
import time
import csv
import concurrent.futures
from api_client import BoltzApiClient
from design_utils import generate_random_sequence, mutate_sequence, parse_confidence_metrics

class ProteinDesigner:
    """
    Manages the gradient-free design optimization loop using parallel API calls.
    Now includes CSV logging and optional temporary file retention.
    """
    def __init__(self, base_yaml_path: str, client: BoltzApiClient):
        self.base_yaml_path = base_yaml_path
        self.client = client
        with open(base_yaml_path, 'r') as f:
            self.base_config = yaml.safe_load(f)
        
        # Create a temporary directory for this design run
        self.work_dir = f"temp_design_run_{int(time.time())}"
        os.makedirs(self.work_dir, exist_ok=True)
        print(f"Temporary working directory created at: {os.path.abspath(self.work_dir)}")


    def _evaluate_one_candidate(self, candidate_sequence_info: tuple) -> tuple:
        """
        A worker function to process a single candidate.
        This function is designed to be called by a thread pool executor.
        """
        generation_index, sequence, binder_chain_id = candidate_sequence_info
        print(f"  [Gen {generation_index}] Evaluating candidate...")
        candidate_yaml_path = self._create_candidate_yaml(sequence, binder_chain_id)
        task_id = self.client.submit_job(candidate_yaml_path)
        if not task_id: return (sequence, -1.0)
        status = self.client.poll_status(task_id)
        if not status or status.get('state') != 'SUCCESS': return (sequence, -1.0)
        results_path = self.client.download_and_unzip_results(task_id, self.work_dir)
        if not results_path: return (sequence, -1.0)
        metrics = parse_confidence_metrics(results_path)
        score = metrics.get('iptm', 0.0)
        print(f"  [Gen {generation_index}] Candidate evaluated. Score (ipTM): {score:.4f}")
        return (sequence, score)


    def run(
        self, 
        iterations: int, 
        population_size: int, 
        binder_chain_id: str, 
        binder_length: int,
        output_csv_path: str,
        keep_temp_files: bool
    ):
        """
        Executes the main parallel design loop with CSV logging and optional cleanup.
        """
        print("--- Starting Parallel Protein Design Run ---")
        print(f"Population Size (Parallel Jobs): {population_size}")
        
        # --- Initialize CSV Logger ---
        print(f"Logging all evaluated designs to: {os.path.abspath(output_csv_path)}")
        try:
            with open(output_csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['generation', 'sequence', 'score_iptm'])
        except IOError as e:
            print(f"Error: Could not write to CSV file at {output_csv_path}. Aborting. Error: {e}")
            return
            
        overall_best_sequence = generate_random_sequence(binder_length)
        overall_best_score = -1.0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=population_size) as executor:
            for i in range(iterations):
                start_time = time.time()
                print(f"\n--- Generation {i+1}/{iterations} ---")
                print(f"Current best overall score: {overall_best_score:.4f}")
                
                candidates_to_evaluate = [(i + 1, overall_best_sequence, binder_chain_id)] 
                for _ in range(population_size - 1):
                    mutated_seq = mutate_sequence(overall_best_sequence, mutation_rate=0.2)
                    candidates_to_evaluate.append((i + 1, mutated_seq, binder_chain_id))

                results = list(executor.map(self._evaluate_one_candidate, candidates_to_evaluate))
                
                valid_results = [res for res in results if res[1] > -1.0]
                if not valid_results:
                    print("Warning: No candidates were successfully evaluated in this generation. Retrying.")
                    continue
                
                # --- Log results from this generation to the CSV ---
                with open(output_csv_path, 'a', newline='') as f:
                    writer = csv.writer(f)
                    for seq, score in valid_results:
                        writer.writerow([i + 1, seq, f"{score:.4f}"])

                generation_best_sequence, generation_best_score = max(valid_results, key=lambda item: item[1])

                if generation_best_score > overall_best_score:
                    overall_best_score = generation_best_score
                    overall_best_sequence = generation_best_sequence
                    print(f"🎉 New overall best sequence found! Score improved to: {overall_best_score:.4f}")
                
                end_time = time.time()
                print(f"--- Generation {i+1} finished in {end_time - start_time:.2f} seconds ---")

        print("\n--- Design Run Finished ---")
        print(f"Final best sequence: {overall_best_sequence}")
        print(f"Final best score (ipTM): {overall_best_score:.4f}")
        print(f"Summary of all designs saved to {os.path.abspath(output_csv_path)}")

        # --- Conditional cleanup of temporary files ---
        if not keep_temp_files:
            print(f"Cleaning up temporary directory: {self.work_dir}")
            shutil.rmtree(self.work_dir)
        else:
            print(f"Temporary files are kept in: {os.path.abspath(self.work_dir)}")

        return overall_best_sequence, overall_best_score

    def _create_candidate_yaml(self, sequence: str, chain_id: str) -> str:
        config = self.base_config.copy()
        found = False
        for i, seq_block in enumerate(config.get('sequences', [])):
            protein_info = seq_block.get('protein', {})
            if isinstance(protein_info.get('id'), list) and protein_info['id'][0] == chain_id:
                config['sequences'][i]['protein']['sequence'] = sequence
                found = True
                break
        if not found:
            raise ValueError(f"Could not find chain ID '{chain_id}' in the base YAML file.")
        yaml_path = os.path.join(self.work_dir, f"candidate_{os.getpid()}_{hash(sequence)}.yaml")
        with open(yaml_path, 'w') as f:
            yaml.dump(config, f)
        return yaml_path