# Support

## Supported environment

The current public beta supports Linux, Git, the `bubblewrap` and `socat` sandbox packages, and
CPython 3.11–3.13. Claude, Codex, and dsh
are supported for both planning and implementation review. Provider CLI authentication, harness
configuration, and model availability remain the operator's responsibility.

The latest minor release receives compatibility and security fixes. Older minor releases remain
available for audit and rollback but are not actively supported unless a security advisory states otherwise.

## Getting help

Use GitHub Issues for reproducible defects and GitHub Discussions for usage questions when Discussions is
enabled. Before filing a defect, include only non-sensitive diagnostics:

- Grounded Build version
- Python, Git, `bubblewrap`, `socat`, and provider CLI versions
- Operating-system distribution and version
- Workflow mode, status, and emitted error classification
- A minimal reproduction using a public or synthetic repository when possible

Do not attach credentials, private source, complete prompts, raw provider logs, or centralized run-state
directories. Redact home paths and repository names when they are not necessary to reproduce the issue.

Feature requests should describe the authority boundary, expected observable behavior, and why existing
typed decisions or configuration cannot represent the need.

## Security issues

Do not report suspected sandbox escapes, credential exposure, state-authentication bypasses, or forged
completion paths in a public issue. Follow [SECURITY.md](SECURITY.md).
