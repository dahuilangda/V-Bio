# /Boltz-WebUI/designer/design_utils.py

import random
import os
import json
import numpy as np

AMINO_ACIDS = "ARNDCQEGHILKMFPSTWYV"

def generate_random_sequence(length: int) -> str:
    # (This function remains unchanged)
    return "".join(random.choice(AMINO_ACIDS) for _ in range(length))

def mutate_sequence(sequence: str, mutation_rate: float = 0.1, plddt_scores: list = None) -> str:
    # (This function remains unchanged)
    new_sequence = list(sequence)
    num_mutations = int(len(sequence) * mutation_rate)
    if num_mutations == 0: num_mutations = 1

    positions_to_mutate = []
    
    if plddt_scores and len(plddt_scores) == len(sequence):
        weights = [100.0 - score for score in plddt_scores]
        total_weight = sum(weights)
        if total_weight > 0:
            probabilities = [w / total_weight for w in weights]
            population_indices = list(range(len(sequence)))
            k = min(num_mutations, len(population_indices))
            positions_to_mutate = np.random.choice(
                population_indices, 
                size=k, 
                replace=False, 
                p=probabilities
            )
        else:
            positions_to_mutate = random.sample(range(len(sequence)), k=num_mutations)
    else:
        if plddt_scores:
             print("Warning: pLDDT scores length mismatch. Falling back to random mutation.")
        positions_to_mutate = random.sample(range(len(sequence)), k=num_mutations)

    for pos in positions_to_mutate:
        original_aa = new_sequence[pos]
        possible_new_aas = [aa for aa in AMINO_ACIDS if aa != original_aa]
        if not possible_new_aas: continue
        new_aa = random.choice(possible_new_aas)
        new_sequence[pos] = new_aa
        
    return "".join(new_sequence)

def parse_confidence_metrics(results_path: str, binder_chain_id: str) -> dict:
    """
    Parses a richer set of confidence metrics from the result files.
    """
    metrics = {
        'iptm': 0.0,
        'ptm': 0.0,
        'complex_plddt': 0.0,
        'binder_avg_plddt': 0.0,
        'plddts': [] # Raw pLDDT list for mutation guidance
    }
    
    # 1. Parse metrics from confidence JSON file
    try:
        json_path = next((os.path.join(results_path, f) for f in os.listdir(results_path) if f.startswith('confidence_') and f.endswith('.json')), None)
        if json_path:
            with open(json_path, 'r') as f:
                data = json.load(f)
                # Get additional metrics
                metrics['ptm'] = data.get('ptm', 0.0)
                metrics['complex_plddt'] = data.get('complex_plddt', 0.0)

                # Get inter-chain ipTM
                pair_iptm = data.get('pair_chains_iptm', {})
                chain_ids = list(pair_iptm.keys())
                if len(chain_ids) > 1:
                    c1, c2 = chain_ids[0], chain_ids[1]
                    metrics['iptm'] = pair_iptm.get(c1, {}).get(c2, 0.0)
                if metrics['iptm'] == 0.0: # Fallback
                    metrics['iptm'] = data.get('iptm', 0.0)
    except Exception as e:
        print(f"Warning: Could not parse confidence metrics from {results_path}. Error: {e}")

    # 2. Parse per-residue pLDDTs from the CIF file
    cif_path = None
    try:
        all_cif_files = [f for f in os.listdir(results_path) if f.endswith('.cif')]
        if all_cif_files:
            rank_1_cif = next((f for f in all_cif_files if 'rank_1' in f), None)
            cif_path = os.path.join(results_path, rank_1_cif or all_cif_files[0])

        if cif_path:
            with open(cif_path, 'r') as f: lines = f.readlines()
            header, atom_lines, in_loop = [], [], False
            for line in lines:
                s_line = line.strip()
                if s_line.startswith('_atom_site.'):
                    header.append(s_line)
                    in_loop = True
                elif in_loop and (s_line.startswith('ATOM') or s_line.startswith('HETATM')):
                    atom_lines.append(s_line)
                elif in_loop and not s_line:
                    in_loop = False
            
            if header and atom_lines:
                h_map = {name: i for i, name in enumerate(header)}
                chain_col = h_map.get('_atom_site.label_asym_id') or h_map.get('_atom_site.auth_asym_id')
                res_col = h_map.get('_atom_site.label_seq_id')
                bfactor_col = h_map.get('_atom_site.B_iso_or_equiv')
                
                if all(c is not None for c in [chain_col, res_col, bfactor_col]):
                    plddts = []
                    last_res = None
                    for atom in atom_lines:
                        fields = atom.split()
                        if fields[chain_col] == binder_chain_id and fields[res_col] != last_res:
                            plddts.append(float(fields[bfactor_col]))
                            last_res = fields[res_col]
                    metrics['plddts'] = plddts
                    if plddts:
                        metrics['binder_avg_plddt'] = np.mean(plddts)

        if not metrics['plddts']:
            if cif_path: print(f"Warning: Found CIF '{os.path.basename(cif_path)}' but couldn't get pLDDTs for chain '{binder_chain_id}'.")
            else: print(f"Warning: No .cif file found in '{results_path}'.")

    except Exception as e:
        print(f"Warning: Error parsing pLDDTs from CIF file in {results_path}. Error: {e}")
        
    return metrics