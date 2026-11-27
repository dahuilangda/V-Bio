# V-Bio API Access Skill

[简体中文](README_zh.md)

Prepare, review, submit, and track V-Bio jobs from a workspace on your own machine or server.

| Workflow | Skill capabilities |
| --- | --- |
| Prediction | Prepare supported biomolecular inputs, submit structure prediction, and download results. |
| Virtual Screening | Submit a compound library against the configured target and retrieve screening results. |
| Docking / Affinity | Submit a protein and ligand inputs for the project's supported scoring mode and download results. |

The deployed Command Builder determines available methods and supported inputs. Peptide Designer and Lead Optimization are web workflows outside this client’s submission scope. Modified residues, cyclic topology, and stereochemistry require method-specific checks. [Capabilities and boundaries](vbio/references/capabilities.md) · [Input mapping](vbio/references/command-builder.md) · [Peptide compatibility](vbio/references/peptide-compatibility.md)

## Install and connect

Copy the full [`vbio/`](vbio/) directory into the assistant's skill directory on the machine running it. For workspace installs, use `.agents/skills/vbio/` in Codex or `.claude/skills/vbio/` in Claude Code. Select `vbio` from the client's skill list. The bundled client requires Python 3.8 or later and curl. Authenticated operations require network access to the V-Bio API Access gateway.

In V-Bio, open **API → Command Builder → Environment** to obtain the gateway URL and project ID. Configure `VBIO_API_BASE`, `VBIO_PROJECT_ID`, and `VBIO_API_TOKEN` on the machine running the assistant. Start the assistant from that environment. The token must be scoped to the intended project and have Submit permission for new jobs. [Connection setup](vbio/references/connection.md)

## Work with a draft

Provide the research input and desired workflow. The assistant prepares a local draft, summarizes the choices, and explains what you can change or what is needed to submit. Local previews can run offline. Submitted jobs return a task ID. Use the same workspace to recover task records and download paths across sessions. [Draft interaction](vbio/references/conversation.md) · [Task follow-up](vbio/references/task-follow-up.md)

## Review workspace statistics

The bundled client summarizes every task recorded in the current workspace as a terminal report: state counts, workflow and method success rates, durations of successful jobs, submission activity, and recurring failure reasons. Run `stats` from the workspace; add `--refresh` to re-read non-terminal tasks first. [Task follow-up](vbio/references/task-follow-up.md)

## Validate the client

Run the local mock-gateway suite from the repository root.

```sh
python3 -m unittest discover -s vbio-api-access/tests
```
