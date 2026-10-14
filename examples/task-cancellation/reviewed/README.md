# Task cancellation: a synthetic Grounded Build demonstration

This repository is an independently constructed demonstration. It contains no company code,
documents, data, or business logic. The cancellation defect is deliberately seeded, not a discovery
in a customer project. Workflow findings and verdicts must come from actual recorded executions.

The miniature service uses cooperative Python thread workers and an in-memory result store. It has
no HTTP server, authentication, database, external dependencies, or production deployment claim.
`TaskAPI` is a plain Python application boundary. Workers receive a cancellation event; a worker that
ignores it cannot be forcibly stopped by this design.

Snapshot results use standard-library `copy.deepcopy` semantics outside the service lock. Ordinary
nested mutable results are detached, and result identity is not preserved across snapshots. A result
that cannot be deep-copied makes `TaskAPI.get` or the snapshot phase of `TaskAPI.cancel` raise the copy
exception; there is no fallback to an aliased result. Arbitrary user-defined copy hooks do not provide
a universal isolation guarantee.

## Seeded baseline

The original committed baseline supported completion and queued cancellation. Running cancellation
changed the visible status only: the worker was not signalled, and its eventual completion could
overwrite the cancelled status and publish a result. That cancellation boundary was deliberately
incomplete. The repaired implementation signals the worker and preserves accepted cancellation
against both late returns and late exceptions.

Run the existing behavior checks:

```bash
python3 -m unittest discover -s tests -v
```

Run the unchanged cancellation assertion, which now passes on the repaired implementation:

```bash
python3 reproduce_cancel.py
```

Synchronization uses events rather than arbitrary sleeps. Timeouts are bounded failure detection,
not an assumption about worker scheduling.
