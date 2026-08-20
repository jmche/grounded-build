# Changelog

All notable changes use semantic versioning.

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
