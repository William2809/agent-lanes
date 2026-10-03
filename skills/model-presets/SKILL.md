---
name: model-presets
description: View or change which AI model, reasoning effort and sandbox each worker preset uses (build, build-hard, review, ...). Use when a new frontier model ships, when the user asks to switch, compare or add a worker model, or says "update the presets", "which models do we use", or names a new model.
---

Model names live in one place: presets. Tools ask for a preset (`wk -m build-hard`, pipeline roles), never a model.

- Defaults: `presets/models.conf` in this repo (the recommended set; roles `build`, `build-light`, `build-hard`, `build-max`, `review`, `review-light`).
- Personal override: `~/.config/agent-lanes/models.conf`, same format, never committed. A later line with the same name wins, and extra names (e.g. short aliases) are allowed.
- Format: `name  model  effort  sandbox(max)  route`. `sandbox(max)` is the most any worker on that model may use. A model whose presets are all `read-only` can only review. `route` is `codex`, or a Codex profile name (e.g. a proxy for other providers).
- `ao-model ls` shows the effective presets; `ao-model get NAME` shows one; `ao-model check MODEL EFFORT SANDBOX` is what run-worker uses to refuse disallowed launches.

## When a new model ships

1. **Evidence first.** Read the release notes and benchmarks from the current year only (agentic tooling ages fast). Note the price per token and any limits on your plan.
2. **Trial it as an extra preset:** add a line to the personal override (e.g. `build-next  <new-model>  medium  full-access  codex`). Defaults stay unchanged.
3. **Compare on real work:** run 3–5 bounded tasks of the same kind on the old and the new preset. Count accepted changes, bugs found after "done", rework and tokens for the whole chain (build + review + fixes), not one run.
4. **Switch the role:** point `build` (or `review`) at the new model in the override. When it holds up for a week, update `presets/models.conf` and log the change in `docs/workflow-log.md` with the evidence.
5. **Smoke test:** `echo "Reply with exactly: OK" | wk zzsmoke -m <preset>`, then `batches wait zzsmoke` and check that the log header shows the expected model, effort and sandbox.

Keep the reviewer on a different model or provider from the builder when you can: different models make different mistakes, so a review by the same model finds less. Different review questions help just as much.
