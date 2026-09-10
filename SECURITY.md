# Security model

Grounded Build is a local workflow for a trusted operating-system account. The repository and spawned planning/review processes are treated as untrusted; the account owner, installed skill code, provider CLI binaries, and provider credential mechanism are trusted.

## Supported versions

| Version | Security support |
|---|---|
| 0.7.x | Supported public beta |
| 0.6.x and earlier | Upgrade required unless an advisory says otherwise |

## Enforced boundaries

- Planning agents and implementation reviewers receive a read-only fixed-SHA worktree and a
  stage-specific context through an allowlist bubblewrap namespace.
- The original checkout, sibling planning slot, other runs, general HOME, user sockets, and unrelated provider credentials are not mounted.
- Only the selected provider's discovered configuration and authentication files are mounted for the
  CLI itself. Model tools are denied access to the private HOME. Claude and Codex retain restricted
  read-only repository inspection; DSH receives path-bounded file tools and no shell, runtime,
  subagent, workflow, or editor tool.
- Native web-search/fetch tools are disabled by default. They are enabled only for investigation assignments initialized with `research_policy=authoritative-web`; the assignment contract limits evidence to official project documentation and the official GitHub repository.
- The research policy is an audited model/tool policy, not a hard network-isolation boundary. Provider CLIs require network access, and shell subprocess egress can depend on the adapter and host. Enforce a host firewall or isolated network when a hard egress boundary is required.
- Provider versions that cannot enforce the documented filesystem or native-tool restrictions are unsupported. Use short-lived or least-privilege credentials where supported.
- Secret-like host environment variables are not inherited. The sandbox receives a minimal environment and private HOME/TMP directories.
- Planning state is HMAC-authenticated and revisions are anchored outside each run directory. This detects subagent writes, accidental edits, stale restoration of a run, and partial artifact corruption.
- Implementation verification uses the stricter execution sandbox and resource controls documented in `references/implementation_workflow.md`.
- The dsh planning and implementation paths inject a fail-closed read boundary that permits file
  tools only inside the frozen Git worktree and invocation context. The implementation controller
  captures a bounded exact fixed-SHA diff in that context before launch, falling back to a bounded
  changed-path manifest for oversized binary patches. DSH returns its complete JSON in
  the final stream and receives no writable reviewer channel; setup never changes shared Git config.
- Environment fingerprinting reads `.venv` content from the controller process and never launches the
  project interpreter. Project `.pth` and startup hooks execute only inside an authorized verification
  sandbox, never as a side effect of preflight, status, review, or state validation.
- The `.venv` root must be a real directory. Fingerprinting records symlink text but never resolves or
  reads a symlink target, rejects special files and unreadable subtrees, and applies file, byte, and
  elapsed-time ceilings to every scan. Cache-named directories receive no content exemption.
- An interpreter reached through a `.venv` launcher symlink is a declared host trust root, not part of
  the venv content digest. Use an immutable toolchain when external-runtime attestation is required.

## Deliberate limit

The workflow cannot defend itself from the same trusted OS account deliberately modifying the installed Python code, deleting/replacing planning integrity keys and revision anchors, or bypassing the CLI. No local program running under one account can create that privilege separation by itself. Planning HMACs and implementation checkpoints detect ordinary corruption and unauthorized sandbox writes; they are not protection against the machine owner. Teams that require an adversarial operator boundary must place signing/attestation in a separately administered service or OS account.

## Reporting

Use GitHub's private vulnerability reporting for this repository when it is enabled. Include the affected
version, trust boundary, minimal reproduction, and impact. If private reporting is unavailable, open a public
issue containing no vulnerability details and ask the maintainers to establish a private channel.

Never attach credentials, private source, complete prompts, centralized run state, or raw workflow logs to a
public issue. Maintainers should acknowledge valid private reports before public discussion and coordinate
disclosure after a fix or documented mitigation is available; no fixed response-time promise is made during
the public beta.
