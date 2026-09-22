# PeptideLM

Language-model peptide design for V-Bio: a residue-monomer causal prior
(Tier 1) driving a target-conditioned closed design loop (Tier 2). The
package lives at `capabilities/peptide_lm`; add that directory to
`sys.path` (or `PYTHONPATH`) — production does the former.

```bash
python -c "import peplm; print(peplm.__version__)"        # 10.0.0
python -m pytest tests/ -q                                # unit tests
```

## Architecture overview

```
                 ┌────────────────────────────────────────────────┐
   Tier 1        │ ModernPrior (Llama-style causal LM, 87.5M)     │
   residue prior │  RoPE / SwiGLU / RMSNorm, d768 / 12L / 12H     │
                 │  + property aux heads (sol/syn/liability)      │
                 │  + additive per-residue SS3 track (ss_track)   │
                 └──────┬─────────────────────────┬───────────────┘
                        │ de novo sampling        │ FIM span edits + point moves
                        ▼                         ▼
                 ┌────────────────────────────────────────────────┐
   Tier 2        │ closed loop: propose → Boltz-2/Protenix oracle │
   design loop   │ → reward → GRPO (KL-anchored to frozen prior)  │
                 │ → elites → next generation                     │
                 └────────────────────────────────────────────────┘
                        │ receptor-conditioned side channel
                        ▼
                 ┌────────────────────────────────────────────────┐
   PepMLM-650M   │ receptor-conditioned generation + pseudo-PPL   │
                 │ + elite refinement (mask & re-infill)          │
                 └────────────────────────────────────────────────┘
```

Key design points:

* **Residue-monomer language.** A peptide is `AC[AIB]GK`: single letters
  are natural amino acids, bracketed tokens are non-natural monomers
  carrying CCD/SMILES metadata (`peplm/residues.py`), so candidates drop
  straight into the Boltz YAML `modifications` protocol.
* **Conditioning is lexical + additive.** Property tags, length buckets
  and modality tokens (`<lin>/<cyc>/<bicy>`) ride the prompt; the SS3
  profile rides a per-position embedding track summed onto the residue
  stream (id 0 = free position — partial specification is native).
* **Constraints are decoder guarantees.** Fixed residues, Cys anchors,
  NCAA pool legality and length bounds are enforced on the logits during
  decoding (`peplm/loop/constraints.py`), not repaired post-hoc.
* **One prior, three modalities.** Linear, cyclic and bicyclic design
  share the checkpoint; the modality token selects the distribution.

## Checkpoints

| Artifact | Role |
|---|---|
| `models/external/pepmlm650/` | PepMLM-650M (ChatterjeeLab, Nat Biotech 2025): the receptor-conditioned prior — one-shot parallel decoding from the all-mask tail, pseudo-PPL ranking, elite refinement. Required whenever a target receptor is supplied; no substitute/fallback exists. |
| `models/ss_track.pt` | Tier-1 prior with the additive SS track — used when the caller supplies an SS profile (`peptideSSProfile`) |
| `models/prior.pt` | Base Tier-1 prior (no SS track) — property-tag conditioned generation, FIM editing, GRPO policy anchor |

Checkpoint lineage and training mixtures: `models/MANIFEST.json`.

SS-track alignment: position *i* predicts token *i+1*, so position *i*'s
embedding carries the SS label of token *i+1* (left shift). This alignment
is the only supported one; it is shared by sampling
(`llama_prior.sample_with_prompt`), scoring (`_token_logprobs`) and GRPO
training (`generate/grpo.py`) — train/inference parity is a hard
requirement, a prefix/track mismatch mis-attributes what the policy
learned.

## Training pipeline

```bash
cd capabilities/peptide_lm

# 1) Tier-1 corpus (UniRef90 chopped + PDB binders + PeptideGPT; FIM 50%,
#    modality + NCAA augmentation) — see module docstring for sources
python -m peplm.data.build_corpus --n_segments 12000000 --out_dir runs/data

# 2) Tier-1 pretraining (modern architecture)
python scripts/train_tier1.py --data runs/data --out models --arch modern \
    --device cuda:0 --epochs 2 --ncaa_aug 0.10

# 3) SS-conditioned fine-tune: warm-start from the Tier-1 prior with the
#    ss_track left-shift objective (see models/MANIFEST.json for the
#    training mixture).

# 4) receptor-conditioned prior: the official PepMLM-650M needs no
#    training — place its HF files in models/external/pepmlm650/.
```

## Inference API

```python
import sys; sys.path.insert(0, "/data/V-Bio/capabilities/peptide_lm")
from peplm.models.train import load_prior

model, vocab = load_prior("models/prior.pt", device="cpu")

# conditional sampling under a decode-time constraint plan
tokens = model.sample_with_prompt(
    ["<sol_h>", "<syn_h>", "<liab_h>", "<L15>", "<lin>"],  # prompt
    n=8, device="cpu", temperature=0.85, top_p=0.95,
    target_len=15,            # min/max length as decoder guarantees
    return_tokens=True,
    ss_track=[1, 1, 1, 2, 2, 2, 3, 3, 3, 0, 0, 0, 0, 0, 0],  # H/E/L ids, 0 = free
)

# per-token log-probs under the same conditioning (ranking / GRPO)
logprobs = model._token_logprobs(ids, ss_ids=track)

# runtime vocabulary extension (user-defined NCAAs)
model.extend_vocab(["[XYZ]"])
```

Higher-level entry points:

* `peplm/loop/engine.py::PeptideLoop` — standalone closed loop (oracle
  backends: Boltz-2 local, Protenix docker, mock).
* `peplm/integrate/backend_proposer.py::BackendProposer` — the production
  proposal engine (below).
* `scripts/run_closed_loop.py`, `scripts/run_benchmark.py` — CLI wrappers.

## Design loop integration

`BackendProposer` is the only peptide design algorithm wired into the V-Bio
backend (`backend/runtime/run_single_prediction.py`); it replaces the old
random-init + GA mutation step and keeps everything else (Celery dispatch,
Boltz scoring, NSGA-II elite selection, progress reporting) untouched.

Per generation:

1. **propose** — `propose(natural_pool, unnatural_pool, elite_rows, n)`
   returns `(base_sequence, modifications, cys_anchors, proposal_group)`:
   generation 0 is pure prior sampling; later generations mix a de novo
   quota (adaptive to reward variance) with FIM edits of the elites and
   NCAA point moves. Operators live in `peplm/generate/proposals.py`;
   the GRPO grouping key rides every proposal.
2. **rank** — `pseudo_perplexity(sequence)` ranks oversamples under the
   same conditioned prior before any GPU oracle call.
3. **learn** — `learn(elite_rows, all_rows)` runs one GRPO update from the
   generation's scored rows (group-relative advantage, KL anchor to the
   frozen prior, gate-rejected rows enter with a floor-scaled reward).

Failure policy: no fallback. A missing prior, an untrained SS track or a
learn failure is raised to the production task.

## Layout

```
peplm/
├── vocab.py residues.py candidate.py   # residue language + candidate record
├── models/    llama_prior (current), gpt2 (deprecated), train* (training loops)
├── generate/  proposals (loop operators), edit (FIM/point moves), grpo
├── loop/      engine, constraints (decode-time plans), reward gating
├── score/     production composite, surrogate, learned property heads
├── oracle/    Boltz-2 / Protenix backends + interface physics
├── integrate/ backend_proposer (production entry point)
└── dpeptide/  D-peptide mirror/docking/scoring pipeline
```
