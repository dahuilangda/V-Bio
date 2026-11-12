## What this changes

<!-- One or two sentences. What behaviour is different after this PR? -->

## Why

<!-- The problem being solved. Link the issue if there is one. -->

## How it was verified

<!-- Commands you ran and what they printed. "Tests pass" on its own is not
     enough — say which, and on what hardware. -->

- [ ] `pytest tests/` passes
- [ ] Ran on GPU, or explicitly not applicable
- [ ] Documentation updated, or explicitly not applicable

## Numerical impact

Does this change inference outputs for an unchanged input and checkpoint?

- [ ] No — structural, documentation, tooling or test-only
- [ ] Yes — and the change is described above, with before/after values

<!-- Anything touching the model, kernels, chunking or caching can move
     numerics. If you are unsure, assume yes and measure. -->
