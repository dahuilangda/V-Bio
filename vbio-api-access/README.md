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

## Worked examples

Invoke the `vbio` skill before sending these requests. Each starts with a draft or a capability check.

### Predict ubiquitin from a sequence

> Predict the structure of this ubiquitin sequence: `MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG`.

**Draft A · Ubiquitin structure prediction**

| Item | Details |
| --- | --- |
| Input | Human ubiquitin, one chain, 76 amino acids |
| Prediction method | Pending confirmation of the project's available methods |
| V-Bio project | Connection pending |
| Job status | Input prepared, awaiting submission |

**Next step**　[Connect a V-Bio project](#install-and-connect) to run the prediction. To choose a method first, ask which methods in the project are suitable for this sequence.

[RCSB PDB 1UBQ](https://www.rcsb.org/structure/1UBQ)

### Predict an existing head-to-tail cyclic peptide

> Predict the structure of the experimentally characterized cyclic hexapeptide cyclo-(Gly–L-Pro–Gly)₂. Use `GPGGPG` for chain A and preserve the head-to-tail peptide bond between Gly6 and Gly1. Check whether my Prediction project supports that closure before submitting.

The draft should identify chain A and the head-to-tail closure. It should say whether the selected backend can represent that topology or what still needs verification. [Published crystal structure](https://pubs.acs.org/doi/10.1021/ja00513a057)

### Preserve a built-in CCD residue

Download and attach the coordinate file for the selenomethionine-containing lysozyme structure. [1CX6 coordinate file](https://files.rcsb.org/download/1CX6.cif)

> Predict the structure of chain A from the attached `1CX6.cif`, preserving its selenomethionine residues as `MSE`.

The assistant should map the structure labels to sequence positions and check whether the selected backend accepts the built-in `MSE` component.

### Supply a custom residue as SMILES

> I have a linear peptide with sequence `AGKG`. In chain A, position 3 is L-ornithine. Preserve it in the structure prediction. If the Builder has no matching built-in entry, use this residue definition: `N[C@@H](CCCN)C(=O)O`.

The draft should show chain A, position 3, L-ornithine, and the selected representation. If the exact built-in entry is unavailable and the backend accepts custom residue uploads, the assistant can prepare a matching Custom CCD for the job. If support is unknown, it should say what needs checking. [Peptide compatibility](vbio/references/peptide-compatibility.md)

### Understand a Peptide Designer configuration

> In my Peptide Designer project, I selected D chirality and bicyclic topology. The options in Design Strategy and Predict & Score changed. Explain what each field controls and where I can see which options my project allows.

The assistant should explain that the two fields select the candidate generator and evaluator, respectively. Each selector filters by its role and the selected chemistry. If the deployed choices are unknown, the assistant can ask for the visible option names or a screenshot with credentials omitted. The project’s Peptide Designer page is where users review settings and submit design jobs. [Capability boundaries](vbio/references/capabilities.md)

## Validate the client

Run the local mock-gateway suite from the repository root.

```sh
python3 -m unittest discover -s vbio-api-access/tests
```
