# Demonstration request

## Objective and authority

TARGET-001: Repair the deliberately incomplete cancellation boundary in this synthetic task service.
Run Grounded Build standard planning against the committed baseline and produce a reviewed,
implementation-ready plan. This is a public-case-study candidate, not company software.

The host will request implementation approval after exporting the reviewed plan. Do not implement
during planning. No publication, push, or access to other projects is authorized.

## Required behavior

1. Cancellation through `TaskAPI.cancel` of a queued task prevents its execution.
2. Accepted cancellation of a running task signals the event supplied to its cooperative worker.
3. Once cancellation is accepted, neither late return nor late exception may replace `cancelled`
   or publish a result. A worker that ignores cancellation may finish computing, but cannot publish.
4. Cancellation and completion have one serialized decision: if completion wins, its terminal result
   remains unchanged and cancellation returns false; if cancellation wins, cancellation remains final.
5. Repeated cancellation returns false. Unknown task ids continue to raise KeyError. Ordinary success
   and failure remain observable. Public method signatures remain compatible.
6. Snapshots are copies, so callers cannot mutate authoritative task state.

## Scope and constraints

Use Python standard library only, cooperative thread workers, an in-memory store, and the existing
plain Python API. No HTTP server, process supervisor, persistent database, user authentication,
automatic retries, unrelated refactors, or platform expansion. Do not forcefully terminate threads.
The baseline defect is intentionally disclosed; do not invent defects, successful outcomes, or savings.

## Acceptance and evidence

Preserve existing passing tests. First reproduce the cancellation failure with `python3 reproduce_cancel.py`.
After implementation the same assertion must pass. Add deterministic, event-coordinated API-level
checks for cooperative cancellation, non-cooperative late return, late exception, both serialized
completion/cancellation orderings, repeated cancellation, queued cancellation, and unknown ids.
Threads must be joined with bounded waits; no sleep-based scheduling assertions.

Plan at most two implementation batches with finite acceptance commands. No migration or deployment
is involved. A rollback, if requested, would restore the synthetic baseline with a patch while keeping
the audit records; do not perform rollback automatically. There is no required elapsed-time or token
budget. Record actual CLI-reported use when available; mark unreported cost as unknown.

Independent planners may choose implementation details that satisfy these semantics. They may inspect
all files in this demonstration repository and local workflow context. No external research is needed.
