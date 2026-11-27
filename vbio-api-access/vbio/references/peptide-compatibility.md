# Peptide compatibility

First determine whether the user wants to predict an existing peptide, evaluate an existing pose, or generate new candidates. Use the matching project workflow. The installed client can prepare supported existing inputs for Prediction or Affinity. Direct new design requests to Peptide Designer in the web app. The Command Builder submits exactly the project's own workflow: a Prediction project shows no design selectors, and a Peptide Designer project always submits the design workflow.

## Design Strategy and Predict & Score

**Design Strategy** selects the engine that proposes candidates. **Predict & Score** selects the engine that evaluates them. The two roles have separate eligibility rules. A backend available for ordinary Prediction may be absent from either design selector.

The project UI filters each selector by active engine, workflow, role, chirality, and design mode. Those filters explain why switching chirality or topology can change the choices in both selectors. Treat the deployed project selectors and Submit configuration as the source for availability. An enabled choice alone does not establish support for a particular residue definition, constraint, or complete engine combination. If verification is needed, ask for the relevant non-secret settings or the names shown in the two selectors. Preserve the user's requested chemistry when explaining an unavailable combination.

## Existing peptide inputs

| Information | What to establish |
| --- | --- |
| Components | Determine the peptide chains and distinguish a short peptide, multiple chains, and multiple rings. |
| Topology | Use the original evidence for connectivity. The Builder's per-component Cyclic control and its residue modification editor are separate. Cyclic alone does not specify bicyclic connectivity or a linker. |
| Residue location | Identify the chain and terminal status or position. A structure residue label may differ from the one-based index in the submitted chain sequence. Resolve the mapping before editing. |
| Residue identity | Use the exact residue definition supplied by the user or established by the source structure. Preserve stereochemistry. Resolve an ambiguous name before assigning a chemical component. |

### Set a noncanonical residue

For a supported existing-molecule Prediction, map the request to the protein component's residue modification in the YAML Builder. Select the residue position and whether it is internal, N-terminal, or C-terminal. Then use the appropriate source.

- **Built-in CCD** uses the exact chemical component supported by the Builder. Verify that the selected code represents the residue and stereochemistry the user intends.
- **Custom SMILES** uses the user's existing residue definition. Verify that the Builder recognizes it as a residue and that the selected backend supports custom residue uploads. A Builder backbone check confirms only that aspect of the input.

If an exact built-in component is unavailable, offer the custom path for a supported existing input. Ask for the residue's existing stereochemically specified SMILES only if the definition is missing. If the user supplies an annotated structure instead, use it as evidence to identify the residue and verify its definition before choosing the input representation. V-Bio can build a custom component from an accepted definition for the prediction job. The custom path does not add an entry to the Built-in CCD selector.

For a verified configuration, preserve the original chain and ensure each intended modification appears at its mapped position in the generated input. For a custom residue, the YAML modification and the submitted custom definition must reference the same component identifier. When using a Builder export, verify the matching custom definition is present in the Submit configuration. Keep the draft pending if the definition or upload was omitted, or if the backend supports standard CCD modifications but cannot accept the custom representation. Affinity uses its own structure and ligand file contract.

### Guide the conversation

Accept a description in plain language and reuse the sequence, structure, and project information the user already supplied. Ask only what remains unresolved. For a residue change, establish which residue, where it goes, and the source of its chemical definition. Request the original annotated structure or exact residue definition if the name is ambiguous. For a method question, explain which requirement rules out an option and what can be checked next. A whole-workflow chirality label does not establish the stereochemistry of an individual modified residue.

Show only the requested topology, residue changes, selected method, and unresolved requirements in the draft table. Offer a relevant next action, such as supplying a missing definition or checking an available backend. Preserve the requested residue and topology unless the user explicitly chooses a replacement. Keep API fields and serialization details out of the normal reply.
