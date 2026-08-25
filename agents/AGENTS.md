<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-26 | Updated: 2026-08-26 -->

# agents

## Purpose
Host-facing presentation and invocation policy for the skill. This is metadata consumed by an agent
host's UI and dispatcher — it declares how the skill is labelled and, critically, that it must never be
invoked implicitly.

## Key Files
| File | Description |
|------|-------------|
| `openai.yaml` | Six-line manifest. `interface`: `display_name` ("Grounded Build"), `short_description`, and `default_prompt` (the pre-filled invocation, which ends "and stop before implementation"). `policy.allow_implicit_invocation: false`. |

## For AI Agents

### Working In This Directory
- **Do not set `allow_implicit_invocation: true`.** Grounded Build spends real money on isolated
  provider calls and is explicitly scoped to run only when the user names it — `SKILL.md` says "Use only
  when the user explicitly requests grounded-build", and generic "implement this plan" requests belong
  to `implement-plan-with-review`.
- The `default_prompt` must keep stopping before implementation. Planning approval is never
  implementation approval, and this string is what a user clicks without reading.
- Keep `display_name` and `short_description` consistent with `../SKILL.md` frontmatter and
  `../README.md`; the description here is the short form of the same contract.
- The filename names the host family. Add a sibling file (rather than editing this one) if another host
  needs different metadata.
- English only — the release gate scans `.yaml` files too.

### Testing Requirements
```bash
python3 -c "import yaml,sys; yaml.safe_load(open('agents/openai.yaml'))"   # if PyYAML is present
python3 scripts/release_check.py
```
No test asserts this file's content today; verify changes by reading it against `SKILL.md`.

### Common Patterns
Declarative metadata only — no logic, no paths, no secrets, no model names.

## Dependencies

### Internal
Mirrors `../SKILL.md` frontmatter (`name`, `description`) and the boundaries in `../README.md`.

### External
Consumed by the agent host that installs this skill; not read by any script in this repository.

<!-- MANUAL: -->
