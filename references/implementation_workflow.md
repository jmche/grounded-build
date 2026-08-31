# Implement mode contract

Execute a user-provided implementation plan without changing the original project until final integration. The current host coding agent implements and fixes the plan in an isolated worktree. A selected `claude` or `codex` CLI reviews each fixed commit in a second isolated worktree. The reviewer may use the same agent family as the host; independence means a fresh process, isolated context, separate worktree, restricted authority, and audited output—not necessarily a different vendor or model.

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
- `reviewer`: exactly `claude` or `codex`; it may match the host/implementer.
- `reviewer_runtime`: the non-secret model identity frozen at initialization. Claude defaults to Opus and
  Codex defaults to `gpt-5.6-sol`; optional `--claude-model`, `--codex-model`,
  `--codex-model-provider`, and `--codex-profile` select GPT, an
  OpenAI-compatible DeepSeek gateway, or another configured model without treating the CLI name
  as the model family.
- `fix_policy`: `ask` by default, `auto` only when explicitly requested, or `never` for review without repair.
- `target_branch`: the current branch unless the user explicitly names another branch.

The agent invoking this skill is always the implementer. Do not launch another implementer. A same-family reviewer is allowed only as a fresh reviewer CLI invocation under the workflow's isolated read-only contract; the current host conversation must never issue its own PASS.

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
<controller-python> <skill-root>/scripts/workflow.py preflight \
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
  --reviewer <claude-or-codex> \
  --fix-policy <ask-auto-or-never> \
  --batches <comma-separated-batch-ids> \
  --target-branch <branch>
```

The current host remains the implementer and is never model-overridden by this workflow. To override the
isolated reviewer's advanced default, append the same selection verified during preflight:

```bash
--claude-model <model>
--codex-model <model> --codex-model-provider <configured-provider> --codex-profile <profile>
```

Use `cli-default` for either model to defer to its CLI. These values are frozen in `reviewer_runtime`; credentials, bearer tokens, and endpoint URLs are
never copied into workflow state.

Initialization snapshots both the plan and batch manifest, records their digests, verifies that the manifest names every declared batch ID, creates a unique implementation branch and reviewer worktree, and leaves the original project's branch, HEAD, index, and files unchanged. The new run starts at `AWAITING_CONTRACT_REVIEW`.

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

If the selected reviewer becomes unavailable—for example, Codex exhausts its token quota—switch subsequent calls without restarting the run:

```bash
<controller-python> <skill-root>/scripts/workflow.py change-reviewer \
  --project <absolute-project-root> --run-id <run-id> \
  --reviewer claude --reason "Codex quota exhausted" --actor <actor>

<controller-python> <skill-root>/scripts/workflow.py change-reviewer \
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

A PASS describes one exact SHA, not the batch. If you commit again after `REVIEW_PASS` — a deferred finding fixed anyway, say — `accept` will refuse because the report no longer authorizes HEAD; run `review` again instead and the new commit gets its own round. Reviewing the same SHA twice is refused, so a PASS that still describes HEAD cannot buy a second paid review. After acceptance the contract is closed: commits beyond the accepted SHA cannot be finalized, and the choice is to reset the implementation worktree back to that SHA or to supersede the run and review the extra commits in a new one.

Read `warnings` and `decision_reasons` on every review result:

- `CROSS_BATCH_RESOLUTION` / `CROSS_BATCH_FINDING`: the reviewer referenced a finding an earlier batch owns. The observation is recorded, that batch keeps authority over its own status, and only a cross-batch P0 raises `CROSS_BATCH_MATERIAL_FINDING` for adjudication.
- `REVIEW_INVOCATION_BUDGET_EXHAUSTED`: the batch spent its reviewer calls, typically on invocations that errored without producing a review. `GRANT_ONE_REVIEW_INVOCATION` releases exactly one more, once per batch, and returns the run to the status it parked from; `DEFER_ELIGIBLE_P1` and `ABORT_RUN` remain. Both budget guards honour the grant, so the extra call really runs.
- `OBLIGATION_DRIFT:<finding-id>`: the finding's `required_outcome` or the files it names changed for the second time. The ledger keeps `original_required_outcome`, `original_location`, and the full `obligation_revisions` history. Present that history when adjudicating: a requirement restated at three sites is a widening class, not one unfixed defect, and `RETURN_TO_FIX` or `DEFER_ELIGIBLE_P1` are both legitimate answers.

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
<controller-python> <skill-root>/scripts/workflow.py adjudicate \
  --project <project> --run-id <run-id> \
  --decision-id <id> --choice <allowed-choice> \
  --reason <reason> --actor <actor>

<controller-python> <skill-root>/scripts/workflow.py adjudicate \
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
is expected, not missing source. For a command whose executable begins `.venv/`, the verifier resolves
the launcher from `<project>/.venv`, mounts that environment and its interpreter chain read-only, and
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
conflicted, every rerun returns that same attempt. Resolve conflicts in the returned
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

Report the project identity, run ID, plan digest, implementer and reviewer, baseline and final SHA, batches accepted, review rounds, verification results, remaining P2 findings, integration status, report location, and whether worktrees remain registered.
