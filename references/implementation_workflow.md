# Implement mode contract

Execute a user-provided implementation plan without changing the original project until final integration. The current host coding agent implements and fixes the plan in an isolated worktree. A selected `claude`, `codex`, `dsh`, or protocol-compatible `other` CLI reviews each fixed commit in a second isolated worktree. The reviewer may use the same agent family as the host; independence means a fresh process, isolated context, separate worktree, restricted authority, and audited output—not necessarily a different vendor or model.

Read `references/causal_analysis.md` before implementing a defect, failed repair, cross-layer
mismatch, or change with an unclear responsibility boundary. Apply its production trace
proportionally to every project; producer-aware evidence is not an agent-project-only mode.
Identify the earliest responsible production boundary and adapt verification to deterministic,
generative, external, human, or hybrid producers. Keep machine facts in deterministic checks and
semantic meaning with an authorized semantic reviewer.

Use the bundled script for Git, worktrees, state, fixed-SHA review dispatch, and integration. The host agent owns plan interpretation, implementation, tests, and semantic repair.

## Choose and preserve the controller interpreter

Resolve a stable CPython 3.11+ executable before preflight and invoke every command for the run with
that same absolute path. The bare name `python3` is a PATH lookup and may select a different Conda,
system, or activated-environment interpreter in a later shell. Preflight reports
`controller_runtime.command_prefix`; initialization freezes that identity, status reports drift, and
state-changing commands reject silent interpreter changes. If the PATH-selected interpreter is
unstable for the repository workload, use a known-good explicit interpreter path. Do not encode one
machine's `/usr/bin/python3` path as a portable skill requirement.

Examples below use `<controller-python>` for that resolved executable.

## Inputs

Collect or infer:

- `plan`: an existing local plan file, resolved to an absolute path.
- `batch_manifest`: a host-authored file that explicitly declares this run's included and excluded total scope and maps every batch ID to plan work. This is execution authority, not reviewer output.
- `reviewer`: exactly `claude`, `codex`, `dsh`, or `other`; it may match the host/implementer.
- `host_adapter`: the current host's CLI adapter when automatic reviewer selection is requested.
- `reviewer_selection_checks`: initialization-bound static and mounted-file probe evidence for each
  automatic candidate considered before the concrete reviewer is frozen.
- `reviewer_runtime`: the non-secret model identity frozen at initialization. Claude defaults to Opus,
  Codex defaults to `gpt-5.6-sol`, and dsh reads the harness selection from `~/.dsh/settings.yaml`.
  Optional `--claude-model`, `--codex-model`, `--codex-model-provider`, `--codex-profile`,
  `--dsh-model`, and `--dsh-model-provider` select a configured model without treating the CLI name
  as the model family.
- `fix_policy`: `ask` by default, `auto` only when explicitly requested, or `never` for review without repair.
- `target_branch`: the current branch unless the user explicitly names another branch.
- `instruction_file`: optional, repeatable paths to uncommitted or external project contracts that
  must govern this run. These are frozen explicitly; unrelated working-tree changes are never copied.
  Each file must be UTF-8 text, at most 1 MiB, and the run accepts at most 16 files.

The agent invoking this skill is always the implementer. Do not launch another implementer. A same-family reviewer is allowed only as a fresh reviewer CLI invocation under the workflow's isolated reviewer contract; the current host conversation must never issue its own PASS. Claude and Codex use their read-only CLI modes. dsh uses path-bounded read-only file tools, reads a controller-captured fixed-SHA diff, and returns one complete structured result through its final output; it receives no writable reviewer channel and does not alter shared Git configuration. `other` uses the generic bridge and isolation contract defined in `references/planning_workflow.md`.

## Storage and isolation

Store all workflow data below:

```text
~/.grounded-build/implementation/
└── projects/<project-slug>-<identity-hash>/
    ├── project.json
    └── runs/<run-id>/
        ├── workflow.json
        ├── plan/original.md
        ├── plan/batches.md             # frozen run scope and batch mapping
        ├── instructions/                # explicitly named supplementary contracts
        ├── worktrees/implementation/
        ├── worktrees/reviewer/
        ├── verification_runs/
        ├── events/                     # append-only hash-linked state events
        ├── contracts/
        ├── decisions/
        ├── reviews/
        ├── logs/
        ├── tests/
        └── final_report.md
```

The identity hash distinguishes repositories with the same directory name. Every new plan execution receives a new run directory and new worktrees. Never reuse an old run as the baseline of a new plan.

Initialization freezes committed source at one baseline SHA. After its run-owned worktrees exist, the
original checkout may change branches, advance, or become dirty without changing implementation or
review. Target movement is integration divergence, not execution staleness.

## Safety boundary

- Review exact commit SHAs, never a moving branch name.
- Do not modify the original project before explicitly approved finalization.
- Do not treat reviewer process failure, malformed output, timeout, stale base, or test-infrastructure failure as code-quality `FAIL`.
- Do not claim a batch passed until the reviewer returns `PASS` for the exact implementation HEAD and required verification passes.
- Do not use `git reset --hard`, `git clean`, automatic stash, silent rebase, or automatic conflict resolution.
- Do not delete worktree directories directly; use the script's cleanup operation.
- Continue fixed-baseline execution if the target advances. Previously issued review approvals do not authorize the combined code; use reviewed reconciliation before integration.
- If a skill update changes the frozen engine files, Implement rejects ordinary commands until the
  operator runs `migrate-engine --reason <reason> --actor <actor>` (preview first, then `--apply`).
  Migration refuses active invocations, preserves frozen artifacts, and marks any current-SHA PASS
  for one fresh review under the new engine before acceptance.
- The ordinary cap is four valid review rounds per batch: one full discovery review and three bounded re-reviews. Reviewer infrastructure errors do not consume a round. Three separately audited exceptions may each add at most one ordinary round: a convergence-boundary grant, a review-budget grant, and a new-commit re-review after an effective PASS. A nonterminal typed decision applied only after those available rounds are exhausted authorizes one terminal closeout review bound to that decision, batch, and exact SHA. Thus the absolute non-legacy maximum is eight. Infrastructure or malformed output does not consume the closeout; one valid non-PASS result does, and cannot open another repair cycle.
- Count every real reviewer invocation separately from valid quality rounds. Infrastructure failures consume invocation budget and remain auditable even though they do not consume repair budget.
- Treat `plan/original.md` and `plan/batches.md` with their recorded digests as the run authority. The manifest defines this run's total included/excluded scope and batch mapping. A later edit or move of either source is informational; a changed snapshot is corruption.
- Never overwrite a contract, review, or verification attempt. Every invocation receives a unique append-only directory and event record.
- A batch cannot be accepted while any same-SHA verification is unresolved or failing. Verification infrastructure errors remain retryable and cannot be bypassed by requesting another static review.
- Keep the reviewer response schema within the portable structural subset (`type`, `properties`, `required`, `additionalProperties`, `items`, and `enum`). Enforce uniqueness, hashes, lengths, and other semantic constraints in Python so unsupported JSON Schema keywords cannot disable every review.
- Grant reviewer CLIs only a round-specific context containing the plan snapshot, current finding ledger, and assignment metadata. Historical reviews remain audit records, not reviewer input.
- Include the frozen batch manifest in both contract-review and code-review contexts. A reviewer may challenge its boundary through `NEEDS_USER_DECISION`, but may not silently expand the run or remap batch identifiers.
- Permit implementer and reviewer to use the same agent family, but preserve process, context, worktree, permission, and output isolation. Model diversity is useful but is not the source of review authority.
- Transport the full authoritative Git range for round one. On later rounds, transport only the delta
  since the immediately preceding reviewed SHA when that SHA remains an ancestor, while retaining the
  full acceptance contract, finding ledger, plan authority, and exact-HEAD worktree. If review history
  is no longer ancestral, the review follows a user decision without a new commit, or the batch is the
  cumulative final review, return to full transport. Never use filenames, line counts, or keyword rules
  to decide semantic review scope.

## Shared review contract

Read `references/reviewer_prompt.md` before initializing a run. It is the installed canonical
detailed contract shared with the independent reviewer; initialization freezes both reviewer
templates into the run and records their SHA-256 digests. Every later contract and batch review reads
those snapshots, so installing edited template files cannot silently change an in-flight run. This is
a template contract, not a freeze of the Python state-machine engine; engine and schema changes still
follow their own migration boundary. The host must reason with the same lifecycle rather than treating
reviewer output as an opaque PASS/FAIL gate.

- Every finding has a stable semantic `fingerprint` and an ID that is reused while the same defect remains.
- Every finding carries `location`, `required_outcome`, and `details`. `required_outcome` is the close condition the implementer works against; the ledger freezes it at first statement, records later restatements as `obligation_revisions` without moving the obligation, and expects a wider obligation to arrive as a new finding.
- `novelty` is exactly one of `INITIAL_REVIEW`, `INTRODUCED_BY_FIX`, `PREVIOUSLY_MASKED`, `PRE_EXISTING`, or `UNRELATED`. The controller binds the reviewed SHA and round to every finding, so the reviewer never repeats them; the reasoning for `INTRODUCED_BY_FIX` and `PREVIOUSLY_MASKED` lives in `details` and is judged by the host and the user, not by the controller.
- `resolved_finding_ids` may name only prior OPEN findings that the reviewed SHA verifiably resolves. A severity change is recorded in the ledger's `severity_history`; its reason lives in `details`.
- The finding ledger owns lifecycle state: blocking findings are `OPEN`, verified repairs become `VERIFIED`, and nonblocking findings become `DEFERRED` with an explicit reason. Missing OPEN findings do not disappear merely because a later reviewer omits them.
- Legacy recovery is an evidence-preserving exception for pre-ledger reports, not permission to invent IDs or fingerprints.
- Acceptance criteria use `COMMAND`, `REPOSITORY_ASSERTION`, or `USER_BOUNDARY` evidence. Deterministic code verifies provenance, SHA, command execution, and state; the independent reviewer decides semantic sufficiency.

## Preflight and latest-code check

Before initializing, inspect the current local baseline:

```bash
<controller-python> <skill-root>/scripts/workflow.py preflight \
  --project <absolute-project-root> \
  --target-branch <branch>
```

Report the target SHA, working-tree state, upstream, and locally known ahead/behind relation. Preflight does not access the network.

If an upstream exists and current remote state matters, ask before running `git fetch`. Fetching may update observations but must not automatically merge, rebase, or pull. After any user-approved synchronization, run preflight again and initialize only from the confirmed target SHA.

Uncommitted project changes are not part of a Git worktree baseline, but they do not block
initialization. Report them as excluded and freeze the exact committed SHA named by `--target-branch`.
Never stash, reset, commit, or copy the dirty checkout automatically. If an uncommitted instruction
such as `AGENTS.md` or `CLAUDE.md` must govern the run, name it explicitly with `--instruction-file`.

## Start a new run

Read the complete plan first. Create a batch manifest that states the run's total included scope, explicitly deferred/excluded scope, ordered batch identifiers, and the mapping of each identifier to plan work, plus dependencies and measurement rationale when relevant. Do not confuse plan item or proposed-commit numbers with the actual Git commits produced during implementation. If the plan has no executable batch boundaries or contains a material ambiguity, ask the user before initialization.

Use a concise manifest such as:

```markdown
# Run scope
Included: planned items 01-09.
Excluded: planned items 10-22; reserved for later runs.

# Batch mapping
- B01: planned items 01-02
- B02: planned items 03-05
- B03: planned items 06-08
- B04: planned item 09

# Rationale
Keep independently measured workstreams in separate runs and batches.
```

Host-supplied acceptance conditions are optional and are never the limit of reviewer inquiry. Preserve useful conditions, but do not force the host to invent a complete manifest before initialization. The independent reviewer forms or challenges the effective acceptance contract after initialization. It may derive a decidable observation when repository contracts make the boundary unambiguous; it must return `NEEDS_USER_DECISION` when making the criterion testable would choose product policy.

Descriptions such as "machine checkable", "robust", "complete", or "sufficiently covered" are warnings rather than automatic rejection: the contract reviewer must either bind them to a named finite observation or report the missing boundary to the host/user.

Check existing runs:

```bash
<controller-python> <skill-root>/scripts/workflow.py list \
  --project <absolute-project-root>
```

Any number of nonterminal runs may coexist. Preserve the returned `run_id`; when more than one run is
active every later command must name it explicitly. Each run owns its command lock, so a busy run
does not serialize or block commands for another run. Only final integration takes the narrow shared
repository lock needed to serialize target-ref updates.

Initialize a new run:

```bash
<controller-python> <skill-root>/scripts/workflow.py init \
  --project <absolute-project-root> \
  --plan <absolute-plan-path> \
  --batch-manifest <absolute-batch-manifest-path> \
  --implementer <current-host-agent-name> \
  --host-adapter <claude-or-codex-or-dsh-or-other> \
  --reviewer <auto-or-claude-or-codex-or-dsh-or-other> \
  --fix-policy <ask-auto-or-never> \
  --batches <comma-separated-batch-ids> \
  --target-branch <branch> \
  [--instruction-file <absolute-contract-path>]...
```

`--reviewer auto` prefers Codex for a Claude or dsh host and falls back to the host when Codex does not
pass the initialization-bound check. For a Codex host it tries Claude, then dsh, then Codex. For an
`other` host it tries Codex, then Claude, then dsh, then `other`. The check
uses the exact model/provider/profile runtime that initialization will freeze; executable presence
alone is insufficient. Explicit reviewer selection remains authoritative and preserves the legacy
non-probing initialization behavior.

To use `other`, set `GROUNDED_BUILD_OTHER_COMMAND` to the absolute path of a bridge implementing
`grounded-build-other-v1` before preflight and initialization. The bridge is used for contract review,
fixed-SHA batch review, and reviewer changes through the same frozen executable digest and read-only
sandbox contract as Plan mode. Grounded Build contains no Pi-, OpenCode-, or vendor-specific dispatch.

The current host remains the implementer and is never model-overridden by this workflow. To override the
isolated reviewer's advanced default, append the same selection verified during preflight:

```bash
--claude-model <model>
--codex-model <model> --codex-model-provider <configured-provider> --codex-profile <profile>
--dsh-model <model> --dsh-model-provider <configured-provider>
```

Use `cli-default` for any reviewer model to defer to its CLI or harness. These values are frozen in `reviewer_runtime`; credentials, bearer tokens, and endpoint URLs are
never copied into workflow state.

Initialization snapshots the plan, batch manifest, and every explicitly named instruction; records
their digests; verifies that the manifest names every declared batch ID; creates a unique implementation
branch and reviewer worktree from the target branch's committed SHA; and leaves the original project's
branch, HEAD, index, and files unchanged. It reports the original working-tree changes as excluded from
the baseline. The new run starts at `AWAITING_CONTRACT_REVIEW`.

Read every returned instruction snapshot before implementation. Contract review and batch review receive
invocation-local copies of the same digest-bound files. A later edit to the source instruction does not
change the run; an edit to the frozen snapshot fails closed. A conflict between a supplementary instruction
and the committed baseline requires a user decision rather than an implicit precedence guess.
Supplementary instructions cannot override the frozen reviewer contract, acceptance contract, run scope,
or workflow security and evidence rules.

The run also freezes its quality-round, reviewer-invocation, contract-invocation, verification-attempt, and evidence-cycle budgets. Changing a global constant later does not silently change an existing run.

## Review the acceptance contract

Before implementation, run:

```bash
<controller-python> <skill-root>/scripts/workflow.py contract-review \
  --project <absolute-project-root> \
  --run-id <run-id>
```

The reviewer considers host criteria, the complete plan, the frozen run scope/batch mapping, and repository contracts. `CONTRACT_READY` stores an immutable effective contract with its own digest and permits implementation. The contract is a minimum obligation, not a whitelist of possible defects. Excluded work reserved for a future run is not an unmet criterion in the current run.

A READY contract must cover every declared batch. Each criterion states an evidence kind. `COMMAND` criteria provide argv and expected exit and are automatically scheduled for fixed-SHA execution; repository assertions remain semantic reviewer judgments.

When it returns `NEEDS_USER_DECISION`, present the issues and proposed observations to the user. Record the chosen boundary in a JSON decision file containing `reason` and normalized `criteria`, preview it, then apply it:

```bash
<controller-python> <skill-root>/scripts/workflow.py contract-adjudicate \
  --project <absolute-project-root> --run-id <run-id> \
  --decision-file <absolute-json-path>

<controller-python> <skill-root>/scripts/workflow.py contract-adjudicate \
  --project <absolute-project-root> --run-id <run-id> \
  --decision-file <absolute-json-path> --apply
```

The decision is preserved under `decisions/`; it does not masquerade as reviewer approval.

Read the returned `run_id` and `implementation_worktree`. Perform every implementation edit and test with that worktree as the working directory.

## Resume a run

Inspect the only active run (omission is refused when several are active):

```bash
<controller-python> <skill-root>/scripts/workflow.py status \
  --project <absolute-project-root>
```

Inspect a historical run explicitly:

```bash
<controller-python> <skill-root>/scripts/workflow.py status \
  --project <absolute-project-root> \
  --run-id <run-id>
```

Resume only from the run's recorded phase, batch, round, and SHAs. `integration.status=DIVERGED`
means the target moved; implementation, review, verification, and acceptance continue against the
frozen baseline. After the candidate is complete, create an explicit merge reconciliation rather than
rebasing or silently combining code.

If status reports `MIGRATION_REQUIRED`, preview and explicitly apply the migration before any mutation:

```bash
<controller-python> <skill-root>/scripts/workflow.py migrate --project <project> --run-id <run-id>
<controller-python> <skill-root>/scripts/workflow.py migrate --project <project> --run-id <run-id> --apply
```

Migration preserves a mode-0600 backup of the prior state, records its digest, labels unverifiable
legacy information, and appends migration events. Schema v7 and earlier runs explicitly snapshot the
currently installed reviewer templates during this operation; the preview lists their source paths
and digests, and the audit record identifies that migration-time basis. Migration never silently
invents missing evidence. Each transition records its actual source schema and preserves any prior
applied migration in `migration_history`, so sequential upgrades never reuse an older backup name.

## Implement each batch

For the next pending batch:

1. Read that batch from the plan snapshot.
2. Inspect governing repository instructions and all affected producers, consumers, failure paths, recovery paths, and compatibility paths. For a material defect, trace authority, actual production input, responsible producer, consumer interpretation, and the earliest supported divergence. For a direct local edit, keep this trace correspondingly small.
3. Implement only that batch in `implementation_worktree`.
4. Add focused tests and run required verification there.
5. Commit all intended changes and leave the implementation worktree clean.
6. Request review.

Use small logical commits. The reviewer evaluates the cumulative batch range from the prior accepted SHA through the current implementation HEAD.

Do not add a validator, resolver, semantic check, fallback, retry, or rejection merely because a
downstream output varies. First establish the production failure, producer and actual inputs,
machine-versus-semantic boundary, repair path, valid counterexamples, and a production-shaped
recovery test. A root repair should still prevent the mismatch if the downstream mechanism vanished.

## Run an independent review

```bash
<controller-python> <skill-root>/scripts/workflow.py review \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --batch <batch-id>
```

The script verifies both worktrees, the plan digest, the implementation branch, the target baseline, and the submitted SHA; detaches the reusable reviewer worktree at that SHA; invokes the selected reviewer; validates structured output; updates the stable finding ledger; applies convergence policy; and stores the complete audit record.

Interpret statuses as follows:

| Status | Meaning | Action |
|---|---|---|
| `REVIEW_PASS` | No effective blocking P0/P1 remains; P2 may be deferred | Run verification, then accept |
| `REVIEW_FAIL` | Actionable P0/P1 implementation defects remain | Follow `fix_policy` |
| `NEEDS_USER_DECISION` | Scope, authority, migration, stale-base, or safety decision | Ask the user; do not consume repair budget |
| `VERIFICATION_REQUIRED` | Reviewer needs a named executable observation | Preview and explicitly approve or reject the request; no review round is consumed |
| `REVIEWER_ERROR` | CLI, timeout, output, SHA, or reviewer-worktree failure | Diagnose infrastructure; do not classify as code failure |
| `WORKFLOW_ERROR` | Git or state invariant failed | Preserve state and report the blocker |

Every real reviewer call is stored under a unique `round_N/invocation_N/` directory. A malformed result or process failure leaves a terminal invocation record but does not create a quality review. Invocation budgets stop repeated infrastructure failures from consuming unbounded external-agent budget.

The reviewer delivery envelope is deliberately small. The workflow owns routing identity, commit
SHAs, legal verdicts, finding IDs, fingerprints, severities, and executable criterion evidence.
Finding explanation fields are semantic reviewer content: providers may omit inapplicable prose or
include additional explanatory keys, and the controller preserves the finding while supplying only
empty compatibility values needed by the lifecycle ledger. Missing lifecycle facts are still a
delivery error; semantic prose is not promoted into a new mechanical gate.

If an auto-selected external reviewer reports a machine-classified rate limit, the engine records
`REVIEWER_AUTO_FALLBACK`, persistently switches to the frozen host runtime, and returns
`retry_required: true`. Repeat the same contract-review or batch-review command; the failed
infrastructure call consumes no quality round and does not change the finding ledger or accepted
evidence. This also applies when the host is a configured `other` bridge.

An explicit reviewer is never replaced automatically. Other infrastructure failures, or a rate
limit when the host is unavailable, remain `REVIEWER_ERROR`. In those cases switch to Claude, Codex,
dsh, or a configured `other` bridge without restarting the run:

```bash
<controller-python> <skill-root>/scripts/workflow.py change-reviewer \
  --project <absolute-project-root> --run-id <run-id> \
  --reviewer claude --reason "Codex quota exhausted" --actor <actor>

<controller-python> <skill-root>/scripts/workflow.py change-reviewer \
  --project <absolute-project-root> --run-id <run-id> \
  --reviewer claude --reason "Codex quota exhausted" --actor <actor> --apply
```

Preview before applying. The new reviewer may match the implementer, but it still runs as a fresh isolated CLI reviewer. Switching preserves the current workflow phase, finding ledger, accepted evidence, quality rounds, verification state, and invocation usage; it never turns the prior infrastructure failure into code `FAIL` and never resets budget.

For dsh, append `--dsh-model <model>` and optionally `--dsh-model-provider <provider>` to either
preview or apply command when the harness selection should not be used.

## Fix loop

For `REVIEW_FAIL`:

- `ask`: summarize P0/P1 findings and ask whether the host should fix them.
- `auto`: fix them while changes remain inside the approved plan and progress continues.
- `never`: report findings and stop.

Pause despite `auto` when a fix expands scope, conflicts with the plan, changes a destructive migration decision, requires new authority, touches unexplained user changes, or two consecutive rounds do not reduce unresolved P0/P1 findings.

Fix in `implementation_worktree`, commit, and call `review` again for the same batch. The script requires a new fix commit after `REVIEW_FAIL` and reuses the reviewer worktree at the new SHA.

The first review carries the complete authoritative range. A subsequent review carries a controller-
captured patch beginning at the prior reviewed SHA when Git ancestry proves that it is a continuation.
This is a transport optimization, not a semantic boundary.
The authoritative coverage range continues to bind the verdict at exact HEAD.
The review output and status expose `review_mode`, both coverage and supplied-diff bases, patch bytes,
and duration. `DELTA` never means "review only these lines": the reviewer retains the exact-HEAD
worktree and expands inspection when a changed authority, contract, state/security boundary, scope,
prior assumption, or consumer invalidates earlier evidence. A rewritten branch automatically returns
to `FULL` transport. A same-SHA review after user adjudication also remains `FULL`, because the changed
decision context—not a Git delta—is the new evidence. Cumulative final reviews remain `FULL` because
they must issue criterion results across every batch.

A PASS describes one exact SHA, not the batch. If you commit again after `REVIEW_PASS` — a deferred finding fixed anyway, say — `accept` will refuse because the report no longer authorizes HEAD; run `review` again instead and the new commit gets its own round. A PASS or FAIL cannot buy another paid review of the same SHA. A typed user decision may legitimately return the same SHA for reconsideration; that review uses full transport because the decision context, rather than a code delta, changed. After acceptance the contract is closed: commits beyond the accepted SHA cannot be finalized, and the choice is to reset the implementation worktree back to that SHA or to supersede the run and review the extra commits in a new one.

Read `warnings` and `decision_reasons` on every review result:

- `FINDING_WITHOUT_CLOSE_CONDITION`: the reviewer reported a finding without `required_outcome`. The finding is kept and the review is not discarded; ask the reviewer for the close condition on the next round rather than guessing one. The ledger adopts the first non-empty statement.
- `SEVERITY_DOWNGRADE`: a P0/P1 finding was re-reported as P2 and stopped blocking on the reviewer's own authority; its `deferred_reason` records the downgrade. Surface it to the user with the reviewer's reason from `details`.
- `CROSS_BATCH_RESOLUTION` / `CROSS_BATCH_FINDING`: the reviewer referenced a finding an earlier batch owns. The observation is recorded, that batch keeps authority over its own status, and only a cross-batch P0 raises `CROSS_BATCH_MATERIAL_FINDING` for adjudication.
- `REVIEW_INVOCATION_BUDGET_EXHAUSTED`: the batch spent its reviewer calls, typically on invocations that errored without producing a review. `GRANT_ONE_REVIEW_INVOCATION` releases exactly one more, once per batch, and returns the run to the status it parked from. Deferring a finding cannot replenish call authority, so it is not offered as an invocation-budget remedy. Before the grant is used, the menu contains that grant plus `SUPERSEDE_RUN` and `ABORT_RUN`; afterward only those honest terminal choices remain. Both budget guards honour the grant, so the extra call really runs, including for a still-unused closeout review.
- Findings in every review result carry the ledger's `status`, frozen `required_outcome`, and `latest_required_outcome`, so the result is the implementer's channel and needs no separate `status` call. A finding whose `obligation_revisions` list is growing is being restated by the reviewer while the ledger keeps judging it against the frozen `required_outcome`. Implement against the frozen text. When such a finding reaches `NO_PROGRESS_FOR_TWO_ROUNDS` or the round cap, present that history when adjudicating: a requirement restated at three sites is a widening class, not one unfixed defect, and `RETURN_TO_FIX` or `DEFER_ELIGIBLE_P1` are both legitimate answers.

Convergence policy:

- Round 1 is the complete discovery review.
- Rounds 2–4 verify prior findings and repair regressions; they do not restart an unlimited architecture review.
- P0 always blocks.
- Evidence-backed, in-scope P1 blocks. On later rounds a newly discovered P1 blocks only when introduced by a fix or previously masked.
- P2, pre-existing findings, unrelated findings, and late nonqualifying P1 findings are recorded as deferred and do not block PASS. Pure preferences without a concrete failure consequence stay out of the finding ledger.
- Reviewer findings are presented in consequence order, P0 then P1 then P2; the workflow preserves the reviewer's order within a tier.
- Finding IDs and semantic fingerprints remain stable across rounds. Missing prior OPEN findings remain open unless the reviewer explicitly verifies their resolution.
- A severity upgrade from deferred to blocking requires user adjudication.
- Two consecutive rounds without reducing effective blocking findings return `NEEDS_USER_DECISION`.
- Round 4 is the final ordinary adjudication round. If blocking findings remain, do not auto-fix again; return `NEEDS_USER_DECISION`.

`NEEDS_USER_DECISION` is a typed state, not prose. Read `pending_decision` from status, show its allowed choices, and preview/apply the selected choice:

```bash
<controller-python> <skill-root>/scripts/workflow.py adjudicate \
  --project <project> --run-id <run-id> \
  --decision-id <id> --choice <allowed-choice> \
  --reason <reason> --actor <actor>

<controller-python> <skill-root>/scripts/workflow.py adjudicate \
  --project <project> --run-id <run-id> \
  --decision-id <id> --choice <allowed-choice> \
  --reason <reason> --actor <actor> --apply
```

P0 cannot be deferred. Each declared review-grant class may be used at most once. Applied decisions are included in subsequent reviewer context and never masquerade as reviewer approval.

If `RETURN_TO_FIX`, `DEFER_ELIGIBLE_P1`, or `RESUME_WITH_DECISION` is applied after the batch has
exhausted every then-available ordinary quality round, the engine records exactly one closeout
authorization. `RETURN_TO_FIX` requires a new commit; deferral or a decision-only resume may retain
the same SHA. The first paid attempt binds the authorization to exact HEAD, while infrastructure,
malformed output, and `NEEDS_VERIFICATION` leave it retryable only at that SHA. The first valid quality
result consumes it. PASS permits acceptance; any other effective result offers only `SUPERSEDE_RUN`
or `ABORT_RUN`, never another fix/defer/review loop. This recovery is a route back to independent review,
not user-authored PASS or completion authority.

Runs created before the finding ledger existed receive a narrow compatibility path. A resolved ID absent from the ledger is tolerated only when a same-batch, pre-policy formal report proves that ID existed; the policy records `LEGACY_UNTRACKED_RESOLUTION` and does not fabricate a fingerprint or ledger entry. Such a stranded batch may use exactly one fifth recovery review. Unknown IDs without that evidence remain `REVIEWER_ERROR`, leave state unchanged, and do not consume a valid round.

Do not repair deferred findings by default. Consider a P2 only when the change is local, clearly low risk, inside scope, and does not require another review cycle. Preserve all other deferred findings in the final report.

## Execute reviewer-requested verification

The reviewer may return `NEEDS_VERIFICATION` with argv-based requests. The workflow records them without consuming a valid review round. Preview the exact command before execution:

```bash
<controller-python> <skill-root>/scripts/workflow.py verify \
  --project <absolute-project-root> --run-id <run-id> \
  --request-id <request-key>
```

After explicit approval, repeat with `--apply`. The default `--network-policy offline` asks bwrap for a private network namespace. If the host cannot provide it, the result is a retryable infrastructure error; do not silently downgrade. Host networking requires an exact-request authorization: either let `verify --network-policy host --apply` create a typed `VERIFICATION_NETWORK_ACCESS` decision and resolve it through `adjudicate`, or supply both `--network-reason <reason>` and `--actor <actor>` to record a direct human authorization. An authorization never applies to another request.

Three infrastructure failures exhaust the normal request budget. The workflow then exposes one bounded `GRANT_ONE_VERIFICATION_ATTEMPT` decision; it never consumes a quality-review round. Required final COMMAND criteria cannot be rejected or waived. If their evidence cannot be obtained, the only safe terminal choices are to supersede or abandon the run.

The sandbox uses an environment allowlist, hides the host home, supplies private HOME/TMP locations,
exposes the fixed-SHA worktree read-only at `/mnt`, and read-only mounts an explicitly selected
interpreter runtime when it lives below the host home. Verification commands must place generated
data in `$TMPDIR`, not mutate source files. The sandbox does not inherit SSH, cloud, proxy, or token
variables. stdout/stderr are size-limited, mode 0600, hashed, and copied with the evidence manifest
into the next reviewer context.

Git-ignored environments do not appear in implementation, reviewer, or verification worktrees. This
is expected, not missing source. For a project-relative command whose executable normalizes beneath
`.venv/`, the verifier resolves the launcher from `<project>/.venv`, mounts that environment and its interpreter chain read-only, and
runs it with the fixed-SHA worktree as the current directory. A reviewer manually entering its detached
worktree therefore cannot rerun `.venv/bin/python ...` directly and must not classify that absence as a
code failure; use the harness evidence instead.

The same rule is enforced as a mechanism, not left to reviewer judgment: a COMMAND criterion or
verification request whose executable is a bare python/pypy name (`python`, `python3`, `python3.x`,
`pypy`) is refused at contract and request validation, because the sandbox resolves it to a system
interpreter that never sees the project environment, so any project-dependency command is guaranteed
to fail with a module-missing error that is not a code failure. Use `.venv/`-relative launchers or an
explicit absolute interpreter path. When a non-`.venv/` python interpreter still produces a
module-missing failure, the verifier records it as a retryable infrastructure error, never a quality
FAIL, and does not consume a quality-review round.

Preflight reports whether `<project>/.venv/bin/python` exists and whether a uv project is detectable.
The run fingerprints `.venv` statically: it does not execute that interpreter, so `.pth` startup code
cannot escape the verification sandbox through a metadata probe. The `.venv` root cannot be a symlink;
all other symlinks contribute only their stored link text, never content from a resolved target. File
modes are included, and file, byte, and time ceilings bound the scan. Ordinary commands compare bounded
metadata while evidence-producing verification compares full in-venv file content both before and after
the command, because a read-only bind mount is not a content snapshot. The interpreter installation
reached through a launcher symlink remains an explicitly reported host trust root rather than causing
the controller to read arbitrary external files; use an immutable toolchain when that runtime also needs attestation.
When the environment is absent, the verifier fails with an infrastructure diagnostic before launching
bubblewrap. It never runs `uv sync` or installs dependencies silently. If the repository uses uv, prepare
the environment explicitly with its locked, documented workflow before verification. Any future automated
provisioning must be an explicit apply operation that records the uv version, lock digest, interpreter, and
network authorization in run-owned state.

If a request is unsafe, unnecessary, or outside authority, preview and apply `--reject-reason <reason>` instead. Rejection is recorded and returned to the reviewer; it is not treated as passing evidence. Verification infrastructure/request cycles do not consume code-review rounds.

## Accept a batch

After `REVIEW_PASS` and required tests pass:

```bash
<controller-python> <skill-root>/scripts/workflow.py accept \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --batch <batch-id> \
  --review-file <absolute-report-path>
```

Only the latest registered `PASS` report for the current clean implementation HEAD can authorize acceptance. The report must contain a PASS result for every batch criterion; COMMAND results must cite current-SHA PASS evidence IDs. Continue with the next declared batch unless the user requested per-batch confirmation.

## Supersede an obsolete run

When the user chooses to abandon an obsolete run:

```bash
<controller-python> <skill-root>/scripts/workflow.py supersede \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

This preserves its branch, worktrees, reports, and state for audit. It does not affect any sibling run.

## Final verification and integration

After the last batch is accepted, the workflow automatically schedules every COMMAND criterion again against the final cumulative SHA. Run each returned request through `verify`. A failure reopens the last batch; all PASS results produce `final_verification=PASS`. If the contract contains no executable criteria, the state records an explicit `NOT_APPLICABLE` reason rather than pretending tests ran. Preview finalization only after this gate closes:

```bash
<controller-python> <skill-root>/scripts/workflow.py finalize \
  --project <absolute-project-root> \
  --run-id <run-id>
```

The preview compares the target ref with the candidate's expected target SHA. An unchanged target may
fast-forward directly. A changed target returns `RECONCILIATION_REQUIRED` without changing execution
state or invalidating the reviewed candidate.

When the target is the original checkout's current branch, preview reports its current porcelain changes
and whether they will block apply. Clean or commit that user-owned work deliberately before integration;
the workflow never stashes or discards it.

For a diverged target, preview and explicitly create a run-owned merge worktree:

```bash
<controller-python> <skill-root>/scripts/workflow.py reconcile \
  --project <project> --run-id <run-id>
<controller-python> <skill-root>/scripts/workflow.py reconcile \
  --project <project> --run-id <run-id> --apply
```

The command records a `PREPARING` transaction before creating Git resources, then uses
`--no-ff --no-commit`. If the process stops after worktree creation, rerun the same command: it validates
and resumes the recorded worktree instead of creating another branch. Once an attempt is ready or
conflicted, every rerun revalidates its registered worktree, branch, merge authority, and observed Git
state before returning that same attempt. Resolve conflicts in the returned
worktree, run appropriate tests, and commit the merge. Then register it:

```bash
<controller-python> <skill-root>/scripts/workflow.py submit-reconciliation \
  --project <project> --run-id <run-id> --attempt-number <number>
```

Submission validates that both the observed target and reviewed source are ancestors, then appends a
synthetic `INTEGRATION_NN` batch. Review, verify, and accept that batch through the ordinary fixed-SHA
workflow. This reuses the finding ledger and bounded review policy instead of inventing a weaker gate.
To discard an active preparation without deleting its evidence, preview and apply
`abandon-reconciliation --attempt-number <number> --reason <reason> --actor <actor>`. Only then may
`reconcile` mint a new numbered attempt; the abandoned branch and worktree remain auditable.

Schema migration authenticates the legacy event chain and final checkpoint before translating any
field. A schema-number downgrade cannot convert edited state into trusted current-schema state. Migration
backups are reusable only when byte-identical to the still-current legacy state, allowing an interrupted
migration to resume without accepting an unrelated backup.
Schema v11 requires explicit migration before a schema-v10 run adopts decision- and SHA-bound closeout
review recovery; migration preserves its frozen reviewer templates and prior review evidence and initializes
an empty closeout-grant ledger rather than inventing an authorization.
Schema v10 adds explicit migration before a schema-v9 run adopts ancestry-bound delta review
transport; it preserves the run's frozen reviewer templates and earlier review evidence. Schema v9
validates the exact run-local paths, regular-file type, and SHA-256 digests of both
reviewer-template snapshots and every explicitly frozen supplementary instruction before any active
transition. Missing, redirected, or edited snapshots fail closed. Schema-v8 runs require an explicit
migration to the current schema; that migration preserves their reviewer templates and records an
empty supplementary instruction set rather than inventing authority.

After approval:

```bash
<controller-python> <skill-root>/scripts/workflow.py finalize \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

Finalization requires final verification and review for the accepted SHA. If the target is the current
checkout it must be clean and is fast-forwarded normally. If it is not checked out anywhere, the engine
uses an atomic `update-ref` with the expected old SHA. It never switches branches. A target that moves
again requires another reconciliation attempt; prior attempts remain auditable.

Finalization first validates checkout preconditions, then persists a `FINALIZING` transaction containing
the expected target SHA before advancing the branch. If Git advanced before the state checkpoint, rerun
`finalize` to recover to `FINALIZED`. If the target moved before Git advanced, `finalize --apply`
auditably aborts the preparation, restores `READY_TO_FINALIZE`, and routes to reconciliation.

## Cleanup

After finalization or explicit supersession, preview and then remove registered worktrees:

```bash
<controller-python> <skill-root>/scripts/workflow.py cleanup \
  --project <absolute-project-root> \
  --run-id <run-id>

<controller-python> <skill-root>/scripts/workflow.py cleanup \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

Cleanup preserves the plan snapshot, state, reviews, logs, tests, final report, branch, and commit history. Never delete the centralized directory before Git worktrees are unregistered.

## Final response

Report the project identity, run ID, plan digest, implementer and reviewer, baseline and final SHA,
working-tree changes excluded at initialization, frozen supplementary instruction paths and digests,
batches accepted, review rounds, verification results, remaining P2 findings, integration status,
report location, and whether worktrees remain registered.
