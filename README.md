# Grounded Build

Grounded Build is a local coding-agent skill for evidence-backed planning and reviewed implementation.
It keeps agent agreement separate from evidence, freezes every run at an exact Git commit, and makes
workflow state—not conversation memory—the authority for the next action.

Version `v0.6.0` is a public beta. The state machines and release gate are production-oriented, but the
project intentionally does not claim general availability until it has broader provider and repository
coverage in real-world use.

## Why Grounded Build

Large coding tasks fail in predictable ways: agents inspect different repository states, reviewers approve
moving branches, implementation begins before scope is settled, and infrastructure failures are mistaken
for code defects. Grounded Build addresses those failure modes with two independent workflows:

- **Plan** — two isolated planning slots investigate one frozen Git snapshot, draft independently,
  cross-review evidence, and review a host-synthesized implementation plan.
- **Implement** — the current host executes an approved plan in a run-owned worktree while a fresh CLI
  reviewer evaluates exact commit SHAs through a bounded review and verification loop.

The workflows use separate state roots and never import or rewrite each other's runs.

## Guarantees

- The original checkout is not modified before explicit final integration.
- Planning and review operate on exact, recorded Git SHAs rather than moving branch names.
- Same-round planning slots receive frozen inputs and run concurrently.
- Provider failures, timeouts, malformed output, and missing environments are infrastructure outcomes,
  not automatic code-quality failures.
- Typed user decisions stop the state machine when product authority is required.
- Planning state is authenticated; implementation state has an append-only event chain and checkpoint.
- Verification uses a read-only worktree, private HOME/TMP, minimal environment, bounded resources, and
  an explicit network policy.

These controls do not create an adversarial boundary against the operating-system account that installed
the skill. Read [SECURITY.md](SECURITY.md) before using it with sensitive repositories.

## Compatibility

| Component | Supported | Notes |
|---|---:|---|
| Linux | Yes | Bubblewrap and Linux namespaces are required. |
| Python | 3.11–3.13 | Runtime uses only the standard library. |
| Git | Yes | Worktrees and atomic ref updates are core primitives. |
| Claude CLI | Plan + Implement reviewer | Tested through a restricted fresh process. |
| Codex CLI | Plan + Implement reviewer | Adapter identity is separate from configured model identity. |
| dsh | Plan only | Uses the model selected by the local harness; not an Implement reviewer. |
| macOS / Windows | No | No silent fallback to a weaker sandbox is provided. |

At least one supported provider CLI must already be installed and authenticated. Grounded Build never
installs provider CLIs, project dependencies, or interpreters on the user's behalf.

## Install

After cloning this repository, choose one installation style.

For development, link the checkout so updates are immediately visible:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)" "$HOME/.agents/skills/grounded-build"
```

For a versioned GitHub release, extract the archive into the skill directory:

```bash
mkdir -p "$HOME/.agents/skills"
tar -xzf grounded-build-v0.6.0.tar.gz -C "$HOME/.agents/skills"
```

The archive contains one top-level `grounded-build/` directory. Verify the adjacent checksum first:

```bash
sha256sum --check grounded-build-v0.6.0.tar.gz.sha256
```

If the destination already exists, move it aside deliberately before installing. Do not overlay two
versions: in-flight planning runs detect engine drift and require an audited `migrate-engine` decision.

## Quick start

### Create a plan

Ask the host explicitly to use the skill:

```text
Use $grounded-build to create a repository-grounded implementation plan for this change.
Use two isolated planners, require evidence-backed cross-review, and stop before implementation.
```

Use deep planning only when the risk justifies the additional paid calls:

```text
Use $grounded-build in deep planning mode for this security-sensitive refactor.
Allow authoritative upstream documentation during investigation, but do not widen scope.
```

### Implement an approved plan

```text
Use $grounded-build to implement this approved plan and batch manifest.
Keep the original checkout untouched until I approve final integration.
```

An existing plan can enter Implement directly; it does not need to pay for Plan again. If the plan has no
batch manifest, the host derives a finite manifest and obtains confirmation before freezing it.

### Resume safely

```text
Resume grounded-build run <run-id>. Determine which workflow owns it, follow the recorded next action,
and do not initialize a replacement.
```

Planning state is stored under `~/.grounded-build/planning/`; implementation state is stored under
`~/.grounded-build/implementation/`. Both roots can be overridden for tests and controlled deployments.

## Operational model

```text
Plan
  frozen request + Git SHA
    -> independent investigations
    -> independent drafts
    -> mutual evidence review
    -> host synthesis
    -> independent final review
    -> explicit implementation approval

Implement
  frozen plan + batch manifest + Git SHA
    -> acceptance-contract review
    -> implementation commit
    -> fixed-SHA review / verification / bounded repair
    -> reviewed reconciliation when the target advanced
    -> explicit final integration
```

The authoritative host contract is [SKILL.md](SKILL.md). Detailed recovery and state semantics live in
[references/planning_workflow.md](references/planning_workflow.md) and
[references/implementation_workflow.md](references/implementation_workflow.md).

## Runtime notes

Use one stable, absolute CPython 3.11+ executable for every `workflow.py` command in an implementation
run. Preflight reports the controller identity and initialization freezes it.

Git-ignored `.venv` directories do not follow worktrees. Fixed-SHA verification reuses the original
project's `.venv` read-only while keeping the reviewed worktree as the current directory. Environment
fingerprinting is static and content-sensitive; it never executes `.pth` startup hooks. Grounded Build
does not run `uv sync` or install missing dependencies implicitly.

Provider CLIs require network access. Planning's `authoritative-web` option controls native research tools
and evidence policy; it is not a hard egress boundary. Use host-level network controls when required.

## Development

From a source checkout, the complete deterministic gate uses fake provider adapters and makes no paid
model calls:

```bash
python3 scripts/release_check.py
```

Focused commands are available for development:

```bash
python3 -m py_compile scripts/plan_workflow.py scripts/workflow.py scripts/package_release.py
python3 -m unittest discover -s tests -v
python3 scripts/package_release.py --output-dir dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for compatibility and pull-request requirements. Behavioral
evaluation prompts are tracked in [evals/evals.json](evals/evals.json).

## Support and security

- Usage and compatibility: [SUPPORT.md](SUPPORT.md)
- Vulnerability reporting and trust model: [SECURITY.md](SECURITY.md)
- Release history: [CHANGELOG.md](CHANGELOG.md)
- Contributor expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## License

Grounded Build is released under the [MIT License](LICENSE).
