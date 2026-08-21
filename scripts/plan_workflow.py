#!/usr/bin/env python3
"""Evidence-backed, resumable planning workflow for grounded-build."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("claude", "codex")
MAX_INVOCATIONS_PER_ASSIGNMENT = 3
MAX_SYNTHESIS_SUBMISSIONS = 2
TERMINAL_STATUSES = {"READY", "ABANDONED"}
SKILL_ROOT = Path(__file__).resolve().parent.parent

#: Codex's tool policy for a planning invocation, expressed as command-line overrides.
#:
#: These used to be written into a synthetic ``config.toml`` inside the private home, which
#: silently REPLACED the user's own. A codex configured against a self-hosted gateway lost its
#: provider, its base URL, and the bearer token that lives beside them, fell back to the vendor
#: default endpoint, and returned 401 -- reported as a planning failure rather than as the
#: configuration loss it was. ``-c`` layers over the file instead of erasing it (``codex --help``:
#: "Override a configuration value that would otherwise be loaded from ~/.codex/config.toml"), so
#: the policy is enforced without the workflow having to own the file, and it is visible in the
#: dry-run where a written file was not.
CODEX_POLICY_OVERRIDES = (
    "-c", 'default_permissions="grounded_build"',
    "-c", 'permissions.grounded_build.filesystem='
          '{"~/.codex"="deny",":workspace_roots"={"."="read"}}',
    "-c", "tools.web_search=false",
)


DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
        "slot": {"type": "string", "enum": ["A", "B"]},
        "baseline_sha": {"type": "string"},
        "summary": {"type": "string"},
        "repository_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["VERIFIED", "INFERRED", "UNRESOLVED"]},
                },
                "required": ["id", "claim", "evidence", "confidence"],
            },
        },
        "plan_markdown": {"type": "string"},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "provider", "slot", "baseline_sha", "summary", "repository_facts",
        "plan_markdown", "unresolved_questions",
    ],
}


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
        "reviewer_slot": {"type": "string", "enum": ["A", "B", "F"]},
        "target": {"type": "string"},
        "baseline_sha": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_USER_DECISION"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "required_change": {"type": "string"},
                },
                "required": ["id", "severity", "claim", "evidence", "required_change"],
            },
        },
    },
    "required": ["provider", "reviewer_slot", "target", "baseline_sha", "verdict", "summary", "findings"],
}


class WorkflowError(RuntimeError):
    pass


class NoFinalAnswer(WorkflowError):
    """The agent never delivered a verdict — as distinct from delivering a bad one.

    An empty output file is not invalid JSON. It is the absence of an answer, and the two have
    different causes and different remedies: a malformed object means the reviewer reached a
    judgement and mis-shaped it, while an empty one means the turn ended with the reviewer still
    reading. Reporting both as "returned invalid JSON" states something true about the bytes and
    something false about what happened, and sends the operator looking for a parsing problem that
    is not there. Three cross-review attempts were spent before the log line that actually said so
    -- codex's own "no last agent message" -- was read out of a 735 KB stderr.

    Carried as its own class so a retry can name the delivery failure without inspecting strings.
    """


def describe_unparseable(provider: str, source: str, exc: json.JSONDecodeError) -> str:
    """Say which of the two things happened: the object was cut off, or it was never an object.

    Both arrive as a JSONDecodeError and they are not the same failure. An answer that begins
    ``{"provider": ...`` and dies inside a string near the end is a COMPLETE judgement truncated in
    transit -- the reviewer did everything asked and the output ran out of room, so the remedy is
    a smaller object, not a different reviewer. An answer that was never JSON is a reviewer that
    submitted its deliberation instead of its verdict, and no amount of room fixes that.

    Calling the truncated case "prose" -- which this function was written to stop doing -- sent the
    operator looking for the second problem while holding the first, one commit after the same
    mistake was fixed one level up.
    """
    text = source.strip()
    # Truncation is structural, not positional: what a cut-off object lacks is its closing
    # bracket. A fraction-of-the-way-in threshold would be a number picked to fit one sample.
    # "Extra data" is the tell for the other shape -- prose that happens to open with a brace,
    # which is what one real attempt produced ("unregistered:{}, status_drift:{}...").
    opener = text[:1]
    closer = {"{": "}", "[": "]"}.get(opener)
    truncated = bool(closer) and not text.endswith(closer) and not exc.msg.startswith("Extra data")
    if truncated:
        return (
            f"{provider} emitted the schema object and it was cut off: {len(source)} characters, "
            f"no closing {closer!r}, parse failed at character {exc.pos} ({exc.msg}). "
            f"An incomplete verdict is not a verdict, but the reviewer did deliver; the object was "
            f"too large for the output it had."
        )
    return (
        f"{provider} delivered something that is not the schema object ({len(source)} characters, "
        f"first parse error at character {exc.pos}: {exc.msg}). The judgement may have been "
        f"reached; it was not emitted in the required shape."
    )


def no_answer_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Quote the CLI's own account of the missing answer, when it gave one."""
    for marker in ("no last agent message", "stream disconnected", "context window"):
        if marker in (result.stderr or ""):
            return f" (the CLI reported: {marker!r})"
    return ""


PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"provider": {"type": "string"}, "ready": {"type": "boolean"}},
    "required": ["provider", "ready"],
}


def probe_provider(provider: str, project: Path, timeout: int = 180) -> dict[str, Any]:
    """Can this CLI authenticate and return one small schema object right now?

    Cheap, and deliberately narrow. It answers the question that cost the most to answer the
    expensive way: a codex whose credentials never reached the sandbox spent 22 minutes and five
    401s before saying so, and the operator read it as a failed planning draft. One trivial call
    says it in seconds, before a topology is frozen around a provider that cannot deliver.

    What it does NOT establish is reliability on a real assignment. A provider that answers this
    can still spend a whole turn reading a repository and never emit its verdict -- which is what
    happened three times after the credential fix landed. Capability is necessary and not
    sufficient, and this reports capability only.

    It runs through the SAME bubblewrap composition as a real invocation, which is the only way it
    can see the failure it exists to catch: the credentials that went missing went missing because
    of the sandbox, so a probe outside the sandbox would have passed while every real call
    returned 401 -- worse than no probe, because it would have vouched for the thing that was
    broken.
    """
    with tempfile.TemporaryDirectory(prefix="grounded-build-probe-") as scratch:
        root = Path(scratch)
        context = root / "context"
        context.mkdir(mode=0o700)
        raw = root / "raw.json"
        prompt = (
            f"Reply with the schema object only: provider={provider}, ready=true. "
            "Do not inspect anything. This is a capability check."
        )
        command = agent_command(provider, project, context, PROBE_SCHEMA, raw, prompt)
        command = isolated_agent_command(command, {}, root, project, context)
        result = run(command, cwd=project, timeout=timeout, env=agent_environment())
        if result.returncode != 0:
            return {"provider": provider, "ok": False,
                    "reason": f"invocation failed ({result.returncode})",
                    "detail": (result.stderr or "").strip()[-400:]}
        try:
            payload = extract_payload(provider, result, raw)
        except NoFinalAnswer as exc:
            return {"provider": provider, "ok": False, "reason": "no schema object",
                    "detail": str(exc)}
        return {"provider": provider, "ok": isinstance(payload, dict) and payload.get("ready") is True,
                "reason": "delivered a schema object"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(code)


def run(
    command: list[str], cwd: Path | None = None, timeout: int = 1800,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(str(exc)) from exc


def git(project: Path, *args: str, check: bool = True) -> str:
    result = run(["git", "-C", str(project), *args], timeout=120)
    if check and result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or result.stdout.strip() or "git failed")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_root() -> Path:
    configured = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".grounded-build" / "planning").resolve()
    )


def integrity_key() -> bytes:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ".integrity-key"
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
    key = path.read_bytes()
    if len(key) != 32:
        raise WorkflowError("planning integrity key is invalid")
    return key


def state_signature(state: dict[str, Any]) -> str:
    payload = dict(state)
    payload.pop("integrity_hmac", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(integrity_key(), encoded, hashlib.sha256).hexdigest()


def resolve_project(value: str) -> Path:
    project = Path(value).expanduser().resolve()
    if not project.is_dir():
        raise WorkflowError(f"project is not a directory: {project}")
    top = git(project, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != project:
        raise WorkflowError(f"--project must be the Git repository root: {top}")
    return project


def project_key(project: Path) -> str:
    suffix = hashlib.sha256(str(project).encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", project.name).strip("-") or "project"
    return f"{safe}-{suffix}"


def run_directory(project: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise WorkflowError("invalid run id")
    return state_root() / project_key(project) / "runs" / run_id


def state_path(project: Path, run_id: str) -> Path:
    return run_directory(project, run_id) / "workflow.json"


def anchor_directory(project: Path, run_id: str) -> Path:
    return state_root() / "anchors" / project_key(project) / run_id


def validate_or_recover_anchor(state: dict[str, Any]) -> None:
    project = Path(state["project"])
    directory = anchor_directory(project, state["run_id"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    anchors = sorted(directory.glob("*.anchor"))
    latest_revision = max((int(path.name.split("-", 1)[0]) for path in anchors), default=0)
    revision = state.get("revision")
    signature = state["integrity_hmac"]
    if not isinstance(revision, int) or revision < 1:
        raise WorkflowError("planning workflow revision is invalid")
    if latest_revision > revision:
        raise WorkflowError("planning workflow state rollback/replay detected")
    expected = directory / f"{revision:012d}-{signature}.anchor"
    if latest_revision == revision and not expected.is_file():
        raise WorkflowError("planning workflow anchor does not match state")
    if latest_revision < revision:
        # State is written before its append-only anchor. A valid HMAC and no newer anchor make
        # this the one recoverable crash window.
        descriptor = os.open(expected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(signature + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def load_state(project: Path, run_id: str) -> dict[str, Any]:
    path = state_path(project, run_id)
    if not path.is_file():
        raise WorkflowError(f"unknown planning run: {run_id}")
    state = json.loads(path.read_text(encoding="utf-8"))
    signature = state.get("integrity_hmac")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, state_signature(state)):
        raise WorkflowError("planning workflow state failed integrity verification")
    validate_or_recover_anchor(state)
    if state.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(f"unsupported planning schema: {state.get('schema_version')}")
    if Path(state.get("project", "")).resolve() != project:
        raise WorkflowError("planning run belongs to a different project")
    validate_artifacts(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    state["revision"] = state.get("revision", 0) + 1
    state["integrity_hmac"] = state_signature(state)
    atomic_json(Path(state["run_directory"]) / "workflow.json", state)
    validate_or_recover_anchor(state)


@contextmanager
def run_lock(project: Path, run_id: str):
    directory = run_directory(project, run_id)
    if not directory.is_dir():
        raise WorkflowError(f"unknown planning run: {run_id}")
    lock_path = directory / ".lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_artifacts(state: dict[str, Any]) -> None:
    for artifact in state.get("artifacts", {}).values():
        path = Path(artifact["path"])
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise WorkflowError(f"authoritative planning artifact changed or disappeared: {path}")


def record_artifact(state: dict[str, Any], key: str, path: Path) -> None:
    state.setdefault("artifacts", {})[key] = {"path": str(path), "sha256": sha256_file(path)}


def available(provider: str) -> bool:
    return shutil.which(provider) is not None


def resolve_topology(backend: str) -> dict[str, str]:
    present = {name for name in SUPPORTED_PROVIDERS if available(name)}
    if backend == "auto":
        if present == set(SUPPORTED_PROVIDERS):
            return {"A": "claude", "B": "codex"}
        if "claude" in present:
            return {"A": "claude", "B": "claude"}
        if "codex" in present:
            return {"A": "codex", "B": "codex"}
        raise WorkflowError("neither claude nor codex is available")
    if backend == "mixed":
        if present != set(SUPPORTED_PROVIDERS):
            raise WorkflowError("mixed topology requires both claude and codex")
        return {"A": "claude", "B": "codex"}
    if backend not in SUPPORTED_PROVIDERS or backend not in present:
        raise WorkflowError(f"requested provider is unavailable: {backend}")
    return {"A": backend, "B": backend}


def required_final_reviewers(state: dict[str, Any]) -> dict[str, str]:
    selected = state["final_reviewer"]
    if selected == "both":
        return dict(state["planners"])
    return {"F": selected}


def independence_section(state: dict[str, Any]) -> str:
    """Reassignments, spelled out in the report — or a line saying there were none.

    A run that swapped a provider and did not say so hands the reader a "provider diversity" field
    describing the topology that was frozen rather than the one that ran. Printing "none" when
    there were none is the half that makes the presence of the section meaningful.
    """
    notes = state.get("independence_notes") or []
    if not notes:
        return "- Reassignments: `none`\n"
    lines = ["- Reassignments (provider independence reduced):\n"]
    for note in notes:
        lines.append(
            f"  - `{note['assignment']}`: {note['from']} -> {note['to']} ({note['decided_at']}). "
            f"{note['reason']}\n"
        )
    return "".join(lines)


def assignment_provider(state: dict[str, Any], assignment: str, default: str) -> str:
    """Which CLI serves this assignment, after any recorded reassignment.

    Topology is frozen at init, and that is right: a run that quietly swaps providers mid-flight
    is a run whose independence claim means nothing. But a frozen topology with no escape turns
    one unusable provider into a dead run -- the alternative being to abandon and re-draft
    everything, which costs the work that DID succeed.

    So reassignment exists and is loud: an explicit user decision, recorded in the decisions
    directory, listed in ``independence_notes``, and surfaced by ``export``. The skill's rule is
    not "never share a provider" -- it is "never DESCRIBE two instances of one provider as
    model-diverse". This keeps the description true while letting the run finish.
    """
    return (state.get("assignment_providers") or {}).get(assignment, default)


def planning_worktrees(state: dict[str, Any]) -> list[Path]:
    """Every checkout this run registered with git, newest layout first, without duplicates."""
    if state.get("worktree"):
        return [Path(state["worktree"])]
    seen: dict[str, None] = {}
    for path in (state.get("worktrees") or {}).values():
        seen.setdefault(path, None)
    return [Path(path) for path in seen]


def reviewer_worktree(state: dict[str, Any], slot: str, provider: str) -> Path:
    """The checkout an invocation reads.

    One per run, shared. The slots do not need a copy each: the tree is detached at the frozen
    baseline and mounted ``--ro-bind``, so no invocation can alter what another one sees, and the
    isolation that matters -- separate process, separate sandbox, separate private home, disjoint
    context files -- is not a property of the directory. A second checkout added nothing but a
    second thing to validate, to lose track of when an attempt is killed, and to fail to clean up.

    Runs initialized before this collapse keep their per-slot map and still resolve here.
    """
    if state.get("worktree"):
        return Path(state["worktree"])
    if slot in {"A", "B"}:
        return Path(state["worktrees"][slot])
    for candidate, candidate_provider in state["planners"].items():
        if candidate_provider == provider:
            return Path(state["worktrees"][candidate])
    return Path(state["worktrees"]["A"])


def validate_planning_worktree(state: dict[str, Any], worktree: Path) -> None:
    if git(worktree, "rev-parse", "HEAD") != state["baseline_sha"]:
        raise WorkflowError(f"planning worktree is not at the frozen baseline: {worktree}")
    if git(worktree, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise WorkflowError(f"planning worktree is dirty: {worktree}")


def agent_command(
    provider: str, worktree: Path, context: Path, schema: dict[str, Any], raw: Path, prompt: str,
) -> list[str]:
    if provider == "codex":
        schema_path = context / "schema.json"
        atomic_json(schema_path, schema)
        return [
            "codex", "-a", "never", "exec", "--ephemeral", "-s", "read-only",
            *CODEX_POLICY_OVERRIDES,
            "-C", str(worktree), "--add-dir", str(context), "--output-schema", str(schema_path),
            "-o", str(raw), prompt,
        ]
    allowed = ",".join(["Read", "Grep", "Glob"])
    return [
        "claude", "-p", "--output-format", "json", "--json-schema",
        json.dumps(schema, separators=(",", ":")), "--permission-mode", "dontAsk",
        "--allowedTools", allowed,
        "--disallowedTools", "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch",
        "--add-dir", str(context), "--no-session-persistence", prompt,
    ]


def sandbox_destination(source: Path, private_home: Path) -> Path:
    """Where a host file appears inside the sandbox.

    Anything under the real home is mirrored into the private home at the same relative path, so
    ``~`` keeps resolving the way the file's own contents assume — a config that says
    ``model_catalog_json = "~/.codex/models.json"`` finds it. Anything else is bound where it is.
    """
    try:
        return private_home / source.relative_to(Path.home())
    except ValueError:
        return source


def provider_trust_store(provider: str) -> list[Path]:
    """The files a CLI needs in order to be itself: credentials AND the configuration around them.

    The workflow used to name one file per provider and treat it as the whole credential store.
    That is true of an ``auth.json``, and false of every deployment that authenticates through a
    configured provider block: for those the token lives in ``config.toml`` beside the ``base_url``
    it belongs to, and a sandbox holding one without the other has a key to a door it cannot find.

    So the store is discovered, not declared. For codex that means its config, its auth file if it
    keeps one, and whatever ``model_catalog_json`` points at — a real path in real installations,
    and a startup failure when it is missing. Only files that exist are returned; a provider with
    none of them is left to whatever it does without a home, which is what a missing login is.
    """
    if provider == "claude":
        candidates = [Path.home() / ".claude" / ".credentials.json"]
    elif provider == "codex":
        codex_home = Path.home() / ".codex"
        config = codex_home / "config.toml"
        candidates = [config, codex_home / "auth.json"]
        catalog = codex_catalog_path(config)
        if catalog is not None:
            candidates.append(catalog)
    else:
        candidates = []
    seen: dict[Path, None] = {}
    for candidate in candidates:
        if candidate.is_file():
            seen.setdefault(candidate.resolve(), None)
    return list(seen)


def codex_catalog_path(config: Path) -> Path | None:
    """``model_catalog_json`` from a codex config, if it names a readable file.

    Parse failures are not this function's business to report: an unreadable config is codex's own
    error to raise, with its own message, rather than a planning workflow's guess at one.
    """
    if not config.is_file():
        return None
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    declared = parsed.get("model_catalog_json")
    if not isinstance(declared, str) or not declared.strip():
        return None
    return Path(declared).expanduser()


def isolated_agent_command(
    command: list[str], state: dict[str, Any], invocation_root: Path,
    worktree: Path, context: Path,
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise WorkflowError("bubblewrap is required to isolate planning instances")
    executable = Path(shutil.which(command[0]) or command[0]).resolve()
    if not executable.is_file():
        raise WorkflowError(f"agent executable does not exist: {executable}")
    private_home = invocation_root / "home"
    private_tmp = invocation_root / "tmp"
    private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider = Path(command[0]).name
    credential_mounts: list[tuple[Path, Path]] = []
    for source in provider_trust_store(provider):
        destination = sandbox_destination(source, private_home)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.touch(mode=0o600, exist_ok=True)
        credential_mounts.append((source, destination))
    if provider != "codex":
        disallowed_index = command.index("--disallowedTools") + 1
        private_root = f"//{str(private_home).lstrip('/')}"
        private_denies = ",".join(
            f"{tool}({private_root}/**)" for tool in ("Read", "Grep", "Glob")
        )
        command[disallowed_index] = command[disallowed_index] + "," + private_denies
    common_value = git(worktree, "rev-parse", "--git-common-dir")
    common_git = Path(common_value)
    if not common_git.is_absolute():
        common_git = (worktree / common_git).resolve()
    inner_command = ["/opt/grounded-build-agent", *command[1:]]
    wrapper = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-pid",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", str(invocation_root), "--bind", str(invocation_root), str(invocation_root),
        "--dir", str(worktree), "--ro-bind", str(worktree), str(worktree),
        "--ro-bind", str(context), str(context),
        "--dir", str(common_git), "--ro-bind", str(common_git), str(common_git),
        "--dir", "/opt", "--ro-bind", str(executable), "/opt/grounded-build-agent",
    ]
    for system_path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(system_path).exists():
            wrapper.extend(["--ro-bind", system_path, system_path])
    for system_file in (
        "/etc/ca-certificates", "/etc/ssl", "/etc/resolv.conf", "/etc/hosts",
        "/etc/nsswitch.conf", "/etc/passwd", "/etc/group", "/etc/localtime",
    ):
        if Path(system_file).exists():
            wrapper.extend(["--ro-bind", system_file, system_file])
    for source, destination in credential_mounts:
        wrapper.extend(["--ro-bind", str(source), str(destination)])
    wrapper.extend([
        "--setenv", "HOME", str(private_home), "--setenv", "TMPDIR", str(private_tmp),
        "--setenv", "PATH", "/usr/bin:/bin", "--chdir", str(worktree), "--", *inner_command,
    ])
    return wrapper


def agent_environment() -> dict[str, str]:
    allowed = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def extract_payload(provider: str, result: subprocess.CompletedProcess[str], raw: Path) -> dict[str, Any]:
    source = raw.read_text(encoding="utf-8") if provider == "codex" and raw.is_file() else result.stdout
    if not source.strip():
        raise NoFinalAnswer(f"{provider} ended its turn without a final message"
                            f"{no_answer_detail(result)}")
    try:
        wrapper = json.loads(source)
    except json.JSONDecodeError as exc:
        raise NoFinalAnswer(describe_unparseable(provider, source, exc)) from exc
    if provider == "codex":
        payload = wrapper
    else:
        payload = wrapper.get("structured_output")
        if not isinstance(payload, dict) and isinstance(wrapper.get("result"), str):
            try:
                payload = json.loads(wrapper["result"])
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"claude result was not structured JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"{provider} did not return a structured object")
    return payload


#: Appended to every assignment that must return a schema object. Says how to deliver a verdict,
#: never which verdict to reach.
#:
#: A reviewer given a repository and no stopping rule will read it. Three cross-review attempts
#: ended 22, 25 and 15 minutes in -- all inside the 30-minute ceiling, so nothing cut them off --
#: two of them mid-search with no final message at all, and one with its own deliberation about
#: severity submitted where the object belonged. The drafting assignment never failed this way,
#: because "write your draft" ends when the draft is written; "review this draft" does not end on
#: its own. The turn is the budget, and the instruction that says so was missing.
DELIVERY_CONTRACT = (
    "\n\nDELIVERY. You have ONE turn. The schema object must be your FINAL message — not a file you "
    "write, not a summary, not your reasoning about what to conclude. Stop investigating while you "
    "still have room to emit it: a verdict you reached and did not send counts as no verdict, and "
    "the run pays for the attempt either way. If you have read enough to judge some claims and not "
    "others, say so IN the object rather than continuing to read.\n\n"
    "The object must also be COMPLETE. One cut off mid-string is discarded whole, so a long "
    "`evidence` string can cost you every finding after it. Keep each field to the shortest text "
    "that still identifies the thing — a path and a line number rather than a quoted passage. "
    "Do NOT drop findings to save room: fewer words per finding, never fewer findings."
)


def delivery_corrective(state: dict[str, Any], assignment: str) -> str:
    """What to tell a retry that the first attempt could not have known.

    A retry with byte-identical inputs is not a retry, it is the same request again, and it fails
    the same way -- three times here, at real cost. What changes is strictly the DELIVERY notice:
    the reviewer is told that its previous attempt's answer never arrived and how. Nothing here
    names a verdict, a severity, or a finding; steering the conclusion to make an attempt land
    would be buying a PASS, which is the one thing this workflow exists to prevent.
    """
    fault = (state.get("delivery_faults") or {}).get(assignment)
    if not fault:
        return ""
    return (
        "RETRY NOTICE. Your previous attempt on this assignment produced no usable verdict: "
        f"{fault}. Whatever you concluded then is unaffected and unconstrained now — reach the "
        "judgement the evidence supports. What failed was its delivery. Budget this turn so the "
        "schema object is emitted.\n\n"
    )


def invoke(
    state: dict[str, Any], assignment: str, provider: str, slot: str, context_files: dict[str, Path],
    schema: dict[str, Any], prompt: str, timeout: int, dry_run: bool,
) -> dict[str, Any]:
    used = state["usage"].get(assignment, 0)
    granted = state.get("extra_invocation_grants", {}).get(assignment, 0)
    if not dry_run and used >= MAX_INVOCATIONS_PER_ASSIGNMENT + granted:
        resume_status = state["status"]
        # "Out of tries" and "this provider does not deliver a verdict for this assignment" both
        # end here, and only one of them is fixed by granting another try. Say which, so the
        # decision in front of the user is the one they actually face.
        fault = (state.get("delivery_faults") or {}).get(assignment)
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "INVOCATION_BUDGET_EXHAUSTED", "assignment": assignment,
            "resume_status": resume_status, "created_at": utc_now(),
            "provider": provider,
            "last_delivery_fault": fault,
            "diagnosis": (
                f"{provider} exhausted its attempts on {assignment} without ever delivering a "
                f"verdict ({fault}). Another attempt repeats the same conditions; REASSIGN_ASSIGNMENT "
                f"moves this one assignment to the other provider and records the loss of "
                f"independence."
                if fault else
                f"{provider} used every attempt on {assignment}."
            ),
        }
        save_state(state)
        raise WorkflowError(
            f"invocation budget exhausted for {assignment}; user decision required"
            + (f" ({fault})" if fault else ""))
    # A preview is not an attempt and must not be filed as one. Naming both `attempt_N_*` put
    # entries in the audit record for invocations that never ran -- two `attempt_4_*` directories
    # for one cross-review, distinguishable only by whether a stderr.log happened to be there --
    # in a workflow whose entire product is a record of what happened.
    prefix = f"preview_{secrets.token_hex(6)}" if dry_run else f"attempt_{used + 1}_{secrets.token_hex(6)}"
    root = Path(state["run_directory"]) / "invocations" / assignment / prefix
    context = root / "context"
    context.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, source in context_files.items():
        destination = context / name
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    raw = root / "raw.json"
    worktree = reviewer_worktree(state, slot, provider)
    validate_planning_worktree(state, worktree)
    prompt = delivery_corrective(state, assignment) + prompt
    command = agent_command(provider, worktree, context, schema, raw, prompt.format(context=context))
    command = isolated_agent_command(command, state, root, worktree, context)
    (root / "prompt.md").write_text(prompt.format(context=context), encoding="utf-8")
    if dry_run:
        return {"dry_run": True, "command": command[:-1] + ["<PROMPT>"], "context": str(context)}
    state["usage"][assignment] = used + 1
    save_state(state)
    result = run(command, cwd=worktree, timeout=timeout, env=agent_environment())
    stdout_path = root / "stdout.log"
    stderr_path = root / "stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    artifact_prefix = f"invocation-{assignment}-{used + 1}"
    for suffix, path in (("prompt", root / "prompt.md"), ("stdout", stdout_path), ("stderr", stderr_path)):
        record_artifact(state, f"{artifact_prefix}-{suffix}", path)
    save_state(state)
    if result.returncode != 0:
        raise WorkflowError(f"{provider} invocation failed ({result.returncode}): {result.stderr[-1000:]}")
    try:
        payload = extract_payload(provider, result, raw)
    except NoFinalAnswer as exc:
        state.setdefault("delivery_faults", {})[assignment] = str(exc)
        save_state(state)
        raise
    state.get("delivery_faults", {}).pop(assignment, None)
    validate_planning_worktree(state, worktree)
    result_path = root / "result.json"
    atomic_json(result_path, payload)
    if raw.is_file():
        record_artifact(state, f"{artifact_prefix}-raw", raw)
    record_artifact(state, f"{artifact_prefix}-result", result_path)
    save_state(state)
    return payload


def validate_draft(payload: dict[str, Any], state: dict[str, Any], slot: str) -> None:
    required = set(DRAFT_SCHEMA["required"])
    if set(payload) != required:
        raise WorkflowError("draft output fields do not match the contract")
    if payload["provider"] != assignment_provider(state, f"draft-{slot}", state["planners"][slot]) \
            or payload["slot"] != slot:
        raise WorkflowError("draft identity mismatch")
    if payload["baseline_sha"] != state["baseline_sha"]:
        raise WorkflowError("draft baseline SHA mismatch")
    if not isinstance(payload["plan_markdown"], str) or not payload["plan_markdown"].strip():
        raise WorkflowError("draft plan is empty")
    facts = payload["repository_facts"]
    if not isinstance(facts, list) or not facts:
        raise WorkflowError("draft must include repository facts")
    ids = [item.get("id") for item in facts if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise WorkflowError("draft fact ids must be unique")


def validate_review(
    payload: dict[str, Any], state: dict[str, Any], provider: str, slot: str, target: str,
) -> None:
    if set(payload) != set(REVIEW_SCHEMA["required"]):
        raise WorkflowError("review output fields do not match the contract")
    if payload["provider"] != provider or payload["reviewer_slot"] != slot:
        raise WorkflowError("review identity mismatch")
    if payload["target"] != target or payload["baseline_sha"] != state["baseline_sha"]:
        raise WorkflowError("review target or baseline mismatch")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise WorkflowError("review findings must be a list")
    if payload["verdict"] != "PASS" and not findings:
        raise WorkflowError("a non-PASS review requires findings")
    blocking = [item for item in findings if item.get("severity") in {"P0", "P1"}]
    if payload["verdict"] == "PASS" and blocking:
        raise WorkflowError("PASS review cannot contain P0/P1 findings")
    if payload["verdict"] == "FAIL" and not blocking:
        raise WorkflowError("FAIL review requires at least one P0/P1 finding")
    ids = [item.get("id") for item in findings if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise WorkflowError("review finding ids must be unique")


def validate_ready_invariants(state: dict[str, Any]) -> None:
    if state.get("status") != "READY":
        raise WorkflowError(f"planning run is not ready: {state.get('status')}")
    if set(state.get("drafts", {})) != {"A", "B"}:
        raise WorkflowError("READY invariant failed: both drafts are required")
    if set(state.get("cross_reviews", {})) != {"A", "B"}:
        raise WorkflowError("READY invariant failed: mutual cross-review is incomplete")
    candidate = state.get("candidate") or {}
    final = state.get("final") or {}
    root = Path(state["run_directory"]).resolve()
    for key in ("plan", "batch_manifest"):
        candidate_path = Path(candidate.get(key, "")).resolve()
        final_path = Path(final.get(key, "")).resolve()
        if not candidate_path.is_relative_to(root) or not final_path.is_relative_to(root / "final"):
            raise WorkflowError(f"READY invariant failed: invalid {key} provenance")
        if sha256_file(candidate_path) != candidate.get(f"{key}_sha256"):
            raise WorkflowError(f"READY invariant failed: candidate {key} digest mismatch")
        if sha256_file(final_path) != final.get(f"{key}_sha256"):
            raise WorkflowError(f"READY invariant failed: final {key} digest mismatch")
        if candidate_path.read_bytes() != final_path.read_bytes():
            raise WorkflowError(f"READY invariant failed: final {key} differs from reviewed candidate")
    required = required_final_reviewers(state)
    if set(state.get("final_reviews", {})) != set(required):
        raise WorkflowError("READY invariant failed: required final reviews are incomplete")
    target = f"candidate-round-{candidate.get('round')}"
    for slot, provider in required.items():
        record = state["final_reviews"][slot]
        payload = json.loads(Path(record["path"]).read_text(encoding="utf-8"))
        validate_review(payload, state, provider, slot, target)
        if payload["verdict"] != "PASS":
            raise WorkflowError(f"READY invariant failed: final reviewer {slot} did not PASS")


def command_preflight(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    topology = resolve_topology(args.backend)
    payload = {
        "status": "PREFLIGHT_OK",
        "project": str(project),
        "baseline_sha": git(project, "rev-parse", "HEAD"),
        "clean": not bool(git(project, "status", "--porcelain")),
        "planners": topology,
        "provider_diversity": len(set(topology.values())) > 1,
    }
    if args.probe:
        probes = {name: probe_provider(name, project) for name in sorted(set(topology.values()))}
        payload["probes"] = probes
        if not all(item["ok"] for item in probes.values()):
            payload["status"] = "PREFLIGHT_PROVIDER_UNUSABLE"
            emit(payload, code=2)
    emit(payload)


def command_init(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    request = Path(args.request).expanduser().resolve()
    if not request.is_file():
        raise WorkflowError(f"request file does not exist: {request}")
    if git(project, "status", "--porcelain"):
        raise WorkflowError("project must be clean so both planners inspect the same Git snapshot")
    topology = resolve_topology(args.backend)
    if args.final_reviewer != "both" and not available(args.final_reviewer):
        raise WorkflowError(f"final reviewer is unavailable: {args.final_reviewer}")
    baseline = git(project, "rev-parse", "HEAD")
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    root = run_directory(project, run_id)
    if root.exists():
        raise WorkflowError(f"run directory already exists: {root}")
    for relative in ("input", "drafts", "cross_reviews", "synthesis", "final_reviews", "decisions", "invocations", "worktrees"):
        (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    request_snapshot = root / "input" / "request.md"
    shutil.copyfile(request, request_snapshot)
    # One checkout, shared by both slots and by the final reviewers. See reviewer_worktree.
    worktree = root / "worktrees" / "baseline"
    result = run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(worktree), baseline],
        timeout=120,
    )
    if result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or "could not create planning worktree")
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "software_version": VERSION,
        "run_id": run_id,
        "run_directory": str(root),
        "project": str(project),
        "baseline_sha": baseline,
        "request_snapshot": str(request_snapshot),
        "planners": topology,
        "provider_diversity": len(set(topology.values())) > 1,
        "final_reviewer": args.final_reviewer,
        "worktree": str(worktree),
        "status": "INITIALIZED",
        "drafts": {},
        "cross_reviews": {},
        "synthesis_submissions": 0,
        "extra_synthesis_grants": 0,
        "extra_invocation_grants": {},
        "candidate": None,
        "final_reviews": {},
        "pending_decision": None,
        "usage": {},
        "artifacts": {},
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "revision": 0,
    }
    record_artifact(state, "request", request_snapshot)
    save_state(state)
    emit({
        "status": state["status"], "run_id": run_id, "run_directory": str(root),
        "planners": topology, "final_reviewer": args.final_reviewer,
        "provider_diversity": state["provider_diversity"],
    })


def command_draft(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    slot = args.slot
    if state["status"] not in {"INITIALIZED", "DRAFTING"}:
        raise WorkflowError(f"draft is not allowed from {state['status']}")
    if slot in state["drafts"] and not args.dry_run:
        raise WorkflowError(f"slot {slot} already produced a draft")
    provider = assignment_provider(state, f"draft-{slot}", state["planners"][slot])
    prompt = (
        "You are independent planning instance {slot}. Read {context}/request.md and inspect the "
        "repository at the assigned baseline. You cannot see the other planner's work. Verify factual "
        "claims against named files, symbols, tests, or commands. Separate VERIFIED facts from INFERRED "
        "or UNRESOLVED claims. Produce a detailed implementation plan with finite batch boundaries, "
        "dependencies, budget-sensitive retry limits, and decidable acceptance observations. Do not edit "
        "the repository. Return only JSON matching the supplied schema. Set provider={provider}, slot={slot}, "
        "and baseline_sha={sha}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, sha=state["baseline_sha"], context="{context}")
    payload = invoke(
        state, f"draft-{slot}", provider, slot, {"request.md": Path(state["request_snapshot"])},
        DRAFT_SCHEMA, prompt, args.timeout, args.dry_run,
    )
    if args.dry_run:
        emit({"status": "DRAFT_DRY_RUN", **payload})
    validate_draft(payload, state, slot)
    path = Path(state["run_directory"]) / "drafts" / f"{slot}.json"
    atomic_json(path, payload)
    markdown = Path(state["run_directory"]) / "drafts" / f"{slot}.md"
    markdown.write_text(payload["plan_markdown"].rstrip() + "\n", encoding="utf-8")
    record_artifact(state, f"draft-{slot}-json", path)
    record_artifact(state, f"draft-{slot}-md", markdown)
    state["drafts"][slot] = {"provider": provider, "path": str(path), "markdown": str(markdown)}
    state["status"] = "DRAFTS_READY" if set(state["drafts"]) == {"A", "B"} else "DRAFTING"
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"], "slot": slot, "draft": str(markdown)})


def command_cross_review(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    slot = args.slot
    if state["status"] not in {"DRAFTS_READY", "CROSS_REVIEWING"}:
        raise WorkflowError(f"cross-review is not allowed from {state['status']}")
    if slot in state["cross_reviews"] and not args.dry_run:
        raise WorkflowError(f"slot {slot} already completed cross-review")
    target_slot = "B" if slot == "A" else "A"
    provider = assignment_provider(state, f"cross-{slot}", state["planners"][slot])
    target = f"draft-{target_slot}"
    prompt = (
        "You are independent reviewer slot {slot}. Read {context}/request.md and the other planner's "
        "{context}/target_draft.json, then inspect the repository at baseline {sha}. Do not assume agreement "
        "means truth: verify claims against repository evidence. Find factual errors, missing dependencies, "
        "unbounded budgets, non-decidable acceptance conditions, unsafe scope, and incompatibilities. P0/P1 "
        "mean the draft cannot safely guide implementation; P2 is advisory. Do not edit files. Return only "
        "schema JSON with provider={provider}, reviewer_slot={slot}, target={target}, baseline_sha={sha}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, target=target, sha=state["baseline_sha"], context="{context}")
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "target_draft.json": Path(state["drafts"][target_slot]["path"]),
    }
    for index, decision in enumerate(sorted((Path(state["run_directory"]) / "decisions").glob("*.json")), 1):
        context_files[f"decision_{index:03d}.json"] = decision
    payload = invoke(
        state, f"cross-{slot}", provider, slot,
        context_files,
        REVIEW_SCHEMA, prompt, args.timeout, args.dry_run,
    )
    if args.dry_run:
        emit({"status": "CROSS_REVIEW_DRY_RUN", **payload})
    validate_review(payload, state, provider, slot, target)
    path = Path(state["run_directory"]) / "cross_reviews" / f"{slot}_reviews_{target_slot}.json"
    atomic_json(path, payload)
    record_artifact(state, f"cross-{slot}", path)
    state["cross_reviews"][slot] = {"target": target_slot, "verdict": payload["verdict"], "path": str(path)}
    if payload["verdict"] == "NEEDS_USER_DECISION":
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "PLANNING_BOUNDARY", "source": f"cross-{slot}", "created_at": utc_now(),
        }
    elif set(state["cross_reviews"]) == {"A", "B"}:
        state["status"] = "SYNTHESIS_REQUIRED"
    else:
        state["status"] = "CROSS_REVIEWING"
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"], "review": str(path), "verdict": payload["verdict"]})


def command_synthesis_context(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] != "SYNTHESIS_REQUIRED":
        raise WorkflowError(f"synthesis-context is not allowed from {state['status']}")
    root = Path(state["run_directory"]) / "synthesis" / f"round_{state['synthesis_submissions'] + 1}"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment = root / "assignment.md"
    assignment.write_text(
        "# Host synthesis assignment\n\n"
        f"Baseline: `{state['baseline_sha']}`\n\n"
        "Read the frozen request, both independent drafts, and both cross-reviews. Resolve claims by "
        "repository evidence rather than vote count. Preserve material disagreements and unresolved product "
        "choices. Produce `implementation_plan.md` and `batches.md`. The plan must distinguish proposed plan "
        "items from future Git commits; each batch needs a finite, decidable exit observation. Do not implement.\n",
        encoding="utf-8",
    )
    decisions = sorted(str(path) for path in (Path(state["run_directory"]) / "decisions").glob("*.json"))
    previous_final_reviews = {
        key: value["path"] for key, value in state["final_reviews"].items()
    }
    emit({
        "status": "SYNTHESIS_REQUIRED", "run_id": state["run_id"], "assignment": str(assignment),
        "request": state["request_snapshot"],
        "drafts": {key: value["path"] for key, value in state["drafts"].items()},
        "cross_reviews": {key: value["path"] for key, value in state["cross_reviews"].items()},
        "decisions": decisions,
        "previous_final_reviews": previous_final_reviews,
        "output_directory": str(root),
    })


def command_submit_synthesis(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] != "SYNTHESIS_REQUIRED":
        raise WorkflowError(f"submit-synthesis is not allowed from {state['status']}")
    synthesis_limit = MAX_SYNTHESIS_SUBMISSIONS + state.get("extra_synthesis_grants", 0)
    if state["synthesis_submissions"] >= synthesis_limit:
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {"type": "SYNTHESIS_BUDGET_EXHAUSTED", "created_at": utc_now()}
        save_state(state)
        emit({"status": state["status"], "pending_decision": state["pending_decision"]}, 3)
    plan = Path(args.plan).expanduser().resolve()
    batches = Path(args.batch_manifest).expanduser().resolve()
    if not plan.is_file() or not batches.is_file():
        raise WorkflowError("both synthesized plan and batch manifest must exist")
    plan_text = plan.read_text(encoding="utf-8")
    batches_text = batches.read_text(encoding="utf-8")
    if not plan_text.strip():
        raise WorkflowError("synthesized plan is empty")
    batch_ids = re.findall(r"(?m)^\s*(?:[-*]\s*|#+\s*)?(B\d{2,})\s*:", batches_text)
    if not batch_ids:
        raise WorkflowError("batch manifest must declare at least one Bxx batch identifier")
    if len(batch_ids) != len(set(batch_ids)):
        raise WorkflowError("batch manifest repeats a batch identifier")
    number = state["synthesis_submissions"] + 1
    destination = Path(state["run_directory"]) / "synthesis" / f"round_{number}"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan_copy = destination / "implementation_plan.md"
    batches_copy = destination / "batches.md"
    shutil.copyfile(plan, plan_copy)
    shutil.copyfile(batches, batches_copy)
    record_artifact(state, f"candidate-{number}-plan", plan_copy)
    record_artifact(state, f"candidate-{number}-batches", batches_copy)
    state["synthesis_submissions"] = number
    state["candidate"] = {
        "round": number, "plan": str(plan_copy), "batch_manifest": str(batches_copy),
        "plan_sha256": sha256_file(plan_copy), "batch_manifest_sha256": sha256_file(batches_copy),
    }
    state["final_reviews"] = {}
    state["pending_decision"] = None
    state["status"] = "FINAL_REVIEW_REQUIRED"
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"], "candidate": state["candidate"]})


def command_final_review(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] not in {"FINAL_REVIEW_REQUIRED", "FINAL_REVIEWING"}:
        raise WorkflowError(f"final-review is not allowed from {state['status']}")
    reviewers = required_final_reviewers(state)
    slot = args.reviewer
    if slot not in reviewers:
        raise WorkflowError(f"reviewer must be one of: {','.join(reviewers)}")
    if slot in state["final_reviews"] and not args.dry_run:
        raise WorkflowError(f"final reviewer {slot} already completed this round")
    provider = assignment_provider(
        state, f"final-{state['candidate']['round']}-{slot}", reviewers[slot])
    target = f"candidate-round-{state['candidate']['round']}"
    prompt = (
        "You are a fresh final planning reviewer ({slot}). Read {context}/request.md, "
        "{context}/implementation_plan.md, and {context}/batches.md. Inspect the repository at baseline {sha}. "
        "Judge factual correctness, full request coverage, dependency order, budget bounds, batch scope, and "
        "whether every exit condition names a finite observation. The plan may choose between drafts only when "
        "the choice is supported by evidence; do not demand excluded work without identifying a request conflict. "
        "Do not edit files. Return schema JSON with provider={provider}, reviewer_slot={slot}, target={target}, "
        "baseline_sha={sha}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, target=target, sha=state["baseline_sha"], context="{context}")
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "implementation_plan.md": Path(state["candidate"]["plan"]),
        "batches.md": Path(state["candidate"]["batch_manifest"]),
    }
    for review_slot, review in state["cross_reviews"].items():
        context_files[f"cross_review_{review_slot}.json"] = Path(review["path"])
    for index, decision in enumerate(sorted((Path(state["run_directory"]) / "decisions").glob("*.json")), 1):
        context_files[f"decision_{index:03d}.json"] = decision
    payload = invoke(
        state, f"final-{state['candidate']['round']}-{slot}", provider, slot,
        context_files,
        REVIEW_SCHEMA, prompt, args.timeout, args.dry_run,
    )
    if args.dry_run:
        emit({"status": "FINAL_REVIEW_DRY_RUN", **payload})
    validate_review(payload, state, provider, slot, target)
    path = Path(state["run_directory"]) / "final_reviews" / f"round_{state['candidate']['round']}_{slot}.json"
    atomic_json(path, payload)
    record_artifact(state, f"final-{state['candidate']['round']}-{slot}", path)
    state["final_reviews"][slot] = {"provider": provider, "verdict": payload["verdict"], "path": str(path)}
    verdicts = [item["verdict"] for item in state["final_reviews"].values()]
    if "NEEDS_USER_DECISION" in verdicts:
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {"type": "FINAL_PLAN_BOUNDARY", "created_at": utc_now()}
    elif set(state["final_reviews"]) == set(reviewers):
        if all(verdict == "PASS" for verdict in verdicts):
            final_dir = Path(state["run_directory"]) / "final"
            final_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            final_plan = final_dir / "implementation_plan.md"
            final_batches = final_dir / "batches.md"
            shutil.copyfile(Path(state["candidate"]["plan"]), final_plan)
            shutil.copyfile(Path(state["candidate"]["batch_manifest"]), final_batches)
            record_artifact(state, "final-plan", final_plan)
            record_artifact(state, "final-batches", final_batches)
            state["final"] = {
                "plan": str(final_plan), "batch_manifest": str(final_batches),
                "plan_sha256": sha256_file(final_plan), "batch_manifest_sha256": sha256_file(final_batches),
            }
            report = Path(state["run_directory"]) / "final_report.md"
            report.write_text(
                "# Grounded Build planning report\n\n"
                f"- Run: `{state['run_id']}`\n"
                f"- Baseline: `{state['baseline_sha']}`\n"
                f"- Planners: `{json.dumps(state['planners'], sort_keys=True)}`\n"
                f"- Provider diversity: `{str(state['provider_diversity']).lower()}`\n"
                + independence_section(state)
                + f"- Final reviewer selection: `{state['final_reviewer']}`\n"
                f"- Synthesis submissions: `{state['synthesis_submissions']}`\n"
                f"- Plan SHA-256: `{state['final']['plan_sha256']}`\n"
                f"- Batch manifest SHA-256: `{state['final']['batch_manifest_sha256']}`\n\n"
                "The plan is reviewed but not authorized for implementation until the user explicitly approves.\n",
                encoding="utf-8",
            )
            state["final"]["report"] = str(report)
            record_artifact(state, "final-report", report)
            state["status"] = "READY"
        elif state["synthesis_submissions"] < MAX_SYNTHESIS_SUBMISSIONS + state.get("extra_synthesis_grants", 0):
            state["status"] = "SYNTHESIS_REQUIRED"
        else:
            state["status"] = "NEEDS_USER_DECISION"
            state["pending_decision"] = {"type": "FINAL_REVIEW_BUDGET_EXHAUSTED", "created_at": utc_now()}
    else:
        state["status"] = "FINAL_REVIEWING"
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"], "verdict": payload["verdict"], "review": str(path)})


def command_adjudicate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] != "NEEDS_USER_DECISION" or not state.get("pending_decision"):
        raise WorkflowError("there is no pending planning decision")
    preview = {
        "status": "DECISION_PREVIEW", "pending_decision": state["pending_decision"],
        "choice": args.choice, "decision": args.decision, "actor": args.actor,
    }
    decision_type = state["pending_decision"]["type"]
    allowed = {
        "PLANNING_BOUNDARY": {"RESOLVE_AND_CONTINUE", "ABANDON"},
        "FINAL_PLAN_BOUNDARY": {"RESOLVE_AND_CONTINUE", "ABANDON"},
        "SYNTHESIS_BUDGET_EXHAUSTED": {"GRANT_ONE_SYNTHESIS", "ABANDON"},
        "FINAL_REVIEW_BUDGET_EXHAUSTED": {"GRANT_ONE_SYNTHESIS", "ABANDON"},
        "INVOCATION_BUDGET_EXHAUSTED": {"GRANT_ONE_INVOCATION", "REASSIGN_ASSIGNMENT", "ABANDON"},
    }.get(decision_type, {"ABANDON"})
    if args.choice not in allowed:
        raise WorkflowError(f"choice {args.choice} is not allowed for {decision_type}: {','.join(sorted(allowed))}")
    pending = dict(state["pending_decision"])
    if args.choice == "GRANT_ONE_SYNTHESIS" and state.get("extra_synthesis_grants", 0) >= 1:
        raise WorkflowError("the one explicit extra synthesis has already been granted")
    if args.choice == "GRANT_ONE_INVOCATION":
        assignment = pending.get("assignment")
        if not assignment:
            raise WorkflowError("invocation decision is missing its assignment")
        if state.setdefault("extra_invocation_grants", {}).get(assignment, 0) >= 1:
            raise WorkflowError("the one explicit extra invocation has already been granted")
    if args.choice == "REASSIGN_ASSIGNMENT":
        assignment = pending.get("assignment")
        if not assignment:
            raise WorkflowError("reassignment decision is missing its assignment")
        current = pending.get("provider")
        alternatives = [name for name in SUPPORTED_PROVIDERS
                        if name != current and shutil.which(name)]
        if not alternatives:
            raise WorkflowError(
                f"no other provider is installed to take over {assignment} from {current}")
        replacement = args.to_provider or alternatives[0]
        if replacement == current:
            raise WorkflowError("reassigning an assignment to the provider that already holds it "
                                "changes nothing")
        if not shutil.which(replacement):
            raise WorkflowError(f"provider is not installed: {replacement}")
        preview["reassignment"] = {"assignment": assignment, "from": current, "to": replacement}
        preview["independence_cost"] = (
            f"{assignment} will be served by {replacement}, which also serves other assignments in "
            f"this run. Its output is no longer independent of theirs in the provider sense; "
            f"process, sandbox, home and context isolation are unchanged."
        )
    if not args.apply:
        emit(preview)
    path = Path(state["run_directory"]) / "decisions" / f"decision_{len(list((Path(state['run_directory']) / 'decisions').glob('*.json'))) + 1:03d}.json"
    record = {**preview, "applied_at": utc_now()}
    atomic_json(path, record)
    record_artifact(state, f"decision-{path.stem}", path)
    state["pending_decision"] = None
    if args.choice == "ABANDON":
        state["status"] = "ABANDONED"
    elif args.choice == "GRANT_ONE_SYNTHESIS":
        state["extra_synthesis_grants"] = 1
        state["status"] = "SYNTHESIS_REQUIRED"
    elif args.choice == "GRANT_ONE_INVOCATION":
        grants = state.setdefault("extra_invocation_grants", {})
        grants[assignment] = 1
        state["status"] = record["pending_decision"].get("resume_status", "SYNTHESIS_REQUIRED")
    elif args.choice == "REASSIGN_ASSIGNMENT":
        move = preview["reassignment"]
        state.setdefault("assignment_providers", {})[move["assignment"]] = move["to"]
        # A fresh provider gets a fresh count. The exhausted attempts measured the one that is
        # being replaced, and charging them to its replacement would decide in advance that the
        # replacement fails too.
        state.setdefault("usage", {})[move["assignment"]] = 0
        state.setdefault("extra_invocation_grants", {}).pop(move["assignment"], None)
        (state.get("delivery_faults") or {}).pop(move["assignment"], None)
        state.setdefault("independence_notes", []).append({
            **move, "reason": preview["independence_cost"], "decided_at": utc_now(),
        })
        state["provider_diversity"] = len(set(
            list(state["planners"].values()) + list(state["assignment_providers"].values())
        )) > 1 and not state["independence_notes"]
        state["status"] = record["pending_decision"].get("resume_status", "SYNTHESIS_REQUIRED")
    elif decision_type == "PLANNING_BOUNDARY" and set(state["cross_reviews"]) != {"A", "B"}:
        state["status"] = "CROSS_REVIEWING"
    else:
        state["status"] = "SYNTHESIS_REQUIRED"
    save_state(state)
    emit({"status": state["status"], "decision_record": str(path)})


def command_status(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    emit({
        "status": state["status"], "run_id": state["run_id"], "baseline_sha": state["baseline_sha"],
        "planners": state["planners"], "provider_diversity": state["provider_diversity"],
        "assignment_providers": state.get("assignment_providers") or {},
        "independence_notes": state.get("independence_notes") or [],
        "final_reviewer": state["final_reviewer"], "drafts": state["drafts"],
        "cross_reviews": state["cross_reviews"], "synthesis_submissions": state["synthesis_submissions"],
        "final_reviews": state["final_reviews"], "pending_decision": state["pending_decision"],
        "final": state.get("final"), "run_directory": state["run_directory"], "usage": state["usage"],
    })


def command_export(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_ready_invariants(state)
    emit({
        "status": "READY", "run_id": state["run_id"], "baseline_sha": state["baseline_sha"],
        "plan": state["final"]["plan"], "batch_manifest": state["final"]["batch_manifest"],
        "plan_sha256": state["final"]["plan_sha256"],
        "batch_manifest_sha256": state["final"]["batch_manifest_sha256"],
        "report": state["final"]["report"],
        "handoff": "Use scripts/workflow.py init only after explicit user approval.",
    })


def command_abandon(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] in TERMINAL_STATUSES:
        raise WorkflowError(f"planning run is already terminal: {state['status']}")
    if not args.apply:
        emit({"status": "ABANDON_PREVIEW", "run_id": state["run_id"], "reason": args.reason})
    state["status"] = "ABANDONED"
    state["abandoned"] = {"reason": args.reason, "actor": args.actor, "at": utc_now()}
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"]})


def command_cleanup(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] not in TERMINAL_STATUSES:
        raise WorkflowError("cleanup is allowed only after READY or ABANDONED")
    existing = [str(path) for path in planning_worktrees(state) if path.exists()]
    if not args.apply:
        emit({"status": "CLEANUP_PREVIEW", "run_id": state["run_id"], "worktrees": existing})
    removed: list[str] = []
    for path in existing:
        if Path(path).exists():
            validate_planning_worktree(state, Path(path))
            result = run(["git", "-C", str(project), "worktree", "remove", path], timeout=120)
            if result.returncode != 0:
                raise WorkflowError(result.stderr.strip() or f"could not remove worktree {path}")
            removed.append(path)
    emit({"status": "CLEANED", "run_id": state["run_id"], "removed_worktrees": removed})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--project", required=True)
    preflight.add_argument("--backend", choices=["auto", "mixed", *SUPPORTED_PROVIDERS], default="auto")
    preflight.add_argument(
        "--probe", action="store_true",
        help="spend one trivial sandboxed call per provider to check it can authenticate and "
             "return a schema object; exits 2 if any cannot")
    preflight.set_defaults(func=command_preflight)

    init = commands.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument("--request", required=True)
    init.add_argument("--backend", choices=["auto", "mixed", *SUPPORTED_PROVIDERS], default="auto")
    init.add_argument("--final-reviewer", choices=["both", *SUPPORTED_PROVIDERS], required=True)
    init.set_defaults(func=command_init)

    for name, function in (("draft", command_draft), ("cross-review", command_cross_review)):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--slot", choices=["A", "B"], required=True)
        command.add_argument("--timeout", type=int, default=1800)
        command.add_argument("--dry-run", action="store_true")
        command.set_defaults(func=function)

    context = commands.add_parser("synthesis-context")
    context.add_argument("--project", required=True)
    context.add_argument("--run-id", required=True)
    context.set_defaults(func=command_synthesis_context)

    submit = commands.add_parser("submit-synthesis")
    submit.add_argument("--project", required=True)
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--plan", required=True)
    submit.add_argument("--batch-manifest", required=True)
    submit.set_defaults(func=command_submit_synthesis)

    final_review = commands.add_parser("final-review")
    final_review.add_argument("--project", required=True)
    final_review.add_argument("--run-id", required=True)
    final_review.add_argument("--reviewer", choices=["A", "B", "F"], required=True)
    final_review.add_argument("--timeout", type=int, default=1800)
    final_review.add_argument("--dry-run", action="store_true")
    final_review.set_defaults(func=command_final_review)

    adjudicate = commands.add_parser("adjudicate")
    adjudicate.add_argument("--project", required=True)
    adjudicate.add_argument("--run-id", required=True)
    adjudicate.add_argument("--decision", required=True)
    adjudicate.add_argument(
        "--choice",
        choices=["RESOLVE_AND_CONTINUE", "GRANT_ONE_SYNTHESIS", "GRANT_ONE_INVOCATION",
                 "REASSIGN_ASSIGNMENT", "ABANDON"],
        required=True,
    )
    adjudicate.add_argument("--actor", required=True)
    adjudicate.add_argument(
        "--to-provider", choices=list(SUPPORTED_PROVIDERS),
        help="REASSIGN_ASSIGNMENT only: which CLI takes the assignment over "
             "(default: the other installed provider)")
    adjudicate.add_argument("--apply", action="store_true")
    adjudicate.set_defaults(func=command_adjudicate)

    for name, function in (("status", command_status), ("export", command_export)):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True)
        command.add_argument("--run-id", required=True)
        command.set_defaults(func=function)

    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--project", required=True)
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--apply", action="store_true")
    cleanup.set_defaults(func=command_cleanup)

    abandon = commands.add_parser("abandon")
    abandon.add_argument("--project", required=True)
    abandon.add_argument("--run-id", required=True)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--actor", required=True)
    abandon.add_argument("--apply", action="store_true")
    abandon.set_defaults(func=command_abandon)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if hasattr(args, "run_id") and hasattr(args, "project"):
            project = resolve_project(args.project)
            with run_lock(project, args.run_id):
                args.func(args)
        else:
            args.func(args)
    except WorkflowError as exc:
        print(json.dumps({"status": "WORKFLOW_ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
