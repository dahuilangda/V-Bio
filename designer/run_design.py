# /Boltz-WebUI/designer/run_design.py

import argparse
import os
import time
from design_logic import ProteinDesigner
from api_client import BoltzApiClient

def main():
    parser = argparse.ArgumentParser(description="Run a parallel protein design job using the Boltz-WebUI API.")
    
    # --- Input Arguments ---
    parser.add_argument("--yaml_template", required=True, help="Path to the template YAML file.")
    parser.add_argument("--binder_chain", required=True, help="The chain ID of the protein to be designed (e.g., 'A').")
    parser.add_argument("--binder_length", required=True, type=int, help="The length of the protein binder.")

    # --- Run Control Arguments ---
    parser.add_argument("--iterations", type=int, default=20, help="Number of design-evaluate generations to run.")
    parser.add_argument("--population_size", type=int, default=4, help="Number of parallel jobs per generation.")
    parser.add_argument(
        "--num_elites",
        type=int,
        default=1,
        help="Number of top candidates (lineages) to maintain and evolve in parallel. Should be less than population_size."
    )

    # --- Output & Logging Arguments ---
    parser.add_argument("--output_csv", default=f"design_summary_{int(time.time())}.csv", help="Path for the output CSV summary file.")
    parser.add_argument("--keep_temp_files", action="store_true", help="If set, do not delete the temporary directory.")
    
    # --- API Connection Arguments ---
    parser.add_argument("--server_url", default="http://127.0.0.1:5000", help="URL of the Boltz-WebUI prediction API server.")
    parser.add_argument("--api_token", help="Your secret API token. Can also be set via API_SECRET_TOKEN environment variable.")

    args = parser.parse_args()

    # Get API token from arguments or environment variable
    api_token = args.api_token or os.environ.get('API_SECRET_TOKEN')
    if not api_token:
        raise ValueError("API token must be provided via --api_token or the API_SECRET_TOKEN environment variable.")

    # 1. Initialize the API client
    client = BoltzApiClient(server_url=args.server_url, api_token=api_token)

    # 2. Initialize the Designer
    designer = ProteinDesigner(base_yaml_path=args.yaml_template, client=client)

    # 3. Start the design run with the new options
    designer.run(
        iterations=args.iterations,
        population_size=args.population_size,
        num_elites=args.num_elites, # Pass the new argument
        binder_chain_id=args.binder_chain,
        binder_length=args.binder_length,
        output_csv_path=args.output_csv,
        keep_temp_files=args.keep_temp_files
    )

if __name__ == "__main__":
    main()