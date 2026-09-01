<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# scripts

## Purpose
The executable core of the skill: two independent, resumable workflow engines plus the deterministic
release gate. Each engine is a single-file `argparse` CLI over an integrity-checked JSON state machine —
the host issues one command, reads one JSON object, and asks for the next legal action. There is no
shared module between the two engines by design: planning state and implementation state must never
cross.

## Key Files
| File | Description |
|------|-------------|
| `plan_workflow.py` | Planning engine (~3.2k lines, `VERSION = "0.6.1"`, `SCHEMA_VERSION = 2`). Adapter discovery and sandboxing, isolated A/B agent invocation, evidence/finding validation, the `next_action` protocol, synthesis diagnostics, typed adjudication, export and audit export. |
| `workflow.py` | Implementation engine (`SCHEMA_VERSION = 7`). Concurrent run discovery, worktree isolation, acceptance-contract review, fixed-SHA reviewer dispatch, environment fingerprints, reviewed reconciliation, finalization, and cleanup. |
| `release_check.py` | No-network release gate: validates `evals/evals.json` shape, required public-release files, LICENSE/SECURITY content, cross-file version synchronization, and the English-only rule; then compiles both engines and runs the unit suites. Optional `--quick-validator <path>` chains an external skill validator. |
| `package_release.py` | Deterministic standard-library packager for versioned `.tar.gz` release archives and SHA-256 checksum files. |

## For AI Agents

### Working In This Directory
- **`plan_workflow.py` is hashed into every planning run's engine contract** (with `SKILL.md` and
  `references/planning_workflow.md`). Any edit makes live runs reject commands as drift; recovery is
  `migrate-engine`, never state editing.
- `release_check.py` regex-asserts `^VERSION = "<VERSION file>"$` in `plan_workflow.py`. Keep that
  literal on its own line.
- Both engines are **standard library only**. Do not add a third-party import; the skill installs by
  being copied into `~/.agents/skills/grounded-build/`, with no dependency install step.
- Both resolve `SKILL_ROOT = Path(__file__).resolve().parent.parent`. Moving a script one level changes
  every template and reference path.
- Preserve preview-before-apply. Commands that mutate history, spend money, or discard evidence
  (`adjudicate`, `abandon`, `cleanup`, `finalize`, `migrate`, `migrate-engine`) must keep requiring
  `--apply`, and agent invocations must keep their `--dry-run` preview.
- Sandbox policy is expressed as CLI overrides (`CODEX_POLICY_OVERRIDES`), deliberately layered with
  `-c` rather than by writing a synthetic `config.toml` — writing the file once silently replaced a
  user's gateway configuration and produced a 401 reported as a planning failure. Do not regress that.
- Resource limits in `workflow.py` (`MAX_VERIFICATION_TEMP_FILES`, `MAX_VERIFICATION_TEMP_BYTES`,
  `MAX_VERIFICATION_PROCESSES`, `MAX_VERIFICATION_MEMORY_BYTES`) were measured against a real suite,
  and the inline comments record the measurements. Change them only with new measurements.
- Never make an engine install dependencies or run `uv sync` implicitly; provisioning stays an explicit
  operator action so network access and build hooks cannot hide inside verification.

### Testing Requirements
```bash
python3 -m unittest discover -s ../tests -v      # from this directory
python3 -m py_compile plan_workflow.py workflow.py
python3 release_check.py
```
Add a regression test for every state-machine transition, validation rule, or failure classification
you touch — the suites drive the real CLIs end to end with fake adapters, so behavior is testable
without paid calls.

### Common Patterns
- `emit(payload, code)` — one JSON object per command on stdout; nothing else is printed.
- `WorkflowError` (plus `ProviderInfrastructureError` / `NoFinalAnswer` / `ReviewContractError`)
  separates operator error, infrastructure failure, and quality failure.
- Planning state uses an HMAC signature and external revision anchor. Implementation state uses an
  immutable event chain and final state checkpoint; legacy migration validates that checkpoint before
  translating state. `run_lock` / `assignment_lock` / `file_lock` use `fcntl` for concurrency.
- Parallel A/B rounds freeze a barrier input, run concurrently, and merge atomically
  (`save_parallel_stage`, `freeze_round_barrier`) so a finisher cannot alter its peer's frozen context.
- Findings get stable `F-*` fingerprints from scope id, problem, and causal chain, so later rounds must
  dispose known ledger keys instead of re-reporting them.
- Agents are launched through `isolated_agent_command()`: bubblewrap allowlist mount, private HOME/TMP,
  minimal environment, read-only fixed-SHA worktree.

### Command Surface
`plan_workflow.py`: `preflight`, `init`, `investigate`, `draft`, `cross-review`, `diverge`,
`synthesis-context`, `check-synthesis`, `submit-synthesis`, `convergence-review`, `final-review`,
`adjudicate`, `status`, `next`, `export`, `audit-export`, `abandon`, `cleanup`, `migrate-engine`.

`workflow.py`: `preflight`, `list`, `init`, `contract-review`, `contract-adjudicate`, `review`,
`verify`, `adjudicate`, `accept`, `status`, `migrate`, `finalize`, `reconcile`, `submit-reconciliation`, `abandon-reconciliation`, `supersede`, `change-reviewer`,
`cleanup`.

## Dependencies

### Internal
- `../references/reviewer_prompt.md` and `../references/contract_reviewer_prompt.md` — loaded by
  `workflow.py` as `PROMPT_TEMPLATE` / `CONTRACT_PROMPT_TEMPLATE`.
- `../SKILL.md` and `../references/planning_workflow.md` — hashed by `plan_workflow.engine_contract()`.
- `../evals/evals.json`, `../LICENSE`, `../SECURITY.md`, `../VERSION`, `../README.md`,
  `../CHANGELOG.md`, `../.github/workflows/ci.yml` — read by `release_check.py`.

### External
Python 3.11+ stdlib (`argparse`, `fcntl`, `hmac`, `hashlib`, `json`, `resource`, `subprocess`,
`tomllib`, `secrets`); `git`; `bwrap`; the `claude` / `codex` / `dsh` CLIs.

<!-- MANUAL: -->
