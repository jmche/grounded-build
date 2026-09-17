<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# tests

## Purpose
Regression coverage for both workflow engines, run as real end-to-end CLI exercises rather than unit
mocks: each suite builds a throwaway Git project in a `TemporaryDirectory`, installs **fake** local
`claude` / `codex` / `dsh` executables and an `other` bridge, points the state root at a temporary directory, and
drives the actual `scripts/*.py` commands. No paid model calls are made.

## Key Files
| File | Description |
|------|-------------|
| `test_plan_workflow.py` | 143 tests over `scripts/plan_workflow.py`. Embeds a `FAKE_AGENT` script that parses the prompt (provider, slot, baseline SHA, scope digest) and returns schema-valid payloads per assignment type, including canary checks that the sandbox hides forbidden paths. Classes: `PlanWorkflowTest`, `ReassignmentTest`, `InputScopedBudgetTest`, `DeliveryFailureTest`, `CodexTrustStoreTest`, `DshAdapterTest`, `InvestigationToolRulesTest`, `HostProtocolTest`. |
| `test_workflow.py` | 195 tests over `scripts/workflow.py`, loaded as a module via `importlib` so internal helpers are directly testable. Classes: `WorkflowIntegrationTests`, `ConvergencePolicyTests`, `CrossBatchFindingTests`, `ObligationDriftTests`, `ReviewerSchemaCompatibilityTests`, `DocumentationContractTests`, `SharedVerificationWorktreeTests`, `ReviewerRuntimeTest` (plus the `ConvergencePolicyFindingBuilder` helper). |
| `test_release.py` | Deterministic archive tests: exact runtime manifest, normalized metadata, reproducible bytes, CLI output, and matching SHA-256 checksum. |

## For AI Agents

### Working In This Directory
- `DocumentationContractTests` in `test_workflow.py` asserts **exact vocabulary** shared between
  `SKILL.md`, `references/implementation_workflow.md`, and `references/reviewer_prompt.md`
  (`fingerprint`, `INTRODUCED_BY_FIX`, `resolved_finding_ids`, `verdict=NEEDS_USER_DECISION`,
  `controller_runtime`, `<controller-python>`, `never runs \`uv sync\``, …). If you reword those
  documents, update this test in the same change — the coupling exists to stop the host contract and the
  reviewer contract from drifting apart.
- Tests must never reach the network or a real provider. Extend the fake-adapter scripts instead of
  loosening isolation, and keep every state root inside the temporary directory.
- The fake agents branch on prompt text (e.g. `"independent evidence investigator"`, `investigator A|B`).
  Changing prompt wording in the engines usually means changing the fake agents too.
- `test_plan_workflow.py` shells out to the CLI; `test_workflow.py` imports the module. Follow the
  existing style of the file you are editing rather than mixing the two.
- Add coverage for both the success path and the refusal path — most of this suite's value is proving
  that the engine *rejects* something (drift, scope widening, unauthorized reviewer, stale target).

### Testing Requirements
```bash
python3 -m unittest discover -s tests -v            # from the repository root
python3 -m unittest tests.test_workflow -v          # one suite
python3 -m unittest tests.test_plan_workflow.DshAdapterTest -v
```
`scripts/release_check.py` runs the same discovery, so a failure here fails CI.

### Common Patterns
- `setUp` builds a git repo with `git init -q -b main`, a local `user.email` / `user.name`, and a
  tracked file; `tearDown` disposes the `TemporaryDirectory`.
- Fake CLI executables are written into a `bin/` directory, `chmod`-ed executable via `stat`, and
  prepended to `PATH` in the child environment.
- `GROUNDED_BUILD_PLAN_HOME` / `GROUNDED_BUILD_IMPLEMENT_HOME` redirect state into the temp tree.
- Assertions read the emitted JSON object from stdout rather than scraping human text.

## Dependencies

### Internal
`../scripts/plan_workflow.py`, `../scripts/workflow.py`, and — for `DocumentationContractTests` —
`../SKILL.md`, `../references/implementation_workflow.md`, `../references/reviewer_prompt.md`.

### External
`unittest` (stdlib, no pytest requirement despite the ignored `.pytest_cache/`), `git`, and on some
paths `bubblewrap`.

<!-- MANUAL: -->
