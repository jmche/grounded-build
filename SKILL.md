---
name: grounded-build
description: Produce and audit repository-grounded implementation plans with isolated planning instances, cross-review, deterministic workflow state, and optional reviewed implementation. Use only when the user explicitly requests grounded-build. For implementing an unrelated existing plan, prefer implement-plan-with-review.
metadata:
  version: 0.2.0
  compatibility: Linux, Git, Python 3.11+, bubblewrap, and at least one Claude or Codex CLI adapter
---

# Grounded Build

Create a repository-evidenced plan through isolated agent calls, then optionally implement the approved plan in reviewed batches. The host may be GPT-, DeepSeek-, Claude-, or another tool-capable model: workflow state, not host memory, determines the next action.

## Route the request

- **Plan:** the user has an objective, draft, or plan that needs independent investigation or audit.
- **Implement:** the user has an approved plan and wants it executed. Read [references/implementation_workflow.md](references/implementation_workflow.md) completely before acting.
- **End to end:** complete Plan, report its reviewed artifacts, and stop. Initialize Implement only after explicit user approval.
- Never open, copy, or mutate a run owned by `implement-plan-with-review`.

## Non-negotiable boundaries

- Work from the absolute Git repository root and freeze its exact SHA.
- Planning requires a clean checkout. Never stash, reset, clean, or commit user work to satisfy this.
- Preserve the original branch, index, checkout, and files until explicitly approved final integration.
- A CLI adapter is not a model identity. Record `agent_runtime`, `provider_diversity`, and `model_diversity` exactly as reported. Codex may run GPT, DeepSeek, or another configured model.
- Agreement is not evidence. Bind claims to files, symbols, tests, commands, or authoritative sources.
- Infrastructure failure, malformed output, timeout, or quota exhaustion is not a quality FAIL.
- Stop at every typed user decision and at implementation approval. Never infer authority from plan approval.

## Plan workflow

Use `scripts/plan_workflow.py`. Detailed state and failure semantics are in [references/planning_workflow.md](references/planning_workflow.md); read that reference when diagnosing, adjudicating, resuming, or recovering a run.

### 1. Preflight

```bash
python3 <skill-root>/scripts/plan_workflow.py preflight \
  --project <absolute-project> --backend <auto|mixed|claude|codex>
```

Use `--probe` when provider authentication/configuration is uncertain; it makes one small paid, sandboxed schema call per selected adapter.

Codex uses the user's default configuration unless explicitly selected:

```bash
--codex-model <model> \
--codex-model-provider <configured-provider> \
--codex-profile <profile>
```

These options support both normal GPT-backed Codex and Codex configured for an OpenAI-compatible DeepSeek gateway. Pass the same selection to `init`; the run freezes only non-secret identity fields. Never paste credentials or endpoint tokens into arguments.

### 2. Freeze request and initialize

Write the objective, constraints, exclusions, and known decisions to a local Markdown request file. Use `final-reviewer=both` by default for independently verified planning; use one adapter only when requested or when cost/availability requires it.

```bash
python3 <skill-root>/scripts/plan_workflow.py init \
  --project <project> --request <request.md> \
  --backend <backend> --final-reviewer <both|claude|codex> \
  [Codex selection options from preflight]
```

Record `run_id`, `agent_runtime`, and the returned `next_action`.

### 3. Follow the deterministic next action

```bash
python3 <skill-root>/scripts/plan_workflow.py next \
  --project <project> --run-id <run-id>
```

- For `RUN_AGENT`, execute `preview_command`, inspect it, then execute `command`.
- For `HOST_SYNTHESIS`, run its command and read every returned artifact. Write `implementation_plan.md` and `batches.md` in the returned output directory. Resolve disagreement by evidence and preserve unresolved product choices.
- For `ASK_USER`, present the evidence and allowed typed decision; preview the corresponding adjudication before `--apply`.
- For `STOP_FOR_IMPLEMENTATION_APPROVAL`, export and stop.

Repeat `next` after each completed action. Do not guess a slot, reviewer, phase, or retry.

Before submitting synthesis, run:

```bash
python3 <skill-root>/scripts/plan_workflow.py check-synthesis \
  --plan <implementation_plan.md> --batch-manifest <batches.md>
```

Fix errors and consider every warning. Each `Bxx:` block should explicitly label `Exit observation:` and `Verification:`. Then submit:

```bash
python3 <skill-root>/scripts/plan_workflow.py submit-synthesis \
  --project <project> --run-id <run-id> \
  --plan <implementation_plan.md> --batch-manifest <batches.md>
```

### 4. Export and stop

At `READY`:

```bash
python3 <skill-root>/scripts/plan_workflow.py export --project <project> --run-id <run-id>
```

Report baseline SHA, adapter/model identities, both diversity fields, limitations, disagreements, review verdicts, digests, and artifact paths. Do not implement until the user explicitly approves. Cleanup is preview-first and permitted only for terminal planning runs.

## Implement workflow

Implementation state lives separately under `~/.grounded-build/implementation/`. For a Plan handoff, initialize with the exported plan and batch manifest. For a user-provided plan without a manifest, derive a finite, observable batch manifest and obtain confirmation before freezing it.

Codex reviewer selection accepts the same `--codex-model`, `--codex-model-provider`, and `--codex-profile` options at `init` and `change-reviewer`. The frozen `reviewer_runtime` must be reported and preserved across review calls.

Follow [references/implementation_workflow.md](references/implementation_workflow.md) exactly. Never treat the current host conversation as its own independent reviewer.

## Resume

Query both mode-specific status commands only when the run's owner is unknown. Resume from the reported state and `next_action`; never copy state between modes, rewrite frozen artifacts, reset budgets, or initialize a replacement silently.
