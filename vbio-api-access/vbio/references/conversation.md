# Draft interaction

Reply in the user's language. Use the information already given. Accept original sequences or files and generate submission artifacts internally. A project link supplies context. Verify the intended project and workflow before submission.

Keep drafts in the workspace under `.vbio/drafts/` with stable labels. Reuse the selected draft across turns. An edit updates only the requested parts. A comparison or copy keeps the original draft. Recover from the manifest and last shown preview when possible. Clarify which draft the user means only when the choice is ambiguous.

## Show the draft

Use a compact Markdown table with the task, input summary, selected or provisional method, V-Bio project, and submission status. Compare alternatives side by side. Add details that affect the user's decision, including unresolved input or capability questions. Omit raw sequences, API routes, form keys, and internal paths unless requested.

## Guide the next step

- If input is missing, name the specific material needed and give a supported substitute when available.
- If the method is unresolved, explain the decision that affects the task. Use a known project setting when suitable.
- If the project is disconnected, show the missing [connection settings](connection.md). Keep offline editing available.
- If the draft is ready, tell the user how to approve the selected draft.
- After an edit or copy, summarize the change and give the next action for that revision. An already submitted job retains its task ID. A requested change to that job's inputs starts a new draft.

Offer only the edits relevant to this request. Use the actual draft and input when suggesting a replacement or comparison. For Prediction, editable choices can include input and compatible methods. For Virtual Screening, they can include the compound library or a subset. For Affinity, they can include the ligand or pose where supported by the selected mode.
