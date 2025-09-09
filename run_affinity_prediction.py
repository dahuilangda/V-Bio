
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
    Supports both complex file mode and separate protein/ligand mode.
    """
    try:
        with open(args_path, 'r') as f:
            args = json.load(f)
        
        task_temp_dir = args['task_temp_dir']
        ligand_resname = args.get('ligand_resname', 'LIG')
        output_csv_path = args['output_csv_path']

        # Check if this is separate input mode or complex file mode
        if 'protein_file_path' in args and 'ligand_file_path' in args:
            # For separate inputs, always use "LIG" as ligand name
            boltzina = Boltzina(
                output_dir=os.path.join(task_temp_dir, 'boltzina_output'),
                work_dir=os.path.join(task_temp_dir, 'boltzina_work'),
                ligand_resname="LIG"  # Fixed ligand name for separate inputs
            )
        else:
            # For complex file mode, use the provided ligand_resname
            boltzina = Boltzina(
                output_dir=os.path.join(task_temp_dir, 'boltzina_output'),
                work_dir=os.path.join(task_temp_dir, 'boltzina_work'),
                ligand_resname=ligand_resname
            )

        # Check if this is separate input mode or complex file mode
        if 'protein_file_path' in args and 'ligand_file_path' in args:
            # Separate protein and ligand files mode
            protein_file_path = args['protein_file_path']
            ligand_file_path = args['ligand_file_path']
            output_prefix = args.get('output_prefix', 'complex')
            
            logger.info(f"Starting affinity prediction with separate files:")
            logger.info(f"  Protein: {protein_file_path}")
            logger.info(f"  Ligand: {ligand_file_path}")
            
            # Run prediction with separate inputs
            boltzina.predict_with_separate_inputs(protein_file_path, ligand_file_path, output_prefix)
            
        elif 'input_file_path' in args:
            # Original complex file mode
            input_file_path = args['input_file_path']
            logger.info(f"Starting affinity prediction for complex file: {input_file_path}")
            
            # Run prediction with complex file
            boltzina.predict([input_file_path])
            
        else:
            raise ValueError("Missing required file arguments. Expected either 'input_file_path' or both 'protein_file_path' and 'ligand_file_path'")

        # Save results
        boltzina.save_results_csv(output_csv_path)

        if not os.path.exists(output_csv_path):
            raise FileNotFoundError("Output CSV file was not generated.")

        logger.info(f"Affinity prediction completed. Results saved to {output_csv_path}")

    except ValueError as ve:
        # Handle input validation errors with detailed messages
        error_message = str(ve)
        if "No ligand molecules (HETATM records) found" in error_message:
            logger.error(f"Input validation failed: {error_message}")
            logger.error("SOLUTION: This error occurs when your PDB file contains only protein atoms and no ligand molecules.")
            logger.error("To fix this issue:")
            logger.error("  1. Use a protein-ligand complex PDB file that contains both ATOM and HETATM records")
            logger.error("  2. Or use the 'separate' input mode with separate protein and ligand files")
            logger.error("  3. Or add ligand coordinates to your PDB file as HETATM records")
        elif "Ligand residue name" in error_message and "not found" in error_message:
            logger.error(f"Input validation failed: {error_message}")
            logger.error("SOLUTION: The specified ligand residue name was not found in your PDB file.")
            logger.error("Check the HETATM records in your PDB file and use the correct residue name.")
        else:
            logger.error(f"Input validation error: {error_message}")
        sys.exit(1)
        
    except RuntimeError as re:
        # Handle processing errors
        error_message = str(re)
        if "zero-size array to reduction operation minimum" in error_message:
            logger.error("Processing failed: Array dimension error during affinity prediction")
            logger.error("This error typically occurs when:")
            logger.error("  1. The input structure lacks proper ligand coordinates")
            logger.error("  2. The ligand structure is malformed or incomplete")
            logger.error("  3. There are issues with the protein-ligand complex structure")
            logger.error("SOLUTION: Please verify that your input contains a valid protein-ligand complex")
        elif "CCD component" in error_message and "not found" in error_message:
            logger.error(f"Chemical component error: {error_message}")
            logger.error("SOLUTION: Use standard PDB ligand names or provide structures with valid chemical components")
        else:
            logger.error(f"Processing error: {error_message}")
        sys.exit(1)
        
    except FileNotFoundError as fe:
        logger.error(f"File error: {fe}")
        sys.exit(1)
        
    except Exception as e:
        logger.error(f"An unexpected error occurred during affinity prediction: {e}")
        import traceback
        logger.error(f"Detailed traceback:\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_affinity_prediction.py <args_file_path>")
        sys.exit(1)
    
    args_file = sys.argv[1]
    run_prediction(args_file)
