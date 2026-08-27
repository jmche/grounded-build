# Grounded Build

Grounded Build is a local coding-agent skill with two independent modes:

1. **Plan:** two isolated Claude/Codex instances investigate the same Git snapshot independently before
   drafting, integrate each other's evidence without widening scope, and converge on a host-synthesized plan.
2. **Implement:** execute an approved plan batch by batch in isolated Git worktrees with fixed-SHA review, bounded repair, evidence tracking, and explicit final integration.

Version `v0.4.5` is a developer preview. Linux, Git, Python 3.11+, bubblewrap, and at least one of the `claude`, `codex`, or `dsh` CLI adapters are required.

The sandbox is exercised with Claude Code 2.1.238 and Codex CLI 0.149.0. Codex is treated as an adapter: its configured model may be GPT, DeepSeek through an OpenAI-compatible gateway, or another model. The run freezes and reports the non-secret model identity separately from the adapter.

## Install

Place this directory at:

```text
~/.agents/skills/grounded-build/
```

The host discovers usage from `SKILL.md`. Runtime history is stored outside the skill:

```text
~/.grounded-build/
├── planning/
└── implementation/
```

## Example

```text
Use $grounded-build to create a repository-grounded plan for this change.
Use Claude and Codex as isolated planners, have them cross-review each other,
use Codex for final review, and stop before implementation.
```

Use `--planning-depth deep` when the risk justifies an additional `draft_03` divergence round and two
bounded convergence reviews. Use `--research-policy authoritative-web` only when official upstream evidence
may be needed; local-only is the default.

If only one provider is available:

```text
Use $grounded-build with two isolated Claude instances. They must draft
independently, review each other, and both review the synthesized plan.
```

An existing plan can go directly to Implement mode without paying for planning again.

## Implementation runtime notes

Invoke `scripts/workflow.py` with one stable, absolute CPython 3.11+ path for the lifetime of a run.
Preflight reports the exact controller identity and initialization freezes it, preventing a later shell
from silently selecting a different `python3` through PATH.

Project `.venv` directories are normally ignored and therefore do not follow Git worktrees. Fixed-SHA
verification deliberately reuses `<project>/.venv` read-only while keeping the reviewed worktree as cwd.
If it is absent, preflight reports whether uv metadata and the uv executable are available, but Grounded
Build does not install dependencies automatically. Provisioning remains an explicit operator action so
network access, lockfile selection, and executed build hooks cannot be hidden inside verification.

## Coexistence with implement-plan-with-review

This skill does not replace or migrate the existing `implement-plan-with-review` installation. Generic requests to implement an existing plan should continue using that skill. Select `grounded-build` when independently generating or auditing the plan, when handing off a plan produced by this skill, or when the user names it explicitly. The two skills use different environment variables and state roots; old runs are never imported automatically.

## Test

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/plan_workflow.py scripts/workflow.py
python3 scripts/release_check.py \
  --quick-validator ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py
```

The planning workflow uses fake local agent adapters in tests; no paid model calls are made by the unit suite.

Run `plan_workflow.py next` after every planning action. Same-round A/B work is returned as a `RUN_AGENT_BATCH`; launch all listed commands concurrently and wait at the barrier. Other states return one legal action. This keeps orchestration independent of host memory while preventing accidental serialization and asymmetric round inputs.

Planning uses Opus and `gpt-5.6-sol` by default. Implementation uses the current host unchanged and defaults only the isolated reviewer to Opus or `gpt-5.6-sol`. Every default can be overridden explicitly, including `cli-default` to defer to a CLI configuration.

## Security

Read [SECURITY.md](SECURITY.md) for the trust model, enforced filesystem boundaries, network-policy
limitations, and private vulnerability-reporting guidance.

## License

Grounded Build is released under the [MIT License](LICENSE).
