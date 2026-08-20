# Security model

Grounded Build is a local workflow for a trusted operating-system account. The repository and spawned planning/review processes are treated as untrusted; the account owner, installed skill code, provider CLI binaries, and provider credential mechanism are trusted.

## Enforced boundaries

- Planning agents receive a read-only fixed-SHA worktree and a stage-specific context through an allowlist bubblewrap namespace.
- The original checkout, sibling planning slot, other runs, general HOME, user sockets, and unrelated provider credentials are not mounted.
- Only the selected provider's minimum login file is mounted for the CLI itself. Claude receives a deny rule for its private HOME and no Bash/Web tools; Codex receives a filesystem deny rule for `~/.codex` and no web-search tool. Provider versions that cannot enforce those inner tool restrictions are unsupported. Use short-lived or least-privilege credentials where supported.
- Secret-like host environment variables are not inherited. The sandbox receives a minimal environment and private HOME/TMP directories.
- Planning state is HMAC-authenticated and revisions are anchored outside each run directory. This detects subagent writes, accidental edits, stale restoration of a run, and partial artifact corruption.
- Implementation verification uses the stricter sandbox and resource controls documented in `references/implementation_workflow.md`.

## Deliberate limit

The workflow cannot defend itself from the same trusted OS account deliberately modifying the installed Python code, deleting/replacing the integrity key and revision anchors, or bypassing the CLI. No local program running under one account can create that privilege separation by itself. Do not describe the local HMAC as protection against the machine owner. Teams that require an adversarial operator boundary must place signing/attestation in a separately administered service or OS account.

## Reporting

Before public release, add the repository's private vulnerability-reporting address here. Do not attach credentials, private source, or complete workflow logs to a public issue.
