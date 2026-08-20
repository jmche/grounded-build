---
name: grounded-build
description: Create an evidence-backed implementation plan through two isolated Claude/Codex planning instances, mutual cross-review, host synthesis, and final review, then optionally carry that verified plan through reviewed implementation. Use this skill whenever a user asks for an independently verified coding plan, two-agent planning, Claude/Codex cross-checking, a plan audit grounded in the real repository, or explicitly asks to use grounded-build. It supports one-provider environments through two isolated instances of the same available agent. For a generic request to implement an already-existing plan, prefer implement-plan-with-review unless the user explicitly selects this skill or the plan was produced by a grounded-build run.
compatibility: Linux, Git, Python 3.11+, bubblewrap, and at least one of the claude or codex CLIs.
metadata:
  version: 0.1.0
---

# Grounded Build

Turn a software objective into a repository-grounded plan and, only with user authority, into reviewed code. Planning and implementation are separate modes with separate state. Never modify or resume a run belonging to `implement-plan-with-review`.

## Choose the mode

- Use **plan mode** when the user has a goal, an uncertain draft, or asks Claude/Codex to investigate and cross-check a plan.
- Use **implement mode** when the user provides an implementation plan and wants it executed in reviewed batches.
- For an end-to-end request, finish plan mode, present the final artifacts, obtain explicit approval, then initialize implement mode. Plan approval does not imply implementation authority.

## Shared rules

- Work from the repository root and record its exact baseline SHA.
- Preserve the user's original checkout, branch, index, and files until explicit final integration.
- Independence means separate process/session, isolated initial context, separate worktree, restricted authority, and audited output. It does not require different vendors.
- Never describe two same-provider instances as model-diverse. Record both `provider_diversity` and process isolation honestly.
- Agent agreement is not evidence. Bind factual claims to named files, symbols, tests, commands, or authoritative external sources.
- Do not turn process failure, malformed JSON, timeout, quota exhaustion, or missing infrastructure into a quality FAIL.
- Keep budgets bounded. A user decision is preferable to an unending improvement loop.

# Plan mode

Plan mode uses `scripts/plan_workflow.py`. Its state is stored below `~/.grounded-build/planning/`, or `GROUNDED_BUILD_PLAN_HOME` in tests. It creates two detached planning worktrees, validates their HEAD and cleanliness around every call, and exposes them read-only through bubblewrap. The run root, sibling slot, original checkout, common secret directories, and secret-like environment variables are hidden from planner tool calls. The selected provider's own credential store remains part of that CLI's trust boundary.

## Inputs

Obtain:

- `project`: absolute Git repository root.
- `request`: a local Markdown file containing the objective, constraints, exclusions, and known decisions.
- `backend`: `auto`, `mixed`, `claude`, or `codex`.
- `final_reviewer`: `both`, `claude`, or `codex`. Ask before initialization if the user has not selected it.

Topology rules:

| Backend | Planner A | Planner B | Diversity |
|---|---|---|---|
| `auto`, both available | Claude | Codex | true |
| `auto`, only Claude | Claude instance A | Claude instance B | false |
| `auto`, only Codex | Codex instance A | Codex instance B | false |
| `mixed` | Claude | Codex | true |
| `claude` or `codex` | two isolated instances | two isolated instances | false |

Cross-review is always mutual: A reviews B and B reviews A. `final_reviewer=both` means both slots independently review the synthesized plan. A single selected provider runs in a fresh session as logical reviewer `F`.

## 1. Preflight

```bash
python3 <skill-root>/scripts/plan_workflow.py preflight \
  --project <absolute-project> --backend <auto|mixed|claude|codex>
```

Planning requires a clean repository because detached worktrees cannot see uncommitted changes. Ask the user to commit or otherwise resolve changes; do not stash them silently.

## 2. Freeze the request and initialize

```bash
python3 <skill-root>/scripts/plan_workflow.py init \
  --project <absolute-project> \
  --request <absolute-request.md> \
  --backend <backend> \
  --final-reviewer <both|claude|codex>
```

Record the returned run ID. Initialization freezes the request, baseline, topology, and worktrees. The two draft contexts do not contain one another's output.

## 3. Generate independent drafts

Preview each paid invocation, then execute it:

```bash
python3 <skill-root>/scripts/plan_workflow.py draft --project <project> --run-id <id> --slot A --dry-run
python3 <skill-root>/scripts/plan_workflow.py draft --project <project> --run-id <id> --slot A
python3 <skill-root>/scripts/plan_workflow.py draft --project <project> --run-id <id> --slot B --dry-run
python3 <skill-root>/scripts/plan_workflow.py draft --project <project> --run-id <id> --slot B
```

The planners independently inspect the same Git SHA. They classify repository claims as `VERIFIED`, `INFERRED`, or `UNRESOLVED` and propose finite batches with decidable exit observations.

## 4. Cross-review

```bash
python3 <skill-root>/scripts/plan_workflow.py cross-review --project <project> --run-id <id> --slot A --dry-run
python3 <skill-root>/scripts/plan_workflow.py cross-review --project <project> --run-id <id> --slot A
python3 <skill-root>/scripts/plan_workflow.py cross-review --project <project> --run-id <id> --slot B --dry-run
python3 <skill-root>/scripts/plan_workflow.py cross-review --project <project> --run-id <id> --slot B
```

Each invocation receives the request and the other planner's draft, not its own hidden transcript. Reviewers verify facts against the repository and check scope, dependencies, budgets, compatibility, and testability.

If a reviewer returns `NEEDS_USER_DECISION`, present the evidence and ask the user. Preview and then record the answer:

```bash
python3 <skill-root>/scripts/plan_workflow.py adjudicate \
  --project <project> --run-id <id> --choice RESOLVE_AND_CONTINUE \
  --decision '<decision and reason>' --actor <actor>
python3 <skill-root>/scripts/plan_workflow.py adjudicate \
  --project <project> --run-id <id> --choice RESOLVE_AND_CONTINUE \
  --decision '<decision and reason>' --actor <actor> --apply
```

## 5. Host synthesis

```bash
python3 <skill-root>/scripts/plan_workflow.py synthesis-context --project <project> --run-id <id>
```

Read every returned artifact. As the host, write `implementation_plan.md` and `batches.md` into the returned output directory. Resolve conflicts by evidence rather than majority vote. Preserve unresolved product decisions instead of inventing policy.

The final plan must state:

- included and excluded total scope;
- numbered plan items, dependencies, affected components, and expected semantics;
- existing budget names and exact meanings rather than invented replacements;
- ordered `Bxx` batches mapping plan items to work;
- a finite exit observation for every batch;
- risks, compatibility constraints, migration/rollback behavior, and verification commands;
- which disputed proposal was chosen and why.

Submit the candidate:

```bash
python3 <skill-root>/scripts/plan_workflow.py submit-synthesis \
  --project <project> --run-id <id> \
  --plan <implementation_plan.md> --batch-manifest <batches.md>
```

The workflow permits the initial synthesis plus one evidence-driven correction. After exhaustion, the user may explicitly grant at most one additional synthesis with `GRANT_ONE_SYNTHESIS`; this decision is recorded and supplied to later synthesis and final-review contexts. It never consumes implementation review or rollback budgets.

## 6. Final review

For `final_reviewer=both`, run reviewers A and B. For a single selected provider, run reviewer F. Preview first.

```bash
python3 <skill-root>/scripts/plan_workflow.py final-review \
  --project <project> --run-id <id> --reviewer <A|B|F> --dry-run
python3 <skill-root>/scripts/plan_workflow.py final-review \
  --project <project> --run-id <id> --reviewer <A|B|F>
```

All required reviewers must PASS the exact synthesized plan and manifest. FAIL returns to host synthesis when one correction remains. A policy boundary or exhausted correction budget becomes `NEEDS_USER_DECISION`.

## 7. Export and hand off

```bash
python3 <skill-root>/scripts/plan_workflow.py status --project <project> --run-id <id>
python3 <skill-root>/scripts/plan_workflow.py export --project <project> --run-id <id>
```

Report the baseline SHA, topology, provider diversity, evidence limitations, disagreements, final review verdicts, plan digest, batch-manifest digest, and artifact paths. Do not start implementation until the user explicitly approves it.

After `READY` or `ABANDONED`, unregister planning worktrees without deleting the audit record:

```bash
python3 <skill-root>/scripts/plan_workflow.py cleanup --project <project> --run-id <id>
python3 <skill-root>/scripts/plan_workflow.py cleanup --project <project> --run-id <id> --apply
```

# Implement mode

Implement mode is the audited engine transplanted from `implement-plan-with-review` at the v0.1.0 baseline. It has its own state below `~/.grounded-build/implementation/` and does not read or mutate the original skill's runs.

Before implementing, read [references/implementation_workflow.md](references/implementation_workflow.md) completely and follow it. Use:

```bash
python3 <skill-root>/scripts/workflow.py preflight --project <project> --target-branch <branch>
```

For a Plan mode handoff, pass the exported `implementation_plan.md` as `--plan` and `batches.md` as `--batch-manifest`. The implementation run snapshots both again; it does not trust planning status by path alone.

## When the user brings only a plan

`--batch-manifest` is required and is **host-authored** — the argument's own help says so. A user
arriving with a plan file has no reason to have written one, and asking them to is asking them to
do the host's job. Derive it, then have them confirm it before `init`, because initialization
freezes it.

Read the plan and write a manifest that states, for every batch:

- an ordered `Bxx` identifier and what it covers;
- which plan items it maps to, and what it deliberately excludes;
- its dependencies on earlier batches;
- **a finite exit observation** — a command, a file state, or a search whose answer is not a
  matter of opinion;
- the verification commands a reviewer should expect to see pass.

The exit observation is the part that decides whether the run converges. "The refactor is
complete and quality has improved" cannot be observed, so a reviewer cannot agree that it
happened, and the review budget is spent arguing about what the sentence meant — a batch of one
run lost five rounds to exactly that, and the fault was the target, not the reviewer.
"`pytest tests/ -q` passes and `bar()` no longer appears in `scripts/foo.py`" can be observed by
anyone, including the reviewer, in one command.

Size batches for convergence, not for tidiness. Measured across four batches of one run: the
batch carrying four new modules and nine wiring sites took three times the effective hours of the
other three and consumed half of all review rounds, and five of its seven serious defects were
introduced by the repair for the previous one. A batch that fits in one reviewable step is
cheaper than a batch that is conceptually elegant.

Present the manifest to the user as the run's scope, in their language, before initializing.
Their objection to a batch boundary costs a sentence now and a full re-initialization later.

The transplanted engine retains its original fixed-SHA acceptance-contract review, isolated implementation/reviewer worktrees, finding ledger, bounded review and invocation budgets, typed decisions, fixed-SHA sandbox verification, cumulative final review, fast-forward-only integration, tamper checks, and crash recovery.

# Resume safely

Determine which mode owns the run before acting:

```bash
python3 <skill-root>/scripts/plan_workflow.py status --project <project> --run-id <planning-id>
python3 <skill-root>/scripts/workflow.py status --project <project> --run-id <implementation-id>
```

Resume only from the reported state. Never copy a state file between modes, alter a frozen artifact, reset a budget, or claim review diversity that did not occur.
