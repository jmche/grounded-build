# Task cancellation: a synthetic Grounded Build demonstration

This repository is an independently constructed demonstration. It contains no company code,
documents, data, or business logic. The cancellation defect is deliberately seeded, not a discovery
in a customer project. Workflow findings and verdicts must come from actual recorded executions.

The miniature service uses cooperative Python thread workers and an in-memory result store. It has
no HTTP server, authentication, database, external dependencies, or production deployment claim.
`TaskAPI` is a plain Python application boundary. Workers receive a cancellation event; a worker that
ignores it cannot be forcibly stopped by this design.

## Seeded baseline

Completion works, and queued cancellation prevents execution. Running cancellation currently changes
the visible status only. The worker is not signalled, and its eventual completion can overwrite the
cancelled status and publish a result. This is one intentionally incomplete cancellation boundary.

Run the existing behavior checks:

```bash
python3 -m unittest discover -s tests -v
```

Reproduce the intended cancellation assertion (expected to fail on the baseline):

```bash
python3 reproduce_cancel.py
```

Synchronization uses events rather than arbitrary sleeps. Timeouts are bounded failure detection,
not an assumption about worker scheduling.
