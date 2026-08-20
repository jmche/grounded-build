# Implement mode contract

Execute a user-provided implementation plan without changing the original project until final integration. The current host coding agent implements and fixes the plan in an isolated worktree. A selected `claude` or `codex` CLI reviews each fixed commit in a second isolated worktree. The reviewer may use the same agent family as the host; independence means a fresh process, isolated context, separate worktree, restricted authority, and audited output—not necessarily a different vendor or model.

Use the bundled script for Git, worktrees, state, fixed-SHA review dispatch, and integration. The host agent owns plan interpretation, implementation, tests, and semantic repair.

## Inputs

Collect or infer:

- `plan`: an existing local plan file, resolved to an absolute path.
- `batch_manifest`: a host-authored file that explicitly declares this run's included and excluded total scope and maps every batch ID to plan work. This is execution authority, not reviewer output.
- `reviewer`: exactly `claude` or `codex`; it may match the host/implementer.
- `fix_policy`: `ask` by default, `auto` only when explicitly requested, or `never` for review without repair.
- `target_branch`: the current branch unless the user explicitly names another branch.

The agent invoking this skill is always the implementer. Do not launch another implementer. A same-family reviewer is allowed only as a fresh reviewer CLI invocation under the workflow's isolated read-only contract; the current host conversation must never issue its own PASS.

## Storage and isolation

Store all workflow data below:

```text
~/.grounded-build/implementation/
└── projects/<project-slug>-<identity-hash>/
    ├── project.json
    ├── active_run.json
    └── runs/<run-id>/
        ├── workflow.json
        ├── plan/original.md
        ├── plan/batches.md             # frozen run scope and batch mapping
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

The original project must remain on its existing branch and SHA throughout initialization, implementation, review, and repair. This protects running tasks that may continue reading scripts, skills, templates, or configuration from the original project.

## Safety boundary

- Review exact commit SHAs, never a moving branch name.
- Do not modify the original project before explicitly approved finalization.
- Do not treat reviewer process failure, malformed output, timeout, stale base, or test-infrastructure failure as code-quality `FAIL`.
- Do not claim a batch passed until the reviewer returns `PASS` for the exact implementation HEAD and required verification passes.
- Do not use `git reset --hard`, `git clean`, automatic stash, silent rebase, or automatic conflict resolution.
- Do not delete worktree directories directly; use the script's cleanup operation.
- Pause if the target branch advances after a run begins. Previously issued review approvals do not authorize code combined with a newer target.
- Use at most four valid review rounds per batch: one full discovery review and three bounded re-reviews. Reviewer infrastructure errors do not consume a round.
- Count every real reviewer invocation separately from valid quality rounds. Infrastructure failures consume invocation budget and remain auditable even though they do not consume repair budget.
- Treat `plan/original.md` and `plan/batches.md` with their recorded digests as the run authority. The manifest defines this run's total included/excluded scope and batch mapping. A later edit or move of either source is informational; a changed snapshot is corruption.
- Never overwrite a contract, review, or verification attempt. Every invocation receives a unique append-only directory and event record.
- A batch cannot be accepted while any same-SHA verification is unresolved or failing. Verification infrastructure errors remain retryable and cannot be bypassed by requesting another static review.
- Keep the reviewer response schema within the portable structural subset (`type`, `properties`, `required`, `additionalProperties`, `items`, and `enum`). Enforce uniqueness, hashes, lengths, and other semantic constraints in Python so unsupported JSON Schema keywords cannot disable every review.
- Grant reviewer CLIs only a round-specific context containing the plan snapshot, current finding ledger, and assignment metadata. Historical reviews remain audit records, not reviewer input.
- Include the frozen batch manifest in both contract-review and code-review contexts. A reviewer may challenge its boundary through `NEEDS_USER_DECISION`, but may not silently expand the run or remap batch identifiers.
- Permit implementer and reviewer to use the same agent family, but preserve process, context, worktree, permission, and output isolation. Model diversity is useful but is not the source of review authority.

## Shared review contract

Read `references/reviewer_prompt.md` before initializing or reviewing a run. It is the canonical detailed contract shared with the independent reviewer; the host must reason with the same lifecycle rather than treating reviewer output as an opaque PASS/FAIL gate.

- Every finding has a stable semantic `fingerprint` and an ID that is reused while the same defect remains.
- `novelty` is exactly one of `INITIAL_REVIEW`, `INTRODUCED_BY_FIX`, `PREVIOUSLY_MASKED`, `PRE_EXISTING`, or `UNRELATED`. `INTRODUCED_BY_FIX` requires `introduced_by_sha`; `PREVIOUSLY_MASKED` requires an explanation of why earlier detection was impossible.
- `resolved_finding_ids` may name only prior OPEN findings that the reviewed SHA verifiably resolves. A severity change requires `severity_change_justification`.
- The finding ledger owns lifecycle state: blocking findings are `OPEN`, verified repairs become `VERIFIED`, and nonblocking findings become `DEFERRED` with an explicit reason. Missing OPEN findings do not disappear merely because a later reviewer omits them.
- Legacy recovery is an evidence-preserving exception for pre-ledger reports, not permission to invent IDs or fingerprints.
- Acceptance criteria use `COMMAND`, `REPOSITORY_ASSERTION`, or `USER_BOUNDARY` evidence. Deterministic code verifies provenance, SHA, command execution, and state; the independent reviewer decides semantic sufficiency.

## Preflight and latest-code check

Before initializing, inspect the current local baseline:

```bash
python3 <skill-root>/scripts/workflow.py preflight \
  --project <absolute-project-root> \
  --target-branch <branch>
```

Report the target SHA, working-tree state, upstream, and locally known ahead/behind relation. Preflight does not access the network.

If an upstream exists and current remote state matters, ask before running `git fetch`. Fetching may update observations but must not automatically merge, rebase, or pull. After any user-approved synchronization, run preflight again and initialize only from the confirmed target SHA.

Uncommitted project changes are not part of a Git worktree baseline. Ask the user to commit or otherwise resolve them before initialization; do not silently omit, stash, or copy them.

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
python3 <skill-root>/scripts/workflow.py list \
  --project <absolute-project-root>
```

If a nonterminal active run exists, resume it or ask whether to supersede it. Never overwrite it.

Initialize a new run:

```bash
python3 <skill-root>/scripts/workflow.py init \
  --project <absolute-project-root> \
  --plan <absolute-plan-path> \
  --batch-manifest <absolute-batch-manifest-path> \
  --implementer <current-host-agent-name> \
  --reviewer <claude-or-codex> \
  --fix-policy <ask-auto-or-never> \
  --batches <comma-separated-batch-ids> \
  --target-branch <branch>
```

Initialization snapshots both the plan and batch manifest, records their digests, verifies that the manifest names every declared batch ID, creates a unique implementation branch and reviewer worktree, and leaves the original project's branch, HEAD, index, and files unchanged. The new run starts at `AWAITING_CONTRACT_REVIEW`.

The run also freezes its quality-round, reviewer-invocation, contract-invocation, verification-attempt, and evidence-cycle budgets. Changing a global constant later does not silently change an existing run.

## Review the acceptance contract

Before implementation, run:

```bash
python3 <skill-root>/scripts/workflow.py contract-review \
  --project <absolute-project-root> \
  --run-id <run-id>
```

The reviewer considers host criteria, the complete plan, the frozen run scope/batch mapping, and repository contracts. `CONTRACT_READY` stores an immutable effective contract with its own digest and permits implementation. The contract is a minimum obligation, not a whitelist of possible defects. Excluded work reserved for a future run is not an unmet criterion in the current run.

A READY contract must cover every declared batch. Each criterion states an evidence kind. `COMMAND` criteria provide argv and expected exit and are automatically scheduled for fixed-SHA execution; repository assertions remain semantic reviewer judgments.

When it returns `NEEDS_USER_DECISION`, present the issues and proposed observations to the user. Record the chosen boundary in a JSON decision file containing `reason` and normalized `criteria`, preview it, then apply it:

```bash
python3 <skill-root>/scripts/workflow.py contract-adjudicate \
  --project <absolute-project-root> --run-id <run-id> \
  --decision-file <absolute-json-path>

python3 <skill-root>/scripts/workflow.py contract-adjudicate \
  --project <absolute-project-root> --run-id <run-id> \
  --decision-file <absolute-json-path> --apply
```

The decision is preserved under `decisions/`; it does not masquerade as reviewer approval.

Read the returned `run_id` and `implementation_worktree`. Perform every implementation edit and test with that worktree as the working directory.

## Resume a run

Inspect the active run:

```bash
python3 <skill-root>/scripts/workflow.py status \
  --project <absolute-project-root>
```

Inspect a historical run explicitly:

```bash
python3 <skill-root>/scripts/workflow.py status \
  --project <absolute-project-root> \
  --run-id <run-id>
```

Resume only from its recorded phase, batch, round, and SHAs. If status reports `STALE`, the target branch has advanced since initialization. Ask the user to choose:

- supersede the old run and start a new run from the latest target; recommended for a new or substantially changed plan;
- leave the run unchanged for audit.

The current script deliberately does not merge or rebase a stale run. Reconciliation changes the review base and invalidates earlier authority, so it is represented by superseding the old run and initializing a new one from the current target rather than mutating history in place.

If status reports `MIGRATION_REQUIRED`, preview and explicitly apply the migration before any mutation:

```bash
python3 <skill-root>/scripts/workflow.py migrate --project <project> --run-id <run-id>
python3 <skill-root>/scripts/workflow.py migrate --project <project> --run-id <run-id> --apply
```

Migration preserves a mode-0600 backup of the prior state, records its digest, labels unverifiable legacy information, and appends a migration event. It never silently invents missing evidence.

## Implement each batch

For the next pending batch:

1. Read that batch from the plan snapshot.
2. Inspect governing repository instructions and all affected producers, consumers, failure paths, recovery paths, and compatibility paths.
3. Implement only that batch in `implementation_worktree`.
4. Add focused tests and run required verification there.
5. Commit all intended changes and leave the implementation worktree clean.
6. Request review.

Use small logical commits. The reviewer evaluates the cumulative batch range from the prior accepted SHA through the current implementation HEAD.

## Run an independent review

```bash
python3 <skill-root>/scripts/workflow.py review \
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

If the selected reviewer becomes unavailable—for example, Codex exhausts its token quota—switch subsequent calls without restarting the run:

```bash
python3 <skill-root>/scripts/workflow.py change-reviewer \
  --project <absolute-project-root> --run-id <run-id> \
  --reviewer claude --reason "Codex quota exhausted" --actor <actor>

python3 <skill-root>/scripts/workflow.py change-reviewer \
  --project <absolute-project-root> --run-id <run-id> \
  --reviewer claude --reason "Codex quota exhausted" --actor <actor> --apply
```

Preview before applying. The new reviewer may match the implementer, but it still runs as a fresh isolated CLI reviewer. Switching preserves the current workflow phase, finding ledger, accepted evidence, quality rounds, verification state, and invocation usage; it never turns the prior infrastructure failure into code `FAIL` and never resets budget.

## Fix loop

For `REVIEW_FAIL`:

- `ask`: summarize P0/P1 findings and ask whether the host should fix them.
- `auto`: fix them while changes remain inside the approved plan and progress continues.
- `never`: report findings and stop.

Pause despite `auto` when a fix expands scope, conflicts with the plan, changes a destructive migration decision, requires new authority, touches unexplained user changes, or two consecutive rounds do not reduce unresolved P0/P1 findings.

Fix in `implementation_worktree`, commit, and call `review` again for the same batch. The script requires a new fix commit after `REVIEW_FAIL` and reuses the reviewer worktree at the new SHA.

Convergence policy:

- Round 1 is the complete discovery review.
- Rounds 2–4 verify prior findings and repair regressions; they do not restart an unlimited architecture review.
- P0 always blocks.
- Evidence-backed, in-scope P1 blocks. On later rounds a newly discovered P1 blocks only when introduced by a fix or previously masked.
- P2, pre-existing findings, unrelated findings, and late nonqualifying P1 findings are recorded as deferred and do not block PASS.
- Finding IDs and semantic fingerprints remain stable across rounds. Missing prior OPEN findings remain open unless the reviewer explicitly verifies their resolution.
- A severity upgrade from deferred to blocking requires user adjudication.
- Two consecutive rounds without reducing effective blocking findings return `NEEDS_USER_DECISION`.
- Round 4 is the final adjudication round. If blocking findings remain, do not auto-fix again; return `NEEDS_USER_DECISION`.

`NEEDS_USER_DECISION` is a typed state, not prose. Read `pending_decision` from status, show its allowed choices, and preview/apply the selected choice:

```bash
python3 <skill-root>/scripts/workflow.py adjudicate \
  --project <project> --run-id <run-id> \
  --decision-id <id> --choice <allowed-choice> \
  --reason <reason> --actor <actor>

python3 <skill-root>/scripts/workflow.py adjudicate \
  --project <project> --run-id <run-id> \
  --decision-id <id> --choice <allowed-choice> \
  --reason <reason> --actor <actor> --apply
```

P0 cannot be deferred. A batch may receive at most one explicit extra quality review. Applied decisions are included in subsequent reviewer context and never masquerade as reviewer approval.

Runs created before the finding ledger existed receive a narrow compatibility path. A resolved ID absent from the ledger is tolerated only when a same-batch, pre-policy formal report proves that ID existed; the policy records `LEGACY_UNTRACKED_RESOLUTION` and does not fabricate a fingerprint or ledger entry. Such a stranded batch may use exactly one fifth recovery review. Unknown IDs without that evidence remain `REVIEWER_ERROR`, leave state unchanged, and do not consume a valid round.

Do not repair deferred findings by default. Consider a P2 only when the change is local, clearly low risk, inside scope, and does not require another review cycle. Preserve all other deferred findings in the final report.

## Execute reviewer-requested verification

The reviewer may return `NEEDS_VERIFICATION` with argv-based requests. The workflow records them without consuming a valid review round. Preview the exact command before execution:

```bash
python3 <skill-root>/scripts/workflow.py verify \
  --project <absolute-project-root> --run-id <run-id> \
  --request-id <request-key>
```

After explicit approval, repeat with `--apply`. The default `--network-policy offline` asks bwrap for a private network namespace. If the host cannot provide it, the result is a retryable infrastructure error; do not silently downgrade. Host networking requires an exact-request authorization: either let `verify --network-policy host --apply` create a typed `VERIFICATION_NETWORK_ACCESS` decision and resolve it through `adjudicate`, or supply both `--network-reason <reason>` and `--actor <actor>` to record a direct human authorization. An authorization never applies to another request.

Three infrastructure failures exhaust the normal request budget. The workflow then exposes one bounded `GRANT_ONE_VERIFICATION_ATTEMPT` decision; it never consumes a quality-review round. Required final COMMAND criteria cannot be rejected or waived. If their evidence cannot be obtained, the only safe terminal choices are to supersede or abandon the run.

The sandbox uses an environment allowlist, hides the host home, supplies private HOME/TMP locations, exposes the fixed-SHA worktree read-only at `/mnt`, and read-only mounts an explicitly selected interpreter runtime when it lives below the host home. Verification commands must place generated data in `$TMPDIR`, not mutate source files. The sandbox does not inherit SSH, cloud, proxy, or token variables. stdout/stderr are size-limited, mode 0600, hashed, and copied with the evidence manifest into the next reviewer context.

If a request is unsafe, unnecessary, or outside authority, preview and apply `--reject-reason <reason>` instead. Rejection is recorded and returned to the reviewer; it is not treated as passing evidence. Verification infrastructure/request cycles do not consume code-review rounds.

## Accept a batch

After `REVIEW_PASS` and required tests pass:

```bash
python3 <skill-root>/scripts/workflow.py accept \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --batch <batch-id> \
  --review-file <absolute-report-path>
```

Only the latest registered `PASS` report for the current clean implementation HEAD can authorize acceptance. The report must contain a PASS result for every batch criterion; COMMAND results must cite current-SHA PASS evidence IDs. Continue with the next declared batch unless the user requested per-batch confirmation.

## Supersede an obsolete run

When the user chooses to abandon an active or stale run in favor of the latest project code:

```bash
python3 <skill-root>/scripts/workflow.py supersede \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

This preserves its branch, worktrees, reports, and state for audit but releases the active-run slot. Initialize a new run afterward; it will use the then-current target SHA rather than any old worktree SHA.

## Final verification and integration

After the last batch is accepted, the workflow automatically schedules every COMMAND criterion again against the final cumulative SHA. Run each returned request through `verify`. A failure reopens the last batch; all PASS results produce `final_verification=PASS`. If the contract contains no executable criteria, the state records an explicit `NOT_APPLICABLE` reason rather than pretending tests ran. Preview finalization only after this gate closes:

```bash
python3 <skill-root>/scripts/workflow.py finalize \
  --project <absolute-project-root> \
  --run-id <run-id>
```

The preview must confirm that the original target branch still equals the recorded baseline and that fast-forward integration is possible. Ask for explicit user approval and confirm that changing the original project is safe for any running tasks.

After approval:

```bash
python3 <skill-root>/scripts/workflow.py finalize \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

Finalization requires the original project to be clean and checked out on the recorded target branch, the final verification record to belong to the final accepted SHA, and then performs `git merge --ff-only`. It never switches branches automatically. Divergence stops the operation; supersede this run and initialize a new run from the new target so the changed base receives a fresh cumulative review.

Finalization first persists a `FINALIZING` transaction and only then advances the target branch. If the process stops after Git advances but before the final state checkpoint, rerun `finalize` to preview and apply the audited recovery to `FINALIZED`.

## Cleanup

After finalization or explicit supersession, preview and then remove registered worktrees:

```bash
python3 <skill-root>/scripts/workflow.py cleanup \
  --project <absolute-project-root> \
  --run-id <run-id>

python3 <skill-root>/scripts/workflow.py cleanup \
  --project <absolute-project-root> \
  --run-id <run-id> \
  --apply
```

Cleanup preserves the plan snapshot, state, reviews, logs, tests, final report, branch, and commit history. Never delete the centralized directory before Git worktrees are unregistered.

## Final response

Report the project identity, run ID, plan digest, implementer and reviewer, baseline and final SHA, batches accepted, review rounds, verification results, remaining P2 findings, integration status, report location, and whether worktrees remain registered.
