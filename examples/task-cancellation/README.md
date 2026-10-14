# Accepted cancellation should stay cancelled

A synthetic Grounded Build case study with executed code, independent planning, fixed-commit reviews,
and retained failure evidence. No company project, code, document, dataset, or business logic was used.
The cancellation defect was deliberately seeded. The findings and verdicts below came from actual runs.

## The visible failure

A caller cancelled a running task and received `accepted: True`. After the worker returned, the same
task appeared completed and exposed its result:

```python
{'accepted': True, 'after_worker_exit': {
    'id': '1', 'status': 'completed', 'result': 'late result', 'error': None
}}
```

The five existing tests passed. The focused reproducer failed. The missing check was in the service's
terminal writers: they held the lock but did not check whether cancellation had already won. The
accepted cancellation also failed to set the Event already supplied to the worker.

The reviewed implementation keeps the caller's accepted cancellation final:

```python
{'accepted': True, 'after_worker_exit': {
    'id': '1', 'status': 'cancelled', 'result': None, 'error': None
}}
```

These outputs are retained in [the baseline observation](evidence/host-observations/baseline-python312-reproduction.txt)
and [the repaired observation](evidence/host-observations/b02-reproduction-passing.txt).

## What the workflow contributed

Two isolated planners investigated the same committed baseline independently. Codex proposed the
cancellation repair and tests. Claude also identified mutable snapshot aliasing: a new snapshot
dictionary still exposed the service's stored nested result object.

During cross-review, Claude returned **FAIL** on Codex's draft because it omitted the snapshot
isolation requirement. It also identified a test-design risk: after cancellation, the repaired worker
intentionally discards late exceptions, so an exception raised only inside a worker cannot reliably
report a test coordination timeout. The integrated tests record wait outcomes and assert them from
the main test thread after bounded joins.

Codex's cross-review returned **PASS** on its integrated plan, with a nonblocking correction to an
invalid non-recursive search command. The host synthesized a two-batch plan from the evidence. A fresh
Codex instance returned **PASS** on that exact plan and its digest-bound batch manifest.

Read [Codex's investigation](evidence/planning/investigation-A.json),
[Claude's investigation](evidence/planning/investigation-B.json),
[Claude's cross-review](evidence/planning/B-reviews-A.json),
[Codex's cross-review](evidence/planning/A-reviews-B.json), and
[the final plan review](evidence/planning/final-review.json).
Duplicate findings in the two investigations are provenance, not separate counts of distinct defects.

## Implementation and observed results

| Snapshot | Unit tests | Unchanged cancellation reproducer | Independent code review |
|---|---|---|---|
| Original baseline | 5 pass | Exit 1 | Not an implementation candidate |
| B01: cancellation repair | 14 pass | Exit 0 | PASS, round 1 |
| B02: snapshot isolation and documentation | 17 pass | Exit 0 | PASS, round 1 |
| Combined final candidate | 17 pass | Exit 0 | PASS, round 1 |

B01 sets the cancellation Event in the accepted decision and makes both terminal writers conditional
on still owning `running`, under the existing lock. Before applying it, the new tests produced five
expected failures against the original service; the five original tests remained green.

B02 copies snapshot results outside the lock and documents the selected copy behavior. Before applying
it, three new tests produced four expected assertion failures, including two subtests, against B01.
Both `get` and the snapshot in a rejected cancellation response are covered.

The tests exercise cooperative signalling, ignored cancellation with a late return, late exceptions,
cancellation-first, success-first, failure-first, repeated cancellation, queued cancellation, unknown
ids, nested mutable results, and explicit copy failures. They use Events and bounded joins; they do
not claim exhaustive scheduler exploration or a concurrency stress benchmark.

All required commands also ran in read-only, fixed-SHA verification worktrees with offline networking.
The [verification projection](evidence/verification.json) records evidence ids, commit ids, exit codes,
output digests, durations, and paths to the copied logs. The [code-review reports](evidence/reviews/FINAL-round-1.json)
bind the final verdict to the exact candidate and cover all 14 acceptance criteria.

## An actual workflow failure and its recovery

The demonstration did not run uninterrupted. Two Claude acceptance-contract deliveries and one Codex
delivery used bare `python3` commands copied from the plan. The verifier requires an explicit
interpreter path and rejected all three deliveries. The contract producer prompt did not convey that
runtime requirement. The run exhausted its three contract calls and stopped for a user decision.

The user approved bounded recovery: preserve all 14 independently produced criterion semantics and
change only four executable bindings to the preflight-verified `/usr/bin/python3` (CPython 3.12.3).
The resulting contract is labelled **ADJUDICATED**, rather than an independently accepted contract.
No code-review PASS was fabricated. All later implementation verdicts came from fresh isolated Codex
review processes. The earlier rejected deliveries and reviewer switch remain in the local raw audit.

[Recovery evidence](evidence/contract-recovery.json) and
[the effective acceptance contract](evidence/acceptance-contract.json) are included. The missing
runtime instruction in Grounded Build itself was **not repaired** as part of this service demonstration.
This is a recorded limitation of the run, not evidence that switching models fixes the cause.

## Replay the code

Python 3.11+ standard library is sufficient for these frozen service snapshots. From this bundle root:

```bash
cd reviewed
python3 -m unittest discover -s tests -v
python3 reproduce_cancel.py
```

Both commands should exit 0. To see the original failure, start from the bundle root again:

```bash
cd baseline
python3 -m unittest discover -s tests -v
python3 reproduce_cancel.py
```

The first command passes five tests; the second intentionally exits 1. `batch-01/` contains the
intermediate accepted snapshot. Re-running the agent workflow additionally requires Grounded Build,
its documented sandbox dependencies, and authenticated provider CLIs.

## Evidence identity and limits

| Item | Identity |
|---|---|
| Baseline | `3179f2e596f580c4b90ddabccdab22783d9dadc0` |
| Accepted B01 | `9fe48e48bd5d98419034fb1e6d2b00bf34f2aafc` |
| Final reviewed and integrated commit | `7371c7d143846215402c6a935f69d56d66e09b37` |
| Planning run | `20261013-175302-201aa8` |
| Implementation run | `20261013-092627-implementation_plan-fe845e` |
| Grounded Build engine | `0.7.9` |

Planning used Codex `gpt-5.6-sol` and Claude's `opus` selection; the engine recorded provider and model
diversity as true. Implementation was authored by the host Codex agent and reviewed by fresh isolated
Codex `gpt-5.6-sol` instances after the recorded reviewer switch. Process/context isolation establishes
independent review; implementation review did not provide model diversity.

[Integration metadata](evidence/integration.json) records the completed, user-approved fast-forward
to local `main` at the reviewed commit. The working tree is clean. The initial
[run metadata](evidence/run-summary.json) remains a record of its earlier export boundary.
At that integration boundary, no push or publication had occurred. There were no excluded source changes, supplementary instruction
snapshots, or deferred implementation findings; registered worktrees remain retained.

[Usage metadata](evidence/usage.json) includes measured invocation durations and CLI-reported costs
where available. Total task cost is unknown. Parallel invocation durations are summed, not elapsed
time; the user approval pause is not a performance measurement. No savings or defect-reduction rate
is claimed from this single constructed case.

The service is in memory and uses cooperative Python threads. It cannot forcibly stop a worker that
ignores cancellation. Snapshot isolation follows standard-library deepcopy semantics for ordinary
mutable data; user-defined copy hooks have their own semantics, and uncopyable results raise from
snapshot access. Non-Exception BaseException handling was explicitly excluded.

The included review JSON is retained output, and source files are exported from recorded Git commits.
Host failure-log display copies replace only exact private demo path prefixes with `<demo>`; their
[provenance](evidence/host-observations/provenance.json) retains both original and exported digests.
Original local runtime records remain intact. This selected evidence bundle does not expose provider
authentication files, private runtime homes, full invocation envelopes, or company information.
Some Claude investigation command receipts omitted output digests; that limitation remains visible.
Bundle checksums establish file integrity, not independent authentication of the complete local run.
