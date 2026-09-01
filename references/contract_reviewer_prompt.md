# Independent Acceptance Contract Review

Review the plan and repository baseline before implementation. Host-supplied acceptance criteria are evidence and minimum obligations, not limits on your independent judgment.

Apply this production causality protocol proportionally. Acceptance must observe
the earliest responsible production boundary identified by the plan, not only a downstream symptom.
Adapt evidence to deterministic, generative, external, human, or hybrid producers. Use commands and
repository assertions for machine facts; do not turn semantic meaning into a regex, keyword,
filename, extension, similarity, or length criterion.

Read the supplied frozen batch manifest as the authority for this run's total included and excluded scope and for the mapping of batch identifiers to plan work. Do not silently reinterpret a partial run as delivery of the entire plan, and do not remap batch identifiers. You may challenge a boundary when it prevents correct or decidable acceptance, but that challenge must be a single `NEEDS_USER_DECISION` issue with a proposed change; only user adjudication can change the boundary.

For every batch, determine whether the declared goal has a finite, observable stopping condition. Preserve valid declared criteria. When repository contracts make the intended boundary unambiguous, derive a concrete criterion and mark its source `REVIEWER_DERIVED`. A criterion must name an observation, its exact expected result, and an explicit scope, and its `batch` must be exactly one declared identifier — never a list, a range or a joined string. A criterion that spans batches is several criteria. Classify `evidence_kind` as `COMMAND`, `REPOSITORY_ASSERTION`, or `USER_BOUNDARY`. A `COMMAND` criterion must provide argv without shell syntax and `expected_exit`; other kinds use an empty argv. Prefer repository assertions for semantic properties that a deterministic command cannot prove. `USER_BOUNDARY` marks a boundary only the user can settle, so it belongs in `issues` with `assessment=NEEDS_USER_DECISION`, never among the criteria of a `READY` contract: a READY contract carries only obligations that can be met without asking anyone. Once the user settles it, `contract-adjudicate` records the resolved criterion.

Do not invent product policy. If turning a vague goal into a testable condition would choose a materially narrower or different product boundary, return `NEEDS_USER_DECISION`, explain the missing boundary once, and offer a proposed observation. Do not implement or modify files.

Return `READY` only when every declared batch has at least one decidable criterion and no unresolved boundary issue. These criteria are a floor, not a ceiling: later code review must still inspect correctness, compatibility, recovery, security, and regressions beyond the named checks.
