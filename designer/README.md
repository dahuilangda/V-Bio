# Protein Designer

This module provides a command-line script to perform de novo protein design by leveraging the `Boltz-WebUI` prediction service as a computational backend.

It uses a gradient-free optimization strategy, where it iteratively generates candidate sequences, submits them for evaluation to the prediction API, and selects the best-performing ones based on metrics like ipTM.

## Prerequisites

1.  Your `Boltz-WebUI` platform must be running.
2.  The `requests`, `pyyaml`, and `numpy` Python libraries must be installed in your environment.
    ```bash
    pip install requests pyyaml numpy
    ```

## How to Run

The design process is initiated by running the `run_design.py` script from your terminal.

### **Step 1: Prepare a Template YAML**

Create a YAML file that describes your target (e.g., the protein you want to bind to) and the ligand. The sequence for the binder chain should be filled with placeholders (like 'X') as its length will be set by a command-line argument.

**Example `template.yaml`:**
```yaml
version: 1
sequences:
- protein:
    id:
    - A
    # This sequence will be replaced by the script
    sequence: XXXXXXXXXXXXXXXXXXXX
    msa: empty
- ligand:
    id:
    - B
    smiles: O=C(NCc1cocn1)c1cnn(C)c1C(=O)Nc1ccn2cc(nc2n1)c1ccccc1
````

### **Step 2: Run the Design Script**

Execute the script from the root directory of the `Boltz-WebUI` project.

```bash
# Make sure your virtual environment is activated
source venv/bin/activate

# Set the API token as an environment variable (recommended)
export API_SECRET_TOKEN='your-super-secret-and-long-token'

# Run the design script
python designer/run_design.py \
    --yaml_template /path/to/your/template.yaml \
    --binder_chain "A" \
    --binder_length 120 \
    --iterations 50 \
    --population_size 16 \
    --server_url "http://127.0.0.1:5000" \
    --output_csv "my_design_run_1_summary.csv" \
    --keep_temp_files
```

### Command-Line Arguments

  * `--yaml_template` (required): Path to your template YAML file.
  * `--binder_chain` (required): The chain ID of the binder to be designed.
  * `--binder_length` (required): The desired length of the binder sequence.
  * `--iterations`: The number of optimization cycles to run. Default is 50.
  * `--server_url`: The URL of your running Boltz-WebUI prediction API. Default is `http://127.0.0.1:5000`.
  * `--api_token`: Your API token. Can also be provided via the `API_SECRET_TOKEN` environment variable.
  * `--population_size`: Number of parallel jobs to run in each generation. Default is 16.
  * `--output_csv`: Path to save the summary CSV file of all evaluated designs.
  * `--keep_temp_files`: If set, temporary files will not be deleted after the run. Useful for debugging.