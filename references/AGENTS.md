<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# references

## Purpose
Progressively disclosed contracts. `SKILL.md` stays short enough for a host to hold in context; the
detail that only matters while diagnosing, resuming, or reviewing lives here. Two of these files are
*documentation* the host reads, and two are *prompt templates* loaded verbatim into isolated reviewer
processes — the second kind is executable input, not prose.

## Key Files
| File | Description |
|------|-------------|
| `planning_workflow.md` | Planning detail read by the host on demand: A/B topology and adapter-diversity table, evidence/scope/priority contracts, the full state progression, the six `next` action types, the synthesis contract and `check-synthesis` semantics, typed decisions, and recovery. Hashed into every run's engine contract. |
| `implementation_workflow.md` | The Implement mode contract, longest file in the repository (~440 lines). Controller-interpreter selection, inputs, storage and isolation, safety boundary, preflight, run start, acceptance-contract review, resume, per-batch implement/review/fix/verify/accept loop, supersede, final verification and integration, cleanup, and the required final response. Must be read completely before acting in Implement mode. |
| `reviewer_prompt.md` | Prompt template for the independent per-batch code reviewer: severity ladder (`P0`–`P2`), finding identity and lifecycle, round scope, decidability requirement, and `verdict=NEEDS_USER_DECISION` rules. Loaded by `scripts/workflow.py` as `PROMPT_TEMPLATE`. |
| `contract_reviewer_prompt.md` | Prompt template for the independent acceptance-contract reviewer: derive or preserve decidable per-batch criteria, classify `evidence_kind` as `COMMAND` / `REPOSITORY_ASSERTION` / `USER_BOUNDARY`, and refuse to invent product policy. Loaded as `CONTRACT_PROMPT_TEMPLATE`. |

## For AI Agents

### Working In This Directory
- **`planning_workflow.md` is hashed by `plan_workflow.engine_contract()`.** Editing it — even
  whitespace — makes every in-flight planning run reject commands as engine drift. Recovery is
  `migrate-engine`, not state editing.
- **Wording here is asserted by tests.** `tests/test_workflow.py::DocumentationContractTests` requires
  a shared vocabulary across `implementation_workflow.md`, `reviewer_prompt.md`, and `SKILL.md`:
  `fingerprint`, `novelty`, `INITIAL_REVIEW`, `INTRODUCED_BY_FIX`, `PREVIOUSLY_MASKED`, `PRE_EXISTING`,
  `UNRELATED`, `resolved_finding_ids`, `introduced_by_sha`, `severity_change_justification`, `OPEN`,
  `VERIFIED`, `DEFERRED`, `legacy recovery`, `decidable`, `machine checkable`, `named`,
  `verdict=NEEDS_USER_DECISION`, `exactly one P1 finding`, `controller_runtime`, `<controller-python>`,
  `original project environment`, and ``never runs `uv sync` ``. Run the suite after any edit.
- The two prompt files are read by a model with authority over a PASS/FAIL verdict. Loosening a
  constraint here weakens the review contract at runtime, with no code change to notice it. Treat edits
  as behavior changes, not documentation changes.
- Keep the host contract and the reviewer contract symmetric: if a lifecycle state, severity meaning, or
  finding field changes in one, change it in the other in the same edit.
- English only — the release gate rejects Han characters in every distributed text file.
- Placeholders such as `<controller-python>` and `<skill-root>` are intentional; do not substitute a
  machine-specific path.

### Testing Requirements
```bash
python3 -m unittest tests.test_workflow.DocumentationContractTests -v   # from the repository root
python3 scripts/release_check.py
```

### Common Patterns
- Contracts are written as obligations and refusals ("must", "never", "return `NEEDS_USER_DECISION`
  when…"), with the escape hatch always routed to a typed user decision rather than agent discretion.
- Every bounded loop states its bound; every terminal state states what it preserves.
- Rationale is kept next to the rule when the rule looks arbitrary (e.g. why a criterion may not span
  batches), because the rule is otherwise likely to be "simplified" away.

## Dependencies

### Internal
- `../scripts/workflow.py` loads `reviewer_prompt.md` and `contract_reviewer_prompt.md` at runtime.
- `../scripts/plan_workflow.py` hashes `planning_workflow.md`.
- `../SKILL.md` links here for both workflows.

### External
None — plain Markdown.

<!-- MANUAL: -->
