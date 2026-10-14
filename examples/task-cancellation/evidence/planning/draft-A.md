## Root cause and repair boundary

`TaskService` owns the earliest divergent decision. It already serializes cancellation and worker publication with `_lock`, but accepted cancellation only changes `status`, while both `_run` publication branches overwrite that status unconditionally. The repair should use the existing lock, state, and cooperative event rather than introduce another authority or attempt to terminate threads. [A-E1, A-E2, A-E6]

## Batch 1 — Make cancellation the serialized terminal decision

**Priority:** STOP_THE_LINE. **Files:** `task_service.py`. **Dependencies:** none.

1. In `TaskService.cancel`, retain the existing `KeyError`, eligibility, return-value, and signature behavior. While holding `_lock`, accept only `queued` or `running` tasks, transition the task to `cancelled`, and set its existing `cancel_event` as part of that same accepted decision. A second cancellation or cancellation after completion/failure must still return `False`.
2. In both `_run` publication branches, reacquire `_lock` and publish `failed`/`error` or `completed`/`result` only if the authoritative status is still `running`. If cancellation acquired the lock first, discard the late return or exception and leave `status == "cancelled"`, `result is None`, and `error is None`. If worker publication acquired the lock first, the terminal success/failure remains unchanged and a later cancel returns `False`.
3. Do not change public signatures, introduce forced thread termination, or add retries, dependencies, persistence, networking, or alternate execution paths.

**Finite batch acceptance:** Run `python3 reproduce_cancel.py` once and require exit 0 with a final cancelled snapshot and no result. Run `python3 -m unittest discover -s tests -v` once and require all five baseline tests to pass. If either command fails, make one targeted correction and rerun only the failed command once; on a second failure, stop and report the exact output rather than retrying indefinitely. [A-E1, A-E2, A-E5, A-E6]

## Batch 2 — Add deterministic API-level regression coverage

**Priority:** MUST_RESOLVE. **Files:** tests under `tests/`; retain `reproduce_cancel.py` as the focused regression. **Dependencies:** Batch 1 complete.

1. Add event-coordinated tests that create, start, inspect, and cancel tasks through `TaskAPI`; use `TaskService.join` only for bounded worker cleanup. Every started worker must be released in `finally` and joined with a finite timeout. Do not use sleeps as scheduling assertions.
2. Cover cooperative running cancellation: wait until the worker has entered, cancel through `TaskAPI`, verify the supplied cancellation event becomes set, permit the worker to exit, and assert cancellation remains final without result or error.
3. Cover a non-cooperative late return and a late exception separately: block each worker on an event, accept cancellation before releasing it, join it, and assert neither return value nor exception is published.
4. Cover both serialized orderings: (a) cancellation wins before a blocked worker returns, leaving cancellation final; (b) successful completion is joined before cancellation is attempted, leaving the completed snapshot unchanged and returning `accepted == False`.
5. Verify repeated cancellation returns `False`, including after accepted running cancellation. Preserve or explicitly retain the existing API-level checks for queued cancellation preventing execution, unknown IDs raising `KeyError`, ordinary success/failure, and detached snapshot dictionaries.

**Finite batch acceptance:** Run `python3 -m unittest discover -s tests -v` once and require all old and new tests to pass without live worker threads. Then run `python3 reproduce_cancel.py` once and require exit 0. Allow at most one targeted fix and one rerun per failed command; if still failing, stop with the failing scenario and command output. CLI-reported timing may be recorded; cost is unknown because no cost reporter or budget is supplied. [A-E3, A-E4, A-E5, A-E6]

## Decidable completion observations

The change is accepted only when both commands exit 0; queued tasks never execute after accepted cancellation; a cooperative running worker observes its supplied event; late return and late exception cannot alter a cancelled snapshot; completion-first cancellation returns false without changing the terminal result; repeated cancellation returns false; unknown IDs still raise `KeyError`; ordinary success and failure remain observable; snapshot dictionary mutation cannot change stored state; and all test workers terminate within bounded joins.
