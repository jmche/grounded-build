# Grounded Build

Grounded Build is a local coding-agent skill with two independent modes:

1. **Plan:** two isolated Claude/Codex instances inspect the same Git snapshot, draft independently, cross-review factual claims, and review a host-synthesized implementation plan.
2. **Implement:** execute an approved plan batch by batch in isolated Git worktrees with fixed-SHA review, bounded repair, evidence tracking, and explicit final integration.

The first public version is `v0.1.0` (Developer Preview). Linux, Git, Python 3.11+, bubblewrap, and at least one of the `claude` or `codex` CLIs are required.

The v0.1.0 sandbox was exercised with Claude Code 2.1.235 and Codex CLI 0.147.0. Older provider versions that do not honor the configured read-deny/tool policy are unsupported.

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

If only one provider is available:

```text
Use $grounded-build with two isolated Claude instances. They must draft
independently, review each other, and both review the synthesized plan.
```

An existing plan can go directly to Implement mode without paying for planning again.

## Coexistence with implement-plan-with-review

This skill does not replace or migrate the existing `implement-plan-with-review` installation. Generic requests to implement an existing plan should continue using that skill. Select `grounded-build` when independently generating or auditing the plan, when handing off a plan produced by this skill, or when the user names it explicitly. The two skills use different environment variables and state roots; old runs are never imported automatically.

## Test

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/plan_workflow.py scripts/workflow.py
```

The planning workflow uses fake local agent adapters in tests; no paid model calls are made by the unit suite.
