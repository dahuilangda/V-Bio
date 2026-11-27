# Mapping Command Builder to a job

The Skill generates the submission files. The client accepts one internal JSON manifest with `workflow`, `fields`, and `files`. Paths under `files` are relative to the manifest directory or absolute. Form names correspond to the Builder's `-F` entries. Put text, numbers, and booleans in `fields`. Put file paths in `files` without the Builder's leading `@`. For repeated `template_files`, use a JSON array. Encode JSON-valued form fields as JSON strings. The client supplies the authenticated project, route, and Prediction/Virtual Screening form workflow.

| Project workflow | Builder submission route | Inputs |
| --- | --- | --- |
| Prediction | `POST /predict` | `yaml_file` and optional `template_files` |
| Virtual Screening | `POST /predict` | Builder-exported `yaml_file` and optional `compounds_file` when the Builder is set to file library mode |
| Affinity | `POST /api/boltz2score` | `protein_file` and the fields and uploads selected for the Builder mode |

Use the selected Builder backend and priority. Match the Builder's MSA settings. For Prediction and Virtual Screening, a backend without MSA support sends `use_msa_server=false` and omits `msa_mode`. For an MSA-capable backend, the Builder sends the selected `msa_mode` and sets `use_msa_server=false` when the mode is `none`. Keep optional engine-specific flags tied to the settings shown in the Builder. For Virtual Screening, use one library source, either inline in Builder YAML or via `compounds_file`.

The current Builder fixes the Virtual Screening method to its screening backend. Use the deployed Builder's value and target configuration. Prediction methods are selected separately.

For Affinity, carry over `mode`, `compute_ipsae`, `use_msa_server` and `msa_mode` as shown in Submit. Use the Builder output as the source of any mode-specific values, chain selections, pocket inputs and scoring flags. Dock mode uses a ligand SMILES with exactly one pocket definition: center coordinates, residue list, or reference ligand file. Existing-pose modes use a ligand file. Docking and pose evaluation are distinct requests even when the same project exposes both. Keep the user-selected project workflow fixed throughout the request.

## Method selection

Use the requested method or verified project settings when they fit the task. Otherwise select a supported backend based on the workflow and input. For complexes, ligands, templates, or modified residues, check whether the candidate backend supports the exact inputs and options. For Virtual Screening and Affinity, use the project's screening or scoring configuration. Set sequence alignment and priority for the selected method and task. If a decision cannot be made from available information, show it as pending and ask only if it affects the next action.

## Prediction input

For a standard protein input, write a version 1 YAML document with a `sequences` list. Each protein entry contains a `protein` mapping with an `id` and `sequence`. Preserve sequence letters and use unique chain IDs. Use the user's original input or an existing valid Builder export when available.

For other molecule types, assemblies, or structural attachments, follow the deployed Builder's input contract and check which combinations the chosen method supports. Preserve biological features the user requested. If the available information cannot establish support or a faithful representation, leave the affected setting pending.

Use `prediction` for the manifest workflow and map the supported settings to `fields` and the generated YAML to `files.yaml_file`. Backend, sequence alignment, and priority depend on the task and deployment.
