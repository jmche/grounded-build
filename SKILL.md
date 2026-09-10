---
name: grounded-build
description: Produce and audit repository-grounded implementation plans with isolated planning instances, cross-review, deterministic workflow state, and optional reviewed implementation. Use only when the user explicitly requests grounded-build. For implementing an unrelated existing plan, prefer implement-plan-with-review.
metadata:
  version: 0.6.4
  compatibility: Linux, Git, Python 3.11+, bubblewrap and socat (both sandbox packages; the provider CLI sandbox is fail-closed), and at least one Claude, Codex, or dsh CLI adapter
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
- Planning freezes the committed SHA selected by `--base-ref`; a dirty source checkout is reported
  and excluded. Never stash, reset, clean, commit, or copy user work to manufacture a baseline.
- Freeze each run at its baseline SHA; after initialization the original checkout may move independently until explicit final integration.
- A CLI adapter is not a model identity. Record `agent_runtime`, `provider_diversity`, and `model_diversity` exactly as reported. Every run freezes the workflow engine, model selection, and invocation argv; reject silent engine drift.
- Agreement is not evidence. Bind claims to files, symbols, tests, commands, or authoritative sources.
- Freeze scope before investigation. Divergence may widen evidence, causal analysis, failure-path coverage,
  alternatives, and verification, but never the user-authorized objective.
- Use [references/causal_analysis.md](references/causal_analysis.md) to trace authority, actual inputs,
  responsible producers, consumer interpretation, and the earliest responsible decision. Apply it
  proportionally; do not force a large causal model onto a direct, uncontested local edit.
- Adapt evidence to deterministic, generative, external, human, or hybrid producers. Deterministic code
  establishes machine facts; an authorized semantic reviewer establishes meaning. Never replace semantic
  judgment with regexes, keywords, filenames, extension allowlists, or similarity thresholds.
- Order work by scope gate, user priority, severity, urgency, blockers/dependencies, causal leverage,
  evidence strength, then effort. Do not promote or demote a finding without new evidence.
- Authentication, overload, rate limit, timeout, adapter startup, or tool-host failure is infrastructure, not a quality FAIL and not a consumed quality attempt.
- Stop at every typed user decision and at implementation approval. Never infer authority from plan approval.

## Plan workflow

Use `scripts/plan_workflow.py`. Detailed state and failure semantics are in [references/planning_workflow.md](references/planning_workflow.md); read that reference when diagnosing, adjudicating, resuming, or recovering a run.

### 1. Preflight

```bash
python3 <skill-root>/scripts/plan_workflow.py preflight \
  --project <absolute-project> --base-ref <ref> \
  --backend <auto|claude|codex|dsh> --host-adapter <claude|codex|dsh> \
  --peer-reviewer <auto|claude|codex|dsh>
```

Use `--probe` for an early availability check. It makes one small paid, sandboxed call per selected
adapter that authenticates, reads a controller-owned mounted file, and returns its contents in a
schema object. Host-aware initialization repeats that check with the exact runtime it freezes, so a
preflight result cannot go stale or be applied to different model/provider settings.

Planning defaults to Claude Opus and Codex `gpt-5.6-sol`; the dsh adapter reads its model from the
harness settings (`~/.dsh/settings.yaml`). With `--host-adapter`, `auto` binds slot A to a fresh
isolated instance of the host adapter and prefers Codex for the non-host slot. If the host is Codex,
it prefers Claude and then dsh. Initialization checks those candidates in order with the exact runtime
being frozen and automatically falls back to the host when no external candidate passes. An explicit
`--peer-reviewer` or final reviewer must pass its initialization-bound check or initialization fails;
`--final-reviewer auto` follows the frozen peer. Without a host binding, legacy
`auto` uses `claude -> dsh -> codex`. Explicit user selection always wins:

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
Markdown request file. Standard planning uses one fresh final reviewer by default. `--final-reviewer auto`
applies the same host-aware preference: Codex for a non-Codex host, Claude then dsh for a Codex host,
and the host's fresh isolated CLI when no preferred external adapter is available. Reserve
`final-reviewer=both` for deep planning, an explicitly requested dual review, or a demonstrated
high-consequence reason. Process and context isolation establish reviewer independence; paying two
final reviewers is not required merely to make a standard run independent.

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
  --project <project> --request <request.md> --base-ref <ref> \
  --backend <backend> --host-adapter <claude|codex|dsh> \
  --peer-reviewer <auto|claude|codex|dsh> \
  --final-reviewer <auto|both|claude|codex|dsh> \
  --planning-depth <standard|deep> \
  --research-policy <local-only|authoritative-web> \
  [Codex selection options from preflight]
```

`--base-ref` defaults to `HEAD`. Planning freezes that ref's committed SHA in its own detached
worktree; staged, modified, and untracked files in the source checkout neither block initialization
nor enter the planning snapshot. Pass an explicit ref whenever the checked-out branch is not the
intended planning authority.

Record `run_id`, `base_ref`, `baseline_sha`, the excluded source-worktree changes,
`agent_runtime`, and the returned `next_action`.
If a later skill update causes an engine-drift rejection, never bypass it by editing state. Preview and obtain
approval for `migrate-engine --reason <reason> --actor <actor> --apply`; this preserves the old and new identities.
If an interrupted controller leaves one assignment marked `RUNNING`, preview and explicitly apply
`recover-invocation` for that assignment before retrying or migrating. Its spent attempt remains charged.

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
For a material defect, the causal chain identifies the governing authority, actual production input,
responsible producer, consumer interpretation, and earliest supported divergence; for a new capability,
identify the earliest responsible authority and design boundary instead of inventing a root cause.
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

One repository may have multiple concurrent implementation runs. Every run freezes its own baseline,
branch, worktrees, state, and reviewer; always preserve the returned `run_id`. When several runs are
active, commands that omit `--run-id` refuse and list the candidates. Target-branch movement never
invalidates baseline execution: finish the reviewed candidate, then use preview-first `reconcile` and
`submit-reconciliation` when the target has diverged. Re-running `reconcile` returns the active attempt;
use preview-first `abandon-reconciliation` before intentionally starting another.

Implementation initialization freezes the named target branch's committed SHA and does not require the
current checkout to be clean. Uncommitted files are reported and excluded from that source baseline.
When an uncommitted project contract must govern the run, pass it explicitly with repeatable
`--instruction-file`; the engine freezes it separately for the host and both reviewer boundaries.
Supplementary instructions are bounded UTF-8 text and cannot override workflow security, frozen scope,
reviewer, or acceptance authority.
The checked-out target must still be clean at final integration so user work cannot be overwritten.
Finalize preview and status expose whether current target-checkout changes will block apply.

The implementation host remains the current host model. With `--reviewer auto --host-adapter ...`, its
isolated reviewer follows the same policy as planning: a non-Codex host prefers Codex and falls back
to the host; a Codex host prefers Claude, then dsh, then Codex. Initialization verifies each candidate
with the exact runtime before freezing it and records `reviewer_selection_checks`. An explicit reviewer
still wins. Claude defaults to Opus, Codex to `gpt-5.6-sol`, and dsh to the model selected in
`~/.dsh/settings.yaml`; `--claude-model`, `--codex-model`, `--codex-model-provider`,
`--codex-profile`, `--dsh-model`, and `--dsh-model-provider` may override that selection at `init`
and `change-reviewer`. The frozen `reviewer_runtime` must be reported and preserved across review calls.

Before implementation preflight, resolve one stable CPython 3.11+ executable and use its absolute path
for every `workflow.py` command in that run. Report and preserve the frozen `controller_runtime`; never
silently switch between a Conda interpreter, system Python, and an activated environment. An ignored
project `.venv` is expected not to appear in Git worktrees: the verifier reuses it read-only from the
original checkout. If it is absent, do not install dependencies or run `uv sync` without explicit user
authority; follow the environment diagnostic in the implementation reference.

Follow [references/implementation_workflow.md](references/implementation_workflow.md) exactly and apply
[references/causal_analysis.md](references/causal_analysis.md) when responsibility or causality is material.
Never treat the current host conversation as its own independent reviewer.

## Resume

Query both mode-specific status commands only when the run's owner is unknown. Resume from the reported state and `next_action`; never copy state between modes, rewrite frozen artifacts, reset budgets, or initialize a replacement silently.
