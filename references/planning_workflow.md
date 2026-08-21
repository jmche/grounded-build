# Planning workflow details

Ordinary runs should follow the `next_action` returned by `plan_workflow.py`. Read this reference for recovery, adjudication, or diagnosis.

## Topology

| Backend | A | B | Adapter diversity |
|---|---|---|---|
| `auto`, both installed | Claude | Codex | true |
| `auto`, one installed | two isolated instances of it | same adapter | false |
| `mixed` | Claude | Codex | true |
| `claude` or `codex` | two isolated instances | same adapter | false |

Separate processes, fresh sessions, isolated context, a detached read-only worktree, and audited output provide process independence. Two Codex instances using the same model are not model-diverse. A Codex adapter configured for DeepSeek remains adapter `codex`, model family `deepseek`.

Cross-review is mutual: A reviews B and B reviews A. `final_reviewer=both` independently reviews the same synthesized candidate through both slots. A single selected adapter uses fresh logical reviewer F.

## State progression

```text
INITIALIZED/DRAFTING
  -> DRAFTS_READY/CROSS_REVIEWING
  -> SYNTHESIS_REQUIRED
  -> FINAL_REVIEW_REQUIRED/FINAL_REVIEWING
  -> READY
```

Any material product boundary or exhausted bounded budget enters `NEEDS_USER_DECISION`. `ABANDONED` and `READY` are terminal.

The `next` command returns exactly one of:

- `RUN_AGENT`: preview and execute the supplied argv arrays.
- `HOST_SYNTHESIS`: collect all named artifacts and author the candidate.
- `ASK_USER`: do not invoke another model until the typed decision is recorded.
- `STOP_FOR_IMPLEMENTATION_APPROVAL`: export and stop.
- `TERMINAL`: no planning work remains.

## Synthesis contract

The final plan and manifest must state total included/excluded scope; numbered items, dependencies, components, and semantics; existing budget meanings; ordered `Bxx` mapping; finite exit observations and verification commands; compatibility, migration, rollback, recovery, risk, evidence limitations, and dispositions of disputed proposals.

`check-synthesis` catches structural errors and warns about missing explicit labels before a paid final review. Its warnings do not prove semantic quality; final reviewers still inspect repository evidence.

## Typed decisions

Read `pending_decision` from status. Show its evidence and allowed choices to the user. Run `adjudicate` once without `--apply`, then apply the exact approved choice with actor and reason.

- Planning/final boundary: resolve and continue, or abandon.
- Synthesis budget exhausted: at most one explicit extra synthesis, or abandon.
- Invocation budget exhausted: one extra invocation, reassign the assignment, resume after genuinely changed input, or abandon.

Reassignment preserves audit history and process isolation but may reduce provider/model diversity. It never converts an infrastructure failure into a plan-quality verdict.

## Recovery

- Use `status` and `next`; do not repeat a completed slot.
- Input-scoped budgets reset only when authoritative input changes.
- A delivery retry is told how output failed to arrive, never which verdict to return.
- Final FAIL permits one evidence-driven correction by default. After exhaustion, ask the user.
- Validate frozen artifacts and worktree cleanliness before every resumed invocation.
- Cleanup unregisters worktrees through Git and preserves the audit record.
