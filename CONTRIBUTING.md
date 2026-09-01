# Contributing

Grounded Build welcomes focused bug fixes, security hardening, documentation improvements, and
well-evidenced workflow changes.

## Before opening a change

Use an issue for behavior changes that alter workflow authority, state transitions, compatibility, or
provider policy. Small correctness fixes and documentation corrections may go directly to a pull request.
Security reports must follow [SECURITY.md](SECURITY.md), not the public issue tracker.

## Development environment

Requirements:

- Linux with bubblewrap
- Git
- CPython 3.11, 3.12, or 3.13

The runtime is standard-library only. Tests install fake provider executables and never call paid models.

```bash
python3 scripts/release_check.py
```

Run focused tests while iterating, then run the complete gate before requesting review:

```bash
python3 -m py_compile scripts/plan_workflow.py scripts/workflow.py scripts/package_release.py
python3 -m unittest discover -s tests -v
git diff --check
```

## Change requirements

- Add a regression test for every state transition, refusal, security boundary, or failure classification.
- Preserve preview-before-apply for operations that mutate history, spend external-agent budget, discard
  evidence, or apply a user decision.
- Keep provider infrastructure outcomes separate from code-quality verdicts.
- Do not add runtime dependencies without a design discussion and an explicit compatibility decision.
- Keep distributed text in English; the release gate enforces this.
- Never weaken the sandbox or mount allowlist to make a test pass.
- Pin GitHub Actions to full commit SHAs; Dependabot may propose reviewed updates.
- Update host contracts, reviewer prompts, tests, and changelog together when their shared semantics change.

Changes to `SKILL.md`, `scripts/plan_workflow.py`, or `references/planning_workflow.md` alter the planning
engine contract. Existing runs will require an explicit audited migration after installation.

## Pull requests

A pull request should explain:

1. The observable problem and a concrete reproduction.
2. The root cause and affected state or trust boundary.
3. Why the proposed change preserves authority and compatibility.
4. The tests that fail before the change and pass after it.
5. Any migration, security, cost, or provider-compatibility impact.

Keep commits reviewable and avoid unrelated formatting churn. Generated runtime state, provider logs,
credentials, and private repository content must never be committed.

## Versioning and releases

Grounded Build uses semantic versioning:

- Patch: compatible correctness, security, and documentation fixes.
- Minor: compatible workflow capability or public-surface additions.
- Major: incompatible state, command, installation, or authority changes that cannot be migrated safely.

A release updates `VERSION`, `SKILL.md`, `README.md`, `CHANGELOG.md`, and
`scripts/plan_workflow.py` together. The release gate checks synchronization. After the release commit:

1. Create an annotated tag matching `v<version>`.
2. Push the commit and tag.
3. Confirm CI on every supported Python version.
4. Confirm the release workflow publishes the deterministic archive and checksum.
5. Verify installation from the published archive in a clean skill directory.
