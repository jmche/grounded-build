# Planning workflow details

Ordinary runs should follow the `next_action` returned by `plan_workflow.py`. Read this reference for recovery, adjudication, or diagnosis.

## Topology

| Backend | Host adapter | A | B | Adapter diversity |
|---|---|---|---|---|
| `auto` | dsh or claude | host | codex when available | varies |
| `auto` | codex | codex | claude, then dsh, when available | varies |
| `auto` | other | other | codex, then claude, then dsh, when available | varies |
| `auto`, preferred external unavailable | any supported host | host | host | false |
| `auto`, no host binding (legacy) | unset | first available | second available | varies |
| `claude`, `codex`, `dsh`, or `other` | any | selected adapter | selected adapter | false |

Host-aware `auto` prefers Codex as the external reviewer for a Claude or dsh host. A Codex host
prefers Claude, then dsh. An `other` host prefers Codex, then Claude, then dsh. Initialization performs
static checks and a real mounted-file read with the exact runtime about to be frozen; it selects the
first usable candidate and falls back to the host after every external candidate fails. An explicit
peer must pass the same check or initialization fails. `final_reviewer=auto` always selects the current
host adapter and invokes it as a fresh isolated process. The checks and rejected candidates
are frozen in `selection_checks`, making an installed but unusable higher-priority adapter observable
without treating it as absent. Runs without
`--host-adapter` retain
the legacy `claude -> dsh -> codex` ordering for compatibility.

Separate processes, fresh sessions, isolated context, a detached read-only worktree, and audited output provide process independence. Two instances of one adapter using the same model are not model-diverse. A Codex adapter configured for DeepSeek remains adapter `codex`, model family `deepseek`; the dsh adapter reports the model selected in the harness settings.

Cross-review is mutual: A reviews B and B reviews A. Every same-round A/B pair is launched concurrently
from one frozen barrier input; a finisher is merged atomically and cannot alter the peer's already frozen
context. `final_reviewer=both` independently reviews the same synthesized candidate through both slots.
`final_reviewer=auto` uses the host adapter as a fresh logical reviewer F.

Planning defaults to Opus through Claude, `gpt-5.6-sol` through Codex, and the harness-saved model through
dsh (`~/.dsh/settings.yaml`). Explicit model, provider, or profile selection wins; `cli-default` defers model
choice to the adapter. Initialization freezes hashes of the engine,
skill contract, and this reference. Each invocation records the requested runtime, redacted actual argv,
context hashes, wall time, reported cost/turns when available, and terminal delivery class.
If a skill upgrade changes those hashes during a run, ordinary commands reject the drift. Preview and explicitly
apply `migrate-engine --reason <reason> --actor <actor> --apply` to continue under the new semantics; the decision
preserves both engine identities and does not rewrite earlier invocation records.

### Generic `other` bridge

`other` is one protocol adapter for hosts such as Pi or OpenCode; it is not a growing list of
agent-specific branches. Bind the current host with an absolute executable path:

```bash
export GROUNDED_BUILD_OTHER_COMMAND=/absolute/path/to/grounded-build-other-bridge
```

The controller calls `<bridge> --version`, then `<bridge> capabilities`. Capabilities must exit zero
and emit one JSON object containing these exact bindings (additional diagnostic fields are allowed):

```json
{
  "protocol": "grounded-build-other-v1",
  "read_only": true,
  "structured_output": true,
  "fresh_process": true
}
```

Each planning or review call executes a newly started bridge process with this argv contract:

```text
<bridge> run --protocol grounded-build-other-v1
  --workspace <read-only-frozen-worktree>
  --context <read-only-invocation-context>
  --schema <read-only-json-schema>
  --output <single-writable-output-file>
  --prompt <read-only-prompt-file>
  --web <enabled|disabled>
```

The bridge must start a fresh host-agent session, constrain its repository access to `--workspace`
and `--context`, honor `--web`, and atomically write exactly one JSON object matching `--schema` to
`--output`. A successful exit without that object is a delivery failure. Grounded Build runs the
bridge inside the same bubblewrap boundary as built-in adapters, with a private HOME/TMP, sanitized
environment, read-only repository and context, and only `--output` writable. Therefore the bridge
must be self-contained or use dependencies visible in the system runtime; authentication must use a
mechanism that does not require Grounded Build to expose the operator's HOME, arbitrary credential
paths, or secret environment variables. A wrapper that cannot meet those constraints must fail its
capability declaration rather than claiming compatibility.

Initialization freezes the bridge's resolved path, SHA-256 digest, version, declaration, and a real
mounted-file read. Later invocations reject bridge digest drift. This gives every conforming host the
same auditable adapter path while leaving host-specific CLI translation outside Grounded Build.

## Evidence, scope, and priority contracts

Initialization freezes `request.md` and `scope_contract.json`. Every scope id must use a class plus a numeric
suffix, such as `TARGET-001` or `EVIDENCE_ONLY-003`; bare class names are invalid. The contract admits
`TARGET`, `REQUIRED_SUPPORT`, and `EVIDENCE_ONLY`; `PROPOSED_EXTENSION` requires a typed user decision and
`OUT_OF_SCOPE` cannot enter a candidate. A later round may deepen evidence or causal/verification coverage,
but it cannot enlarge the objective.

The initial contract authorizes only `TARGET-001`, whose authority is the digest-bound request. Support
and evidence ids refine that objective but cannot redefine it. A proposed extension is not authorized
merely because an agent labels it: after both investigations, the engine batches blocking user questions
into one `INVESTIGATION_BOUNDARY`. Each accepted extension is recorded explicitly with
`--authorize-scope-id PROPOSED_EXTENSION-NNN`; omitted proposals remain excluded. Draft and integration
outputs declare `plan_scope_ids`, and the engine rejects unauthorized extensions and `OUT_OF_SCOPE` ids.

`preflight` and `init` accept `--base-ref <ref>` (default `HEAD`) and resolve it once to a committed
SHA. A dirty source checkout is reported but does not block either command; staged, modified, and
untracked content is excluded from the detached planning worktree. Use an explicit ref when `HEAD`
is not the intended planning authority.

Both slots investigate independently before either may draft. Each draft must re-check its material repository
facts. Evidence first discovered during drafting is emitted through `new_evidence` with full provenance before
the draft may cite it. Local repository evidence is primary. With
`research_policy=authoritative-web`, investigation calls alone may consult official project documentation or
the official GitHub repository. Each external record includes its URL, retrieval time, version/tag/commit,
content digest, and supported claim. Search snippets are discovery aids only.

Evidence receipt fields are consequential. Claims, locators, retrieval/version bindings, and digests are
non-empty; digests are lowercase SHA-256 values; repository and command evidence bind
`version_or_commit` to the frozen baseline. These checks establish traceability, not semantic truth—the
authorized reviewer still judges whether the evidence supports the claim.
Repository locators are baseline-relative file paths with an optional line range, and their digest is
checked against the complete frozen file. Command and external-source digests remain producer receipts
because replaying commands or network retrieval inside a deterministic validator would change authority.

Residual questions are structured with scope id, decision owner, blocking flag, rationale, explicit
options, and evidence ids. A blocking planner-owned question is invalid because the investigator still
owns that work. Blocking user-owned questions stop before draft and are presented as one batch. Drafts
and synthesis may carry only non-blocking residual uncertainty.

Every planning stage receives `causal_analysis.md`. Use its universal production trace for material
defects and responsibility disputes, then adapt evidence to deterministic, generative, external,
human, or hybrid producers. This is one causal model with producer-aware verification, not a keyword
gate that labels a repository as an "agent project." Keep the trace proportional for direct local work.

Evidence states are `VERIFIED`, `STRONGLY_INFERRED`, `WEAKLY_INFERRED`, `UNRESOLVED`, and `REFUTED`.
Priority lanes are `STOP_THE_LINE`, `MUST_RESOLVE`, `INVESTIGATE_IF_BUDGET`, and `RECORD_ONLY`.
Rank only after the scope gate: user priority, severity, urgency, blockers/dependencies, causal leverage,
evidence strength, then effort. Verification priority and solution priority are distinct. A priority change
requires new evidence in the record.

The workflow, not an agent, fingerprints findings from normalized scope, problem, and causal chain. The
resulting `F-*` keys live in the frozen finding ledger. Integration schemas enumerate the exact keys available
at the round barrier. Integration rounds may accept or reject known keys; new discoveries must carry the
complete finding contract before receiving a stable key. Evidence-backed `finding_aliases` may identify
multiple observations of the same underlying defect without deleting provenance. Repeated local IDs,
wording similarity, or agreement alone do not establish semantic equivalence.
Every draft_02/draft_03 output must place each key from its frozen round ledger exactly once in
`accepted_finding_ids` or `rejected_finding_ids`. PASS cannot coexist with a P0/P1 review finding or a
blocking unresolved user question.

## State progression

```text
INITIALIZED/INVESTIGATING
  -> EVIDENCE_READY/DRAFTING
  -> DRAFTS_READY/CROSS_REVIEWING (two integrated draft_02 plans)
  -> [deep only] DIVERGENCE_REQUIRED/DIVERGING (two draft_03 plans)
  -> SYNTHESIS_REQUIRED
  -> [deep only] CONVERGENCE_REVIEW_REQUIRED/CONVERGENCE_REVIEWING
  -> FINAL_REVIEW_REQUIRED/FINAL_REVIEWING
  -> READY
```

Any material product boundary or exhausted bounded budget enters `NEEDS_USER_DECISION`. `ABANDONED` and `READY` are terminal.

Standard mode stops divergence after `draft_02`. Deep mode adds exactly one further divergent round and one
extra convergence review; final review is the second convergence round. Failed convergence may consume the
single bounded synthesis correction, but it cannot create an unbounded review loop. The guarantee is finite,
honest termination—not forced consensus and not a claim of absolute correctness.

The `next` command returns exactly one of:

- `RUN_AGENT_BATCH`: preview all commands, launch every listed slot concurrently, then wait at the barrier.
- `RUN_AGENT`: preview and execute the supplied argv arrays.
- `HOST_SYNTHESIS`: collect all named artifacts and author the candidate.
- `ASK_USER`: do not invoke another model until the typed decision is recorded.
- `STOP_FOR_IMPLEMENTATION_APPROVAL`: export and stop.
- `TERMINAL`: no planning work remains.

## Synthesis contract

The final plan and batch manifest must state total included/excluded scope; scope ids; numbered items, dependencies,
components, and semantics; existing budget meanings; ordered `Bxx` mapping; finite exit observations and
verification commands; compatibility, migration, rollback, recovery, risk, evidence limitations, and
dispositions of disputed proposals. Material findings use solution form: problem, evidence, root cause or
honest unresolved status, affected surfaces, recommended solution, alternatives/tradeoffs, and verification.

Host synthesis also produces `synthesis_manifest.json`. This machine-readable receipt binds the baseline,
scope digest, plan digest, and batch-manifest digest; declares plan scope ids; disposes every stable finding
exactly once; maps accepted blocking findings to batches; cites evidence for rejections; and carries no
blocking unresolved question. It does not decide whether a solution is meaningful—the final reviewer does.

`check-synthesis` catches structural errors and warns before a paid final review. It compares plan and manifest
batch sets, validates dependency references and acyclicity, flags repeatable work without numeric bounds, and
checks stable finding dispositions when run during submission. Every `Bxx:` block must use the exact labels
`Exit observation:` and `Verification:` so the deterministic check can recognize it. Its warnings do not prove
semantic quality; final reviewers still inspect repository evidence.

Submission rejects a missing or invalid synthesis manifest. Final and convergence reviewers receive it and
return a receipt bound to the exact candidate digests. The receipt covers request coverage, scope control,
evidence/root cause, dependency order, budget bounds, and batch acceptance exactly once, plus every blocking
stable finding. Only an all-PASS receipt can contribute to `READY`.

## Typed decisions

Read the decision and its stable key from `pending_decisions` in status. Show its evidence and allowed choices to the user. Pass that key as `--decision-id` when running `adjudicate` once without `--apply`, then apply the exact same decision ID, approved choice, actor, and reason. A stale preview must be refreshed rather than applied to a different pending decision.

If the controller is interrupted while an invocation record says `RUNNING`, reacquire that exact assignment through `recover-invocation`, preview it, and apply with an actor and reason. Recovery preserves the spent quality attempt and audit record. It is also the required escape before `migrate-engine` when engine drift and a stale invocation coincide; never delete or edit the record by hand.

- Investigation boundary: answer the batched questions, explicitly authorize each accepted extension id,
  and continue, or abandon. Omitted proposed extensions remain excluded.
- Planning, convergence, or final boundary: resolve and continue, or abandon. Resolving a
  convergence boundary returns to synthesis; it never treats the interrupted review as approval.
- Synthesis budget exhausted: at most one explicit extra synthesis, or abandon.
- Invocation budget exhausted: one extra invocation, reassign the assignment, resume after genuinely changed input, or abandon.
- Provider infrastructure failure: resolve the external condition and continue, reassign, or abandon. It does not consume a quality attempt.

For an automatically selected external B provider, a machine-classified rate limit is recovered
without a user decision: the engine records the failed infrastructure invocation, persistently routes
all remaining B assignments to the frozen host, marks both diversity claims false, and asks the host
to retry the same assignment. The failed call consumes no quality attempt. Explicit `--backend` or
`--peer-reviewer` choices are never replaced automatically, and an unavailable host leaves the normal
typed provider-infrastructure decision in place. Authentication, timeout, overload, startup, malformed
output, and quality failures do not trigger this fallback.

Reassignment preserves audit history and process isolation but breaks the original two-slot
diversity claim. After any reassignment, both `provider_diversity` and `model_diversity` are
reported as false; the decision record names the adapter change and the isolation that remains.
Reassignment never converts an infrastructure failure into a plan-quality verdict.

## Recovery

- Use `status` and `next`; do not repeat a completed slot.
- `status` remains readable during paid calls and reports invocation records whose terminal status is still `RUNNING`.
- Input-scoped budgets reset only when authoritative input changes.
- A delivery or deterministic contract retry is told exactly how its prior output failed, never which verdict to return.
- Final FAIL permits one evidence-driven correction by default. After exhaustion, ask the user.
- Validate frozen artifacts and worktree cleanliness before every resumed invocation.
- Cleanup unregisters worktrees through Git and preserves the audit record.
- Private per-invocation homes and temporary caches are measured and removed immediately after the CLI exits;
  prompts, stdout/stderr, results, invocation metadata, and hashes remain auditable.
- `audit-export` works for both terminal states. `ABANDONED` preserves the last candidate, reviews, resource
  totals, decisions, and terminal reason while stating that no plan was approved.
