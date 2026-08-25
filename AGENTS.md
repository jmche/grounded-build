<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# grounded-build

## Purpose
A local coding-agent skill (v0.4.1, developer preview) with two independent modes. **Plan** runs two
isolated Claude/Codex/dsh CLI instances against one frozen Git SHA, has them investigate independently,
cross-review each other's evidence, and converge on a host-synthesized implementation plan. **Implement**
executes an approved plan batch by batch in isolated Git worktrees with fixed-SHA independent review,
bounded repair, and explicit final integration. The two modes never share state. The design premise is
that *workflow state, not host memory, determines the next action*: the host asks the engine what to do
next rather than reconstructing a state machine from prose.

## Key Files
| File | Description |
|------|-------------|
| `SKILL.md` | Host-facing skill contract and entry point: routing (Plan / Implement / end-to-end), non-negotiable boundaries, and the command sequence for both workflows. Frontmatter carries `name`, `description`, and `metadata.version`. |
| `README.md` | Human-facing overview, install location, examples, runtime notes, and coexistence rules with `implement-plan-with-review`. |
| `VERSION` | Single source of truth for the release version (`0.4.1`). The release gate rejects drift against SKILL.md, README.md, CHANGELOG.md, and `scripts/plan_workflow.py`. |
| `CHANGELOG.md` | Semantic-versioned history; entries record measured behavior (e.g. limits derived from a completed 223-verification run), not intentions. |
| `SECURITY.md` | Trust model, enforced bubblewrap/filesystem boundaries, the deliberate limit of local HMAC, and private vulnerability reporting. Content is asserted by the release gate. |
| `LICENSE` | MIT license. The release gate requires it to start with `MIT License`. |
| `.github/workflows/ci.yml` | Least-privilege CI (`contents: read`) on Ubuntu 24.04: installs bubblewrap, then runs `scripts/release_check.py` as the single gate. |
| `.gitignore` | Ignores `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `*.log`, `.ipynb_checkpoints/`. |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `scripts/` | The workflow engines and the release gate (see `scripts/AGENTS.md`) |
| `references/` | Progressively disclosed contracts and agent prompts (see `references/AGENTS.md`) |
| `tests/` | Unit and integration suites using fake local adapters (see `tests/AGENTS.md`) |
| `evals/` | Behavioral evaluation prompts and assertions (see `evals/AGENTS.md`) |
| `agents/` | Host UI metadata and invocation policy (see `agents/AGENTS.md`) |

Not documented (generated or transient): `.omc/`, `.pytest_cache/`, `.ipynb_checkpoints/`, `__pycache__/`.

## For AI Agents

### Working In This Directory
- **Runtime state lives outside this repository.** Planning writes to `~/.grounded-build/planning/`
  (override: `GROUNDED_BUILD_PLAN_HOME`), implementation to `~/.grounded-build/implementation/`
  (override: `GROUNDED_BUILD_IMPLEMENT_HOME`). Never create run state inside the skill directory.
- **Editing `scripts/plan_workflow.py`, `SKILL.md`, or `references/planning_workflow.md` invalidates
  in-flight planning runs.** `engine_contract()` hashes exactly those three files; a run whose hashes
  changed rejects ordinary commands as engine drift. The only sanctioned recovery is
  `migrate-engine --reason … --actor … --apply` — never edit run state to bypass it.
- **The version must be changed in five places at once**: `VERSION`, `SKILL.md`
  (`version: X`), `README.md` (`vX`), `CHANGELOG.md` (`[X]`), and `plan_workflow.py`
  (`VERSION = "X"`). `release_check.py` fails otherwise.
- **All distributed text must be English.** The release gate scans every `.md`, `.py`, `.json`,
  `.toml`, `.txt`, `.yaml`, `.yml`, and extensionless file for Han characters and fails on any hit.
  This applies to these AGENTS.md files too.
- Documentation wording in `SKILL.md` and `references/` is asserted by `tests/test_workflow.py`
  (`DocumentationContractTests`). Rewording a contract can break the suite — that coupling is deliberate.
- Skill code is trusted; the repository under analysis and every spawned agent process are not.
  Preserve the sandbox allowlist, the private HOME/TMP, and the "no secret env inheritance" property.

### Testing Requirements
```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/plan_workflow.py scripts/workflow.py
python3 scripts/release_check.py   # the full gate: evals + release invariants + compile + unittest
```
No paid model calls occur: the suites install fake `claude`/`codex`/`dsh` executables on `PATH`.

### Common Patterns
- Every state-changing command is preview-first: a `--dry-run` or preview form is inspected before the
  real call, and destructive/adjudicating commands require an explicit `--apply`.
- Every command emits one JSON object on stdout (`emit()`); exit codes carry the classification.
- State files are written atomically and authenticated (HMAC signature plus an external revision anchor).
- Provider **adapter** identity is always recorded separately from **model** identity; the code never
  treats a CLI name as a model family.
- Infrastructure failure (auth, overload, rate limit, timeout, adapter startup) is classified apart from
  quality failure and does not consume a quality attempt.

## Dependencies

### Internal
Self-contained. `scripts/` reads `references/reviewer_prompt.md` and
`references/contract_reviewer_prompt.md` at runtime through `SKILL_ROOT`.

### External
- Python 3.11+ standard library only (`tomllib` requires 3.11) — no third-party runtime packages.
- Git (worktrees are the isolation primitive), `bubblewrap` (sandbox namespaces), Linux.
- At least one provider CLI: `claude`, `codex`, or `dsh`. Exercised with Claude Code 2.1.238 and
  Codex CLI 0.149.0.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
