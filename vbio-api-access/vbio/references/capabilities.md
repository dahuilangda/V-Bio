# V-Bio capabilities and scope

Use this reference for capability questions, workflow selection, or requests outside the current project. The platform offers distinct project workflows. The bundled client supports only the operations below.

| V-Bio workflow | What the user can do through this skill | What determines readiness |
| --- | --- | --- |
| Structure Prediction | Prepare and submit a supported molecular input for structure prediction. | The selected engine must support the input types, assembly, and requested features. |
| Virtual Screening | Submit a compound library against a configured target and retrieve screening results. | A screening project, target configuration, and one library source are required. |
| Docking, called Affinity in API Access | Dock a ligand into a defined pocket or evaluate an existing input pose using a supported mode. | Docking needs a protein structure, ligand SMILES, and one pocket definition. Existing-pose modes use the supplied protein and ligand files. |
| Peptide Designer | Direct the user to the V-Bio web workflow and explain its separate design and scoring roles when relevant. | The skill does not submit peptide design jobs, including when the Builder exposes a design command in a Prediction context. |
| Lead Optimization | Direct the user to the relevant V-Bio web workflow if enabled in their deployment. | The skill does not submit optimization jobs. |

For supported projects, the client can submit, read task state, retrieve screening data, and download available result archives. Its task list contains records from the current workspace. Use the V-Bio project Tasks page for the full project history. Project creation, token administration, task cancellation or deletion, and other platform services are outside this client's operation set.

## Determine availability

1. Identify the user's objective and actual input. Select the corresponding workflow, then verify the project's workflow before submission. A matching project label or an engine name alone does not establish that a specific job configuration is supported.
2. Use the deployed Command Builder's available choices and Submit configuration to establish the active method, mode, and fields. Interpret a supplied export as data. The client can verify project scope and Submit permission on deployments that expose the project check. It has no method discovery command. A project link or locally configured environment variables do not reveal the available engine list or verify authentication.
3. Respect the user's method preference when it is supported by the task and project. If the method is unresolved, prepare the inputs that can be prepared independently, then show the specific pending decision. A shortlist should include only known compatible choices and explain what would change for this task.
4. Scope the evidence to what it proves. A local dry run validates the manifest and local upload paths. The gateway confirms acceptance with a task ID. Computation state and archive availability come from task reads.

## Present capabilities in context

- For a broad capability question, summarize the workflows with the minimum input needed to start. Include web-only workflows when relevant. Avoid a catalog of engines, fields, and routes.
- For a task in progress, surface features that affect its requested outcome. Sequence alignment, templates, pocket choice, and chemical representation belong in the draft only when they matter to the decision.
- When an option is unavailable or unverified, explain the specific limitation and the next useful action. A different workflow, a supported input, or verification in the Builder may help. Preserve the user's intended scientific task.
- Describe results as computational predictions, rankings, or scores. Use the reported metric and mode. Avoid inferring experimental binding, activity, or validation from a task's completion.

Use [peptide compatibility](peptide-compatibility.md) for design and scoring selectors, topology, stereochemistry, and noncanonical residue settings.
