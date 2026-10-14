# Synthetic task cancellation: implementation plan

Baseline: `3179f2e596f580c4b90ddabccdab22783d9dadc0`.
Authorized scope: `TARGET-001`, frozen request digest
`3ac4f6f5bf046cb4c334cd055844492d27977f72deafbe1469a61d847146bd93`.
Risk: L1, a local in-memory demonstration with no deployment or company material.
This document proposes work; it does not claim that implementation, verification, or integration has occurred.

## Included and excluded scope

Included: repair accepted cancellation, preserve terminal decisions, isolate snapshot results,
add consequential API-level tests, and correct documentation directly affected by the repair.
Only `task_service.py`, new `tests/test_cancellation.py`, and `README.md` may change.
Keep `task_api.py`, `reproduce_cancel.py`, and the five baseline tests unchanged.

Excluded: `OUT_OF_SCOPE-001` / `F-39dc2ca20534c4c3` (non-Exception BaseException handling),
HTTP, persistence, process management, authentication, dependencies, retries, forced thread termination,
platform expansion, publication, remote push, and unrelated refactors. No scope extension is authorized.

## Evidence and causal decisions

The frozen baseline suite passes five tests; the reproducer exits 1 after accepted cancellation becomes
`completed` with `late result`. [A-E3, A-E4, A-E5, A-E6]

1. **Missing signal.** `TaskAPI.cancel` calls `TaskService.cancel`, which changes state under `_lock`
   without setting the Event supplied to the operation. This is the earliest divergence from required
   behavior 2. The owner and repair boundary are `TaskService.cancel`, not the API presentation.
   [A-E1, A-E2, B-E2; F-446ca2822216f07d, F-b34f64665c1a764f]
2. **Late publication.** Both success and exception branches of `_run` reacquire the existing lock but
   publish without checking whether cancellation already won. This overwrites the authoritative terminal
   decision. The reproduction proves late success; the same branch structure supports late-exception
   risk. Repair both writers, rather than filtering snapshots. [A-E1, A-E4, A-E6, B-E1;
   F-b69fcff8209085cf, F-8815ec4830214403]
3. **Coverage gap.** The five tests pass despite the failing reproducer. Add the running-cancellation
   scenarios before changing service behavior, so the new assertions demonstrate their consequences.
   [A-E3, A-E5, A-E6, B-E8; F-c7bdf14bdd9594a0, F-17d03e1bcd215b8d]
4. **Snapshot aliasing.** `snapshot` creates a dictionary but exposes the stored result reference.
   Required behavior 6 says callers cannot mutate authoritative state; top-level dictionary copying
   alone is insufficient for ordinary nested mutable results. Use standard-library deep-copy semantics
   for the result outside the service lock. [B-E4, B-E8; F-520de2f06e762701]
5. **Test observation.** After cancellation, suppressing late worker exceptions is intentional.
   Consequently, raising a coordination-timeout exception only inside a cancelled worker would be an
   inadequate assertion. This is a strongly inferred test-design risk, not a measured production
   failure. Record worker wait outcomes and assert them in the main test thread after joining.
   [B-E1, B-E7; F-14bdafad7a2e4fe6]
6. **Preservation and documentation.** Preserve queued cancellation, repeated false, unknown-id KeyError,
   ordinary success/failure, and method signatures. Correct the README's present-tense baseline claims
   after repair, while retaining its synthetic-case disclosure. [B-E3, B-E8, B-E9;
   F-5e617e3e05496baf, F-253f92adf8817464]

The duplicate signal, publication, and coverage observations remain individually dispositioned in the
manifest; they do not represent independent counts of distinct defects. Repository evidence binds full
file digests to the baseline. B's command receipts explicitly omit digests; preserve that limitation and
do not call those receipts digest-verified. The plan's baseline execution claims also have A's command
receipts and host-retained command output. No benchmark, defect-reduction rate, or cost saving is claimed.

## B01: serialize cancellation and protect it with API tests

Dependency: none. Components: `task_service.py`, `tests/test_cancellation.py`.
Priority: the P1 signal/publication repairs and required acceptance coverage come first.

1. Reproduce the known failure and retain command output. Add cancellation tests before the repair.
   Against the unchanged service, require failures in cooperative signalling, late return, late exception,
   and cancellation-first finality while the five baseline tests remain green. Do not commit an
   intermediate failing implementation batch.
2. In `cancel`, retain the signature, KeyError, eligibility set, and Boolean decisions. Under the existing
   lock, an accepted queued/running task becomes `cancelled` and its existing Event is set before returning
   true. Setting it for queued tasks keeps one accepted path; those tasks still cannot start.
3. In both `_run` publication branches, write terminal state/result/error only while status is still
   `running`, with the state check and publication under the same existing lock. If cancellation wins,
   write nothing: result and error remain None. If worker publication wins, subsequent cancellation is
   false and leaves its completed or failed outcome unchanged.
4. Do not introduce another lock, transition framework, timeout, production test hook, or broader
   exception catcher. Do not call another lock-taking service method while holding the non-reentrant lock.
5. All behavior-driving create/start/cancel/get calls in new tests go through TaskAPI. Use service.join
   only to observe worker exit and clean up. Register cleanup immediately after starting each task;
   cleanup releases every blocking test event and joins with a finite timeout, even after assertion failure.
6. Use entered/release Events and bounded waits (two seconds per wait/join is sufficient for this local
   demonstration, and a timeout is a test failure). Workers record wait results; the main test thread
   asserts them after join. An intentional late exception occurs only after its release wait succeeded.
   Do not use sleep-based scheduling assertions. Code review must inspect the actual synchronization;
   a textual search is not proof of scheduling correctness.
7. Cover cooperative cancellation and observed signal; ignored cancellation with a late return;
   a late exception; cancellation-first; success-first and failure-first; repeated cancellation before
   and after worker exit; queued cancellation preventing execution; unknown ids; and unchanged baseline
   behavior. Assert accepted Boolean, cancel response snapshot, and final get snapshot where applicable.
   Success-first/failure-first use bounded join before cancellation. Cancellation-first blocks the worker
   until after accepted cancellation. These establish the two serialized outcomes without claiming an
   exhaustive scheduler exploration or live-contention benchmark.

Exit observation: all new running-cancellation assertions fail for their intended reason before repair;
after repair both acceptance commands pass, cancellation remains final with no result/error, cooperative
workers observe the signal, completion/failure-first remain unchanged, and every started worker joins.
Verification: `python3 -m unittest discover -s tests -v` and `python3 reproduce_cancel.py`.
Inspect the exact batch diff, including the untracked new test, for the two allowed paths.

## B02: isolate mutable snapshot results and document repaired behavior

Dependency: B01 accepted. Components: `task_service.py`, `tests/test_cancellation.py`, `README.md`.

1. Before changing snapshot, add a failing API test with a completed `{"k": [1]}` result: mutate a nested
   value obtained from get and require a subsequent get to retain the original result. Exercise the
   cancelled-response snapshot of a completed task as well (accepted false); it must also be detached.
2. Capture task fields consistently under `_lock`, release it, then deep-copy the result into the returned
   dictionary using `copy.deepcopy`. Do not execute user copy hooks under the non-reentrant lock.
   Preserve scalar success/failure, state, and signatures. No fallback returns an aliased mutable result.
3. Compatibility decision: result identity across snapshots is not retained. An intrinsically uncopyable
   result causes the standard copy exception to propagate from get/cancel rather than silently aliasing.
   Add an API test using a result whose `__deepcopy__` raises, and ensure the call raises promptly after
   ordinary worker completion. Do not turn this into a new type gate or claim arbitrary user-defined
   copy hooks provide perfect isolation. The demonstrated isolation is for standard nested mutable data
   under Python deepcopy semantics. This proposed behavior change must be visible in implementation
   approval and README; there is no deployed consumer to migrate in this synthetic repository.
4. Update the README's baseline section to describe the original defect in past tense and say the same
   reproducer now passes. Retain the seeded-defect, cooperative-thread, and no-company-material disclosures.
   Add the snapshot copy behavior/limitation beside the service description. Do not add performance or
   cost claims. Preserve the immutable baseline commit and external raw audit records.

Exit observation: the nested-result assertion fails before the snapshot repair, then passes for both
get and cancel response snapshots; copy failures remain explicit; all B01 and baseline cases still pass;
the README describes the observed repaired behavior and its copy limitation.
Verification: `python3 -m unittest discover -s tests -v` and `python3 reproduce_cancel.py`.
Read back README and inspect the exact batch diff for the three allowed paths.

## Budgets, review, recovery, and completion

These are plan items, not already-created commits. Each batch becomes one reviewed implementation
candidate; Grounded Build owns its recorded review/quality budget. Do not reset or enlarge that budget.
For local acceptance debugging, allow at most two targeted correction attempts after an unexpected
failing run per batch, then stop with the failure evidence. Deliberate failing-first baseline observations
are evidence steps, not repair attempts. After any correction, rerun both acceptance commands.
No elapsed-time, token, or monetary ceiling is specified. Record CLI-reported use when supplied;
unreported cost is unknown, and a partial provider total is not total task cost.

Implementation remains in a run-owned worktree and uses fixed-SHA independent review before each
batch is accepted. No integration is implied by plan approval. Final integration requires the user's
explicit decision. There is no schema migration, data migration, deployment, or external service.
For an unexpected acceptance or infrastructure failure, preserve the raw output, follow the engine's
recorded next action, and report the boundary instead of creating an alternate execution path.
Rollback is only on request, with a patch restoring the synthetic baseline while keeping audit records.

Reject changing BaseException handling under this request: the underlying Exception-only catch is
visible in B-E1, but the observation belongs to OUT_OF_SCOPE-001. Do not misrepresent rejection as a
repair. No blocking product question remains for planning; implementation approval includes the
explicit deepcopy compatibility decision above. This plan cannot establish implementation success.
