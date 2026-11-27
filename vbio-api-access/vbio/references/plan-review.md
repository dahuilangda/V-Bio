# Plan review and switching

Use a separate draft for each alternative. Keep the source files intact and give the drafts short labels the user can refer to, such as A and B. Present the selected project, workflow, backend, input files, explicit settings and unresolved choices. If the user changes one setting, say what changed and preserve the rest of that draft.

For exact input requirements such as modified residues and existing cyclic topology, see [peptide compatibility](peptide-compatibility.md). Keep unsupported or unresolved settings visible in the draft.

1. Preview each supported variant without submitting. An offline preview can start the discussion. Before requesting submission approval, preview the selected manifest with the intended gateway and project configured.
2. A switch or preview request selects the draft. An explicit submission confirmation approves the selected preview. Approval applies to one selected preview and one submission. If the user edits the draft while confirming, show the edit and obtain approval for the revised submission.
3. Submit the reviewed manifest with `--approved-preview <preview_id>`. The ID covers the gateway, project, form fields, upload paths and file contents. A stale ID fails locally. Show what changed before asking again.
4. If a submission might have reached the gateway, use the [task follow-up guidance](task-follow-up.md) to check for the original job before retrying.

Changing the workflow requires verifying that the selected project supports it. Peptide Design uses the V-Bio web app.
