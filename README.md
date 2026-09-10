# Grounded Build

**Stop coding agents from reviewing a moving target.**

Grounded Build turns a risky repository change into an evidence-backed plan and a fixed-SHA,
independently reviewed implementation. Every agent works from a recorded Git snapshot, workflow
state—not conversation memory—decides what happens next, and your original checkout stays untouched
until explicit final integration.

[![CI](https://github.com/jmche/grounded-build/actions/workflows/ci.yml/badge.svg)](https://github.com/jmche/grounded-build/actions/workflows/ci.yml)
[![Python 3.11–3.13](https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Linux](https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black)](https://www.kernel.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## The problem it solves

Large agent-driven changes often fail for reasons that ordinary code review cannot see:

- one agent investigates a different commit from the agent reviewing it;
- a reviewer approves a branch that moved after inspection;
- implementation starts before total scope and acceptance criteria are settled;
- an authentication failure, timeout, or missing environment is reported as a code defect;
- a long conversation loses the decision that should govern the next action.

Grounded Build makes those boundaries explicit and auditable.

```text
Your repository at one frozen SHA
                 │
        ┌────────┴────────┐
        │                 │
 independent         independent
 investigation       investigation
        │                 │
        └──── evidence cross-review ────┐
                                        │
                              implementation plan
                                        │
                         run-owned implementation tree
                                        │
                         independent fixed-SHA review
                                        │
                              explicit integration
```

## Install

Grounded Build is currently installed from source.

### Prerequisites

| Requirement | Why | Missing behaviour |
|---|---|---|
| Linux | Namespaces are the isolation primitive | Unsupported platform |
| Git | Worktrees and atomic ref updates | Cannot initialize a run |
| CPython 3.11–3.13 | Controller and workflow engine | Cannot start |
| `bubblewrap` (`bwrap`) | The fixed-SHA verification sandbox, and the namespace every agent is launched into | Verification and agent launch refuse to run |
| `socat` | Required by the provider CLI's own sandbox, which Grounded Build enables **fail-closed** for every planning and review invocation | The reviewer CLI refuses to start: `sandbox is enabled but dependencies are missing: socat not installed` |
| At least one authenticated agent CLI | Planning slots and the independent reviewer | No adapter to invoke |

`socat` is easy to miss because Grounded Build never invokes it directly — it is a transitive
requirement of the provider CLI's sandbox, and it surfaces only as a CLI startup error at the first
review. Install both sandbox packages together:

```bash
sudo apt install bubblewrap socat      # Debian/Ubuntu
```

Verify before the first run:

```bash
bwrap --version && command -v socat
```

There is no fallback for either. A missing sandbox dependency refuses to start rather than quietly
downgrading to a weaker boundary, which is the same rule the rest of the workflow follows.

```bash
git clone https://github.com/jmche/grounded-build.git
cd grounded-build
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)" "$HOME/.agents/skills/grounded-build"
```

If that destination already exists, move it aside deliberately instead of overlaying two versions.
An in-flight run detects controller drift and will not silently continue under changed engine code.

## Quick start

Then ask your host agent explicitly:

```text
Use $grounded-build to create a repository-grounded implementation plan for this change.
Use two isolated planners, require evidence-backed cross-review, and stop before implementation.
```

Already have an approved plan? Enter implementation directly:

```text
Use $grounded-build to implement this approved plan and batch manifest.
Keep the original checkout untouched until I approve final integration.
```

Grounded Build does not require you to pay for Plan again before Implement. If an existing plan has
no finite batch manifest, the host derives one and obtains confirmation before freezing it.

Plan mode accepts `--base-ref <ref>` (default `HEAD`) and freezes that ref's committed SHA. Like
Implement mode, it can start while the source checkout is dirty: uncommitted content is excluded from
the run-owned worktree rather than copied, stashed, committed, or used as hidden planning authority.
The init result records the selected ref, frozen SHA, and every excluded status entry.

Implement mode freezes the committed SHA of the selected target branch, so a dirty current checkout
does not block initialization and is never copied into run worktrees. The init result lists those
excluded changes. If an uncommitted `AGENTS.md`, `CLAUDE.md`, or another project contract must govern
the run, name it explicitly with repeatable `--instruction-file`; it is frozen separately and supplied
to contract and code reviewers. Supplementary instructions are bounded UTF-8 text and cannot override
workflow security, scope, or review authority. Final integration still refuses a dirty checked-out target,
and both status and finalize preview expose that apply blocker.

## What makes it different

| Capability | Typical direct agent workflow | Grounded Build |
|---|---:|---:|
| Every reviewer sees one recorded Git snapshot | Not guaranteed | Yes |
| Planning evidence is reviewed separately from agreement | Rarely | Yes |
| Repairs target the earliest responsible production boundary | Inconsistent | Yes |
| Review is bound to an exact implementation commit | Inconsistent | Yes |
| Original checkout stays untouched during work | Not guaranteed | Yes |
| Infrastructure failures stay separate from quality failures | Rarely | Yes |
| Resume follows durable workflow state | Conversation-dependent | Yes |
| Integration requires an explicit decision | Tool-dependent | Yes |

Grounded Build is designed for migrations, security-sensitive refactors, cross-cutting features,
state-machine changes, and other work where a plausible-looking answer is not enough. It is usually
too heavy for a typo or a tiny local edit.

## Operational model

Plan and Implement are independent workflows with separate state and review boundaries.

### Plan

Two isolated planning slots inspect one frozen Git commit. They investigate independently, draft
independently, and cross-review evidence. Standard mode sends the host-synthesized plan to one fresh
instance of the current host adapter for final review; deep or explicitly high-assurance runs may send
it to both planning slots instead. Slot B and the implementation reviewer prefer a usable non-host
adapter, while explicit user selections always win.

```text
frozen request + Git SHA
  -> independent investigations
  -> independent drafts
  -> mutual evidence review
  -> host synthesis
  -> independent final review
  -> explicit implementation approval
```

For higher-risk work, deep mode adds another convergence round:

```text
Use $grounded-build in deep planning mode for this security-sensitive refactor.
Allow authoritative upstream documentation during investigation, but do not widen scope.
```

### Implement

The current host implements an approved plan in a run-owned worktree. A fresh CLI reviewer first
derives the acceptance contract, then evaluates fixed commits through a bounded review,
verification, and repair loop.

The first code review receives the complete authoritative range. Follow-up reviews receive the patch
since the preceding reviewed SHA while retaining the full contract, finding ledger, and exact-HEAD
worktree. Rewritten history falls back to full transport, and semantic impact can always widen the
reviewer's inspection beyond the supplied patch. Same-SHA reviews after a user decision and cumulative
final reviews retain full transport because their new evidence is not bounded by a code delta.

```text
frozen plan + batch manifest + Git SHA
  -> acceptance-contract review
  -> implementation commit
  -> fixed-SHA review and verification
  -> bounded repair when needed
  -> reviewed reconciliation if the target advanced
  -> explicit final integration
```

Plan and Implement use separate state roots. They never import or rewrite each other's runs.

Across both workflows, Grounded Build traces the production chain from authority through actual input,
producer output, consumer interpretation, and observed behavior. It uses reproducible checks for machine
facts and independent semantic review for meaning, adapting the evidence to deterministic, generative,
external, human, and hybrid producers without adding keyword-based project gates.

## Guarantees

- The original checkout is not modified before explicit final integration.
- Planning and review operate on recorded Git SHAs, not moving branch names.
- Same-round planning slots receive frozen inputs and run concurrently.
- Provider failures, timeouts, malformed output, and missing environments are infrastructure
  outcomes—not automatic code-quality failures.
- Typed user decisions stop the state machine when product authority is required.
- Planning state is authenticated; implementation state has an append-only event chain and
  checkpoint.
- Verification uses a read-only worktree, private HOME/TMP, minimal environment, bounded resources,
  and an explicit network policy.

These controls do not create an adversarial security boundary against the operating-system account
that installed the skill. Read [SECURITY.md](SECURITY.md) before using it with sensitive repositories.

## Compatibility

| Component | Supported | Notes |
|---|---:|---|
| Linux | Yes | Bubblewrap and Linux namespaces are required. |
| Python | 3.11–3.13 | Runtime uses only the standard library. |
| Git | Yes | Worktrees and atomic ref updates are core primitives. |
| `bubblewrap` | Required | Verification sandbox and agent launch namespaces. |
| `socat` | Required | Needed by the provider CLI's fail-closed sandbox; not invoked by Grounded Build itself. |
| Claude CLI | Plan + Implement reviewer | Tested through a restricted fresh process. |
| Codex CLI | Plan + Implement reviewer | Adapter identity is separate from model identity. |
| dsh | Plan + Implement reviewer | Uses the local harness selection unless overridden. |
| Generic `other` bridge | Plan + Implement reviewer | One `grounded-build-other-v1` protocol supports conforming hosts without agent-specific engine branches. |
| macOS / Windows | No | No silent fallback to a weaker sandbox is provided. |

At least one provider CLI must already be installed and authenticated. For Pi, OpenCode, or another
host without a built-in adapter, set `GROUNDED_BUILD_OTHER_COMMAND` to an absolute executable that
implements the bridge protocol documented in `references/planning_workflow.md`. Grounded Build never
installs provider CLIs, bridge wrappers, project dependencies, or interpreters on your behalf.

## Resume without guessing

```text
Resume grounded-build run <run-id>. Determine which workflow owns it, follow the recorded next
action, and do not initialize a replacement.
```

Planning state lives under `~/.grounded-build/planning/`; implementation state lives under
`~/.grounded-build/implementation/`. Both roots can be overridden for tests and controlled
deployments.

## Public beta status

Version `v0.7.0` is a public beta. Its state machines, sandbox boundaries, deterministic tests, and
release gate are production-oriented, but broader provider and repository coverage is still needed
before a general-availability claim.

No versioned GitHub Release is published yet. Install from source for now; release archives and
checksums will be offered through GitHub Releases after the first tagged release.

## Runtime notes

Use one stable, absolute CPython 3.11+ executable for every `workflow.py` command in an implementation
run. Preflight reports the controller identity and initialization freezes it.

Git-ignored `.venv` directories do not follow worktrees. Fixed-SHA verification reuses the original
project's `.venv` read-only while keeping the reviewed worktree as the current directory. Environment
fingerprinting is static and content-sensitive; it never executes `.pth` startup hooks. Grounded
Build does not run `uv sync` or install missing dependencies implicitly.

Provider CLIs require network access. Planning's `authoritative-web` option controls native research
tools and evidence policy; it is not a hard egress boundary. Use host-level network controls when a
hard boundary is required.

The authoritative host contract is [SKILL.md](SKILL.md). Detailed recovery and state semantics live
in [references/planning_workflow.md](references/planning_workflow.md) and
[references/implementation_workflow.md](references/implementation_workflow.md).

## Development

The complete deterministic release gate uses fake provider adapters and makes no paid model calls:

```bash
python3 scripts/release_check.py
```

Focused commands:

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
