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

## Evidence, scope, and priority contracts

Initialization freezes `request.md` and `scope_contract.json`. The contract admits `TARGET`,
`REQUIRED_SUPPORT`, and `EVIDENCE_ONLY`; `PROPOSED_EXTENSION` requires a typed user decision and
`OUT_OF_SCOPE` cannot enter a candidate. A later round may deepen evidence or causal/verification coverage,
but it cannot enlarge the objective.

Both slots investigate independently before either may draft. Local repository evidence is primary. With
`research_policy=authoritative-web`, investigation calls alone may consult official project documentation or
the official GitHub repository. Each external record includes its URL, retrieval time, version/tag/commit,
content digest, and supported claim. Search snippets are discovery aids only.

Evidence states are `VERIFIED`, `STRONGLY_INFERRED`, `WEAKLY_INFERRED`, `UNRESOLVED`, and `REFUTED`.
Priority lanes are `STOP_THE_LINE`, `MUST_RESOLVE`, `INVESTIGATE_IF_BUDGET`, and `RECORD_ONLY`.
Rank only after the scope gate: user priority, severity, urgency, blockers/dependencies, causal leverage,
evidence strength, then effort. Verification priority and solution priority are distinct. A priority change
requires new evidence in the record.

The workflow, not an agent, fingerprints findings from normalized scope, problem, and causal chain. The
resulting `F-*` keys live in the frozen finding ledger. Integration rounds may accept or reject known keys;
new discoveries must carry the complete finding contract before receiving a stable key. Repeated local IDs
or agreement do not create duplicate facts.

## State progression

```text
INITIALIZED/INVESTIGATING
  -> EVIDENCE_READY/DRAFTING
  -> DRAFTS_READY/CROSS_REVIEWING (two integrated draft_02 plans)
  -> [deep only] DIVERGENCE_REQUIRED/DIVERGING (two draft_03 plans)
  -> SYNTHESIS_REQUIRED
  -> [deep only] CONVERGENCE_REVIEW_REQUIRED/CONVERGENCE_REVIEWING
  -> FINAL_REVIEW_REQUIRED/FINAL_REVIEWING
  -> READY
```

Any material product boundary or exhausted bounded budget enters `NEEDS_USER_DECISION`. `ABANDONED` and `READY` are terminal.

Standard mode stops divergence after `draft_02`. Deep mode adds exactly one further divergent round and one
extra convergence review; final review is the second convergence round. Failed convergence may consume the
single bounded synthesis correction, but it cannot create an unbounded review loop. The guarantee is finite,
honest termination—not forced consensus and not a claim of absolute correctness.

The `next` command returns exactly one of:

- `RUN_AGENT`: preview and execute the supplied argv arrays.
- `HOST_SYNTHESIS`: collect all named artifacts and author the candidate.
- `ASK_USER`: do not invoke another model until the typed decision is recorded.
- `STOP_FOR_IMPLEMENTATION_APPROVAL`: export and stop.
- `TERMINAL`: no planning work remains.

## Synthesis contract

The final plan and manifest must state total included/excluded scope; scope ids; numbered items, dependencies,
components, and semantics; existing budget meanings; ordered `Bxx` mapping; finite exit observations and
verification commands; compatibility, migration, rollback, recovery, risk, evidence limitations, and
dispositions of disputed proposals. Material findings use solution form: problem, evidence, root cause or
honest unresolved status, affected surfaces, recommended solution, alternatives/tradeoffs, and verification.

`check-synthesis` catches structural errors and warns before a paid final review. Every `Bxx:`
block should use the exact labels `Exit observation:` and `Verification:` so the deterministic
check can recognize them. Its warnings do not prove semantic quality; final reviewers still
inspect repository evidence.

## Typed decisions

Read `pending_decision` from status. Show its evidence and allowed choices to the user. Run `adjudicate` once without `--apply`, then apply the exact approved choice with actor and reason.

- Planning/final boundary: resolve and continue, or abandon.
- Synthesis budget exhausted: at most one explicit extra synthesis, or abandon.
- Invocation budget exhausted: one extra invocation, reassign the assignment, resume after genuinely changed input, or abandon.

Reassignment preserves audit history and process isolation but breaks the original two-slot
diversity claim. After any reassignment, both `provider_diversity` and `model_diversity` are
reported as false; the decision record names the adapter change and the isolation that remains.
Reassignment never converts an infrastructure failure into a plan-quality verdict.

## Recovery

- Use `status` and `next`; do not repeat a completed slot.
- Input-scoped budgets reset only when authoritative input changes.
- A delivery retry is told how output failed to arrive, never which verdict to return.
- Final FAIL permits one evidence-driven correction by default. After exhaustion, ask the user.
- Validate frozen artifacts and worktree cleanliness before every resumed invocation.
- Cleanup unregisters worktrees through Git and preserves the audit record.
