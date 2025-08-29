
import os
import json
import sys
import logging
import shutil
from pathlib import Path
from affinity.main import Boltzina

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_prediction(args_path: str):
    """
    Runs the affinity prediction based on arguments from a JSON file.
    """
    try:
        with open(args_path, 'r') as f:
            args = json.load(f)
        
        task_temp_dir = args['task_temp_dir']
        input_file_path = args['input_file_path']
        ligand_resname = args['ligand_resname']
        output_csv_path = args['output_csv_path']

        logger.info(f"Starting affinity prediction for {input_file_path}")

        # Initialize Boltzina
        boltzina = Boltzina(
            output_dir=os.path.join(task_temp_dir, 'boltzina_output'),
            work_dir=os.path.join(task_temp_dir, 'boltzina_work'),
            ligand_resname=ligand_resname
        )

        # Run prediction
        boltzina.predict([input_file_path])

        # Save results
        boltzina.save_results_csv(output_csv_path)

        if not os.path.exists(output_csv_path):
            raise FileNotFoundError("Output CSV file was not generated.")

        logger.info(f"Affinity prediction completed. Results saved to {output_csv_path}")

    except Exception as e:
        logger.exception(f"An error occurred during affinity prediction: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_affinity_prediction.py <args_file_path>")
        sys.exit(1)
    
    args_file = sys.argv[1]
    run_prediction(args_file)
