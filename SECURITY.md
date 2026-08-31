# Security model

Grounded Build is a local workflow for a trusted operating-system account. The repository and spawned planning/review processes are treated as untrusted; the account owner, installed skill code, provider CLI binaries, and provider credential mechanism are trusted.

## Enforced boundaries

- Planning agents receive a read-only fixed-SHA worktree and a stage-specific context through an allowlist bubblewrap namespace.
- The original checkout, sibling planning slot, other runs, general HOME, user sockets, and unrelated provider credentials are not mounted.
- Only the selected provider's discovered configuration and authentication files are mounted for the CLI itself. Model tools are denied access to the private HOME, while both adapters retain shell access so they can inspect and measure the read-only repository.
- Native web-search/fetch tools are disabled by default. They are enabled only for investigation assignments initialized with `research_policy=authoritative-web`; the assignment contract limits evidence to official project documentation and the official GitHub repository.
- The research policy is an audited model/tool policy, not a hard network-isolation boundary. Provider CLIs require network access, and shell subprocess egress can depend on the adapter and host. Enforce a host firewall or isolated network when a hard egress boundary is required.
- Provider versions that cannot enforce the documented filesystem or native-tool restrictions are unsupported. Use short-lived or least-privilege credentials where supported.
- Secret-like host environment variables are not inherited. The sandbox receives a minimal environment and private HOME/TMP directories.
- Planning state is HMAC-authenticated and revisions are anchored outside each run directory. This detects subagent writes, accidental edits, stale restoration of a run, and partial artifact corruption.
- Implementation verification uses the stricter sandbox and resource controls documented in `references/implementation_workflow.md`.
- Environment fingerprinting reads `.venv` content from the controller process and never launches the
  project interpreter. Project `.pth` and startup hooks execute only inside an authorized verification
  sandbox, never as a side effect of preflight, status, review, or state validation.
- The `.venv` root must be a real directory. Fingerprinting records symlink text but never resolves or
  reads a symlink target, and file, byte, and elapsed-time ceilings bound every scan.
- An interpreter reached through a `.venv` launcher symlink is a declared host trust root, not part of
  the venv content digest. Use an immutable toolchain when external-runtime attestation is required.

## Deliberate limit

The workflow cannot defend itself from the same trusted OS account deliberately modifying the installed Python code, deleting/replacing the integrity key and revision anchors, or bypassing the CLI. No local program running under one account can create that privilege separation by itself. Do not describe the local HMAC as protection against the machine owner. Teams that require an adversarial operator boundary must place signing/attestation in a separately administered service or OS account.

## Reporting

Use GitHub's private vulnerability reporting for this repository when it is enabled. If it is unavailable, open a public issue containing no sensitive details and ask the maintainers for a private reporting channel. Never attach credentials, private source, or complete workflow logs to a public issue.
