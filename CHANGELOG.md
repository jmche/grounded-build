# Changelog

All notable changes use semantic versioning.

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
