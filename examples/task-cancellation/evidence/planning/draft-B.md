# Plan B: TARGET-001 cancellation boundary

Baseline `3179f2e596f580c4b90ddabccdab22783d9dadc0`. Scope: TARGET-001 only. Two batches, ordered P1/U1 first, then P2.

## Root cause (proven)
- **Late publish** (B-F1; B-E1, B-E11, B-E13): `_run` at `task_service.py:44-51` writes its terminal state without checking the current status.
- **No signal** (B-F2; B-E2, B-E13): `cancel` at `task_service.py:53-59` never calls `task.cancel_event.set()`.
- Both share one existing `Lock` (B-E5), so the fix is a status check under that lock, not new machinery.

## Batch 1 (P1/U1): serialized decision, signal, and tests
Files: `task_service.py`, new `tests/test_cancellation.py`. Depends on nothing.

**Code**
1. `TaskService.cancel`: inside the existing `with self._lock`, after setting `status = "cancelled"`, call `task.cancel_event.set()` before `return True`. Applies to both queued and running (see B-PQ2). `Event.set` runs no user code, so it is safe under the lock.
2. `TaskService._run`: in both the `except Exception` and `else` branches, under the lock, publish only `if task.status == "running"`; otherwise discard the result or exception and write nothing. `result` and `error` stay `None` for a cancelled task.
3. No change to `start`, `submit`, `join`, `task_api.py`, signatures, the `Lock` type, or `except Exception` breadth. Do not call a locking method while holding the lock.

**Tests** (`tests/test_cancellation.py`, all through `TaskAPI`; join via `service.join`)
Shared pattern: `entered`/`release` Events, every `wait`/`join` has a timeout and is asserted, `release.set()` plus `service.join` in `addCleanup`/`finally`, no `time.sleep`.
- `test_cooperative_cancellation`: worker sets `entered`, then `cancel_event.wait(2)`, records what it saw, returns. Assert cancel accepted, worker saw the event set, final `cancelled`, `result is None`, `error is None`.
- `test_noncooperative_late_return`: worker ignores the event, returns after `release`. Final `cancelled`, `result is None`.
- `test_late_exception`: worker raises after `release`. Final `cancelled`, `error is None`.
- `test_completion_wins`: start, join, record snapshot; `cancel` returns `accepted False` and the snapshot is unchanged (`completed`, same result). Same for a failed task.
- `test_cancellation_wins`: cancel while blocked on `release`, then release and join; state stays `cancelled`.
- `test_repeated_cancellation`: second cancel of a running-cancelled task returns `accepted False`, before and after worker exit.
- `test_queued_cancellation_never_runs`: cancel queued, `start` returns False, operation never ran.
- `test_unknown_ids`: `get`, `start`, `cancel` raise `KeyError`.

**Acceptance (all decidable)**
- Before editing: `python3 reproduce_cancel.py` exits 1 with the line-31 assertion (matches B-E11).
- With only the new test file in place, the running-cancel tests fail against baseline `task_service.py`.
- After the fix: `python3 reproduce_cancel.py` exits 0 and prints `status: cancelled`, `result: None`.
- `python3 -m unittest discover -s tests -v` exits 0: the 5 existing tests plus the new ones, none skipped.
- `grep -n "sleep" tests/` returns nothing.
- `git diff --stat` touches only `task_service.py` and `tests/test_cancellation.py`.

## Batch 2 (P2): snapshot isolation and README
Files: `task_service.py`, `tests/test_cancellation.py`, `README.md`. Depends on Batch 1 passing.

1. `TaskService.snapshot` (B-F4; B-E4, B-E14): read the fields under the lock, then return the dict with `copy.deepcopy(result)` done after releasing the lock, so a user `__deepcopy__` never runs under it. Stdlib `copy` only. See B-PQ1 for the known limit.
2. Test `test_snapshot_result_is_detached`: complete a task with `{"k": [1]}`, mutate through `api.get(...)["result"]`, assert a later `api.get` still returns `{"k": [1]}`. Existing `test_success` and `test_snapshot_dictionary_is_detached` must still pass.
3. `README.md` (B-F6; B-E9): reword only the 'Seeded baseline' section to past tense and state that the repro now passes. No other edits (see B-PQ3).

**Acceptance**
- `python3 -m unittest discover -s tests -v` exits 0, including the new snapshot test.
- `python3 reproduce_cancel.py` exits 0.
- `git diff --stat` for this batch touches only the three files above.

## Limits per batch
- At most 2 fix attempts after the first failing acceptance run; then stop and report the failing output unchanged.
- No elapsed-time or token budget is required. Record CLI-reported use if available; otherwise mark cost unknown.

## Preserved and not planned
- Regression guards, unchanged: queued cancel, repeated false, KeyError, success, failure, signatures (B-F5; B-E3, B-E8, B-E12, B-E16).
- Simultaneous contention is not exercised: the orderings are sequenced deterministically, because no production seam exists between operation return and lock acquisition, and the request does not ask for one (B-F3).
- Threads are never forcibly stopped; a non-cooperative worker runs to its own end and is only denied publication.
- Rollback: only on request, by reverse patch to the baseline; audit records kept. Not automatic.
- No push, publication, dependencies, persistence, or retries.
