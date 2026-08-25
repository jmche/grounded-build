<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# evals

## Purpose
Behavioral evaluation cases for the skill as a whole: given a realistic user prompt, what must the host
do, and which invariants must hold. These test *the contract a host agent follows*, not the Python
engines — the engine-level regressions live in `tests/`. The file's structure is enforced by the release
gate, so a malformed or non-actionable eval fails CI.

## Key Files
| File | Description |
|------|-------------|
| `evals.json` | Single JSON object: `skill_name` (must be exactly `"grounded-build"`) plus a non-empty `evals` array. Each entry has exactly the fields `id` (unique int), `prompt`, `expected_output`, `files`, `assertions` (non-empty). |

## For AI Agents

### Working In This Directory
- `scripts/release_check.py::validate_evals` enforces the schema strictly: `set(entry) != {...}` means
  **no extra and no missing fields**, ids must be unique integers, and `prompt` plus `assertions` must
  be non-empty. Adding a convenience field like `notes` breaks the release gate.
- Ids are not sequential in file order (the file currently runs 1, 9, 2, …). Keep ids stable when
  reordering; renumbering an existing eval loses its identity.
- Write assertions as **falsifiable invariants about behavior**, not restatements of the prompt. Good:
  "Planning does not create an implementation branch or modify the original checkout." Bad: "The plan is
  high quality."
- Cover the refusal and honesty paths, not only the happy path — several existing evals exist to check
  that the host records `provider_diversity: false`, refuses to treat same-provider agreement as proof,
  and stops before implementation.
- English only; the release gate scans this file for Han characters like every other distributed text
  file.

### Testing Requirements
```bash
python3 -c "import json; json.load(open('evals/evals.json'))"   # from the repository root
python3 scripts/release_check.py                                # runs validate_evals first
```

### Common Patterns
- `prompt` is written in the user's voice, including the constraints the user would realistically state
  ("Codex must perform final review", "I only have Claude", "Do not implement").
- `expected_output` narrates the required sequence of workflow stages in one sentence.
- `assertions` are the machine- or reviewer-checkable claims; `files` stays `[]` for repository-agnostic
  cases.

## Dependencies

### Internal
Read by `../scripts/release_check.py`; the behaviors asserted are specified in `../SKILL.md` and
`../references/`.

### External
None — plain JSON.

<!-- MANUAL: -->
