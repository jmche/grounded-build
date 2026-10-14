# Implementation batch manifest

Baseline: `3179f2e596f580c4b90ddabccdab22783d9dadc0`.
Included scope: TARGET-001. Excluded: OUT_OF_SCOPE-001 and every extension described as excluded in the plan.
Exactly two batches; these are proposed work units, not completed commits.

B01: Serialize cancellation and add consequential regression tests
Scope: TARGET-001.
Dependencies: none.
Files: task_service.py; tests/test_cancellation.py (new).
Plan items: signal acceptance under the existing lock; condition both terminal writers on running;
API-level cancellation/ordering/regression tests with bounded cleanup and main-thread wait assertions.
Before repair: record the failing reproducer, add tests, and show the running-cancellation cases fail
against the original service while the five baseline tests pass.
Exit observation: both commands pass after repair; signal delivery, late-return/exception suppression,
cancel-first and completion/failure-first, repeated cancel, queued cancel, and KeyError are observed;
all test workers join. Inspect the exact batch diff for the two allowed paths.
Verification:
- python3 -m unittest discover -s tests -v
- python3 reproduce_cancel.py
Review: fixed-SHA independent review with the full frozen contract and finding ledger.
Bounds: at most 2 local targeted corrections after unexpected acceptance failure, rerunning both
commands after each; Grounded Build review budgets remain authoritative and unchanged.

B02: Isolate snapshot results and align the README
Scope: TARGET-001.
Dependencies: B01.
Files: task_service.py; tests/test_cancellation.py; README.md.
Plan items: failing-first nested-result API test; deepcopy outside the lock; explicit copy-error test;
document result-copy compatibility and repaired cancellation without altering historical records.
Before snapshot repair: demonstrate that nested snapshot mutation changes a later snapshot.
Exit observation: get and cancel response snapshots detach ordinary nested mutable results; a
copy hook failure raises explicitly; all prior tests and the original reproducer pass; README matches
those observations and states copy limitations. Inspect the exact batch diff for the three allowed paths.
Verification:
- python3 -m unittest discover -s tests -v
- python3 reproduce_cancel.py
Review: fixed-SHA independent review against the exact new revision.
Bounds: at most 2 local targeted corrections after unexpected acceptance failure, rerunning both
commands after each; no implicit retry-budget expansion.

Final acceptance requires both accepted batches, passing commands at the final revision, scope/diff
inspection, and independent review. Explicit user approval is required for final integration;
no push or publication is authorized. No migration is applicable to this in-memory synthetic project.
