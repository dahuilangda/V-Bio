# /Boltz-WebUI/designer/design_utils.py

import random
import os
import json
import numpy as np

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"

# BLOSUM62 substitution matrix. Favors mutations to similar amino acids.
BLOSUM62 = {
    'A': {'A': 4, 'R': -1, 'N': -2, 'D': -2, 'C': 0, 'Q': -1, 'E': -1, 'G': 0, 'H': -2, 'I': -1, 'L': -1, 'K': -1, 'M': -1, 'F': -2, 'P': -1, 'S': 1, 'T': 0, 'W': -3, 'Y': -2, 'V': 0},
    'R': {'A': -1, 'R': 5, 'N': 0, 'D': -2, 'C': -3, 'Q': 1, 'E': 0, 'G': -2, 'H': 0, 'I': -3, 'L': -2, 'K': 2, 'M': -1, 'F': -3, 'P': -2, 'S': -1, 'T': -1, 'W': -3, 'Y': -2, 'V': -3},
    'N': {'A': -2, 'R': 0, 'N': 6, 'D': 1, 'C': -3, 'Q': 0, 'E': 0, 'G': 0, 'H': 1, 'I': -3, 'L': -3, 'K': 0, 'M': -2, 'F': -3, 'P': -2, 'S': 1, 'T': 0, 'W': -4, 'Y': -2, 'V': -3},
    'D': {'A': -2, 'R': -2, 'N': 1, 'D': 6, 'C': -3, 'Q': 0, 'E': 2, 'G': -1, 'H': -1, 'I': -3, 'L': -4, 'K': -1, 'M': -3, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -4, 'Y': -3, 'V': -3},
    'C': {'A': 0, 'R': -3, 'N': -3, 'D': -3, 'C': 9, 'Q': -3, 'E': -4, 'G': -3, 'H': -3, 'I': -1, 'L': -1, 'K': -3, 'M': -1, 'F': -2, 'P': -3, 'S': -1, 'T': -1, 'W': -2, 'Y': -2, 'V': -1},
    'Q': {'A': -1, 'R': 1, 'N': 0, 'D': 0, 'C': -3, 'Q': 5, 'E': 2, 'G': -2, 'H': 0, 'I': -3, 'L': -2, 'K': 1, 'M': 0, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -2, 'Y': -1, 'V': -2},
    'E': {'A': -1, 'R': 0, 'N': 0, 'D': 2, 'C': -4, 'Q': 2, 'E': 5, 'G': -2, 'H': 0, 'I': -3, 'L': -3, 'K': 1, 'M': -2, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -3, 'Y': -2, 'V': -2},
    'G': {'A': 0, 'R': -2, 'N': 0, 'D': -1, 'C': -3, 'Q': -2, 'E': -2, 'G': 6, 'H': -2, 'I': -4, 'L': -4, 'K': -2, 'M': -3, 'F': -3, 'P': -2, 'S': 0, 'T': -2, 'W': -2, 'Y': -3, 'V': -3},
    'H': {'A': -2, 'R': 0, 'N': 1, 'D': -1, 'C': -3, 'Q': 0, 'E': 0, 'G': -2, 'H': 8, 'I': -3, 'L': -3, 'K': -1, 'M': -2, 'F': -1, 'P': -2, 'S': -1, 'T': -2, 'W': -2, 'Y': 2, 'V': -3},
    'I': {'A': -1, 'R': -3, 'N': -3, 'D': -3, 'C': -1, 'Q': -3, 'E': -3, 'G': -4, 'H': -3, 'I': 4, 'L': 2, 'K': -3, 'M': 1, 'F': 0, 'P': -3, 'S': -2, 'T': -1, 'W': -3, 'Y': -1, 'V': 3},
    'L': {'A': -1, 'R': -2, 'N': -3, 'D': -4, 'C': -1, 'Q': -2, 'E': -3, 'G': -4, 'H': -3, 'I': 2, 'L': 4, 'K': -2, 'M': 2, 'F': 0, 'P': -3, 'S': -2, 'T': -1, 'W': -2, 'Y': -1, 'V': 1},
    'K': {'A': -1, 'R': 2, 'N': 0, 'D': -1, 'C': -3, 'Q': 1, 'E': 1, 'G': -2, 'H': -1, 'I': -3, 'L': -2, 'K': 5, 'M': -1, 'F': -3, 'P': -1, 'S': 0, 'T': -1, 'W': -3, 'Y': -2, 'V': -2},
    'M': {'A': -1, 'R': -1, 'N': -2, 'D': -3, 'C': -1, 'Q': 0, 'E': -2, 'G': -3, 'H': -2, 'I': 1, 'L': 2, 'K': -1, 'M': 5, 'F': 0, 'P': -2, 'S': -1, 'T': -1, 'W': -1, 'Y': -1, 'V': 1},
    'F': {'A': -2, 'R': -3, 'N': -3, 'D': -3, 'C': -2, 'Q': -3, 'E': -3, 'G': -3, 'H': -1, 'I': 0, 'L': 0, 'K': -3, 'M': 0, 'F': 6, 'P': -4, 'S': -2, 'T': -2, 'W': 1, 'Y': 3, 'V': -1},
    'P': {'A': -1, 'R': -2, 'N': -2, 'D': -1, 'C': -3, 'Q': -1, 'E': -1, 'G': -2, 'H': -2, 'I': -3, 'L': -3, 'K': -1, 'M': -2, 'F': -4, 'P': 7, 'S': -1, 'T': -1, 'W': -4, 'Y': -3, 'V': -2},
    'S': {'A': 1, 'R': -1, 'N': 1, 'D': 0, 'C': -1, 'Q': 0, 'E': 0, 'G': 0, 'H': -1, 'I': -2, 'L': -2, 'K': 0, 'M': -1, 'F': -2, 'P': -1, 'S': 4, 'T': 1, 'W': -3, 'Y': -2, 'V': -2},
    'T': {'A': 0, 'R': -1, 'N': 0, 'D': -1, 'C': -1, 'Q': -1, 'E': -1, 'G': -2, 'H': -2, 'I': -1, 'L': -1, 'K': -1, 'M': -1, 'F': -2, 'P': -1, 'S': 1, 'T': 5, 'W': -2, 'Y': -2, 'V': 0},
    'W': {'A': -3, 'R': -3, 'N': -4, 'D': -4, 'C': -2, 'Q': -2, 'E': -3, 'G': -2, 'H': -2, 'I': -3, 'L': -2, 'K': -3, 'M': -1, 'F': 1, 'P': -4, 'S': -3, 'T': -2, 'W': 11, 'Y': 2, 'V': -3},
    'Y': {'A': -2, 'R': -2, 'N': -2, 'D': -3, 'C': -2, 'Q': -1, 'E': -2, 'G': -3, 'H': 2, 'I': -1, 'L': -1, 'K': -2, 'M': -1, 'F': 3, 'P': -3, 'S': -2, 'T': -2, 'W': 2, 'Y': 7, 'V': -1},
    'V': {'A': 0, 'R': -3, 'N': -3, 'D': -3, 'C': -1, 'Q': -2, 'E': -2, 'G': -3, 'H': -3, 'I': 3, 'L': 1, 'K': -2, 'M': 1, 'F': -1, 'P': -2, 'S': -2, 'T': 0, 'W': -3, 'Y': -1, 'V': 4}
}

def generate_random_sequence(length: int) -> str:
    return "".join(random.choice(AMINO_ACIDS) for _ in range(length))

def mutate_sequence(
    sequence: str, 
    mutation_rate: float = 0.1, 
    plddt_scores: list = None,
    temperature: float = 1.0
) -> str:
    """
    Introduces point mutations into a sequence.
    - Positions are chosen based on low pLDDT scores.
    - Amino acid substitutions are guided by the BLOSUM62 matrix using a Softmax function.
    """
    new_sequence = list(sequence)
    num_mutations = int(len(sequence) * mutation_rate)
    if num_mutations == 0: num_mutations = 1

    # --- Step 1: Choose WHERE to mutate (pLDDT-guided) ---
    positions_to_mutate = []
    if plddt_scores and len(plddt_scores) == len(sequence):
        # Invert scores to use as weights (low plddt = high weight)
        weights = [100.0 - score for score in plddt_scores]
        total_weight = sum(weights)
        if total_weight > 0:
            probabilities = [w / total_weight for w in weights]
            indices = list(range(len(sequence)))
            k = min(num_mutations, len(indices))
            positions_to_mutate = np.random.choice(indices, size=k, replace=False, p=probabilities)
        else:
            positions_to_mutate = random.sample(range(len(sequence)), k=min(num_mutations, len(sequence)))
    else:
        if plddt_scores:
             print("Warning: pLDDT scores length mismatch. Falling back to random mutation position.")
        positions_to_mutate = random.sample(range(len(sequence)), k=min(num_mutations, len(sequence)))

    # --- Step 2: Choose WHAT to mutate to (BLOSUM62-guided with Softmax) ---
    for pos in positions_to_mutate:
        original_aa = new_sequence[pos]
        substitution_scores = BLOSUM62.get(original_aa, {})
        
        possible_aas = []
        scores = []
        for aa in AMINO_ACIDS:
            if aa != original_aa:
                possible_aas.append(aa)
                # Raw BLOSUM score, default to 0 if not found
                scores.append(substitution_scores.get(aa, 0))

        if not possible_aas: continue

        # Convert scores to probabilities using Softmax
        # The 'temperature' here controls the sharpness of the distribution
        # High temp -> more random, Low temp -> more greedy towards best BLOSUM score
        scores_array = np.array(scores) / temperature
        probabilities = np.exp(scores_array) / np.sum(np.exp(scores_array))

        # Choose the new amino acid based on the probabilities
        new_aa = np.random.choice(possible_aas, p=probabilities)
        new_sequence[pos] = new_aa
        
    return "".join(new_sequence)

def parse_confidence_metrics(results_path: str, binder_chain_id: str) -> dict:
    metrics = {
        'iptm': 0.0, 'ptm': 0.0, 'complex_plddt': 0.0,
        'binder_avg_plddt': 0.0, 'plddts': []
    }
    try:
        json_path = next((os.path.join(results_path, f) for f in os.listdir(results_path) if f.startswith('confidence_') and f.endswith('.json')), None)
        if json_path:
            with open(json_path, 'r') as f: data = json.load(f)
            metrics.update({
                'ptm': data.get('ptm', 0.0),
                'complex_plddt': data.get('complex_plddt', 0.0)
            })
            pair_iptm = data.get('pair_chains_iptm', {})
            chain_ids = list(pair_iptm.keys())
            if len(chain_ids) > 1:
                c1, c2 = chain_ids[0], chain_ids[1]
                metrics['iptm'] = pair_iptm.get(c1, {}).get(c2, 0.0)
            if metrics['iptm'] == 0.0: metrics['iptm'] = data.get('iptm', 0.0)
    except Exception as e: print(f"Warning: Could not parse confidence metrics from {results_path}. Error: {e}")
    try:
        cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
        if cif_files:
            rank_1_cif = next((f for f in cif_files if 'rank_1' in f), cif_files[0])
            cif_path = os.path.join(results_path, rank_1_cif)
            with open(cif_path, 'r') as f: lines = f.readlines()
            header, atom_lines, in_loop = [], [], False
            for line in lines:
                s_line = line.strip()
                if s_line.startswith('_atom_site.'): header.append(s_line); in_loop = True
                elif in_loop and (s_line.startswith('ATOM') or s_line.startswith('HETATM')): atom_lines.append(s_line)
                elif in_loop and not s_line: in_loop = False
            if header and atom_lines:
                h_map = {name: i for i, name in enumerate(header)}
                chain_col = h_map.get('_atom_site.label_asym_id') or h_map.get('_atom_site.auth_asym_id')
                res_col, bfactor_col = h_map.get('_atom_site.label_seq_id'), h_map.get('_atom_site.B_iso_or_equiv')
                if all(c is not None for c in [chain_col, res_col, bfactor_col]):
                    plddts, last_res = [], None
                    for atom in atom_lines:
                        fields = atom.split()
                        if len(fields) > max(chain_col, res_col, bfactor_col) and fields[chain_col] == binder_chain_id and fields[res_col] != last_res:
                            plddts.append(float(fields[bfactor_col])); last_res = fields[res_col]
                    if plddts: metrics['plddts'] = plddts; metrics['binder_avg_plddt'] = np.mean(plddts)
    except Exception as e: print(f"Warning: Error parsing pLDDTs from CIF file. Error: {e}")
    return metrics