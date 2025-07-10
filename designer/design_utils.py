# /Boltz-WebUI/designer/design_utils.py

import random
import os
import json
import numpy as np

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"

def generate_random_sequence(length: int) -> str:
    """Generates a random amino acid sequence of a given length."""
    return "".join(random.choice(AMINO_ACIDS) for _ in range(length))

def mutate_sequence(sequence: str, mutation_rate: float = 0.1) -> str:
    """
    Introduces random point mutations into a sequence.
    A more advanced version could use pLDDT scores to guide mutations.
    """
    new_sequence = list(sequence)
    num_mutations = int(len(sequence) * mutation_rate)
    
    for _ in range(num_mutations):
        pos = random.randint(0, len(sequence) - 1)
        new_aa = random.choice(AMINO_ACIDS)
        new_sequence[pos] = new_aa
        
    return "".join(new_sequence)

def parse_confidence_metrics(results_path: str) -> dict:
    """
    Parses the confidence metrics from the downloaded result files.
    This function assumes a certain structure for the result files,
    which you may need to adjust based on your Boltz-WebUI output.
    """
    metrics = {
        'iptm': 0.0,
        'plddt': 0.0,
    }
    try:
        for filename in os.listdir(results_path):
            if filename.startswith('confidence_') and filename.endswith('.json'):
                json_path = os.path.join(results_path, filename)
                with open(json_path, 'r') as f:
                    data = json.load(f)
                    metrics['iptm'] = data.get('iptm', 0.0)
                    metrics['plddt'] = data.get('complex_plddt', 0.0)
                break
    except Exception as e:
        print(f"Warning: Could not parse confidence metrics from {results_path}. Error: {e}")
        
    return metrics