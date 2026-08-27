---
name: grounded-build
description: Produce and audit repository-grounded implementation plans with isolated planning instances, cross-review, deterministic workflow state, and optional reviewed implementation. Use only when the user explicitly requests grounded-build. For implementing an unrelated existing plan, prefer implement-plan-with-review.
metadata:
  version: 0.4.5
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
- A CLI adapter is not a model identity. Record `agent_runtime`, `provider_diversity`, and `model_diversity` exactly as reported. Every run freezes the workflow engine, model selection, and invocation argv; reject silent engine drift.
- Agreement is not evidence. Bind claims to files, symbols, tests, commands, or authoritative sources.
- Freeze scope before investigation. Divergence may widen evidence, causal analysis, failure-path coverage,
  alternatives, and verification, but never the user-authorized objective.
- Order work by scope gate, user priority, severity, urgency, blockers/dependencies, causal leverage,
  evidence strength, then effort. Do not promote or demote a finding without new evidence.
- Authentication, overload, rate limit, timeout, adapter startup, or tool-host failure is infrastructure, not a quality FAIL and not a consumed quality attempt.
- Stop at every typed user decision and at implementation approval. Never infer authority from plan approval.

## Plan workflow

Use `scripts/plan_workflow.py`. Detailed state and failure semantics are in [references/planning_workflow.md](references/planning_workflow.md); read that reference when diagnosing, adjudicating, resuming, or recovering a run.

### 1. Preflight

```bash
python3 <skill-root>/scripts/plan_workflow.py preflight \
  --project <absolute-project> --backend <auto|claude|codex|dsh>
```

Use `--probe` when provider authentication/configuration is uncertain; it makes one small paid, sandboxed schema call per selected adapter.

Planning defaults to Claude Opus and Codex `gpt-5.6-sol`; the dsh adapter reads its model from the
harness settings (`~/.dsh/settings.yaml`). `auto` fills the two slots from the preference order
`claude -> dsh -> codex` (the default pair is claude + dsh). Explicit user selection always wins:

```bash
--codex-model <model> \
--codex-model-provider <configured-provider> \
--codex-profile <profile>
--claude-model <model>
--dsh-model <model> \
--dsh-model-provider <configured-provider>
```

Use `cli-default` as a model value to defer to that CLI/harness. A Codex provider/profile override without a model preserves the configured model. Pass the same selection to `init`; the run freezes only non-secret identity fields. Never paste credentials or endpoint tokens into arguments.

### 2. Freeze request and initialize

Write the objective, constraints, exclusions, priorities, success conditions, and known decisions to a local
Markdown request file. Use `final-reviewer=both` by default for independently verified planning; use one
adapter only when requested or when cost/availability requires it.

Choose the planning depth explicitly:

- `standard` (default): two independent investigations, two independent `draft_01` plans, two mutual
  evidence-checked integrations (`draft_02`), host synthesis, and final review.
- `deep`: all standard evidence work, then two additional divergent `draft_03` plans, host synthesis,
  convergence review round one, and final review as convergence round two. Deep mode is bounded and is
  never selected silently.

External research is off by default. Select `authoritative-web` only when local evidence may be insufficient;
investigators may then use official project documentation or the official GitHub repository. Search snippets
and third-party summaries are never evidence.

```bash
python3 <skill-root>/scripts/plan_workflow.py init \
  --project <project> --request <request.md> \
  --backend <backend> --final-reviewer <both|claude|codex|dsh> \
  --planning-depth <standard|deep> \
  --research-policy <local-only|authoritative-web> \
  [Codex selection options from preflight]
```

Record `run_id`, `agent_runtime`, and the returned `next_action`.
If a later skill update causes an engine-drift rejection, never bypass it by editing state. Preview and obtain
approval for `migrate-engine --reason <reason> --actor <actor> --apply`; this preserves the old and new identities.

### 3. Follow the deterministic next action

```bash
python3 <skill-root>/scripts/plan_workflow.py next \
  --project <project> --run-id <run-id>
```

- For `RUN_AGENT_BATCH`, inspect every `preview_commands` entry, then launch every listed `commands` entry concurrently and wait at the stated barrier. Never wait for A before starting B in the same round.
- For `RUN_AGENT`, execute `preview_command`, inspect it, then execute `command` (used for single-reviewer stages).
- For `HOST_SYNTHESIS`, run its command and read every returned artifact. Write `implementation_plan.md` and `batches.md` in the returned output directory. Resolve disagreement by evidence and preserve unresolved product choices.
- For `ASK_USER`, present the evidence and allowed typed decision; preview the corresponding adjudication before `--apply`.
- For `STOP_FOR_IMPLEMENTATION_APPROVAL`, export and stop.

Repeat `next` after each completed action. Do not guess a slot, reviewer, phase, or retry.

Every investigation finding must state its scope id, severity (`P0`–`P3`), urgency (`U0`–`U3`),
priority lane, evidence status, problem, evidence ids, root-cause status and causal chain, affected surfaces,
recommended solution, alternatives/tradeoffs, and verification. Keep unresolved claims visible. A bounded
convergence run guarantees an honest terminal state or typed user decision—not consensus or correctness.
The workflow computes stable `F-*` fingerprints from scope, problem, and causal chain; later rounds must
dispose those ledger keys and must submit genuinely new findings in full solution form. Draft-time repository
discoveries go into structured `new_evidence`; unrecorded observations cannot be cited. Integrators may propose
evidence-backed aliases for semantically duplicate `F-*` observations, but uncertain equivalence remains separate.

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

At either `READY` or `ABANDONED`, use `audit-export` to retrieve the terminal audit report. An abandoned report preserves the last candidate, review evidence, resource totals, and reason, and explicitly states that no plan was approved.

## Implement workflow

Implementation state lives separately under `~/.grounded-build/implementation/`. For a Plan handoff, initialize with the exported plan and batch manifest. For a user-provided plan without a manifest, derive a finite, observable batch manifest and obtain confirmation before freezing it.

The implementation host remains the current host model. Its isolated reviewer defaults to Opus for Claude or `gpt-5.6-sol` for Codex; `--claude-model`, `--codex-model`, `--codex-model-provider`, and `--codex-profile` may override that selection at `init` and `change-reviewer`. The frozen `reviewer_runtime` must be reported and preserved across review calls.

Before implementation preflight, resolve one stable CPython 3.11+ executable and use its absolute path
for every `workflow.py` command in that run. Report and preserve the frozen `controller_runtime`; never
silently switch between a Conda interpreter, system Python, and an activated environment. An ignored
project `.venv` is expected not to appear in Git worktrees: the verifier reuses it read-only from the
original checkout. If it is absent, do not install dependencies or run `uv sync` without explicit user
authority; follow the environment diagnostic in the implementation reference.

Follow [references/implementation_workflow.md](references/implementation_workflow.md) exactly. Never treat the current host conversation as its own independent reviewer.

## Resume

Query both mode-specific status commands only when the run's owner is unknown. Resume from the reported state and `next_action`; never copy state between modes, rewrite frozen artifacts, reset budgets, or initialize a replacement silently.
