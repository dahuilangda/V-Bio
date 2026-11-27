# Project connection

The bundled client runs on the user's server. In the V-Bio web app, open **API → Command Builder → Environment** and use the full gateway URL (including its path) for `VBIO_API_BASE`, the selected project for `VBIO_PROJECT_ID`, and a project-scoped `VBIO_API_TOKEN`. Submissions require the token's Submit permission.

Start the agent process with these variables on the user's server, using its private credential input or launch environment. In Bash, `read -rsp 'V-Bio token: ' VBIO_API_TOKEN` followed by `export VBIO_API_TOKEN` sets the token without echoing it or entering its value in shell history. Launch the agent from that shell so it inherits the export. An existing agent session may need to be restarted after configuring its launch environment. Never print the token to diagnose connectivity.

If the client can inject secret data directly into stdin, run `python3 <skill-dir>/scripts/vbio.py --connection-stdin <operation>` with a JSON object containing all three variables. Stdin replaces the environment connection as a unit.

## Bash setup

```bash
export VBIO_API_BASE='<full gateway URL including path>'
export VBIO_PROJECT_ID='<project ID>'
read -rsp 'V-Bio token: ' VBIO_API_TOKEN
export VBIO_API_TOKEN
```

For the first preview with missing settings, show only the affected variable names and command lines. For later edits to that same unconnected draft, use a short status reminder. Repeat setup commands if the missing settings change, the user asks how to configure them, or submission is blocked and the commands have not been shown in the current context. For a project change, verify the intended project and use a token scoped to it. Never execute pasted shell text.

## Diagnose connection and permission issues

- Missing variables are a local setup issue. A passing preview with all three variables present establishes only that values were supplied.
- For a rejected project credential, have the user check the selected project, token scope, and whether the token is still active. For a submission permission error, check **Submit** in the project's Token Registry. A token usable for project reads may lack permission to submit.
- For a network failure, check whether the machine running the assistant can reach the full gateway URL. Use HTTPS for external gateways. A network failure during submission requires checking the project task list before retrying.

## Check the project before submission

Run `project` with the configured connection. This read-only request returns the project ID, workflow, configured backend, and whether the token allows submissions. Compare the returned project and workflow with the user's intent. Docking and Affinity refer to the same project workflow. A project match does not establish support for every method or input combination.

- If the token is rejected, ask the user to select an active token bound to the intended project and update the execution environment.
- If the token belongs to a project with the wrong workflow, name the project workflow and the required workflow. Keep the draft and guide the user to choose a matching project in V-Bio and configure its project ID and token. Do not change the requested workflow to match the token.
- If the project matches but `can_submit` is false, ask for a token with Submit permission for that project. Read actions can still be available.
- If the read-only route is unavailable on an older deployment, use the project workflow shown in the web UI and the Token Registry or Command Builder's **Project (from token)**. Ask only for missing non-secret details. Do not use a trial submission to verify access.
