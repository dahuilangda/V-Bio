# Start with CIF files and prepare your own training data.

[finetune.md](finetune.md) runs this together with MSA search and
fine-tuning, end to end; this page details the preprocessing step and its
output formats.

## Data Preparation

1. **Prepare CIF Files**: Place the CIF files you want to convert into training data in a folder. Alternatively, you can use a `txt` file to record the paths to these CIF files, with each line corresponding to the path of a specific CIF file.

2. **Protein Clustering File (Optional)**: The clustering file assigns each `[PDB ID]_[Entity ID]` to a cluster of protein sequences at 40% sequence identity, and training samples are weighted by cluster. The download script places the RCSB PDB clustering at `$MOL_ROOT_DIR/common/clusters-by-entity-40.txt`; pass it with `-c`.

    Clusters are looked up by PDB ID and entity ID, so they apply only to real PDB entries. Every protein chain of 10 or more residues that is not found — including all chains of your own structures, and all chains when `-c` is not given — gets the cluster ID `NotInClusterTxt`, and these chains share one cluster's weight. Shorter chains are clustered by their sequence. To cluster your own structures, write a text file in the same format: one cluster per line, with `[PDB ID]_[Entity ID]` entries separated by spaces.

3. **Update the CCD (Chemical Component Dictionary) Cache File (If needed)**: We provide a pre-processed file that records the reference conformers for each CCD Code. It has a cutoff date, and a CCD Code released after it is missing from the cache. If your training data uses such a code, run the following script to download and update the CCD CIF files:

    ```bash
    python3 scripts/gen_ccd_cache.py -c [ccd_cache_dir] -n [num_cpu]
    ```

    After running the script, three files will be generated in the specified "ccd_cache_dir":
    
    - `components.cif` (CCD CIF file downloaded from RCSB)
    - `components.cif.rdkit_mol.pkl` (pre-processed dictionary, where the key is the CCD Code and the value is an RDKit Mol object with 3D structure)
    - `components.txt` (a list containing all the CCD Codes)

    Mol-1 reads the cache from `$MOL_ROOT_DIR/common/components.cif` and `$MOL_ROOT_DIR/common/components.cif.rdkit_mol.pkl`; pass `-c $MOL_ROOT_DIR/common`, or copy the generated files there.
    Notes:
    - The `-c` parameter is optional. If not specified, files will be saved in the "release_data/ccd_cache" folder within the Mol-1 code directory by default.
    - You can add the `-d` parameter when running the script to skip the CIF file download step, in which case the script will directly process the "components.cif" file located in the "ccd_cache_dir".

## Data Preprocessing
Execute the script to preprocess the data:
```bash
python3 scripts/prepare_training_data.py -i [input_path] -o [output_csv] -b [output_dir] -c [cluster_txt] -n [num_cpu]
```

The preprocessed structures will be saved as `.pkl.gz` files. Additionally, a `CSV` file will be generated to catalog the chains and interfaces within these structures, which will facilitate sampling during the training process.

You can view the explanation of the parameters by using the `--help` command.
```
python3 scripts/prepare_training_data.py --help
```

Note that there is an optional parameter `-d` in the script. Without it, the script treats each CIF file as a PDB entry: it skips entries whose assembly has more than 1000 polymer chains, expands the structure to assembly 1, and applies the training-set filters of AlphaFold 3, in this order:

- Removing water molecules and hydrogen atoms
- Removing polymer chains made entirely of unknown residues
- Removing polymer chains in which the Cα atoms of two consecutive residues are more than 10 Å apart
- Removing ligand atoms that are absent from the file, and unresolved leaving atoms
- Removing atoms of element X
- Removing chains with no resolved residues
- For structures solved by crystallography, removing crystallization aids
- Removing chains that clash: a chain is removed when more than 30% of its resolved token-centre atoms lie within 1.6 Å of another chain
- When more than 20 chains remain, keeping the 20 chains nearest to an interface atom chosen at random with a fixed seed. A ligand covalently bonded to a polymer counts as part of that chain.
- Keeping only the largest group of chains connected through interfaces, so that chains not in contact with it are dropped

For CIF files generated through model inference where these filters aren't wanted, run the script with `-d`. The structure is then kept as given, without expansion to assembly 1, and only water, hydrogens and element X are removed.


## Output Format
### Bioassembly Dict
In the folder specified by the `-b` parameter of the data preprocessing script, a corresponding `[pdb_id].pkl.gz` file is generated for each successfully processed CIF file. This file is a gzip-compressed pickle of a dictionary with the following contents:
```
| Key                        | Value Type    | Description                                                                   |
|----------------------------|---------------|-------------------------------------------------------------------------------|
| pdb_id                     | str           | PDB Code                                                                      |
| assembly_id                | str           | Assembly ID                                                                   |
| sequences                  | dict[str, str]| Key is polymer's label_entity_id, value is canonical_sequence                 |
| release_date               | str           | PDB's Release Date                                                            |
| num_assembly_polymer_chains| int           | Number of assembly polymer chains (pdbx_struct_assembly.oligomeric_count)     |
| num_prot_chains            | int           | Number of protein chains in AtomArray                                         |
| entity_poly_type           | dict[str, str]| Key is polymer's label_entity_id, value is corresponding to entity_poly.type  |
| resolution                 | float         | Resolution; if no resolution, value is -1                                     |
| num_tokens                 | int           | Number of tokens                                                              |
| atom_array                 | AtomArray     | AtomArray from structure processing                                           |
| token_array                | TokenArray    | TokenArray generated based on AtomArray                                       |
| msa_features               | None          | (Placeholder)                                                                 |
| template_features          | None          | (Placeholder)                                                                 |
```
Further keys (`stat_log`, `is_dissociated`, `resolved_atom_num`, `resolved_atom_num_in_assembly1`, `entity_pair_to_chain_pairs`, `no_polymer_remain`) record what the filters did.

### Indices CSV
After the script successfully completes, the CSV file named by `-o` is written. 
Each row contains information about a pre-processed chain or interface, and the content of each column is described as follows: 
```
| Column Name    | Value Type | Meaning                                                                | Required |
|----------------|------------|------------------------------------------------------------------------|----------|
| type           | str        | "chain" or "interface"                                                 | Y        |
| pdb_id         | str        | PDB Code (entry.id)                                                    | Y        |
| cluster_id     | str        | Cluster ID of the chain; for an interface, the two chains' cluster IDs, sorted and joined by ":" | Y        |
| release_date   | str        | Release date                                                           | N        |
| resolution     | float      | Resolution; if no resolution, value is -1                              | N        |
| num_tokens     | int        | Number of tokens in AtomArray of Bioassembly Dict                      | N        |
| num_prot_chains| int        | Number of protein chains in AtomArray of Bioassembly Dict              | N        |
| eval_type      | str        | Classification used for evaluation                                     | N        |
| entity_1_id    | str        | Chain 1's label_entity_id                                              | Y        |
| chain_1_id     | str        | Chain 1's chain ID                                                     | Y        |
| mol_1_type     | str        | Chain 1's mol_type: "prot", "nuc" (DNA or RNA) or "ligand" (including ions) | Y        |
| sub_mol_1_type | str        | Sub-classification of Chain 1's entity corresponding to mol_type       | N        |
| cluster_1_id   | str        | Chain 1's cluster ID                                                   | Y        |
| entity_2_id    | str        | Chain 2's label_entity_id                                              | Y        |
| chain_2_id     | str        | Chain 2's chain ID                                                     | Y        |
| mol_2_type     | str        | Chain 2's mol_type, as for mol_1_type                                  | Y        |
| sub_mol_2_type | str        | Sub-classification of Chain 2's entity corresponding to mol_type       | N        |
| cluster_2_id   | str        | Chain 2's cluster_id                                                   | Y        |
| mol_type_group | str        | "intra_<mol_type>" for a chain; the two mol_types, sorted and joined by "_", for an interface | N        |
```
Notes: 
- In the table, columns marked with 'Y' under 'Required' indicate that these columns are essential for training. If you are creating your own CSV for training purposes, these columns must be included. Columns marked with 'N' are optional and can be excluded. 
- For rows where the "type" is "chain", the values in columns related to Chain 2 should all be filled with empty strings.
- The sampling weights are keyed by `mol_type`, so a hand-made CSV must use exactly "prot", "nuc" and "ligand"; any other value gets no weight.
