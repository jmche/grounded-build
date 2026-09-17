# Changelog

All notable changes use semantic versioning.

## [0.7.8] - 2026-09-17

- Made the final integration review its own stage (`review --batch FINAL`) instead of a property
  of the last plan batch. Every plan batch, including the last one, is now reviewed from the
  previously accepted SHA against its own criteria; the final review runs once after the last
  acceptance, covers `baseline..HEAD` and every criterion, and repairs in ordinary `DELTA` rounds.
  Folding the cumulative review into every round of the last batch had made each of those rounds
  a full baseline review that re-judged accepted batches (117-132 KB patches, 13 criteria, and
  cross-batch warnings on every round of one observed run). `accept --batch FINAL` records the
  final verification from the current-SHA command evidence the review already cited; a command that
  passed at exact HEAD is never paid for twice, and supplied evidence is bound to the SHA rather than
  to the requesting stage. Runs that already scheduled the older final verification finish on that
  path.
- Gave Implement review findings one shape for the reviewer, the validator, and the ledger: `id`,
  `fingerprint`, `severity`, `novelty`, `location`, `required_outcome`, `details`. The provider
  schema is now derived from that shape instead of restated, so the prompt can no longer name a
  field the wire cannot carry. 0.7.5 had removed `introduced_by_sha` and
  `why_not_detectable_earlier` from the wire while the validator still demanded them, which made
  `INTRODUCED_BY_FIX` and `PREVIOUSLY_MASKED` unreportable: a truthful regression report was
  rejected as a contract error, and the only reportable label deferred the regression as a late
  discovery. A test now proves every `novelty` value is deliverable under the strict schema.
- Stopped asking the reviewer for facts the controller already owns. The reviewed SHA and round
  are bound to every finding at receipt; `introduced_by_sha` is retired.
- Froze each finding's `required_outcome` at its first statement. Restatements are recorded as
  `obligation_revisions` with the latest wording in `latest_required_outcome`, and the finding is
  judged against the frozen text; a wider obligation is a new finding. The `OBLIGATION_DRIFT`
  decision and its path-set comparison are retired: whether a restatement widened the obligation is
  a semantic judgement, and the machine had also flagged a shrinking file set as drift. The
  no-progress and round-cap rules still bound the loop.
- Retired `severity_change_justification` as a machine gate. A severity change is recorded in
  `severity_history`; the reviewer's reason lives in `details` and is judged by the user. A
  downgrade to P2 records `deferred_reason: SEVERITY_DOWNGRADE:<from>->P2` and a
  `SEVERITY_DOWNGRADE` warning, so a batch never unblocks without a visible receipt.
- Review results now report ledger-bound findings: each carries the ledger `status`, the frozen
  `required_outcome`, and `latest_required_outcome`, so the implementer's primary channel shows
  the authoritative close condition rather than the round's restatement.
- A finding delivered without `required_outcome` no longer discards the review. It is disclosed
  as `FINDING_CLOSE_CONDITION_MISSING`, warned as `FINDING_WITHOUT_CLOSE_CONDITION`, and the
  ledger adopts the first non-empty statement; the controller never invents a close condition.

## [0.7.7] - 2026-09-17

- Restored strict-compatible Implement reviewer delivery: closed finding objects use a minimal
  semantic `details` field, and lifecycle arrays remain explicit in the provider schema.

## [0.7.6] - 2026-09-16

- Added audited Implement `migrate-engine` recovery for post-update engine drift. Active
  invocations are refused, frozen artifacts remain intact, and current-SHA PASS results require
  fresh review under the migrated engine before acceptance.

## [0.7.5] - 2026-09-16

- Reduced Implement reviewer delivery to a small lifecycle envelope. Optional semantic finding
  prose and empty collections are normalized at the workflow boundary instead of invalidating a
  usable review for mechanical omissions.

## [0.7.4] - 2026-09-16

- Added repeated `init --attach <path>` for frozen, SHA-256-bound external review material. Attachments
  are mounted read-only in every planning context through a generated manifest; original host paths are
  never used as agent-readable locations.
- Added advisory disclosure for absolute request paths outside readable roots and a distinct
  `MATERIAL_UNREADABLE` diagnostic that preserves the blocking contract and gives the actionable
  `--attach` recovery.

## [0.7.3] - 2026-09-15

- Kept `bash` enabled in the DSH review sandbox. The evidence contract requires a content digest of
  the complete baseline file, and a slot that may not run a command cannot produce one: both slots
  answered that required field with all-zero placeholders and were rejected. The capability probe
  now composes its disable entry from `tool-pwsh` instead.
- Replaced the blanket "do not use external network sources" instruction with a REGISTRATION
  requirement. With `authoritative-web`, external material is allowed and must appear as its own
  evidence entry (OFFICIAL_DOCS/OFFICIAL_GITHUB with URL, retrieval time, version/tag/commit and
  digest); a `local-only` run keeps its claims in the frozen worktree and marks anything drawn from
  outside it UNRESOLVED instead of presenting it as a repository fact.
- Accepted honest `UNRESOLVED` evidence without a digest, and rejected all-zero placeholder digests
  with the exact command to run (`sha256sum <path>` inside the frozen worktree).
- Named the frozen worktree path and the digest command in the investigation prompt, so a slot does
  not have to discover where the baseline is mounted.
- Persisted the delivered payload (`payload.json`) and the adapter's own raw output BEFORE any
  contract check. A rejected delivery used to keep only `stdout.log`, which is the one case a
  diagnosis most needs.
- Restricted `REASSIGN_ASSIGNMENT` to adapters the run itself recorded. The candidate list was "any
  installed provider", so a rate-limited peer could be moved onto a provider the run never selected.
- Surfaced `delivery_faults` in the `status` and `next` payloads, so the cause of a rejection
  travels with the run state instead of only living in the invocation record.
- Clarified the adapter authority rule in `SKILL.md` — the run's recorded selection is the
  authority, a blocked default is never authorization, and a fallback target is the adapter the run
  recorded — and rewrote the `--peer-reviewer` / `--final-reviewer` error messages to state the rule
  instead of only the missing argument.

- Made the evidence digest an ENGINE fact (adjudicated as d2). The digest of a cited baseline file
  is computed and recorded by the engine; a digest the agent supplies is an optional cross-check.
  A value that differs is recorded as `evidence_digest_disclosures` (surfaced in `status`/`next`)
  instead of rejecting the delivery, and the record always carries the engine's value — so a
  fabricated digest cannot reach a plan, and a model that cannot run a hash is no longer pushed into
  inventing one to pass. Live evidence for the change: a slot answered 39 required digest fields with
  a constructed sequence (`00019f2c…`, `00029f2c…`, … sharing a 60-character suffix), and an earlier
  attempt copied the prompt's own `scope_digest` into the field.

- Stated the wire vocabularies in PROSE in the investigation and draft prompts. A live slot put
  `root_cause_status`'s value `PROVEN` into `evidence_status` — two enums in one schema, similar
  names, different sets — and the schema's own complaint (`value is not in enum`) never said which
  values were allowed. The complaint now names them, and the prompt says plainly that similar field
  names do not share values.

- Stopped spending paid retries on wire-shape details that carry no risk. Two live deliveries were
  rejected for facts the engine already holds or that nothing reads: a slot added an undefined field
  (`causal_chain_note`), and a slot used evidence ids without its slot prefix. Undefined fields are
  now IGNORED and recorded (`schema_disclosures`), with the values captured; a missing slot prefix is
  STAMPED by the engine with every reference rewritten in the same pass and recorded
  (`id_normalizations`). Both are surfaced in `status`/`next`. The strict behaviour is unchanged for
  callers that do not collect disclosures — a host or a person can simply fix the shape.

- Resolved the locators models actually write. A live draft cited
  `scripts/a.py:1-5; scripts/b.py:390-490; …` and the delivery was rejected for not being "one file
  per entry". The engine now resolves every file a locator names: the first existing one anchors the
  digest, the rest are recorded in `locator_disclosures` (with the digest-bound path) so a reader
  still sees everything the claim rests on. An entry naming nothing resolvable is still rejected —
  it would have no verifiable anchor at all.

- Handed the final reviewer the blocking set it must check. The receipt has to name every P0/P1
  stable finding exactly once, while the reviewer's context listed dispositions WITHOUT severity, so
  a live final review was rejected for a set it could not derive. The context now carries
  `blocking_findings.json` (`required_blocking_finding_ids` plus `severity_by_id`), the prompt names
  it as the exact set to return, and the complaint reports what was missing, unexpected or repeated.

## [0.7.2] - 2026-09-15

- Made planning evidence receipts consequential: required evidence, root-cause traces, repository
  facts, and residual questions can no longer satisfy the workflow with empty declarations or
  unbound digests.
- Added a pre-draft user boundary for blocking investigation questions and explicit authorization
  for proposed scope extensions; unresolved planner-owned questions cannot be handed downstream.
- Required every integrated draft to declare its plan scope, disposition the complete frozen
  finding ledger, and keep PASS consistent with blocking findings and unresolved decisions.
- Added a digest-bound synthesis manifest with complete finding dispositions and a final-review
  criteria receipt bound to the exact candidate, scope, and blocking ledger.
- Bound parallel review merging to all three candidate artifacts and the submission round.
  Explicit exclusion explanations remain valid; included scope is declared structurally and
  checked semantically by reviewers. Legacy completed plans migrate to re-synthesis and fresh
  review, retaining historical final artifacts and one bounded migration submission when needed.
- Archived incompatible partial legacy investigations before requesting fresh structured outputs.
  Deep-round blocking questions now pause for a recorded user decision and resume the unfinished
  round with that decision in context.

## [0.7.1] - 2026-09-14

- Closed the exhausted-review liveness gap: a nonterminal typed fix, deferral, or decision-only
  resume applied at the quality-round boundary now authorizes one audited closeout review bound to
  the decision, batch, and exact implementation SHA. Infrastructure and verification detours do not
  consume it; one valid non-PASS result terminates the repair loop without fabricating approval.
  Implementation state advances to schema v11 so schema-v10 runs adopt this route only through an
  explicit evidence-preserving migration.
- Made review triage explicit and deterministic: findings are returned in P0, P1, P2 consequence
  order, P2 remains constructive and nonblocking, and preference-only advice stays outside the
  finding ledger.
- Removed dead invocation-budget choices: the one extra reviewer call is advertised only while it
  remains available, deferral is not presented as call-capacity recovery, and exhausted runs expose
  only auditable supersession or abandonment.

## [0.7.0] - 2026-09-10

- Added the generic `grounded-build-other-v1` bridge for Pi, OpenCode, and other host agents without
  adding per-agent branches to the workflow engines. The bridge path, executable digest, version,
  capability declaration, and real mounted-file probe are frozen and audited.
- Made host-aware planning bind A and the default fresh final reviewer to the current host. Slot B
  prefers Codex for Claude/dsh hosts, Claude then dsh for Codex, and Codex then Claude then dsh for
  `other`; every chain falls back to the host when it is the only usable provider.
- Applied the same external preference and host fallback to implementation review while preserving
  explicit peer, final-reviewer, backend, and implementation-reviewer selections as authoritative.
- Fixed Codex companion-host mounting and classified successful exits containing router startup
  errors as infrastructure failures rather than valid reviews; planning and implementation now share
  the same verified runtime-selection and isolation path.
- Added an observable runtime quota fallback: an auto-selected external planning B slot or
  implementation reviewer now moves persistently to the frozen host and retries without spending a
  quality attempt. Explicit selections remain authoritative, and unavailable hosts preserve the
  existing manual recovery path.

## [0.6.4] - 2026-09-07

- Made one fresh final reviewer the standard planning recommendation while retaining dual final
  review for deep, explicitly requested, or demonstrated high-consequence work.
- Added ancestry-bound delta transport for implementation re-reviews. Round one and rewritten
  histories still carry the full authoritative range; later rounds carry only the change since the
  prior reviewed SHA without narrowing the acceptance contract or exact-HEAD review authority.
- Kept same-SHA reviews after user adjudication and cumulative final reviews on full transport: their
  decision context or cross-batch evidence obligation cannot be represented by a fix-only Git delta.
- Exposed review mode, coverage and delta bases, supplied patch bytes, and wall time in review
  results, durable records, metadata, and status summaries so latency improvements are measurable.
- Raised implementation state to schema v10 so active schema-v9 runs require an explicit audited
  migration before adopting the new review-transport semantics.

## [0.6.3] - 2026-09-06

- Hardened planning-provider isolation: model tools cannot read mounted credentials, invocation
  outputs no longer make the whole run directory writable, DSH loses shell/runtime/subagent escape
  paths, and nested DSH responses receive deterministic schema validation before semantic review.
- Made planning barriers concurrency-safe by reserving paid attempts before launch, merging findings
  only while holding the run lock, preserving simultaneous typed decisions, rejecting stale engine
  epochs, and making terminal states absorb late worker results.
- Allowed planning preflight and initialization from an explicit `--base-ref` even when the source
  checkout is dirty; only the selected committed SHA enters the detached planning worktree.
- Separated convergence and round-budget review grants, added one audited fixed-SHA re-review after a
  PASS is superseded by a correction, and fixed reviewer timeout handling so infrastructure failures
  are durably recorded instead of crashing the controller.
- Replaced DSH's writable incremental review channel with a path-bounded read-only tool profile and
  final structured output. Unknown DSH tools fail closed, the real DSH overlay composition is checked
  during preflight, and every implementation reviewer now runs inside the same outer bubblewrap
  boundary as planning agents. Reviewer setup never mutates shared repository configuration.
- Bound adjudication previews to stable decision IDs, made interrupted planning calls explicitly
  recoverable without refunding their reserved attempt, and fixed reimbursement after an input-scoped
  budget reset. Planning init now records the selected ref, frozen SHA, and every excluded dirty path.
- Bounded controller-side Git diff capture with a changed-path fallback for oversized binary patches;
  restricted the automatic post-PASS re-review to controller-effective PASS results; and made exhausted
  review-budget choices reflect which exceptional grants remain available.

## [0.6.2] - 2026-09-02

- Allowed implementation runs to initialize from an explicitly selected target-branch SHA while the
  current checkout is dirty; recorded every excluded working-tree change without stashing, copying,
  resetting, or committing it.
- Added repeatable `--instruction-file` snapshots for explicit uncommitted project contracts, bound
  them by SHA-256, and supplied invocation-local copies to acceptance-contract and fixed-SHA reviewers.
- Raised implementation state to schema v9 so older engines reject runs carrying supplementary
  instruction authority; schema-v8 migration preserves existing reviewer contracts without inventing instructions.
- Kept final integration fail-closed when the target branch is the dirty current checkout, preserving
  the user's in-progress work at the only boundary that mutates the original project, and exposed that
  apply blocker in finalize previews and status.

## [0.6.1] - 2026-09-01

- Added one production-causality protocol shared by planning, host synthesis, acceptance-contract
  review, implementation, and fixed-SHA review. It traces authority, actual inputs, responsible
  producers, consumer interpretation, and the earliest supported divergence across deterministic,
  generative, external, human, and hybrid systems.
- Kept machine facts in deterministic validation and semantic judgments with independent reviewers,
  while requiring new gates, resolvers, and fallbacks to justify valid variations and complete recovery.
- Established a pre-1.0 release policy: compatible refinements remain on the current patch line,
  substantial capabilities may advance the 0.x minor line, and 1.0 requires an explicit maintainer
  product-readiness decision plus a reviewed release-gate change.
- Froze both implementation reviewer templates inside each run, bound them by SHA-256, and raised
  implementation state to schema v8 so older runs must explicitly migrate before adopting newly
  installed review semantics.
- Preserved sequential migration history while deriving every backup from the run's actual loaded
  schema, preventing an earlier schema-6 backup from blocking a later schema-7-to-8 transition.
- Hardened migration previews and recovery around null fingerprints, missing legacy contract
  directories, malformed history, dangling backup symlinks, and prior reviewer-contract audit data.

## [0.6.0] - 2026-09-01

- Reopened every re-reported P0 finding as blocking even when an earlier P1 observation had been
  deferred, and added an end-to-end regression proving that the superseded PASS cannot authorize
  batch acceptance.
- Normalized project-relative `.venv` launcher paths and prevented both literal and symlink-disguised
  host-root mounts in the verification sandbox.
- Bound convergence and final planning reviews to the exact synthesized candidate digest, rejected
  late results from superseded candidates, and gave convergence-boundary decisions a real recovery path.
- Added a deterministic versioned archive with normalized metadata, a SHA-256 checksum, and a
  tag-gated GitHub release workflow.
- Reworked the public README around guarantees, compatibility, installation, quick starts, operations,
  and explicit beta limitations.
- Added contribution, support, conduct, issue, pull-request, and dependency-update policies; expanded
  the deterministic release gate to verify the public surface and packaging contract.
- Expanded CI across CPython 3.11, 3.12, and 3.13 while preserving the zero-paid-call fake-adapter suite.
- Pinned every GitHub Action to a full commit SHA and configured monthly reviewed update proposals.
- Enabled bubblewrap's user-namespace prerequisite explicitly on ephemeral Ubuntu 24.04 runners and
  pinned the Node 24 generations of the official checkout and Python setup actions.
- Removed the accidental PyYAML dependency from dsh identity discovery and taught verification to
  mount a declared absolute Python runtime by its narrow, read-only installation prefix.
- Replaced a shared-runner wall-clock concurrency threshold with direct simultaneous-invocation
  evidence, eliminating load-dependent CI failures without weakening the concurrency assertion.
- Synchronized the public and host contracts with the implemented dsh acceptance-contract and
  fixed-SHA code-review paths, including model overrides and the audited incremental output channel.

## [0.5.0] - 2026-08-31

- Added concurrent implementation runs from one frozen baseline, with explicit run selection when more than one run is active.
- Added run-owned merge reconciliation worktrees whose combined commits pass the normal fixed-SHA verification and independent review loop.
- Added non-executing, content-sensitive project-environment fingerprints, pre/post-verification drift
  detection, and infrastructure classification without launching `.venv` startup hooks.
- Reclassified target-branch movement as integration divergence rather than execution staleness.
- Allowed finalization to atomically advance a target branch that is not checked out without switching the user's checkout.
- Raised implementation state to schema v7 with explicit, backed-up environment-fingerprint migration from schema v6.
- Added crash recovery for reconciliation worktree preparation and for target movement during a
  prepared finalization transaction.
- Made reconciliation attempts idempotent, explicitly selectable at submission, and auditable to abandon
  without deleting their worktree or branch.
- Authenticated checkpointed legacy state before schema translation, preventing schema downgrades from
  laundering forged completion, and made byte-identical interrupted migration backups resumable.
- Removed cache-directory fingerprint exemptions, rejected special files and unreadable venv subtrees,
  and revalidated recorded reconciliation Git resources before returning an idempotent result.

## [0.4.5] - 2026-08-27

- Gave the reviewer-invocation budget the same single explicit grant the round budget beside it
  already had. Exhausting it offered only `ABORT_RUN` and `DEFER_ELIGIBLE_P1`, and `DEFER` refuses
  outright when the batch holds no eligible OPEN P1 -- so a bounded resource that one decision could
  release could instead end the run. `GRANT_ONE_REVIEW_INVOCATION` releases one more call, once per
  batch, and restores the status the run parked from. Both the decision guard and the guard inside
  `next_invocation` honour the grant; honouring only the first would have handed out a grant that
  bought nothing.

## [0.4.4] - 2026-08-26

- Made TARGET_ADVANCED parking resumable instead of terminal-only. `target_staleness` re-reads the
  branch on every call, so once the target is back on the baseline the run may return to the status
  it parked from (`IMPLEMENTING`, `CHANGES_REQUESTED`, `AWAITING_ACCEPTANCE`, or
  `READY_TO_FINALIZE`) via the `RESUME_WITH_DECISION` choice; a still-stale target re-parks on the
  next move instead of being silently bypassed. `INTEGRATION_NOT_FAST_FORWARD` keeps only terminal
  choices, and a preview finalize no longer writes the park it would apply.
- Stopped the bare-interpreter verification trap mechanically instead of by reviewer instruction.
  COMMAND criteria and reviewer verification requests naming a bare python/pypy interpreter
  (`python`, `python3`, `python3.x`, `pypy`) are refused at validation, because the sandbox would
  resolve them to a system interpreter that never sees the project environment. A non-`.venv/`
  python interpreter that still fails with a module-missing error is recorded as retryable
  infrastructure, never a quality FAIL, so the documented environment trap can no longer consume a
  quality attempt. `verify` now also admits `CHANGES_REQUESTED`, mirroring the review admission, so
  the remaining requests of a failed round stay executable.

## [0.4.3] - 2026-08-26

- Gave a run a forward move after a commit lands on top of a passing review. A PASS authorizes one
  exact SHA, so a later commit left `accept` refusing on the SHA binding while `review` refused on
  status, and the only escapes were resetting the worktree or superseding the run. The batch is not
  accepted yet, so `review` now runs from `AWAITING_ACCEPTANCE` and the new commit earns its own
  round; reviewing a SHA that already holds a PASS is refused so the change cannot buy a second paid
  review. Acceptance still binds to the exact reviewed SHA through every existing check.
- Made the two remaining head-movement refusals actionable instead of terminal-looking: finalize now
  names the accepted SHA, the current HEAD, and both legal exits, and the plan-snapshot error names
  the file to restore. Commits past an accepted SHA stay outside the batch contract by design.

## [0.4.2] - 2026-08-26

- Stopped one finding ID from silently absorbing a widening class of defects. `required_outcome` and
  the files a finding names are now frozen when first stated; each restatement is recorded in
  `obligation_revisions`, and a second one stops the batch for a typed decision instead of another
  round. Measured on nine recorded runs, five findings had been restated up to five times across
  three files each, which is how batches reached round five without converging. Line numbers are
  excluded from the comparison because they move with every fix commit.
- Stopped rejecting a whole review because it referenced an earlier batch's finding. The cumulative
  final review's base is the run baseline, so it is required to judge every batch and will confirm or
  re-observe findings an accepted batch owns; that rejection returned no verdict at all and cost 49.8
  minutes across two adapters. Both cases are now warnings, authority stays with the owning batch, and
  only a cross-batch P0 raises a typed decision.

## [0.4.1] - 2026-08-25

- Recorded and froze the implementation workflow's controller Python identity so PATH changes cannot
  silently switch interpreters while resuming a run.
- Made preflight report the project `.venv`, uv availability, lock metadata, and the exact read-only
  runtime bridge used for fixed-SHA verification.
- Added an actionable infrastructure error for missing relative `.venv` launchers and documented why
  ignored environments do not follow worktrees.
- Kept dependency provisioning explicit: Grounded Build never runs `uv sync` or installs packages
  silently during verification.

## [0.4.0] - 2026-08-24

- Made every same-round A/B stage genuinely parallel with an atomic merge barrier and observable running invocation records.
- Froze engine files, model/runtime selection, context digests, and redacted actual argv per invocation; runs now reject silent engine drift.
- Split provider infrastructure failures from quality attempts and added typed user decisions for authentication, overload, rate-limit, adapter, timeout, and tool-host failures.
- Added deterministic validation diagnostics to retries, exact scope-id instructions, draft-time `new_evidence`, dynamic ledger-key enums, and evidence-backed semantic finding aliases.
- Strengthened synthesis checks for batch-set mismatch, missing/unknown dependencies, cycles, undisposed stable findings, and unbounded repeatable work.
- Added honest terminal audit exports for abandoned runs, live status fields, aggregate resource/cost reporting, and immediate private scratch cleanup.
- Defaulted planning agents and isolated implementation reviewers to Opus and `gpt-5.6-sol`, while preserving explicit user overrides and leaving the implementation host unchanged.

## [0.3.0] - 2026-08-24

- Added the MIT license and a least-privilege GitHub Actions release gate.
- Corrected the security model to document shell access, opt-in native web research, and the limits
  of process-level network isolation.
- Extended release validation to reject missing public-release files, version drift, unfinished
  security placeholders, and Chinese characters in distributed text sources.
- Made two independent evidence investigations a hard prerequisite for drafting.
- Added standard and explicit deep planning modes. Deep mode adds two `draft_03` plans and an extra
  convergence review, while all loops remain bounded.
- Froze a scope contract and prohibited divergent rounds from widening the user-authorized objective.
- Added structured evidence states, severity/urgency lanes, root-cause chains, solution-form findings,
  authoritative-source provenance, and evidence-backed priority ordering.
- Added opt-in official web research for investigation calls only; local-only remains the default.
- Added state-machine and regression coverage for investigation gates, web policy, deep divergence, and
  both convergence rounds.

## [0.2.0] - 2026-08-22

- Added a deterministic `next` action protocol so GPT, DeepSeek, Claude, and other host models do
  not have to reconstruct the planning state machine from prose.
- Separated CLI adapter identity from model identity and froze non-secret Codex model,
  `model_provider`, profile, family, and CLI version in planning and implementation runs.
- Added explicit GPT/DeepSeek-compatible Codex selection to preflight, initialization, and reviewer
  changes while preserving the user's configured gateway and credential trust boundary.
- Added synthesis diagnostics before paid final review and split planning recovery details into a
  progressively disclosed reference.
- Added UI metadata and brought skill frontmatter back into the supported validator schema.

## [0.1.0] - 2026-08-20

Initial developer preview.

- Added evidence-backed planning with two isolated Claude/Codex instances.
- Added automatic mixed-provider and single-provider dual-instance topologies.
- Added mutual cross-review, host synthesis, bounded correction, final review, and auditable user decisions.
- Transplanted the audited fixed-SHA implementation workflow into an independent state namespace.
- Added allowlist planning sandboxes, authenticated state, external revision anchors, and replay detection.
- Added planning and implementation regression coverage.

The implementation engine ships with fixes measured against a completed 223-verification run
rather than derived from reading the code:

- The temporary-entry ceiling rejected a healthy test suite as an infrastructure failure. A suite
  giving each of ~2,250 tests its own directory writes five figures of small files while byte
  usage sits at 7% of its own budget; the count now sits an order of magnitude above that.
- A reviewer confirming that a deferred non-blocking finding had been fixed anyway had its whole
  payload rejected, with no way for either side to break the loop.
- `introduced_by_sha` demanded 40 hex characters. Reviewers write short SHAs, and four complete
  reports — one of them a PASS — were discarded over the spelling. A 7-character SHA identifies a
  commit as well as a 40-character one.
- `cleanup` walked its own record of verification worktrees and so could not see the ones whose
  record was lost when an attempt was killed mid-run. It now enumerates through
  `git worktree list`, which is the authority on what exists.
- Nothing ever removed the per-attempt sandbox TMPDIR. One run held 7.7 GB across 230 of them
  against 1.9 MB of evidence.
- Verifications of one commit now share one read-only checkout instead of each building its own.
  The sandbox mounts the tree `--ro-bind`, so two verifications of a commit receive a
  byte-identical tree neither can modify — measured empty 223 times out of 223. Scratch is
  released as soon as a PASS is recorded and kept on every failure, so a failed run stays
  inspectable. Peak workflow state for a run of that shape: ~9.3 GB to ~280 MB, no longer growing
  with the number of verifications.

Documented, because the argument was required and nothing said who supplied it: when a user
brings only a plan, the host derives the batch manifest and has the user confirm it before
initialization freezes it.
