#!/usr/bin/env python3
"""Evidence-backed, resumable planning workflow for grounded-build."""

from __future__ import annotations

import argparse
import copy
import fcntl
import functools
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
import time
import tomllib
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


VERSION = "0.7.0"
SCHEMA_VERSION = 2
SUPPORTED_PROVIDERS = ("claude", "codex", "dsh", "other")
OTHER_BRIDGE_PROTOCOL = "grounded-build-other-v1"
MAX_INVOCATIONS_PER_ASSIGNMENT = 3
MAX_SYNTHESIS_SUBMISSIONS = 2
MAX_INFRASTRUCTURE_ATTEMPTS = 3
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
#: `auto` fills the two planning slots with the first two available adapters in this order and
#: degrades gracefully when one is missing; the third is the fallback. The default pair is
#: therefore claude + dsh (codex is the fallback).
AUTO_PREFERENCE = ("claude", "dsh", "codex")
TERMINAL_STATUSES = {"READY", "ABANDONED"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
CAUSAL_ANALYSIS = SKILL_ROOT / "references" / "causal_analysis.md"

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
    # A reviewer has to be able to read what it is reviewing. The model catalog ships
    # `truncation_policy: {mode: "tokens", limit: 10000}` per model, and a review of a plan
    # comfortably exceeds that: one cross-review reported its target "truncated at 13331 tokens"
    # and spent four turns failing to reassemble it. `tool_output_token_limit` is the documented
    # key for that budget ("Token budget for storing individual tool/function outputs in history")
    # and is absent from `codex --help`, so it was found in the published config reference rather
    # than locally. Measured: the same 93,322-byte file that truncated at the default reads back
    # complete -- chars_seen 93322, tail the real `]\n}\n` -- in 29,663 tokens.
    "-c", "tool_output_token_limit=200000",
)

EVIDENCE_STATUSES = ["VERIFIED", "STRONGLY_INFERRED", "WEAKLY_INFERRED", "UNRESOLVED", "REFUTED"]
PRIORITY_LANES = ["STOP_THE_LINE", "MUST_RESOLVE", "INVESTIGATE_IF_BUDGET", "RECORD_ONLY"]

EVIDENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string"}, "scope_id": {"type": "string"},
        "claim": {"type": "string"},
        "status": {"type": "string", "enum": EVIDENCE_STATUSES},
        "source_type": {"type": "string", "enum": [
            "REPOSITORY", "COMMAND", "OFFICIAL_DOCS", "OFFICIAL_GITHUB"]},
        "locator": {"type": "string"}, "retrieved_at": {"type": "string"},
        "version_or_commit": {"type": "string"}, "content_sha256": {"type": "string"},
    },
    "required": ["id", "scope_id", "claim", "status", "source_type", "locator",
                 "retrieved_at", "version_or_commit", "content_sha256"],
}

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string"}, "scope_id": {"type": "string"},
        "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
        "urgency": {"type": "string", "enum": ["U0", "U1", "U2", "U3"]},
        "lane": {"type": "string", "enum": PRIORITY_LANES},
        "evidence_status": {"type": "string", "enum": EVIDENCE_STATUSES},
        "problem": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "root_cause_status": {"type": "string", "enum": ["PROVEN", "HYPOTHESIS", "UNRESOLVED"]},
        "causal_chain": {"type": "array", "items": {"type": "string"}},
        "affected_surfaces": {"type": "array", "items": {"type": "string"}},
        "recommended_solution": {"type": "string"},
        "alternatives_and_tradeoffs": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "scope_id", "severity", "urgency", "lane", "evidence_status",
                 "problem", "evidence_ids", "root_cause_status", "causal_chain",
                 "affected_surfaces", "recommended_solution", "alternatives_and_tradeoffs",
                 "verification"],
}


INVESTIGATION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
        "slot": {"type": "string", "enum": ["A", "B"]},
        "baseline_sha": {"type": "string"},
        "scope_digest": {"type": "string"},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": EVIDENCE_ITEM_SCHEMA},
        "findings": {"type": "array", "items": FINDING_SCHEMA},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["provider", "slot", "baseline_sha", "scope_digest", "summary", "evidence",
                 "findings", "unresolved_questions"],
}


DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
        "slot": {"type": "string", "enum": ["A", "B"]},
        "baseline_sha": {"type": "string"},
        "summary": {"type": "string"},
        "scope_digest": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "new_evidence": {"type": "array", "items": EVIDENCE_ITEM_SCHEMA},
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
        "provider", "slot", "baseline_sha", "scope_digest", "evidence_ids", "new_evidence", "summary", "repository_facts",
        "plan_markdown", "unresolved_questions",
    ],
}


INTEGRATION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "provider": {"type": "string", "enum": list(SUPPORTED_PROVIDERS)},
        "slot": {"type": "string", "enum": ["A", "B"]},
        "reviewer_slot": {"type": "string", "enum": ["A", "B"]},
        "target": {"type": "string"},
        "round": {"type": "integer", "enum": [2, 3]},
        "baseline_sha": {"type": "string"}, "scope_digest": {"type": "string"},
        "summary": {"type": "string"}, "plan_markdown": {"type": "string"},
        "accepted_finding_ids": {"type": "array", "items": {"type": "string"}},
        "rejected_finding_ids": {"type": "array", "items": {"type": "string"}},
        "new_findings": {"type": "array", "items": FINDING_SCHEMA},
        "finding_aliases": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "canonical_finding_id": {"type": "string"},
                "alias_finding_ids": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["canonical_finding_id", "alias_finding_ids", "rationale", "evidence_ids"],
        }},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NEEDS_USER_DECISION"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "claim": {"type": "string"}, "evidence": {"type": "string"},
                "required_change": {"type": "string"},
            },
            "required": ["id", "severity", "claim", "evidence", "required_change"],
        }},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["provider", "slot", "reviewer_slot", "target", "round", "baseline_sha", "scope_digest", "summary",
                 "plan_markdown", "accepted_finding_ids", "rejected_finding_ids",
                 "new_findings", "finding_aliases", "verdict", "findings", "unresolved_questions"],
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


class ProviderInfrastructureError(WorkflowError):
    """A provider/adapter failure that says nothing about assignment quality."""

    def __init__(self, provider: str, kind: str, detail: str, *, retryable: bool) -> None:
        super().__init__(f"{provider} infrastructure failure [{kind}]: {detail}")
        self.provider = provider
        self.kind = kind
        self.detail = detail
        self.retryable = retryable


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


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the structural JSON-schema subset used by provider contracts."""
    expected = schema.get("type")
    matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected not in matches:
        raise WorkflowError(f"unsupported JSON schema type at {path}: {expected!r}")
    if not matches[expected](value):
        raise WorkflowError(f"provider result schema mismatch at {path}: expected {expected}")
    if "enum" in schema and not any(
            type(value) is type(candidate) and value == candidate for candidate in schema["enum"]):
        raise WorkflowError(f"provider result schema mismatch at {path}: value is not in enum")
    if expected == "object":
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise WorkflowError(
                f"provider result schema mismatch at {path}: missing {','.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise WorkflowError(
                    f"provider result schema mismatch at {path}: unexpected {','.join(extras)}")
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                validate_json_schema(item, child_schema, f"{path}.{name}")
    elif expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")


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
    "properties": {
        "provider": {"type": "string"}, "ready": {"type": "boolean"},
        "observed": {"type": "string"},
    },
    "required": ["provider", "ready", "observed"],
}


def probe_provider(
    provider: str, project: Path, timeout: int = 180,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Can this CLI authenticate, read one mounted file, and return a schema object right now?

    Cheap, and deliberately narrow. It answers the question that cost the most to answer the
    expensive way: a codex whose credentials never reached the sandbox spent 22 minutes and five
    401s before saying so, and the operator read it as a failed planning draft. One trivial call
    says it in seconds, before a topology is frozen around a provider that cannot deliver.

    The file read is deliberate. The old probe asked the model not to inspect anything, so Codex
    could pass it even when every production read failed because its code-mode host was absent.
    This remains a narrow capability check, not proof that a provider will complete a long review.

    It runs through the SAME bubblewrap composition as a real invocation, which is the only way it
    can see the failure it exists to catch: the credentials that went missing went missing because
    of the sandbox, so a probe outside the sandbox would have passed while every real call
    returned 401 -- worse than no probe, because it would have vouched for the thing that was
    broken.
    """
    with tempfile.TemporaryDirectory(prefix="grounded-build-probe-") as scratch:
        root = Path(scratch)
        # A capability probe has no repository-shaped question. Giving it the caller's checkout
        # merely exposes dirty and untracked material to a model before a baseline is frozen.
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        initialized = run(["git", "init", "-q"], cwd=workspace, timeout=30)
        if initialized.returncode != 0:
            raise WorkflowError("could not create the empty capability-probe repository")
        context = root / "context"
        context.mkdir(mode=0o700)
        probe_token = secrets.token_hex(16)
        probe_input = context / "probe-input.txt"
        probe_input.write_text(probe_token + "\n", encoding="utf-8")
        raw = root / "raw.json"
        prompt = (
            f"Read {probe_input}. Reply with the schema object only: provider={provider}, "
            "ready=true, observed=<the exact file contents without the trailing newline>. "
            "This is a capability check."
        )
        command = agent_command(provider, workspace, context, PROBE_SCHEMA, raw, prompt, runtime)
        command = isolated_agent_command(
            command, {"agent_runtime": {provider: runtime or {}}}, root, workspace, context)
        result = run(command, cwd=workspace, timeout=timeout, env=agent_environment())
        if result.returncode != 0:
            return {"provider": provider, "ok": False,
                    "reason": f"invocation failed ({result.returncode})",
                    "detail": (result.stderr or "").strip()[-400:]}
        try:
            payload = extract_payload(provider, result, raw)
            validate_json_schema(payload, PROBE_SCHEMA)
        except NoFinalAnswer as exc:
            return {"provider": provider, "ok": False, "reason": "no schema object",
                    "detail": str(exc)}
        except WorkflowError as exc:
            return {"provider": provider, "ok": False, "reason": "invalid schema object",
                    "detail": str(exc)}
        ok = (
            isinstance(payload, dict) and payload.get("ready") is True
            and payload.get("provider") == provider and payload.get("observed") == probe_token
        )
        return {
            "provider": provider, "ok": ok,
            "reason": (
                "read the mounted probe and delivered a schema object"
                if ok else "schema object did not reproduce the mounted probe contents"),
        }


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


def load_state(
    project: Path, run_id: str, *, allow_engine_drift: bool = False,
) -> dict[str, Any]:
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
    frozen_engine = state.get("engine_contract")
    if frozen_engine:
        current = engine_contract()
        if frozen_engine != current and not allow_engine_drift:
            raise WorkflowError(
                "planning engine drift detected; resume with the frozen engine or an explicit "
                "migration instead of silently changing prompts or semantics")
    validate_artifacts(state)
    return state


def pending_decision_identity(decision: dict[str, Any]) -> str:
    """Stable identity for independently produced decisions; timestamps are not identity."""
    parts = [str(decision.get("type", "UNKNOWN"))]
    for field in ("assignment", "source", "candidate_sha256"):
        if decision.get(field) is not None:
            parts.append(f"{field}={decision[field]}")
    return "|".join(parts)


def add_pending_decision(state: dict[str, Any], decision: dict[str, Any]) -> None:
    decisions = state.setdefault("pending_decisions", {})
    decisions[pending_decision_identity(decision)] = decision
    state["pending_decision"] = decisions[sorted(decisions)[0]]


def normalize_pending_decisions(state: dict[str, Any]) -> None:
    """Keep the legacy singular view while preserving every concurrent decision."""
    decisions = state.setdefault("pending_decisions", {})
    current = state.get("pending_decision")
    if state.get("status") == "NEEDS_USER_DECISION":
        if current:
            decisions.setdefault(pending_decision_identity(current), current)
        state["pending_decision"] = decisions[sorted(decisions)[0]] if decisions else None
    elif state.get("status") not in TERMINAL_STATUSES:
        decisions.clear()
        state["pending_decision"] = None


def save_state(state: dict[str, Any]) -> None:
    normalize_pending_decisions(state)
    state["updated_at"] = utc_now()
    state["revision"] = state.get("revision", 0) + 1
    state["integrity_hmac"] = state_signature(state)
    atomic_json(Path(state["run_directory"]) / "workflow.json", state)
    validate_or_recover_anchor(state)


def recompute_resource_usage(state: dict[str, Any]) -> None:
    aggregate = {"wall_seconds": 0.0, "reported_cost_usd": 0.0,
                 "reported_turns": 0, "scratch_bytes_removed": 0}
    root = Path(state["run_directory"]) / "invocations"
    for path in root.glob("*/*/invocation.json") if root.exists() else []:
        try:
            metrics = json.loads(path.read_text(encoding="utf-8")).get("metrics", {})
        except (OSError, json.JSONDecodeError):
            continue
        for key in aggregate:
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                aggregate[key] += value
    state["resource_usage"] = aggregate


def active_invocation_records(state: dict[str, Any]) -> dict[str, Any]:
    active: dict[str, Any] = {}
    root = Path(state["run_directory"]) / "invocations"
    for path in root.glob("*/*/invocation.json") if root.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("status") == "RUNNING":
            active[record.get("assignment", path.parent.parent.name)] = {
                "provider": record.get("provider"), "slot": record.get("slot"),
                "started_at": record.get("started_at"), "invocation": str(path),
                "context_digest": record.get("context_digest"),
            }
    return active


def recover_interrupted_assignment(
    state: dict[str, Any], assignment: str, actor: str, reason: str,
) -> list[str]:
    """Terminalize stale records for one assignment after its process lock has been reacquired."""
    recovered: list[str] = []
    root = Path(state["run_directory"]) / "invocations" / assignment
    for path in root.glob("*/invocation.json") if root.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"cannot recover malformed invocation record {path}: {exc}") from exc
        if record.get("status") != "RUNNING":
            continue
        record.update({
            "status": "CONTROLLER_INTERRUPTED", "completed_at": utc_now(),
            "recovered_by": actor, "recovery_reason": reason,
        })
        atomic_json(path, record)
        recovered.append(str(path))
    if recovered:
        state.setdefault("invocation_recoveries", []).append({
            "assignment": assignment, "actor": actor, "reason": reason,
            "invocations": recovered, "recovered_at": utc_now(),
        })
    return recovered


def persist_invocation_state(
    state: dict[str, Any], assignment: str, *, clear_fault: bool = False,
) -> None:
    """Merge one assignment's accounting without overwriting its parallel peer."""
    project = Path(state["project"])
    with run_lock(project, state["run_id"]):
        latest = load_state(project, state["run_id"])
        for field in ("usage", "infrastructure_usage", "extra_invocation_grants"):
            if assignment in state.get(field, {}):
                latest.setdefault(field, {})[assignment] = state[field][assignment]
        if assignment in state.get("_reimbursed_assignments", set()):
            latest.setdefault("usage", {}).pop(assignment, None)
        if assignment in state.get("charged_context_digests", {}):
            latest.setdefault("charged_context_digests", {})[assignment] = list(
                state["charged_context_digests"][assignment])
        if assignment in state.get("invocation_reservations", {}):
            latest.setdefault("invocation_reservations", {})[assignment] = dict(
                state["invocation_reservations"][assignment])
        latest.setdefault("artifacts", {}).update({
            key: value for key, value in state.get("artifacts", {}).items()
            if key.startswith(f"invocation-{assignment}-")
        })
        fault = state.get("delivery_faults", {}).get(assignment)
        if fault:
            latest.setdefault("delivery_faults", {})[assignment] = fault
        elif clear_fault:
            latest.get("delivery_faults", {}).pop(assignment, None)
        incoming_fallbacks = state.get("automatic_fallbacks") or []
        known_fallbacks = {
            (item.get("trigger_assignment"), item.get("at"))
            for item in latest.setdefault("automatic_fallbacks", [])
        }
        for fallback in incoming_fallbacks:
            identity = (fallback.get("trigger_assignment"), fallback.get("at"))
            if identity not in known_fallbacks:
                latest["automatic_fallbacks"].append(fallback)
                known_fallbacks.add(identity)
        if incoming_fallbacks:
            latest.setdefault("slot_provider_overrides", {}).update(
                state.get("slot_provider_overrides") or {}
            )
            known_notes = {
                (item.get("assignment"), item.get("from"), item.get("to"), item.get("decided_at"))
                for item in latest.setdefault("independence_notes", [])
            }
            for note in state.get("independence_notes") or []:
                identity = (
                    note.get("assignment"), note.get("from"), note.get("to"),
                    note.get("decided_at"),
                )
                if identity not in known_notes:
                    latest["independence_notes"].append(note)
                    known_notes.add(identity)
            latest["provider_diversity"] = False
            latest["model_diversity"] = False
        incoming_decisions = dict(state.get("pending_decisions") or {})
        current = state.get("pending_decision")
        if current and current.get("assignment") == assignment:
            incoming_decisions[pending_decision_identity(current)] = current
        if latest.get("status") not in TERMINAL_STATUSES:
            for decision in incoming_decisions.values():
                add_pending_decision(latest, decision)
            if incoming_decisions:
                latest["status"] = "NEEDS_USER_DECISION"
        recompute_resource_usage(latest)
        save_state(latest)
        state.clear()
        state.update(latest)


def save_parallel_stage(state: dict[str, Any], stage: str) -> None:
    """Merge a completed A/B artifact and derive the barrier state atomically."""
    project = Path(state["project"])
    with run_lock(project, state["run_id"]):
        latest = load_state(project, state["run_id"])
        binding = state.get("_completed_invocation_binding") or {}
        if binding and (binding.get("engine_epoch", 0) != latest.get("engine_epoch", 0)
                or binding.get("engine_contract") != latest.get("engine_contract")):
            raise WorkflowError(
                f"stale {stage} result produced under a different engine epoch or contract")
        if latest.get("status") in TERMINAL_STATUSES:
            new_artifacts = {
                key: value for key, value in state.get("artifacts", {}).items()
                if key not in latest.setdefault("artifacts", {})
            }
            latest["artifacts"].update(new_artifacts)
            latest.setdefault("late_parallel_results", []).append({
                "stage": stage, "terminal_status": latest["status"], "retained_at": utc_now(),
                "invocation": binding.get("invocation"),
                "artifact_keys": sorted(new_artifacts),
            })
            save_state(latest)
            state.clear()
            state.update(latest)
            return
        candidate_bound_field = {
            "convergence-review": "convergence_reviews",
            "final-review": "final_reviews",
        }.get(stage)
        if candidate_bound_field:
            current_digest = (latest.get("candidate") or {}).get("plan_sha256")
            incoming = state.get(candidate_bound_field, {})
            stale_slots = sorted(
                slot for slot, record in incoming.items()
                if record.get("candidate_sha256") != current_digest
            )
            if stale_slots:
                raise WorkflowError(
                    f"stale {stage} result for a superseded candidate: {','.join(stale_slots)}"
                )
        for field in ("investigations", "drafts", "cross_reviews", "finding_aliases"):
            latest.setdefault(field, {}).update(state.get(field, {}))
        if candidate_bound_field:
            latest.setdefault(candidate_bound_field, {}).update(
                state.get(candidate_bound_field, {})
            )
        for round_number in ("1", "2", "3"):
            latest.setdefault("draft_rounds", {}).setdefault(round_number, {}).update(
                state.get("draft_rounds", {}).get(round_number, {}))
        latest.setdefault("artifacts", {}).update(state.get("artifacts", {}))
        # Merge ledger observations rather than allowing the second finisher to erase the first.
        for key, record in state.get("finding_ledger", {}).items():
            if key not in latest.setdefault("finding_ledger", {}):
                latest["finding_ledger"][key] = record
            else:
                seen = {(item.get("source"), item.get("source_id"))
                        for item in latest["finding_ledger"][key]["observations"]}
                latest["finding_ledger"][key]["observations"].extend(
                    item for item in record["observations"]
                    if (item.get("source"), item.get("source_id")) not in seen)
        incoming_decisions = dict(state.get("pending_decisions") or {})
        if state.get("pending_decision"):
            incoming_decisions[pending_decision_identity(state["pending_decision"])] = state["pending_decision"]
        for decision in incoming_decisions.values():
            add_pending_decision(latest, decision)
        if latest.get("status") == "NEEDS_USER_DECISION" or incoming_decisions:
            latest["status"] = "NEEDS_USER_DECISION"
        elif stage == "investigate":
            latest["status"] = "EVIDENCE_READY" if set(latest["investigations"]) == {"A", "B"} else "INVESTIGATING"
        elif stage == "draft":
            latest["status"] = "DRAFTS_READY" if set(latest["drafts"]) == {"A", "B"} else "DRAFTING"
            if latest["status"] == "DRAFTS_READY":
                freeze_round_barrier(latest, "cross-review")
        elif stage == "cross-review":
            latest["status"] = (
                "DIVERGENCE_REQUIRED" if set(latest["cross_reviews"]) == {"A", "B"}
                and latest["planning_depth"] == "deep" else
                "SYNTHESIS_REQUIRED" if set(latest["cross_reviews"]) == {"A", "B"} else
                "CROSS_REVIEWING")
            if latest["status"] == "DIVERGENCE_REQUIRED":
                freeze_round_barrier(latest, "diverge")
        elif stage == "diverge":
            latest["status"] = (
                "SYNTHESIS_REQUIRED" if set(latest["draft_rounds"]["3"]) == {"A", "B"}
                else "DIVERGING")
        elif stage == "convergence-review":
            if set(latest["convergence_reviews"]) == {"A", "B"}:
                latest["convergence_round_one_complete"] = True
                verdicts = [item["verdict"] for item in latest["convergence_reviews"].values()]
                if all(verdict == "PASS" for verdict in verdicts):
                    latest["status"] = "FINAL_REVIEW_REQUIRED"
                elif latest["synthesis_submissions"] < (
                        MAX_SYNTHESIS_SUBMISSIONS + latest.get("extra_synthesis_grants", 0)):
                    latest["status"] = "SYNTHESIS_REQUIRED"
                else:
                    latest["status"] = "NEEDS_USER_DECISION"
                    latest["pending_decision"] = {
                        "type": "CONVERGENCE_BUDGET_EXHAUSTED", "created_at": utc_now()}
            else:
                latest["status"] = "CONVERGENCE_REVIEWING"
        elif stage == "final-review":
            required = required_final_reviewers(latest)
            if set(latest["final_reviews"]) == set(required):
                verdicts = [item["verdict"] for item in latest["final_reviews"].values()]
                if all(verdict == "PASS" for verdict in verdicts):
                    finalize_ready_candidate(latest)
                elif latest["synthesis_submissions"] < (
                        MAX_SYNTHESIS_SUBMISSIONS + latest.get("extra_synthesis_grants", 0)):
                    latest["status"] = "SYNTHESIS_REQUIRED"
                else:
                    latest["status"] = "NEEDS_USER_DECISION"
                    latest["pending_decision"] = {
                        "type": "FINAL_REVIEW_BUDGET_EXHAUSTED", "created_at": utc_now()}
            else:
                latest["status"] = "FINAL_REVIEWING"
        atomic_json(Path(latest["finding_ledger_path"]), {"findings": latest["finding_ledger"]})
        record_artifact(latest, "finding-ledger", Path(latest["finding_ledger_path"]))
        save_state(latest)
        state.clear()
        state.update(latest)


def freeze_round_barrier(state: dict[str, Any], stage: str) -> None:
    """Snapshot mutable shared inputs before either same-round slot may add observations."""
    existing = state.setdefault("round_barriers", {}).get(stage)
    if existing:
        return
    directory = Path(state["run_directory"]) / "input" / "barriers"
    path = directory / f"{stage}-finding-ledger.json"
    atomic_json(path, {"findings": state.get("finding_ledger", {})})
    state["round_barriers"][stage] = {
        "finding_ledger": str(path), "finding_keys": sorted(state.get("finding_ledger", {})),
        "created_at": utc_now(),
    }
    record_artifact(state, f"barrier-{stage}-finding-ledger", path)


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


@contextmanager
def assignment_lock(project: Path, run_id: str, assignment: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", assignment)
    lock_path = run_directory(project, run_id) / f".assignment-{safe}.lock"
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


def engine_contract() -> dict[str, Any]:
    """Identity of the local semantics that a run is allowed to use."""
    files = {
        "plan_workflow.py": Path(__file__).resolve(),
        "dsh_read_boundary.mjs": SKILL_ROOT / "scripts" / "dsh_read_boundary.mjs",
        "SKILL.md": SKILL_ROOT / "SKILL.md",
        "planning_workflow.md": SKILL_ROOT / "references" / "planning_workflow.md",
        "causal_analysis.md": CAUSAL_ANALYSIS,
    }
    return {
        "software_version": VERSION,
        "files": {name: sha256_file(path) for name, path in files.items()},
    }


def valid_scope_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(
        r"(?:TARGET|REQUIRED_SUPPORT|EVIDENCE_ONLY|PROPOSED_EXTENSION|OUT_OF_SCOPE)-\d{3,}", value))


def finding_fingerprint(finding: dict[str, Any]) -> str:
    normalized = json.dumps({
        "scope_id": finding.get("scope_id"),
        "problem": " ".join(str(finding.get("problem", "")).lower().split()),
        "causal_chain": [" ".join(str(item).lower().split()) for item in finding.get("causal_chain", [])],
    }, sort_keys=True, separators=(",", ":"))
    return "F-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def record_findings(state: dict[str, Any], findings: list[dict[str, Any]], source: str) -> None:
    ledger = state.setdefault("finding_ledger", {})
    for finding in findings:
        key = finding_fingerprint(finding)
        observation = {"source": source, "source_id": finding["id"], "at": utc_now(), **finding}
        if key not in ledger:
            ledger[key] = {"fingerprint": key, "canonical": finding, "observations": [observation]}
        else:
            ledger[key]["observations"].append(observation)


def resolve_other_bridge() -> tuple[Path | None, str | None]:
    """Resolve the generic bridge while keeping an irrelevant bad setting non-authoritative."""
    configured = os.environ.get("GROUNDED_BUILD_OTHER_COMMAND", "").strip()
    if not configured:
        return None, None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        return None, "GROUNDED_BUILD_OTHER_COMMAND must be an absolute executable path"
    resolved = candidate.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return (
            None,
            "GROUNDED_BUILD_OTHER_COMMAND does not name an executable file: " + str(resolved),
        )
    return resolved, None


def provider_executable(provider: str) -> Path | None:
    if provider == "other":
        return resolve_other_bridge()[0]
    executable = shutil.which(provider)
    return Path(executable).resolve() if executable else None


def available(provider: str) -> bool:
    return provider_executable(provider) is not None


@functools.lru_cache(maxsize=None)
def executable_version(provider: str) -> str | None:
    """Return a short CLI version without making a model/API call."""
    if not available(provider):
        return None
    executable_path = provider_executable(provider)
    executable = str(executable_path) if executable_path else None
    if not executable:
        return None
    # PATH is preserved so a script-based CLI (dsh is `#!/usr/bin/env node`) can resolve its
    # interpreter; `--version` is a local no-network call, so this leaks nothing sensitive.
    version_env = {**agent_environment(), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = run([executable, "--version"], timeout=30, env=version_env)
    if result.returncode != 0:
        return None
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0][:200] if value else None


def codex_runtime_identity(
    model: str | None = None, model_provider: str | None = None, profile: str | None = None,
) -> dict[str, Any]:
    """Describe Codex without persisting credentials or endpoint URLs.

    Codex is a CLI adapter, not a model family. GPT-backed installations and Codex configured for
    an OpenAI-compatible DeepSeek gateway use the same adapter, so the non-secret selection is
    frozen separately from the executable name.
    """
    configured: dict[str, Any] = {}
    codex_home = Path.home() / ".codex"
    configs = [codex_home / "config.toml"]
    if profile:
        configs.append(codex_home / f"{profile}.config.toml")
    for config in configs:
        if not config.is_file():
            continue
        try:
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        for key in ("model", "model_provider"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                configured[key] = value.strip()
    selected_model = model or configured.get("model")
    selected_provider = model_provider or configured.get("model_provider")
    lowered = " ".join(str(value).lower() for value in (selected_model, selected_provider) if value)
    family = "deepseek" if "deepseek" in lowered else "gpt" if any(
        marker in lowered for marker in ("gpt", "openai", "o1", "o3", "o4")
    ) else "unknown"
    return {
        "adapter": "codex",
        "cli_version": executable_version("codex"),
        "model": selected_model,
        "model_provider": selected_provider,
        "profile": profile,
        "model_family": family,
        "identity_source": "explicit_override" if any((model, model_provider, profile)) else "codex_config",
    }


def dsh_runtime_identity(
    model: str | None = None, model_provider: str | None = None,
) -> dict[str, Any]:
    """Describe the ``dsh`` adapter without persisting credentials or endpoint URLs.

    ``dsh`` is DeepSeek Harness's CLI adapter, not a model family. Its effective model and provider
    come from the harness settings file (the user's saved selection layered over the profile
    composition default), so the non-secret selection is read from ``settings.yaml`` rather than
    invented here. Explicit ``--dsh-model`` / ``--dsh-model-provider`` win, matching codex/claude.
    """
    dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
    configured = dsh_settings_selection(dsh_home / "settings.yaml")
    # The harness composition default (dsh-base/cordis.patch.yml `agent-default-model`) is
    # deepseek-v4-flash; report it honestly so an unpinned run does not claim "no model" while the
    # fast tier actually serves it.
    selected_model = model or configured.get("model") or "deepseek-v4-flash"
    selected_provider = model_provider or configured.get("provider") or "deepseek-official"
    lowered = " ".join(str(value).lower() for value in (selected_model, selected_provider) if value)
    family = "deepseek" if "deepseek" in lowered else "gpt" if any(
        marker in lowered for marker in ("gpt", "openai", "o1", "o3", "o4")
    ) else "unknown"
    return {
        "adapter": "dsh",
        "cli_version": executable_version("dsh"),
        "model": selected_model,
        "model_provider": selected_provider,
        "profile": None,
        "model_family": family,
        "identity_source": "explicit_override" if any((model, model_provider)) else (
            "dsh_settings" if configured else "harness_default"),
    }


def dsh_settings_selection(settings: Path) -> dict[str, str]:
    """Read dsh's two non-secret selection scalars without adding a YAML dependency.

    The runtime is deliberately standard-library only.  dsh owns the complete YAML document;
    Grounded Build needs only the generated top-level ``agent-default-model`` mapping and ignores
    every other key, including credentials and endpoint URLs.
    """
    try:
        lines = settings.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    configured: dict[str, str] = {}
    in_selection = False
    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line.strip()
        if not in_selection:
            if indent == 0 and re.fullmatch(r"agent-default-model:\s*(?:#.*)?", content):
                in_selection = True
            continue
        if indent == 0:
            break
        match = re.fullmatch(r"(provider|model):\s*(.*?)\s*", content)
        if not match or not match.group(2):
            continue
        value = match.group(2).split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            configured[match.group(1)] = value
    return configured


def agent_runtime(args: argparse.Namespace | None = None) -> dict[str, dict[str, Any]]:
    """Build the non-secret adapter identity frozen into a new run."""
    model = getattr(args, "codex_model", None) if args else None
    model_provider = getattr(args, "codex_model_provider", None) if args else None
    profile = getattr(args, "codex_profile", None) if args else None
    claude_model = getattr(args, "claude_model", None) if args else None
    dsh_model = getattr(args, "dsh_model", None) if args else None
    dsh_model_provider = getattr(args, "dsh_model_provider", None) if args else None
    if profile and not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        raise WorkflowError("--codex-profile must be a simple profile name")
    for option, value in (
        ("--codex-model", model), ("--codex-model-provider", model_provider),
        ("--dsh-model", dsh_model), ("--dsh-model-provider", dsh_model_provider),
    ):
        if value and (len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_./:+-]+", value)):
            raise WorkflowError(f"{option} must be a simple non-secret selector")
    explicit_codex = any((model, model_provider, profile))
    selected_codex_model = None if model == "cli-default" else model
    if not explicit_codex:
        selected_codex_model = DEFAULT_CODEX_MODEL
    selected_claude_model = DEFAULT_CLAUDE_MODEL if claude_model is None else claude_model
    if selected_claude_model == "cli-default":
        selected_claude_model = None
    if dsh_model == "cli-default":
        dsh_model = None
    other_executable = provider_executable("other")
    runtime = {
        "codex": codex_runtime_identity(selected_codex_model, model_provider, profile),
        "claude": {
            "adapter": "claude", "cli_version": executable_version("claude"),
            "model": selected_claude_model, "model_provider": "anthropic", "profile": None,
            "model_family": "claude",
            "identity_source": "cli_default" if selected_claude_model is None else (
                "explicit_override" if claude_model is not None else "grounded_build_default"),
        },
        "dsh": dsh_runtime_identity(dsh_model, dsh_model_provider),
        "other": {
            "adapter": "other", "cli_version": executable_version("other"),
            "model": None, "model_provider": None, "profile": None,
            "model_family": "other", "identity_source": "generic_bridge",
            "executable": str(other_executable) if other_executable else None,
            "executable_sha256": sha256_file(other_executable) if other_executable else None,
            "protocol": OTHER_BRIDGE_PROTOCOL,
        },
    }
    if not explicit_codex:
        runtime["codex"]["identity_source"] = "grounded_build_default"
    return runtime


def adapter_capabilities(provider: str) -> dict[str, Any]:
    """Check the non-network CLI surface this workflow depends on."""
    if provider == "other":
        _, configuration_error = resolve_other_bridge()
        if configuration_error:
            return {"ok": False, "missing": [configuration_error]}
    executable_path = provider_executable(provider)
    if not executable_path:
        return {"ok": False, "missing": ["executable"]}
    executable = str(executable_path)
    if provider == "other":
        result = run(
            [executable, "capabilities"], timeout=30,
            env={**agent_environment(), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        try:
            declaration = json.loads(result.stdout)
        except json.JSONDecodeError:
            declaration = None
        required = {
            "protocol": OTHER_BRIDGE_PROTOCOL,
            "read_only": True,
            "structured_output": True,
            "fresh_process": True,
        }
        ok = result.returncode == 0 and isinstance(declaration, dict) and all(
            declaration.get(key) == value for key, value in required.items()
        )
        return {
            "ok": ok,
            "missing": [] if ok else ["grounded-build-other-v1 capability declaration"],
            "capability_exit": result.returncode,
            "declaration": declaration,
            "executable": executable,
            "executable_sha256": sha256_file(executable_path),
        }
    if provider == "codex":
        help_args = [executable, "exec", "--help"]
        required = ["--output-schema", "--output-last-message", "--ephemeral", "--sandbox", "--config"]
    elif provider == "dsh":
        # Launcher flags belong to global help. The headless profile's own help is checked below;
        # conflating the two made supported global flags appear absent.
        help_args = [executable, "--help"]
        required = ["--profile", "--patch", "--dump-config"]
    else:
        help_args = [executable, "--help"]
        required = ["--json-schema", "--output-format", "--permission-mode", "--no-session-persistence"]
    result = run(help_args, timeout=30)
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    missing = [flag for flag in required if flag not in text]
    capability = {"ok": result.returncode == 0 and not missing, "missing": missing,
                  "help_exit": result.returncode}
    if provider == "codex":
        # The enforced Codex policy exposes repository reads through code-mode. Standalone
        # releases ship the matching host beside the resolved CLI; without it the CLI can still
        # authenticate and emit JSON but cannot perform the review it is being selected for.
        code_mode_host = Path(executable).resolve().with_name("codex-code-mode-host")
        host_ready = code_mode_host.is_file() and os.access(code_mode_host, os.X_OK)
        capability["code_mode_host"] = {
            "ok": host_ready, "path": str(code_mode_host) if host_ready else None,
        }
        capability["ok"] = bool(capability["ok"] and host_ready)
        if not host_ready:
            capability["missing"].append("matching executable codex-code-mode-host")
    if provider == "dsh" and capability["ok"]:
        profile_help = run([executable, "--profile", "headless", "--help"], timeout=30)
        profile_ready = profile_help.returncode == 0
        capability["headless_profile_probe"] = {
            "ok": profile_ready, "exit": profile_help.returncode,
        }
        capability["ok"] = bool(capability["ok"] and profile_ready)
        if not profile_ready:
            capability["missing"].append("working headless profile")
        try:
            with Path(executable).resolve().open("rb") as handle:
                first_line = handle.readline(64).decode("utf-8", "replace")
        except OSError:
            first_line = ""
        # The production adapter is the Node CLI. Test doubles and future native launchers still
        # have their argv surface checked above; the current Node shape additionally proves that
        # the overlay is composed, instead of trusting an advertised but ignored --patch flag.
        if "node" in first_line:
            with tempfile.TemporaryDirectory(prefix="grounded-build-dsh-capability-") as scratch:
                root = Path(scratch)
                dsh_home = root / "dsh-home"
                dsh_home.mkdir(mode=0o700)
                patch = root / "capability.patch.yml"
                patch.write_text(
                    "- id: tool-bash\n  disabled: true\n"
                    "- insert:\n"
                    "    - id: grounded-build-capability-marker\n"
                    "      name: cordis:group\n"
                    "      group: true\n"
                    "      config: []\n",
                    encoding="utf-8",
                )
                environment = agent_environment()
                environment.update({
                    "HOME": str(root / "home"), "DSH_HOME": str(dsh_home),
                    "DSH_PERMISSION_MODE": "read-only", "DSH_TOOLS_MODE": "native",
                    "PATH": ":".join(dict.fromkeys([
                        str(Path(shutil.which("node") or "/usr/bin/node").parent),
                        "/usr/bin", "/bin",
                    ])),
                })
                composed = run(
                    [executable, "--profile", "headless", "--patch", str(patch),
                     "--dump-config"],
                    timeout=30, env=environment,
                )
                loaded = (
                    composed.returncode == 0
                    and "grounded-build-capability-marker" in composed.stdout
                    and "id: tool-bash" in composed.stdout
                    and "disabled: true" in composed.stdout
                )
                capability["patch_composition_probe"] = {
                    "ok": loaded, "exit": composed.returncode,
                }
                capability["ok"] = bool(capability["ok"] and loaded)
                if not loaded:
                    capability["missing"].append("working --patch composition")
    return capability


def resolve_topology(backend: str) -> dict[str, str]:
    return resolve_host_topology(backend, None)


def external_reviewer_preference(host_adapter: str) -> tuple[str, ...]:
    """Ordered fresh-CLI reviewers outside the host's own adapter identity."""
    if host_adapter == "codex":
        return ("claude", "dsh")
    if host_adapter == "other":
        return ("codex", "claude", "dsh")
    return ("codex",)


def resolve_host_topology(
    backend: str, host_adapter: str | None, peer_reviewer: str = "auto",
) -> dict[str, str]:
    present = {name for name in SUPPORTED_PROVIDERS if available(name)}
    if peer_reviewer != "auto" and (backend != "auto" or not host_adapter):
        raise WorkflowError("--peer-reviewer requires --backend auto and --host-adapter")
    if backend == "auto":
        if host_adapter:
            if host_adapter not in present:
                raise WorkflowError(f"host adapter is unavailable: {host_adapter}")
            if peer_reviewer != "auto":
                if peer_reviewer not in present:
                    raise WorkflowError(f"peer reviewer is unavailable: {peer_reviewer}")
                external = peer_reviewer
            else:
                external = next(
                    (name for name in external_reviewer_preference(host_adapter) if name in present),
                    None,
                )
            return {"A": host_adapter, "B": external or host_adapter}
        # Take the first two available adapters in preference order; a missing adapter degrades to
        # the next one, and a lone adapter fills both slots as two isolated instances (same adapter,
        # not model-diverse). The third entry in AUTO_PREFERENCE is the fallback.
        ordered = [name for name in AUTO_PREFERENCE if name in present]
        if len(ordered) >= 2:
            return {"A": ordered[0], "B": ordered[1]}
        if len(ordered) == 1:
            return {"A": ordered[0], "B": ordered[0]}
        raise WorkflowError("no supported provider is available")
    if backend not in SUPPORTED_PROVIDERS or backend not in present:
        raise WorkflowError(f"requested provider is unavailable: {backend}")
    return {"A": backend, "B": backend}


def resolve_final_reviewer(
    selected: str, host_adapter: str | None, peer_provider: str | None = None,
) -> str:
    if selected != "auto":
        if selected != "both" and not available(selected):
            raise WorkflowError(f"final reviewer is unavailable: {selected}")
        return selected
    if not host_adapter:
        raise WorkflowError("--final-reviewer auto requires --host-adapter")
    if not available(host_adapter):
        raise WorkflowError(f"host adapter is unavailable: {host_adapter}")
    return host_adapter


def verify_provider_selection(
    provider: str, project: Path, runtime: dict[str, Any],
) -> dict[str, Any]:
    """Verify static requirements and one real read using the exact runtime about to be frozen."""
    capability = adapter_capabilities(provider)
    if not capability.get("ok"):
        return {
            "provider": provider, "ok": False, "reason": "static requirements failed",
            "capability": capability,
        }
    return {
        **probe_provider(provider, project, runtime=runtime),
        "capability": capability,
    }


def resolve_verified_host_selection(
    args: argparse.Namespace, project: Path, runtime: dict[str, dict[str, Any]],
    *, perform_probe: bool = True,
) -> tuple[dict[str, str], str | None, str, dict[str, dict[str, Any]]]:
    """Resolve host-aware routing from checks performed at the initialization boundary."""
    host = args.host_adapter
    topology = resolve_host_topology(args.backend, host, args.peer_reviewer)
    frozen_peer = topology["B"] if host and args.backend == "auto" else None
    final_reviewer = resolve_final_reviewer(args.final_reviewer, host, frozen_peer)
    if not host:
        return topology, frozen_peer, final_reviewer, {}

    checks: dict[str, dict[str, Any]] = {}

    def check(provider: str) -> dict[str, Any]:
        if provider not in checks:
            if perform_probe:
                checks[provider] = verify_provider_selection(provider, project, runtime[provider])
            else:
                capability = adapter_capabilities(provider)
                checks[provider] = {
                    "provider": provider, "ok": bool(capability.get("ok")),
                    "reason": "static requirements passed" if capability.get("ok")
                    else "static requirements failed",
                    "capability": capability,
                }
        return checks[provider]

    host_is_selected = args.backend == "auto" or args.final_reviewer == "auto"
    if host_is_selected:
        host_check = check(host)
        if not host_check["ok"]:
            raise WorkflowError(
                "host adapter failed the initialization-bound capability probe: "
                + json.dumps(host_check, sort_keys=True)
            )

    if args.backend == "auto":
        if args.peer_reviewer != "auto":
            peer_check = check(args.peer_reviewer)
            if not peer_check["ok"]:
                raise WorkflowError(
                    "explicit peer reviewer failed the initialization-bound capability probe: "
                    + json.dumps(peer_check, sort_keys=True)
                )
            frozen_peer = args.peer_reviewer
        else:
            frozen_peer = host
            for candidate in external_reviewer_preference(host):
                if check(candidate)["ok"]:
                    frozen_peer = candidate
                    break
        topology = {"A": host, "B": frozen_peer}

    if args.final_reviewer == "auto":
        final_reviewer = host
    elif args.final_reviewer != "both":
        final_check = check(args.final_reviewer)
        if not final_check["ok"]:
            raise WorkflowError(
                "explicit final reviewer failed the initialization-bound capability probe: "
                + json.dumps(final_check, sort_keys=True)
            )

    for provider in set(topology.values()):
        provider_check = check(provider)
        if not provider_check["ok"]:
            raise WorkflowError(
                "selected planner failed the initialization-bound capability probe: "
                + json.dumps(provider_check, sort_keys=True)
            )
    return topology, frozen_peer, final_reviewer, checks


def required_final_reviewers(state: dict[str, Any]) -> dict[str, str]:
    selected = state["final_reviewer"]
    if selected == "both":
        return dict(state["planners"])
    return {"F": selected}


def split_draft_for_review(state: dict[str, Any], slot: str) -> dict[str, Path]:
    """Hand the reviewer the plan as prose and the claims as data, instead of one JSON blob.

    The reviewer used to receive `<slot>.json`, whose `plan_markdown` field is the entire plan as a
    single escaped string. Two things follow, and both were measured on one run: the JSON is 93,322
    bytes where the same plan as markdown is 42,636 -- escaping more than doubles it -- and no file
    reader can page through a document that is one line. The reviewer of the large draft said so in
    its own transcript ("the target_draft.json was truncated at B1's text ... first 344 lines, then
    truncated at 13331 tokens") and spent four whole turns trying to reassemble it. The reviewer of
    the small draft, 28,874 bytes, passed on its first attempt.

    That asymmetry read as a difference between the two CLIs. It was a difference between the two
    drafts, and the workflow created it by choosing the larger, unreadable representation of a file
    it had already written in the better one.

    Splitting also keeps the failure honest if it recurs: a reviewer that truncates a markdown plan
    can still read the rest of it, where a truncated JSON string yields nothing after the cut.
    """
    record = state["drafts"][slot]
    payload = json.loads(Path(record["path"]).read_text(encoding="utf-8"))
    claims = {key: value for key, value in payload.items() if key != "plan_markdown"}
    claims_path = Path(state["run_directory"]) / "drafts" / f"{slot}.claims.json"
    atomic_json(claims_path, claims)
    return {
        "target_plan.md": Path(record["markdown"]),
        "target_claims.json": claims_path,
    }


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


def write_terminal_report(state: dict[str, Any], outcome: str) -> Path:
    """Write an honest audit handoff for both successful and abandoned runs."""
    report = Path(state["run_directory"]) / "final_report.md"
    candidate = state.get("candidate") or {}
    reviews = {
        "convergence": state.get("convergence_reviews") or {},
        "final": state.get("final_reviews") or {},
    }
    abandoned = state.get("abandoned") or {}
    report.write_text(
        "# Grounded Build planning report\n\n"
        f"- Outcome: `{outcome}`\n"
        f"- Run: `{state['run_id']}`\n"
        f"- Baseline: `{state['baseline_sha']}`\n"
        f"- Engine contract: `{json.dumps(state.get('engine_contract', {}), sort_keys=True)}`\n"
        f"- Host adapter: `{state.get('host_adapter') or 'unbound-legacy'}`\n"
        f"- Peer reviewer: `{state.get('peer_reviewer') or 'unbound-legacy'}`\n"
        f"- Selection checks: `{json.dumps(state.get('selection_checks', {}), sort_keys=True)}`\n"
        f"- Planners: `{json.dumps(state['planners'], sort_keys=True)}`\n"
        f"- Provider diversity: `{str(state['provider_diversity']).lower()}`\n"
        f"- Model diversity: `{str(state.get('model_diversity', False)).lower()}`\n"
        f"- Agent runtime: `{json.dumps(state.get('agent_runtime', {}), sort_keys=True)}`\n"
        f"- Runtime warnings: `{json.dumps(state.get('runtime_warnings', []), sort_keys=True)}`\n"
        f"- Resource usage: `{json.dumps(state.get('resource_usage', {}), sort_keys=True)}`\n"
        f"- Quality attempts: `{json.dumps(state.get('usage', {}), sort_keys=True)}`\n"
        f"- Infrastructure attempts: `{json.dumps(state.get('infrastructure_usage', {}), sort_keys=True)}`\n"
        f"- Automatic provider fallbacks: `{json.dumps(state.get('automatic_fallbacks', []), sort_keys=True)}`\n"
        f"- Candidate: `{json.dumps(candidate, sort_keys=True)}`\n"
        f"- Reviews: `{json.dumps(reviews, sort_keys=True)}`\n"
        f"- Abandonment: `{json.dumps(abandoned, sort_keys=True)}`\n"
        + independence_section(state)
        + "\n"
        + ("The reviewed plan is not authorized for implementation until the user explicitly approves.\n"
           if outcome == "READY" else
           "No plan was approved. This report preserves the last candidate, reviews, and terminal reason for audit.\n"),
        encoding="utf-8",
    )
    record_artifact(state, "final-report", report)
    return report


def finalize_ready_candidate(state: dict[str, Any]) -> None:
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
        "plan_sha256": sha256_file(final_plan),
        "batch_manifest_sha256": sha256_file(final_batches),
    }
    state["status"] = "READY"
    state["final"]["report"] = str(write_terminal_report(state, "READY"))


def context_digest(context_files: dict[str, Path]) -> str:
    """What this assignment is being asked ABOUT, as one hash.

    Decisions are excluded on purpose. They accumulate in the context as the run adjudicates, so a
    digest that counted them would change on every adjudication and reset the very budget the
    adjudication was needed for -- a cap that lifts itself whenever it is reached is not a cap.
    What is hashed is the material: the request, the draft or plan under review, and their names.
    """
    parts = []
    for name in sorted(context_files):
        if name.startswith("decision_"):
            continue
        parts.append(f"{name}:{sha256_file(context_files[name])}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def budget_reset_due(used: int, charged: list[str], digest: str) -> bool:
    """Has this assignment been charged for attempts at material it is no longer being asked about?

    True only when attempts were charged, at least one digest was recorded, and the incoming one
    matches none of them. A first attempt does not reset (nothing to release), and a repeat of any
    previously charged input does not reset -- which is what keeps this from being a way to buy
    unlimited attempts by asking the same question again.
    """
    return bool(used) and bool(charged) and digest not in charged


def assignment_provider(state: dict[str, Any], assignment: str, default: str) -> str:
    """Which CLI serves this assignment after a recorded exact or slot-wide change.

    Topology is frozen at init, and that is right: a run that quietly swaps providers mid-flight
    is a run whose independence claim means nothing. But a frozen topology with no escape turns
    one unusable provider into a dead run -- the alternative being to abandon and re-draft
    everything, which costs the work that DID succeed.

    A user may reassign one exact invocation. Separately, an auto-selected B provider that reports
    a rate limit moves the remaining B slot to the frozen host. Both changes are recorded and
    downgrade diversity rather than describing two instances of one provider as model-diverse.
    """
    exact = (state.get("assignment_providers") or {}).get(assignment)
    if exact:
        return exact
    slot = assignment.rsplit("-", 1)[-1]
    return (state.get("slot_provider_overrides") or {}).get(slot, default)


def automatic_planning_host_fallback(
    state: dict[str, Any], assignment: str, slot: str, provider: str,
    infrastructure: ProviderInfrastructureError,
) -> dict[str, Any] | None:
    """Persist an auto-selected B slot's rate-limit fallback to its frozen host."""
    host = state.get("host_adapter")
    if not (
        infrastructure.kind == "RATE_LIMIT"
        and slot == "B"
        and state.get("backend_requested") == "auto"
        and state.get("peer_reviewer_requested") == "auto"
        and assignment not in (state.get("assignment_providers") or {})
        and isinstance(host, str)
        and host in SUPPORTED_PROVIDERS
        and provider != host
    ):
        return None
    capability = adapter_capabilities(host)
    if not capability.get("ok"):
        return None
    runtime = (state.get("agent_runtime") or {}).get(host) or {}
    if host == "other":
        executable = provider_executable("other")
        if not (
            executable
            and runtime.get("executable") == str(executable)
            and runtime.get("executable_sha256") == sha256_file(executable)
        ):
            return None
    fallback = {
        "type": "AUTOMATIC_RATE_LIMIT_FALLBACK",
        "scope": "slot-B",
        "trigger_assignment": assignment,
        "from": provider,
        "to": host,
        "infrastructure_kind": infrastructure.kind,
        "at": utc_now(),
        "quality_attempt_consumed": False,
        "persistent_for_remaining_slot_assignments": True,
    }
    state.setdefault("slot_provider_overrides", {})["B"] = host
    state.setdefault("automatic_fallbacks", []).append(fallback)
    state.setdefault("independence_notes", []).append({
        "assignment": "slot-B", "from": provider, "to": host,
        "reason": "auto-selected external slot hit a rate limit; remaining B work uses the host",
        "decided_at": fallback["at"], "automatic": True,
    })
    state["provider_diversity"] = False
    state["model_diversity"] = False
    return fallback


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
    runtime: dict[str, Any] | None = None, allow_web: bool = False,
) -> list[str]:
    if provider == "other":
        runtime = runtime or {}
        executable = runtime.get("executable")
        if not isinstance(executable, str) or not executable:
            raise WorkflowError("other adapter has no frozen bridge executable")
        schema_path = context / "schema.json"
        prompt_path = context / "prompt.md"
        atomic_json(schema_path, schema)
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt_path.chmod(0o600)
        return [
            executable, "run", "--protocol", OTHER_BRIDGE_PROTOCOL,
            "--workspace", str(worktree), "--context", str(context),
            "--schema", str(schema_path), "--output", str(raw),
            "--prompt", str(prompt_path), "--web", "enabled" if allow_web else "disabled",
        ]
    if provider == "dsh":
        # dsh-headless has no structured-output flag, so the schema travels IN the prompt and the
        # host parses and validates the printed final message (approach (a)). The adapter is told
        # to emit one bare JSON object and nothing else.
        schema_text = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        dsh_prompt = (
            prompt
            + "\n\nOUTPUT CONTRACT. Reply with ONLY a single JSON object matching this schema "
            + "exactly: no prose, no markdown code fences, and nothing before or after the object.\n"
            + schema_text
        )
        return ["dsh", "--profile", "headless", dsh_prompt]
    if provider == "codex":
        schema_path = context / "schema.json"
        atomic_json(schema_path, schema)
        runtime = runtime or {}
        selection: list[str] = []
        if runtime.get("profile"):
            selection.extend(["-p", str(runtime["profile"])])
        if runtime.get("model"):
            selection.extend(["-m", str(runtime["model"])])
        if runtime.get("model_provider"):
            selection.extend(["-c", f'model_provider={json.dumps(runtime["model_provider"])}'])
        policy = list(CODEX_POLICY_OVERRIDES)
        if allow_web:
            index = policy.index("tools.web_search=false")
            policy[index] = "tools.web_search=true"
        return [
            "codex", "-a", "never", "exec", "--ephemeral", "-s", "read-only",
            *selection,
            *policy,
            "-C", str(worktree), "--add-dir", str(context), "--output-schema", str(schema_path),
            "-o", str(raw), prompt,
        ]
    # Bash is ALLOWED, and read-only is enforced where it can actually be enforced: the worktree
    # is mounted --ro-bind, so a write fails at the filesystem rather than at a tool's discretion.
    #
    # It used to be disallowed here while codex ran with `-s read-only`, which includes a shell. So
    # one planner could execute and the other could not, on the same request -- a request that asks
    # in three places for measurements to be RUN rather than reasoned about. The claude drafts duly
    # said "this session had no shell" and left those items unresolved, while codex executed
    # `gate_code_discovery.py` and `extract_urd_delivery_languages` and settled them. That asymmetry
    # was this file's doing, not a difference between the two CLIs, and it silently halved the
    # evidence one side of the cross-check could bring.
    allowed_tools = ["Read", "Grep", "Glob", "Bash"]
    if allow_web:
        allowed_tools.extend(["WebFetch", "WebSearch"])
    allowed = ",".join(allowed_tools)
    disallowed = ["Edit", "Write", "NotebookEdit"]
    if not allow_web:
        disallowed.extend(["WebFetch", "WebSearch"])
    return [
        "claude", "-p", *(["--model", str((runtime or {}).get("model"))]
                            if (runtime or {}).get("model") else []),
        "--output-format", "json", "--json-schema",
        json.dumps(schema, separators=(",", ":")), "--permission-mode", "dontAsk",
        "--allowedTools", allowed,
        "--disallowedTools", ",".join(disallowed),
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


def provider_trust_store(provider: str, codex_profile: str | None = None) -> list[Path]:
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
        if codex_profile:
            profile_config = codex_home / f"{codex_profile}.config.toml"
            candidates.append(profile_config)
            profile_catalog = codex_catalog_path(profile_config)
            if profile_catalog is not None:
                candidates.append(profile_catalog)
        catalog = codex_catalog_path(config)
        if catalog is not None:
            candidates.append(catalog)
    elif provider == "dsh":
        # The harness home holds the credentials file and the settings file (model selection). The
        # `profiles` directory is deliberately NOT mounted: dsh writes its composed profile
        # (cordis.yml) there during boot, so it must stay a fresh writable directory in the
        # private home.
        dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
        candidates = [dsh_home / ".credentials.yaml", dsh_home / "settings.yaml"]
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
    worktree: Path, context: Path, allow_web: bool = False,
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
    other_runtime = ((state.get("agent_runtime") or {}).get("other") or {})
    frozen_other = other_runtime.get("executable")
    provider = (
        "other"
        if isinstance(frozen_other, str) and Path(frozen_other).resolve() == executable
        else Path(command[0]).name
    )
    if provider == "other":
        expected_digest = other_runtime.get("executable_sha256")
        if not isinstance(expected_digest, str) or sha256_file(executable) != expected_digest:
            raise WorkflowError("other bridge executable changed after runtime selection")
    controller_inputs: list[Path] = []
    writable_outputs: list[Path] = []
    credential_mounts: list[tuple[Path, Path]] = []
    profile = ((state.get("agent_runtime") or {}).get("codex") or {}).get("profile")
    for source in provider_trust_store(provider, profile):
        destination = sandbox_destination(source, private_home)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.touch(mode=0o600, exist_ok=True)
        credential_mounts.append((source, destination))
    if provider == "claude":
        disallowed_index = command.index("--disallowedTools") + 1
        private_root = f"//{str(private_home).lstrip('/')}"
        private_denies = ",".join(
            f"{tool}({private_root}/**)" for tool in ("Read", "Grep", "Glob")
        )
        command[disallowed_index] = command[disallowed_index] + "," + private_denies
        sandbox_settings = {
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "excludedCommands": [],
                "allowUnsandboxedCommands": False,
                "filesystem": {"denyRead": [str(private_home)]},
            }
        }
        command[1:1] = ["--settings", json.dumps(sandbox_settings, separators=(",", ":"))]
    elif provider in {"codex", "other"}:
        raw_index = command.index("--output" if provider == "other" else "-o") + 1
        raw_output = Path(command[raw_index])
        descriptor = os.open(
            raw_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        writable_outputs.append(raw_output)
    elif provider == "dsh":
        boundary_source = SKILL_ROOT / "scripts" / "dsh_read_boundary.mjs"
        boundary_path = invocation_root / "dsh-read-boundary.mjs"
        shutil.copyfile(boundary_source, boundary_path)
        boundary_path.chmod(0o600)
        patch_path = invocation_root / "dsh-no-shell.patch.yml"
        descriptor = os.open(
            patch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                "- insert:\n"
                "    - id: grounded-build-read-boundary\n"
                f"      name: {json.dumps(boundary_path.as_uri())}\n"
                "      inject: [tools]\n"
                "      config:\n"
                f"        workspaceRoot: {json.dumps(str(worktree))}\n"
                f"        allowWeb: {'true' if allow_web else 'false'}\n"
                "        allowedRoots:\n"
                f"          - {json.dumps(str(worktree))}\n"
                f"          - {json.dumps(str(context))}\n"
                "- id: tool-bash\n  disabled: true\n"
                "- id: tool-pwsh\n  disabled: true\n"
                "- id: jobs\n  disabled: true\n"
                "- id: tool-jobs\n  disabled: true\n"
                "- id: tool-skill\n  disabled: true\n"
                "- id: tool-todo\n  disabled: true\n"
                "- id: tool-goal\n  disabled: true\n"
                "- id: code-runtime\n  disabled: true\n"
                "- id: subagent\n  disabled: true\n"
                "- id: subagent-spawn-in-process\n  disabled: true\n"
                "- id: subagent-fork-in-process\n  disabled: true\n"
                "- id: tool-subagent-control\n  disabled: true\n"
                "- id: tool-subagent-list-agents\n  disabled: true\n"
                "- id: tool-subagent\n  disabled: true\n"
                "- id: tool-subagent-fork\n  disabled: true\n"
                "- id: tool-subagent-report\n  disabled: true\n"
                "- id: workflow-worker-thread\n  disabled: true\n"
                "- id: tool-workflow\n  disabled: true\n"
                "- id: tool-ralph\n  disabled: true\n"
                "- id: tool-str-replace-editor\n  disabled: true\n"
                + ("" if allow_web else
                   "- id: web\n  disabled: true\n"
                   "- id: web-search-deepseek\n  disabled: true\n"
                   "- id: tool-web\n  disabled: true\n")
            )
        command[-1:-1] = ["--patch", str(patch_path)]
        controller_inputs.extend([patch_path, boundary_path])
    common_value = git(worktree, "rev-parse", "--git-common-dir")
    common_git = Path(common_value)
    if not common_git.is_absolute():
        common_git = (worktree / common_git).resolve()
    inner_command = ["/opt/grounded-build-agent", *command[1:]]
    runtime_mounts: list[tuple[Path, Path]] = []
    extra_setenv: list[str] = []
    dsh_node_mode = False
    if provider == "dsh":
        # Real dsh is a Node script that must resolve its imports against its package tree, so it
        # needs the node runtime and the @deepseek-ai/dsh package mounted at their ORIGINAL paths
        # (bwrap creates the ancestor directories) and is invoked through node. A self-contained
        # replacement (a test fake, for example) is executed directly like claude/codex. The two
        # shapes are told apart by shebang rather than by path layout.
        shebang = ""
        try:
            with executable.open("rb") as handle:
                first_line = handle.readline(64).decode("utf-8", "replace")
            if first_line.startswith("#!"):
                shebang = first_line[2:].strip()
        except OSError:
            pass
        dsh_node_mode = "node" in shebang
        if dsh_node_mode:
            node_executable = shutil.which("node")
            if not node_executable:
                raise WorkflowError("dsh requires the node runtime, which is not on PATH")
            node_binary = Path(node_executable).resolve()
            if not node_binary.is_file():
                raise WorkflowError(f"node runtime does not exist: {node_binary}")
            node_prefix = node_binary.parent.parent
            dsh_package = executable.parent.parent  # .../@deepseek-ai/dsh
            inner_command = [str(node_binary), str(executable), *command[1:]]
            # The harness home's `profiles` directory is deliberately NOT mounted: dsh writes its
            # COMPOSED profile (cordis.yml) there during boot, so it must be a fresh writable
            # directory in the private home. Credentials and settings are the read-only inputs and
            # arrive via the credential mounts above.
            runtime_mounts = [(node_prefix, node_prefix), (dsh_package, dsh_package)]
        # read-only tells the agent it cannot modify files; the bwrap ro-binds are the enforcement.
        # Approval stays "ask" under read-only and fails closed in headless (no answerer).
        extra_setenv = [
            "--setenv", "DSH_PERMISSION_MODE", "read-only",
            "--setenv", "DSH_TOOLS_MODE", "native",
        ]
    wrapper = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-pid",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", str(invocation_root),
        "--bind", str(private_home), str(private_home),
        "--bind", str(private_tmp), str(private_tmp),
        "--dir", str(worktree), "--ro-bind", str(worktree), str(worktree),
        "--ro-bind", str(context), str(context),
        "--dir", str(common_git), "--ro-bind", str(common_git), str(common_git),
    ]
    for path in controller_inputs:
        wrapper.extend(["--ro-bind", str(path), str(path)])
    for path in writable_outputs:
        wrapper.extend(["--bind", str(path), str(path)])
    if provider != "dsh" or not dsh_node_mode:
        wrapper.extend(["--dir", "/opt", "--ro-bind", str(executable), "/opt/grounded-build-agent"])
    for source, destination in runtime_mounts:
        wrapper.extend(["--ro-bind", str(source), str(destination)])
    # Standalone Codex keeps the matching code-mode host beside the versioned CLI executable but
    # spawns it at /opt/codex-code-mode-host. Resolve beside the executable so an old frozen CLI
    # never receives a host from a different `current` release, then mount that one binary read-only.
    if provider == "codex":
        code_mode_host = executable.with_name("codex-code-mode-host")
        if code_mode_host.is_file():
            wrapper.extend([
                "--ro-bind", str(code_mode_host.resolve()), "/opt/codex-code-mode-host",
            ])
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
        *extra_setenv,
        "--setenv", "PATH", "/usr/bin:/bin", "--chdir", str(worktree), "--", *inner_command,
    ])
    return wrapper


def agent_environment() -> dict[str, str]:
    allowed = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "NO_COLOR")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def extract_dsh_object(provider: str, source: str) -> dict[str, Any]:
    """Parse the free-text final message of a dsh-headless run into its schema object.

    dsh has no structured-output flag (approach (a): prompt + host-side parse), so the final
    message may arrive as a bare object, a fenced block, or prose surrounding an object. Accept a
    bare object and a single fenced object, then fall back to the first balanced ``{...}`` span.
    """
    text = source.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\n?(.*?)\n?```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise NoFinalAnswer(
        f"{provider} delivered something that is not the schema object ({len(source)} characters). "
        f"The verdict may have been reached; it was not emitted as one JSON object in the required shape."
    )


def extract_payload(provider: str, result: subprocess.CompletedProcess[str], raw: Path) -> dict[str, Any]:
    source = (
        raw.read_text(encoding="utf-8")
        if provider in {"codex", "other"} and raw.is_file()
        else result.stdout
    )
    if not source.strip():
        raise NoFinalAnswer(f"{provider} ended its turn without a final message"
                            f"{no_answer_detail(result)}")
    if provider == "dsh":
        return extract_dsh_object(provider, source)
    try:
        wrapper = json.loads(source)
    except json.JSONDecodeError as exc:
        raise NoFinalAnswer(describe_unparseable(provider, source, exc)) from exc
    if provider in {"codex", "other"}:
        payload = wrapper
    else:
        if wrapper.get("is_error"):
            detail = str(wrapper.get("result") or wrapper.get("error") or "provider API error")
            status = str(wrapper.get("api_error_status") or "").strip()
            joined = f"{status} {detail}".lower()
            if "401" in joined or "auth" in joined or "unauthorized" in joined:
                kind, retryable = "AUTHENTICATION", False
            elif status == "429" or is_rate_limit_failure(detail):
                kind, retryable = "RATE_LIMIT", True
            elif "529" in joined or "overload" in joined:
                kind, retryable = "PROVIDER_OVERLOAD", True
            else:
                kind, retryable = "PROVIDER_API", True
            raise ProviderInfrastructureError(provider, kind, detail[-800:], retryable=retryable)
        payload = wrapper.get("structured_output")
        if not isinstance(payload, dict) and isinstance(wrapper.get("result"), str):
            try:
                payload = json.loads(wrapper["result"])
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"claude result was not structured JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"{provider} did not return a structured object")
    return payload


def classify_failed_invocation(
    provider: str, result: subprocess.CompletedProcess[str], raw: Path,
) -> ProviderInfrastructureError:
    detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1200:]
    lowered = detail.lower()
    if "401" in lowered or "unauthorized" in lowered or "authentication" in lowered:
        return ProviderInfrastructureError(provider, "AUTHENTICATION", detail, retryable=False)
    if is_rate_limit_failure(lowered):
        return ProviderInfrastructureError(provider, "RATE_LIMIT", detail, retryable=True)
    if "529" in lowered or "overload" in lowered:
        return ProviderInfrastructureError(provider, "PROVIDER_OVERLOAD", detail, retryable=True)
    if "code-mode-host" in lowered and ("spawn" in lowered or "not found" in lowered):
        return ProviderInfrastructureError(provider, "TOOL_HOST_STARTUP", detail, retryable=False)
    return ProviderInfrastructureError(
        provider, "ADAPTER_EXIT", detail or f"exit code {result.returncode}", retryable=True)


def is_rate_limit_failure(detail: str) -> bool:
    """Recognize bounded provider rate-limit evidence, not incidental identifiers."""
    lowered = detail.lower()
    if any(marker in lowered for marker in (
        "rate limit", "rate_limit", "ratelimit", "too many requests",
        "rate exceeded", "insufficient_quota",
    )):
        return True
    if any(re.search(pattern, lowered) for pattern in (
        r"\bhttp(?:\s+status)?\s*[:=]?\s*429\b",
        r"\bstatus(?:\s+code)?\s*[:=]?\s*429\b",
        r"\b(?:error|response)\s+(?:code\s*)?[:=]?\s*429\b",
        r"\b429\s+(?:rate|quota|too\s+many\s+requests)\b",
    )):
        return True
    exhaustion = r"(?:exhausted|exceeded|depleted|reached|used\s+up|hit)"
    return bool(
        re.search(rf"\b(?:quota|usage\s+limit)\b.{{0,48}}\b{exhaustion}\b", lowered)
        or re.search(rf"\b{exhaustion}\b.{{0,48}}\b(?:quota|usage\s+limit)\b", lowered)
    )


def classify_successful_exit_infrastructure_failure(
    provider: str, result: subprocess.CompletedProcess[str], raw: Path,
) -> ProviderInfrastructureError | None:
    """Recognize machine-reported tool failure hidden behind an adapter exit code of zero.

    Codex may emit a schema-valid final answer after every attempted file read failed. Its startup
    warning alone is not proof of impact: only the router's ERROR record establishes that the
    unavailable host was actually called during this invocation.
    """
    if provider != "codex":
        return None
    for line in (result.stderr or "").splitlines():
        router_record = re.match(
            r"^(?:\d{4}-\d{2}-\d{2}T\S+\s+)?ERROR codex_core::tools::router:\s*(.*)$",
            line.strip(), flags=re.IGNORECASE,
        )
        if not router_record:
            continue
        lowered = router_record.group(1).lower()
        if (
            "code-mode-host" in lowered
            and ("failed to spawn" in lowered or "not found" in lowered or "no such file" in lowered)
        ):
            detail = line.strip()[-1200:]
            return ProviderInfrastructureError(
                provider, "TOOL_HOST_STARTUP", detail, retryable=False)
    return None


def tree_bytes(path: Path) -> int:
    total = 0
    if path.exists():
        for candidate in path.rglob("*"):
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def invocation_metrics(
    provider: str, result: subprocess.CompletedProcess[str], elapsed: float,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {"wall_seconds": round(elapsed, 3)}
    if provider == "claude":
        try:
            wrapper = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            wrapper = {}
        cost = wrapper.get("total_cost_usd")
        turns = wrapper.get("num_turns")
        if isinstance(cost, (int, float)):
            metrics["reported_cost_usd"] = float(cost)
        if isinstance(turns, int):
            metrics["reported_turns"] = turns
        model_usage = wrapper.get("modelUsage")
        if isinstance(model_usage, dict):
            metrics["observed_models"] = sorted(model_usage)
    return metrics


def update_resource_usage(state: dict[str, Any], metrics: dict[str, Any]) -> None:
    aggregate = state.setdefault("resource_usage", {})
    for key in ("wall_seconds", "reported_cost_usd", "reported_turns", "scratch_bytes_removed"):
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            aggregate[key] = aggregate.get(key, 0) + value


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
    validator: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    used = state["usage"].get(assignment, 0)
    granted = state.get("extra_invocation_grants", {}).get(assignment, 0)
    # The budget counts attempts AT AN INPUT, not attempts forever. Every charged attempt records
    # what it was asked about; when that changes -- because a defect in how the material was
    # prepared got fixed, which is the case this exists for -- the attempts that measured the old
    # material stop being evidence about the new one, and the count starts again. Repeating an
    # identical request cannot reach this: the digest would be unchanged.
    digest = context_digest(context_files)
    charged = state.setdefault("charged_context_digests", {}).setdefault(assignment, [])
    if not dry_run and budget_reset_due(used, charged, digest):
        state["usage"][assignment] = 0
        state.setdefault("extra_invocation_grants", {}).pop(assignment, None)
        (state.get("delivery_faults") or {}).pop(assignment, None)
        state.setdefault("budget_resets", []).append({
            "assignment": assignment, "attempts_released": used,
            "previous_digests": list(charged), "new_digest": digest, "at": utc_now(),
        })
        charged.clear()
        used = 0
        granted = 0
        persist_invocation_state(state, assignment, clear_fault=True)
        charged = state.setdefault("charged_context_digests", {}).setdefault(assignment, [])
    if not dry_run and used >= MAX_INVOCATIONS_PER_ASSIGNMENT + granted:
        # "Out of tries" and "this provider does not deliver a verdict for this assignment" both
        # end here, and only one of them is fixed by granting another try. Say which, so the
        # decision in front of the user is the one they actually face.
        fault = (state.get("delivery_faults") or {}).get(assignment)
        resume_status = state.get("status", "SYNTHESIS_REQUIRED")
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
        persist_invocation_state(state, assignment)
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
    runtime = (state.get("agent_runtime") or {}).get(provider) or {}
    allow_web = (
        assignment.startswith("investigate-")
        and state.get("research_policy") == "authoritative-web"
    )
    command = agent_command(
        provider, worktree, context, schema, raw, prompt.format(context=context), runtime, allow_web)
    command = isolated_agent_command(
        command, state, root, worktree, context, allow_web=allow_web
    )
    rendered_prompt = prompt.format(context=context)
    (root / "prompt.md").write_text(rendered_prompt, encoding="utf-8")
    invocation_path = root / "invocation.json"
    invocation_record: dict[str, Any] = {
        "assignment": assignment,
        "provider": provider,
        "slot": slot,
        "quality_attempt": used + 1,
        "context_digest": digest,
        "context_files": {name: sha256_file(path) for name, path in context_files.items()},
        "requested_runtime": runtime,
        "engine_epoch": state.get("engine_epoch", 0),
        "engine_contract": state.get("engine_contract") or engine_contract(),
        "argv": command[:-1] + ["<PROMPT>"],
        "started_at": utc_now(),
        "status": "PREVIEW" if dry_run else "RUNNING",
    }
    if dry_run:
        atomic_json(invocation_path, invocation_record)
        return {"dry_run": True, "command": invocation_record["argv"],
                "context": str(context), "invocation": str(invocation_path)}
    # Reserve before the paid process starts. A controller crash or BaseException after this
    # point leaves a RUNNING record and a consumed quality attempt; only a classified
    # infrastructure failure reimburses it.
    state["usage"][assignment] = used + 1
    digest_added = digest not in charged
    if digest_added:
        charged.append(digest)
    state.setdefault("invocation_reservations", {})[assignment] = {
        "assignment": assignment, "provider": provider, "slot": slot,
        "quality_attempt": used + 1, "invocation": str(invocation_path),
        "reserved_at": invocation_record["started_at"],
    }
    persist_invocation_state(state, assignment)
    charged = state.setdefault("charged_context_digests", {}).setdefault(assignment, [])
    # Publish RUNNING only after the durable budget reservation. A crash on either side cannot
    # create an uncharged paid call; a published-but-interrupted record has an explicit recovery.
    atomic_json(invocation_path, invocation_record)
    launch_binding = {
        "engine_epoch": invocation_record["engine_epoch"],
        "engine_contract": invocation_record["engine_contract"],
        "invocation": str(invocation_path),
    }
    started = time.monotonic()
    try:
        result = run(command, cwd=worktree, timeout=timeout, env=agent_environment())
    except WorkflowError as exc:
        elapsed = time.monotonic() - started
        scratch_bytes = tree_bytes(root / "home") + tree_bytes(root / "tmp")
        for scratch in (root / "home", root / "tmp"):
            if scratch.exists():
                shutil.rmtree(scratch)
        infrastructure = ProviderInfrastructureError(
            provider, "ADAPTER_EXECUTION", str(exc), retryable=True)
        count = state.setdefault("infrastructure_usage", {}).get(assignment, 0) + 1
        state["infrastructure_usage"][assignment] = count
        if used:
            state["usage"][assignment] = used
        else:
            state["usage"].pop(assignment, None)
            state.setdefault("_reimbursed_assignments", set()).add(assignment)
        if digest_added and digest in state["charged_context_digests"][assignment]:
            state["charged_context_digests"][assignment].remove(digest)
        resume_status = state.get("status", "SYNTHESIS_REQUIRED")
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "PROVIDER_INFRASTRUCTURE_FAILURE", "assignment": assignment,
            "provider": provider, "kind": infrastructure.kind, "detail": infrastructure.detail,
            "retryable": True, "attempt": count,
            "maximum_infrastructure_attempts": MAX_INFRASTRUCTURE_ATTEMPTS,
            "resume_status": resume_status, "created_at": utc_now(),
            "choices": (["RESOLVE_AND_CONTINUE", "REASSIGN_ASSIGNMENT", "ABANDON"]
                        if count < MAX_INFRASTRUCTURE_ATTEMPTS else
                        ["REASSIGN_ASSIGNMENT", "ABANDON"]),
        }
        invocation_record.update({
            "finished_at": utc_now(), "status": "INFRASTRUCTURE_FAILURE",
            "diagnostic": str(infrastructure),
            "metrics": {"wall_seconds": round(elapsed, 3),
                        "scratch_bytes_removed": scratch_bytes},
        })
        atomic_json(invocation_path, invocation_record)
        record_artifact(state, f"invocation-{assignment}-{root.name}-record", invocation_path)
        persist_invocation_state(state, assignment)
        raise infrastructure from exc
    elapsed = time.monotonic() - started
    stdout_path = root / "stdout.log"
    stderr_path = root / "stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    artifact_prefix = f"invocation-{assignment}-{root.name}"
    for suffix, path in (("prompt", root / "prompt.md"), ("stdout", stdout_path), ("stderr", stderr_path)):
        record_artifact(state, f"{artifact_prefix}-{suffix}", path)
    metrics = invocation_metrics(provider, result, elapsed)
    scratch_bytes = tree_bytes(root / "home") + tree_bytes(root / "tmp")
    for scratch in (root / "home", root / "tmp"):
        if scratch.exists():
            shutil.rmtree(scratch)
    metrics["scratch_bytes_removed"] = scratch_bytes
    invocation_record.update({
        "finished_at": utc_now(), "exit_code": result.returncode, "metrics": metrics,
    })
    infrastructure_error: ProviderInfrastructureError | None = None
    if result.returncode != 0:
        infrastructure_error = classify_failed_invocation(provider, result, raw)
    else:
        infrastructure_error = classify_successful_exit_infrastructure_failure(provider, result, raw)
    try:
        if infrastructure_error:
            raise infrastructure_error
        payload = extract_payload(provider, result, raw)
        validate_json_schema(payload, schema)
        if validator is not None:
            validator(payload)
    except NoFinalAnswer as exc:
        state.setdefault("delivery_faults", {})[assignment] = str(exc)
        invocation_record["status"] = "NO_FINAL_ANSWER"
        invocation_record["diagnostic"] = str(exc)
        update_resource_usage(state, metrics)
        atomic_json(invocation_path, invocation_record)
        record_artifact(state, f"{artifact_prefix}-record", invocation_path)
        persist_invocation_state(state, assignment)
        raise
    except ProviderInfrastructureError as exc:
        count = state.setdefault("infrastructure_usage", {}).get(assignment, 0) + 1
        state["infrastructure_usage"][assignment] = count
        if used:
            state["usage"][assignment] = used
        else:
            state["usage"].pop(assignment, None)
            state.setdefault("_reimbursed_assignments", set()).add(assignment)
        if digest_added and digest in state["charged_context_digests"][assignment]:
            state["charged_context_digests"][assignment].remove(digest)
        resume_status = state["status"]
        fallback = automatic_planning_host_fallback(
            state, assignment, slot, provider, exc,
        )
        if fallback:
            state["status"] = resume_status
            state["pending_decision"] = None
        else:
            state["status"] = "NEEDS_USER_DECISION"
            state["pending_decision"] = {
                "type": "PROVIDER_INFRASTRUCTURE_FAILURE", "assignment": assignment,
                "provider": provider, "kind": exc.kind, "detail": exc.detail,
                "retryable": exc.retryable, "attempt": count,
                "maximum_infrastructure_attempts": MAX_INFRASTRUCTURE_ATTEMPTS,
                "resume_status": resume_status, "created_at": utc_now(),
                "choices": (["RESOLVE_AND_CONTINUE", "REASSIGN_ASSIGNMENT", "ABANDON"]
                            if count < MAX_INFRASTRUCTURE_ATTEMPTS else
                            ["REASSIGN_ASSIGNMENT", "ABANDON"]),
            }
        invocation_record["status"] = "INFRASTRUCTURE_FAILURE"
        invocation_record["diagnostic"] = str(exc)
        if fallback:
            invocation_record["automatic_fallback"] = fallback
        update_resource_usage(state, metrics)
        atomic_json(invocation_path, invocation_record)
        record_artifact(state, f"{artifact_prefix}-record", invocation_path)
        persist_invocation_state(state, assignment)
        raise
    except WorkflowError as exc:
        diagnostic = f"deterministic contract rejection: {exc}"
        state.setdefault("delivery_faults", {})[assignment] = diagnostic
        invocation_record["status"] = "VALIDATION_REJECTED"
        invocation_record["diagnostic"] = diagnostic
        update_resource_usage(state, metrics)
        atomic_json(invocation_path, invocation_record)
        record_artifact(state, f"{artifact_prefix}-record", invocation_path)
        persist_invocation_state(state, assignment)
        raise
    state.get("delivery_faults", {}).pop(assignment, None)
    validate_planning_worktree(state, worktree)
    result_path = root / "result.json"
    atomic_json(result_path, payload)
    if raw.is_file():
        record_artifact(state, f"{artifact_prefix}-raw", raw)
    record_artifact(state, f"{artifact_prefix}-result", result_path)
    invocation_record["status"] = "DELIVERED"
    atomic_json(invocation_path, invocation_record)
    record_artifact(state, f"{artifact_prefix}-record", invocation_path)
    update_resource_usage(state, metrics)
    persist_invocation_state(state, assignment, clear_fault=True)
    state["_completed_invocation_binding"] = launch_binding
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
    if payload["scope_digest"] != state["scope_digest"]:
        raise WorkflowError("draft scope digest mismatch")
    if not isinstance(payload["plan_markdown"], str) or not payload["plan_markdown"].strip():
        raise WorkflowError("draft plan is empty")
    investigation = json.loads(Path(state["investigations"][slot]["path"]).read_text(encoding="utf-8"))
    new_evidence = payload.get("new_evidence")
    if not isinstance(new_evidence, list):
        raise WorkflowError("draft new_evidence must be an array")
    new_ids = [item.get("id") for item in new_evidence if isinstance(item, dict)]
    if len(new_ids) != len(set(new_ids)):
        raise WorkflowError("draft new evidence ids must be unique")
    if any(not valid_scope_id(item.get("scope_id")) for item in new_evidence):
        raise WorkflowError("draft new evidence has an invalid scope id")
    known_evidence = {item["id"] for item in investigation["evidence"]} | set(new_ids)
    if not set(payload["evidence_ids"]).issubset(known_evidence):
        raise WorkflowError("draft cites evidence outside its independent investigation")
    facts = payload["repository_facts"]
    if not isinstance(facts, list) or not facts:
        raise WorkflowError("draft must include repository facts")
    ids = [item.get("id") for item in facts if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise WorkflowError("draft fact ids must be unique")


def validate_investigation(payload: dict[str, Any], state: dict[str, Any], slot: str) -> None:
    if set(payload) != set(INVESTIGATION_SCHEMA["required"]):
        raise WorkflowError("investigation output fields do not match the contract")
    provider = assignment_provider(state, f"investigate-{slot}", state["planners"][slot])
    if payload["provider"] != provider or payload["slot"] != slot:
        raise WorkflowError("investigation identity mismatch")
    if payload["baseline_sha"] != state["baseline_sha"] or payload["scope_digest"] != state["scope_digest"]:
        raise WorkflowError("investigation baseline or scope mismatch")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise WorkflowError("investigation must contain evidence")
    evidence_ids = [item.get("id") for item in evidence if isinstance(item, dict)]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise WorkflowError("investigation evidence ids must be unique")
    if any(not valid_scope_id(item.get("scope_id")) for item in evidence):
        raise WorkflowError("investigation evidence has an invalid scope id")
    finding_ids = [item.get("id") for item in payload.get("findings", []) if isinstance(item, dict)]
    if len(finding_ids) != len(set(finding_ids)):
        raise WorkflowError("investigation finding ids must be unique")
    known = set(evidence_ids)
    for finding in payload.get("findings", []):
        if not valid_scope_id(finding.get("scope_id")):
            raise WorkflowError(f"finding {finding.get('id')} has an invalid scope id")
        if not set(finding.get("evidence_ids", [])).issubset(known):
            raise WorkflowError(f"finding {finding.get('id')} cites unknown evidence")
        if finding.get("lane") == "STOP_THE_LINE" and finding.get("evidence_status") not in {
            "VERIFIED", "STRONGLY_INFERRED"
        }:
            raise WorkflowError("STOP_THE_LINE requires verified or strongly inferred evidence")


def validate_integration(
    payload: dict[str, Any], state: dict[str, Any], slot: str, round_number: int,
    finding_keys: list[str] | None = None,
) -> None:
    if set(payload) != set(INTEGRATION_SCHEMA["required"]):
        raise WorkflowError("integrated draft fields do not match the contract")
    assignment = f"cross-{slot}" if round_number == 2 else f"diverge-{slot}"
    provider = assignment_provider(state, assignment, state["planners"][slot])
    if (payload["provider"] != provider or payload["slot"] != slot
            or payload["reviewer_slot"] != slot or payload["round"] != round_number):
        raise WorkflowError("integrated draft identity mismatch")
    if payload["baseline_sha"] != state["baseline_sha"] or payload["scope_digest"] != state["scope_digest"]:
        raise WorkflowError("integrated draft baseline or scope mismatch")
    if not payload["plan_markdown"].strip():
        raise WorkflowError("integrated draft plan is empty")
    if payload["verdict"] != "PASS" and not payload["findings"]:
        raise WorkflowError("a non-PASS integrated review requires findings")
    dispositions = payload["accepted_finding_ids"] + payload["rejected_finding_ids"]
    if len(dispositions) != len(set(dispositions)):
        raise WorkflowError("a finding cannot be both accepted and rejected")
    allowed_keys = set(state.get("finding_ledger", {})) if finding_keys is None else set(finding_keys)
    unknown = set(dispositions) - allowed_keys
    if unknown:
        raise WorkflowError(f"integrated draft dispositions cite unknown finding keys: {sorted(unknown)}")
    for finding in payload["new_findings"]:
        if not valid_scope_id(finding.get("scope_id")):
            raise WorkflowError(f"new finding {finding.get('id')} has an invalid scope id")
    ledger_keys = allowed_keys
    aliases_seen: set[str] = set()
    for alias in payload["finding_aliases"]:
        canonical = alias["canonical_finding_id"]
        members = alias["alias_finding_ids"]
        if canonical not in ledger_keys or not members:
            raise WorkflowError("finding alias must name a known canonical key and at least one alias")
        unknown_aliases = set(members) - ledger_keys
        if unknown_aliases:
            raise WorkflowError(f"finding aliases cite unknown keys: {sorted(unknown_aliases)}")
        if canonical in members or aliases_seen.intersection(members):
            raise WorkflowError("finding aliases must be acyclic and non-overlapping")
        aliases_seen.update(members)


def record_finding_aliases(state: dict[str, Any], aliases: list[dict[str, Any]], source: str) -> None:
    registry = state.setdefault("finding_aliases", {})
    for proposal in aliases:
        canonical = proposal["canonical_finding_id"]
        for alias in proposal["alias_finding_ids"]:
            registry[alias] = {
                "canonical": canonical, "source": source, "rationale": proposal["rationale"],
                "evidence_ids": proposal["evidence_ids"], "at": utc_now(),
            }


def integration_schema(
    state: dict[str, Any], finding_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Constrain dispositions and alias proposals to the current stable ledger keys."""
    schema = copy.deepcopy(INTEGRATION_SCHEMA)
    keys = sorted(state.get("finding_ledger", {})) if finding_keys is None else sorted(finding_keys)
    for field in ("accepted_finding_ids", "rejected_finding_ids"):
        schema["properties"][field]["items"] = {"type": "string", "enum": keys}
    alias_items = schema["properties"]["finding_aliases"]["items"]["properties"]
    alias_items["canonical_finding_id"] = {"type": "string", "enum": keys}
    alias_items["alias_finding_ids"]["items"] = {"type": "string", "enum": keys}
    return schema


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
    if set(state.get("investigations", {})) != {"A", "B"}:
        raise WorkflowError("READY invariant failed: both investigations are required")
    if set(state.get("draft_rounds", {}).get("2", {})) != {"A", "B"}:
        raise WorkflowError("READY invariant failed: both draft_02 integrations are required")
    if state.get("planning_depth") == "deep":
        if set(state.get("draft_rounds", {}).get("3", {})) != {"A", "B"}:
            raise WorkflowError("READY invariant failed: both draft_03 plans are required")
        if set(state.get("convergence_reviews", {})) != {"A", "B"}:
            raise WorkflowError("READY invariant failed: deep mode convergence round one is incomplete")
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
    for slot, default_provider in required.items():
        assignment = f"final-{candidate.get('round')}-{slot}"
        provider = assignment_provider(state, assignment, default_provider)
        record = state["final_reviews"][slot]
        if record.get("provider") != provider:
            raise WorkflowError("READY invariant failed: final reviewer provider mismatch")
        payload = json.loads(Path(record["path"]).read_text(encoding="utf-8"))
        validate_review(payload, state, provider, slot, target)
        if payload["verdict"] != "PASS":
            raise WorkflowError(f"READY invariant failed: final reviewer {slot} did not PASS")


def model_diversity(topology: dict[str, str], runtime: dict[str, dict[str, Any]]) -> bool:
    identities = {
        (
            runtime.get(adapter, {}).get("model_provider"),
            runtime.get(adapter, {}).get("model_family"),
            runtime.get(adapter, {}).get("model"),
        )
        for adapter in topology.values()
    }
    return len(identities) > 1


def runtime_warnings(runtime: dict[str, dict[str, Any]]) -> list[str]:
    warnings = [
        f"{adapter} model family is unknown; diversity uses the exact provider/model identity"
        for adapter, identity in runtime.items() if identity.get("model_family") == "unknown"
    ]
    for adapter, identity in runtime.items():
        if identity.get("adapter") == "dsh" and identity.get("identity_source") == "harness_default":
            warnings.append(
                f"{adapter} model is not pinned; the harness default "
                f"{identity.get('model')!r} (the fast tier) will be used")
    return warnings


def next_action(state: dict[str, Any]) -> dict[str, Any]:
    """Return one deterministic host action for the current planning state."""
    def incomplete_slot(stage: str, candidates: Any, completed: dict[str, Any]) -> str:
        remaining = [slot for slot in candidates if slot not in completed]
        if not remaining:
            raise WorkflowError(
                f"workflow state is inconsistent: {state['status']} has no incomplete {stage} slot")
        return remaining[0]

    base = [sys.executable, str(Path(__file__).resolve())]
    common = ["--project", state["project"], "--run-id", state["run_id"]]
    status = state["status"]

    def parallel_actions(stage: str, flag: str, completed: dict[str, Any]) -> dict[str, Any]:
        slots = [slot for slot in ("A", "B") if slot not in completed]
        if not slots:
            raise WorkflowError(
                f"workflow state is inconsistent: {status} has no incomplete {stage} slot")
        commands = [[*base, stage, *common, flag, slot] for slot in slots]
        return {
            "kind": "RUN_AGENT_BATCH", "stage": stage, "parallel": True, "slots": slots,
            "commands": commands, "preview_commands": [[*command, "--dry-run"] for command in commands],
            "barrier": "wait for every listed slot before advancing to the next stage",
        }

    if status in {"INITIALIZED", "INVESTIGATING"}:
        return parallel_actions("investigate", "--slot", state["investigations"])
    if status in {"EVIDENCE_READY", "DRAFTING"}:
        return parallel_actions("draft", "--slot", state["drafts"])
    if status in {"DRAFTS_READY", "CROSS_REVIEWING"}:
        return parallel_actions("cross-review", "--slot", state["cross_reviews"])
    if status in {"DIVERGENCE_REQUIRED", "DIVERGING"}:
        return parallel_actions("diverge", "--slot", state["draft_rounds"]["3"])
    if status == "SYNTHESIS_REQUIRED":
        return {"kind": "HOST_SYNTHESIS", "stage": "synthesis",
                "command": [*base, "synthesis-context", *common],
                "requires": ["implementation_plan.md", "batches.md"]}
    if status in {"CONVERGENCE_REVIEW_REQUIRED", "CONVERGENCE_REVIEWING"}:
        return parallel_actions("convergence-review", "--reviewer", state["convergence_reviews"])
    if status in {"FINAL_REVIEW_REQUIRED", "FINAL_REVIEWING"}:
        required = required_final_reviewers(state)
        if set(required) == {"A", "B"}:
            return parallel_actions("final-review", "--reviewer", state["final_reviews"])
        slot = incomplete_slot("final-review", required, state["final_reviews"])
        command = [*base, "final-review", *common, "--reviewer", slot]
        return {"kind": "RUN_AGENT", "stage": "final-review", "slot": slot,
                "command": command, "preview_command": [*command, "--dry-run"]}
    if status == "NEEDS_USER_DECISION":
        return {"kind": "ASK_USER", "stage": "adjudication",
                "pending_decision": state.get("pending_decision")}
    if status == "READY":
        return {"kind": "STOP_FOR_IMPLEMENTATION_APPROVAL", "stage": "ready",
                "command": [*base, "export", *common]}
    if status == "ABANDONED":
        return {"kind": "TERMINAL", "stage": "abandoned"}
    return {"kind": "INSPECT_STATUS", "stage": status.lower()}


def synthesis_diagnostics(
    plan_text: str, batches_text: str,
    ledger: dict[str, Any] | set[str] | None = None,
) -> dict[str, list[str]]:
    """Check the host-authored handoff before paid final review."""
    errors: list[str] = []
    warnings: list[str] = []
    if not plan_text.strip():
        errors.append("implementation plan is empty")
    matches = list(re.finditer(r"(?m)^\s*(?:[-*]\s*|#+\s*)?(B\d{2,})\s*:", batches_text))
    ids = [match.group(1) for match in matches]
    if not ids:
        errors.append("batch manifest must declare at least one Bxx batch identifier")
    if len(ids) != len(set(ids)):
        errors.append("batch manifest repeats a batch identifier")
    declared = set(ids)
    plan_ids = set(re.findall(r"\bB\d{2,}\b", plan_text))
    if plan_ids - declared:
        errors.append(f"plan cites batches absent from manifest: {sorted(plan_ids - declared)}")
    if declared - plan_ids:
        warnings.append(f"manifest batches not referenced by plan: {sorted(declared - plan_ids)}")
    dependencies: dict[str, set[str]] = {identifier: set() for identifier in declared}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(batches_text)
        block = batches_text[match.start():end]
        if not re.search(r"(?im)^\s*(?:[-*]\s*)?(?:exit observation|exit|stopping condition)\s*:", block):
            warnings.append(f"{match.group(1)} has no explicitly labelled finite exit observation")
        if not re.search(r"(?im)^\s*(?:[-*]\s*)?(?:verification|verify|commands?)\s*:", block):
            warnings.append(f"{match.group(1)} has no explicitly labelled verification command")
        dependency_match = re.search(
            r"(?im)^\s*(?:[-*]\s*)?(?:depends on|dependencies?)\s*:\s*([^\n]+)", block)
        if dependency_match:
            dependencies[match.group(1)] = set(re.findall(r"\bB\d{2,}\b", dependency_match.group(1)))
        unknown = dependencies[match.group(1)] - declared
        if unknown:
            errors.append(f"{match.group(1)} depends on unknown batches: {sorted(unknown)}")
        if re.search(r"(?i)\b(retry|poll|wait|park|provider)\b", block) and not re.search(
                r"(?i)\b(?:max(?:imum)?|limit|at most|up to|timeout)\b[^\n]{0,60}\d+", block):
            warnings.append(f"{match.group(1)} mentions repeatable work without an explicit numeric bound")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cyclic = any(visit(item) for item in dependencies.get(node, set()) if item in declared)
        visiting.remove(node)
        visited.add(node)
        return cyclic

    if any(visit(identifier) for identifier in declared):
        errors.append("batch dependency graph contains a cycle")
    if ledger is not None:
        ledger_keys = set(ledger)
        mentioned = set(re.findall(r"\bF-[0-9a-f]{16}\b", plan_text + "\n" + batches_text))
        missing = ledger_keys - mentioned
        blocking = {
            key for key in missing
            if isinstance(ledger, dict)
            and (ledger.get(key, {}).get("canonical") or {}).get("severity") in {"P0", "P1"}
        }
        if blocking:
            errors.append(f"candidate omits blocking stable finding keys: {sorted(blocking)}")
        advisory = missing - blocking
        if advisory:
            warnings.append(f"candidate does not explicitly disposition stable finding keys: {sorted(advisory)}")
    if not re.search(r"(?i)\bexcluded\b", plan_text + "\n" + batches_text):
        warnings.append("total excluded scope is not explicit")
    return {"errors": errors, "warnings": warnings}


def command_preflight(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    runtime = agent_runtime(args)
    selection_checks: dict[str, dict[str, Any]] = {}
    if args.host_adapter and args.peer_reviewer == "auto":
        selection_args = argparse.Namespace(**vars(args), final_reviewer="auto")
        topology, frozen_peer, preferred_final, selection_checks = (
            resolve_verified_host_selection(
                selection_args, project, runtime, perform_probe=args.probe,
            )
        )
    else:
        topology = resolve_host_topology(args.backend, args.host_adapter, args.peer_reviewer)
        frozen_peer = topology["B"] if args.host_adapter and args.backend == "auto" else None
        preferred_final = (
            resolve_final_reviewer("auto", args.host_adapter, frozen_peer)
            if args.host_adapter else None
        )
    selected_adapters = set(topology.values()) | ({preferred_final} if preferred_final else set())
    baseline = git(project, "rev-parse", "--verify", f"{args.base_ref}^{{commit}}")
    payload = {
        "status": "PREFLIGHT_OK",
        "project": str(project),
        "base_ref": args.base_ref,
        "baseline_sha": baseline,
        "clean": not bool(git(project, "status", "--porcelain")),
        "planners": topology,
        "host_adapter": args.host_adapter,
        "peer_reviewer": frozen_peer,
        "preferred_final_reviewer": preferred_final,
        "selection_checks": selection_checks,
        "provider_diversity": len(set(topology.values())) > 1,
        "model_diversity": model_diversity(topology, runtime),
        "agent_runtime": {name: runtime[name] for name in sorted(selected_adapters)},
        "runtime_warnings": runtime_warnings({name: runtime[name] for name in selected_adapters}),
        "capabilities": {
            "bubblewrap": shutil.which("bwrap") is not None,
            "git": executable_version("git"),
            "python": sys.version.split()[0],
            "adapters": {
                name: adapter_capabilities(name) for name in sorted(selected_adapters)
            },
        },
    }
    static_ok = payload["capabilities"]["bubblewrap"] and all(
        item["ok"] for item in payload["capabilities"]["adapters"].values()
    )
    if not static_ok:
        payload["status"] = "PREFLIGHT_STATIC_REQUIREMENT_FAILED"
        emit(payload, code=2)
    if args.probe:
        probes = selection_checks or {
            name: probe_provider(name, project, runtime=runtime[name])
            for name in sorted(selected_adapters)
        }
        payload["probes"] = probes
        if not selection_checks and not all(item["ok"] for item in probes.values()):
            payload["status"] = "PREFLIGHT_PROVIDER_UNUSABLE"
            emit(payload, code=2)
    emit(payload)


def command_init(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    request = Path(args.request).expanduser().resolve()
    if not request.is_file():
        raise WorkflowError(f"request file does not exist: {request}")
    runtime = agent_runtime(args)
    topology, frozen_peer, final_reviewer, selection_checks = resolve_verified_host_selection(
        args, project, runtime,
    )
    baseline = git(project, "rev-parse", "--verify", f"{args.base_ref}^{{commit}}")
    original_changes = git(project, "status", "--porcelain").splitlines()
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    root = run_directory(project, run_id)
    if root.exists():
        raise WorkflowError(f"run directory already exists: {root}")
    for relative in ("input", "investigations", "drafts", "cross_reviews", "synthesis", "convergence_reviews", "final_reviews", "decisions", "invocations", "worktrees"):
        (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    request_snapshot = root / "input" / "request.md"
    shutil.copyfile(request, request_snapshot)
    scope_contract = root / "input" / "scope_contract.json"
    atomic_json(scope_contract, {
        "objective_id": "TARGET-001",
        "objective_source": "request.md",
        "objective_sha256": sha256_file(request_snapshot),
        "in_scope_rule": "Only work required to satisfy the frozen request and recorded user decisions.",
        "scope_id_format": "^(TARGET|REQUIRED_SUPPORT|EVIDENCE_ONLY|PROPOSED_EXTENSION|OUT_OF_SCOPE)-[0-9]{3,}$",
        "support_classes": ["TARGET", "REQUIRED_SUPPORT", "EVIDENCE_ONLY"],
        "extension_classes": ["PROPOSED_EXTENSION", "OUT_OF_SCOPE"],
        "extension_rule": "PROPOSED_EXTENSION requires a typed user decision before entering the plan.",
    })
    scope_digest = sha256_file(scope_contract)
    finding_ledger_path = root / "input" / "finding_ledger.json"
    atomic_json(finding_ledger_path, {"findings": {}})
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
        "engine_contract": engine_contract(),
        "engine_epoch": 0,
        "run_id": run_id,
        "run_directory": str(root),
        "project": str(project),
        "baseline_sha": baseline,
        "base_ref": args.base_ref,
        "original_worktree_clean_at_start": not bool(original_changes),
        "original_worktree_changes_at_start": original_changes,
        "request_snapshot": str(request_snapshot),
        "scope_contract": str(scope_contract),
        "scope_digest": scope_digest,
        "finding_ledger_path": str(finding_ledger_path),
        "finding_ledger": {},
        "finding_aliases": {},
        "planning_depth": args.planning_depth,
        "research_policy": args.research_policy,
        "backend_requested": args.backend,
        "host_adapter": args.host_adapter,
        "peer_reviewer_requested": args.peer_reviewer,
        "final_reviewer_requested": args.final_reviewer,
        "peer_reviewer": frozen_peer,
        "selection_checks": selection_checks,
        "planners": topology,
        "provider_diversity": len(set(topology.values())) > 1,
        "model_diversity": model_diversity(topology, runtime),
        "agent_runtime": runtime,
        "runtime_warnings": runtime_warnings(runtime),
        "final_reviewer": final_reviewer,
        "worktree": str(worktree),
        "status": "INITIALIZED",
        "investigations": {},
        "drafts": {},
        "draft_rounds": {"1": {}, "2": {}, "3": {}},
        "round_barriers": {},
        "cross_reviews": {},
        "convergence_reviews": {},
        "convergence_round_one_complete": False,
        "synthesis_submissions": 0,
        "extra_synthesis_grants": 0,
        "extra_invocation_grants": {},
        "candidate": None,
        "final_reviews": {},
        "pending_decision": None,
        "pending_decisions": {},
        "usage": {},
        "infrastructure_usage": {},
        "slot_provider_overrides": {},
        "automatic_fallbacks": [],
        "resource_usage": {"wall_seconds": 0.0, "reported_cost_usd": 0.0,
                           "reported_turns": 0, "scratch_bytes_removed": 0},
        "active_invocations": {},
        "artifacts": {},
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "revision": 0,
    }
    record_artifact(state, "request", request_snapshot)
    record_artifact(state, "scope-contract", scope_contract)
    record_artifact(state, "finding-ledger", finding_ledger_path)
    save_state(state)
    emit({
        "status": state["status"], "run_id": run_id, "run_directory": str(root),
        "base_ref": state["base_ref"], "baseline_sha": state["baseline_sha"],
        "original_worktree_clean_at_start": state["original_worktree_clean_at_start"],
        "original_worktree_changes_at_start": state["original_worktree_changes_at_start"],
        "host_adapter": args.host_adapter,
        "peer_reviewer": frozen_peer,
        "selection_checks": selection_checks,
        "planners": topology, "final_reviewer": final_reviewer,
        "provider_diversity": state["provider_diversity"],
        "model_diversity": state["model_diversity"], "agent_runtime": runtime,
        "runtime_warnings": state["runtime_warnings"],
        "planning_depth": state["planning_depth"], "research_policy": state["research_policy"],
        "next_action": next_action(state),
    })


def command_investigate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    slot = args.slot
    if state["status"] not in {"INITIALIZED", "INVESTIGATING"}:
        raise WorkflowError(f"investigate is not allowed from {state['status']}")
    if slot in state["investigations"] and not args.dry_run:
        raise WorkflowError(f"slot {slot} already completed investigation")
    provider = assignment_provider(state, f"investigate-{slot}", state["planners"][slot])
    web_rule = (
        "When repository evidence is insufficient, you MAY use only the upstream project's official "
        "documentation or official GitHub repository. Record the exact URL, retrieval time, version/tag/commit "
        "and a content digest. Search snippets and third-party summaries are discovery leads, never evidence."
        if state["research_policy"] == "authoritative-web" else
        "Do not use external network sources in this run; mark claims UNRESOLVED when the repository cannot establish them."
    )
    prompt = (
        "You are independent evidence investigator {slot}. Read {context}/request.md and "
        "{context}/scope_contract.json, then inspect the complete relevant repository surface at baseline {sha}. "
        "Do not draft or edit the solution yet. Read {context}/causal_analysis.md and establish what is true. "
        "Use its proportional production trace and producer-aware evidence model; do not infer system shape from keywords. "
        "Every claim and finding must map to a scope id "
        "matching exactly TARGET-001, REQUIRED_SUPPORT-NNN, EVIDENCE_ONLY-NNN, PROPOSED_EXTENSION-NNN, or OUT_OF_SCOPE-NNN "
        "(NNN is at least three digits; a bare class name is invalid). "
        "anything outside the frozen objective is OUT_OF_SCOPE or a PROPOSED_EXTENSION and must not enter the plan. "
        "Trace symptoms to root cause where evidence permits; label hypotheses honestly. Rank first by scope gate, then "
        "user priority, severity, urgency, dependency/blocking effect, causal leverage, evidence strength, and effort. "
        "Keep verification priority separate from solution priority. {web_rule} Return only schema JSON with "
        "provider={provider}, slot={slot}, baseline_sha={sha}, scope_digest={scope_digest}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, sha=state["baseline_sha"],
             scope_digest=state["scope_digest"], web_rule=web_rule, context="{context}")
    payload = invoke(
        state, f"investigate-{slot}", provider, slot,
        {"request.md": Path(state["request_snapshot"]),
         "scope_contract.json": Path(state["scope_contract"]),
         "causal_analysis.md": CAUSAL_ANALYSIS},
        INVESTIGATION_SCHEMA, prompt, args.timeout, args.dry_run,
        validator=lambda value: validate_investigation(value, state, slot),
    )
    if args.dry_run:
        emit({"status": "INVESTIGATION_DRY_RUN", **payload})
    validate_investigation(payload, state, slot)
    path = Path(state["run_directory"]) / "investigations" / f"{slot}.json"
    atomic_json(path, payload)
    record_artifact(state, f"investigation-{slot}", path)
    record_findings(state, payload["findings"], f"investigation-{slot}")
    state["investigations"][slot] = {"provider": provider, "path": str(path)}
    state["status"] = "EVIDENCE_READY" if set(state["investigations"]) == {"A", "B"} else "INVESTIGATING"
    save_parallel_stage(state, "investigate")
    emit({"status": state["status"], "run_id": state["run_id"], "slot": slot,
          "investigation": str(path)})


def command_draft(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    slot = args.slot
    if state["status"] not in {"EVIDENCE_READY", "DRAFTING"}:
        raise WorkflowError(f"draft is not allowed from {state['status']}")
    if slot in state["drafts"] and not args.dry_run:
        raise WorkflowError(f"slot {slot} already produced a draft")
    provider = assignment_provider(state, f"draft-{slot}", state["planners"][slot])
    prompt = (
        "You are independent planning instance {slot}. Read {context}/request.md, {context}/scope_contract.json, "
        "{context}/causal_analysis.md, and only your own independently produced {context}/investigation.json. "
        "You cannot see the other planner's work. "
        "Re-check repository facts before planning. If that check discovers evidence absent from investigation.json, "
        "record it in new_evidence with a valid suffixed scope id and cite it from evidence_ids; never cite an unrecorded "
        "observation. Use evidence ids and preserve uncertainty. Produce a detailed "
        "implementation plan ordered by the frozen priority rules, with root-cause-driven solutions, finite batch boundaries, "
        "dependencies, budget-sensitive retry limits, and decidable acceptance observations. Do not edit "
        "the repository. Return only JSON matching the supplied schema. Set provider={provider}, slot={slot}, "
        "baseline_sha={sha}, and scope_digest={scope_digest}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, sha=state["baseline_sha"],
             scope_digest=state["scope_digest"], context="{context}")
    payload = invoke(
        state, f"draft-{slot}", provider, slot,
        {"request.md": Path(state["request_snapshot"]),
         "scope_contract.json": Path(state["scope_contract"]),
         "causal_analysis.md": CAUSAL_ANALYSIS,
         "investigation.json": Path(state["investigations"][slot]["path"])},
        DRAFT_SCHEMA, prompt, args.timeout, args.dry_run,
        validator=lambda value: validate_draft(value, state, slot),
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
    state["draft_rounds"]["1"][slot] = state["drafts"][slot]
    state["status"] = "DRAFTS_READY" if set(state["drafts"]) == {"A", "B"} else "DRAFTING"
    save_parallel_stage(state, "draft")
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
    barrier = state.get("round_barriers", {}).get("cross-review") or {}
    barrier_ledger = Path(barrier.get("finding_ledger", state["finding_ledger_path"]))
    barrier_keys = barrier.get("finding_keys")
    provider = assignment_provider(state, f"cross-{slot}", state["planners"][slot])
    target = f"draft-{target_slot}"
    prompt = (
        "You are independent reviewer and integrator slot {slot}, producing draft_02. Read {context}/request.md, "
        "{context}/scope_contract.json, {context}/causal_analysis.md, {context}/finding_ledger.json, your own investigation and draft, and the other planner's "
        "plan at {context}/target_plan.md, and its claims at {context}/target_claims.json (the same "
        "draft, split so the plan is readable prose rather than one escaped JSON string). Read the "
        "plan IN FULL, in parts if your reader truncates it, before judging it. Then inspect the "
        "repository at baseline {sha}. Re-verify material claims; do not assume agreement "
        "means truth: verify claims against repository evidence. Find factual errors, missing dependencies, "
        "unbounded budgets, non-decidable acceptance conditions, unsafe scope, and incompatibilities. P0/P1 "
        "mean the draft cannot safely guide implementation; P2/P3 are advisory. Integrate every supported useful "
        "part into a complete plan, explicitly disposition known stable F-* ledger keys, and return any genuinely new "
        "finding in full solution form so the workflow can fingerprint it. When two stable F-* keys describe the same "
        "underlying defect, propose an evidence-backed finding_aliases entry; do not merge them merely because wording "
        "looks similar. Reject scope extensions. This round "
        "is divergent: add missing in-scope causal, failure-path, alternative, and verification coverage, never a new "
        "objective. Do not edit files. Return only schema JSON with provider={provider}, slot={slot}, "
        "reviewer_slot={slot}, target={target}, round=2, baseline_sha={sha}, scope_digest={scope_digest}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, target=target, sha=state["baseline_sha"],
             scope_digest=state["scope_digest"], context="{context}")
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "scope_contract.json": Path(state["scope_contract"]),
        "causal_analysis.md": CAUSAL_ANALYSIS,
        "finding_ledger.json": barrier_ledger,
        "own_investigation.json": Path(state["investigations"][slot]["path"]),
        "peer_investigation.json": Path(state["investigations"][target_slot]["path"]),
        "own_plan.md": Path(state["drafts"][slot]["markdown"]),
        **split_draft_for_review(state, target_slot),
    }
    for index, decision in enumerate(sorted((Path(state["run_directory"]) / "decisions").glob("*.json")), 1):
        context_files[f"decision_{index:03d}.json"] = decision
    payload = invoke(
        state, f"cross-{slot}", provider, slot,
        context_files,
        integration_schema(state, barrier_keys), prompt, args.timeout, args.dry_run,
        validator=lambda value: validate_integration(value, state, slot, 2, barrier_keys),
    )
    if args.dry_run:
        emit({"status": "CROSS_REVIEW_DRY_RUN", **payload})
    validate_integration(payload, state, slot, 2, barrier_keys)
    path = Path(state["run_directory"]) / "cross_reviews" / f"{slot}_reviews_{target_slot}.json"
    atomic_json(path, payload)
    markdown = Path(state["run_directory"]) / "drafts" / f"round_2_{slot}.md"
    markdown.write_text(payload["plan_markdown"].rstrip() + "\n", encoding="utf-8")
    record_artifact(state, f"cross-{slot}", path)
    record_artifact(state, f"draft-2-{slot}-md", markdown)
    record_findings(state, payload["new_findings"], f"draft-02-{slot}")
    record_finding_aliases(state, payload["finding_aliases"], f"draft-02-{slot}")
    state["cross_reviews"][slot] = {"target": target_slot, "verdict": payload["verdict"], "path": str(path)}
    state["draft_rounds"]["2"][slot] = {"provider": provider, "path": str(path), "markdown": str(markdown)}
    if payload["verdict"] == "NEEDS_USER_DECISION":
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "PLANNING_BOUNDARY", "source": f"cross-{slot}", "created_at": utc_now(),
        }
    elif set(state["cross_reviews"]) == {"A", "B"}:
        state["status"] = "DIVERGENCE_REQUIRED" if state["planning_depth"] == "deep" else "SYNTHESIS_REQUIRED"
    else:
        state["status"] = "CROSS_REVIEWING"
    save_parallel_stage(state, "cross-review")
    emit({"status": state["status"], "run_id": state["run_id"], "review": str(path), "verdict": payload["verdict"]})


def command_diverge(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    slot = args.slot
    if state["planning_depth"] != "deep" or state["status"] not in {"DIVERGENCE_REQUIRED", "DIVERGING"}:
        raise WorkflowError(f"diverge is not allowed from {state['status']}")
    if slot in state["draft_rounds"]["3"] and not args.dry_run:
        raise WorkflowError(f"slot {slot} already produced draft_03")
    provider = assignment_provider(state, f"diverge-{slot}", state["planners"][slot])
    barrier = state.get("round_barriers", {}).get("diverge") or {}
    barrier_ledger = Path(barrier.get("finding_ledger", state["finding_ledger_path"]))
    barrier_keys = barrier.get("finding_keys")
    target = "draft-02-both"
    prompt = (
        "You are independent deep-planning slot {slot}, producing divergent draft_03. Read the frozen request, "
        "scope contract, causal analysis protocol, stable finding ledger, both investigations, and both draft_02 plans. Re-check repository evidence before accepting "
        "a claim. Mine only in-scope omissions: deeper causal chains, failure paths, required support, alternatives, "
        "and verification. Novelty without a scope id or evidentiary effect must be rejected. Do not broaden the user "
        "objective. Preserve priority order and explicitly disposition stable F-* ledger keys; return a genuinely new "
        "finding in full solution form. Propose evidence-backed finding_aliases when multiple stable keys are observations "
        "of one defect; preserve separate keys when equivalence is not established. Return schema JSON with "
        "provider={provider}, slot={slot}, reviewer_slot={slot}, target={target}, round=3, baseline_sha={sha}, "
        "scope_digest={scope_digest}." + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, target=target, sha=state["baseline_sha"],
             scope_digest=state["scope_digest"])
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "scope_contract.json": Path(state["scope_contract"]),
        "causal_analysis.md": CAUSAL_ANALYSIS,
        "finding_ledger.json": barrier_ledger,
        "investigation_A.json": Path(state["investigations"]["A"]["path"]),
        "investigation_B.json": Path(state["investigations"]["B"]["path"]),
        "draft_02_A.md": Path(state["draft_rounds"]["2"]["A"]["markdown"]),
        "draft_02_B.md": Path(state["draft_rounds"]["2"]["B"]["markdown"]),
    }
    payload = invoke(state, f"diverge-{slot}", provider, slot, context_files,
                     integration_schema(state, barrier_keys), prompt, args.timeout, args.dry_run,
                     validator=lambda value: validate_integration(value, state, slot, 3, barrier_keys))
    if args.dry_run:
        emit({"status": "DIVERGENCE_DRY_RUN", **payload})
    validate_integration(payload, state, slot, 3, barrier_keys)
    path = Path(state["run_directory"]) / "drafts" / f"round_3_{slot}.json"
    markdown = Path(state["run_directory"]) / "drafts" / f"round_3_{slot}.md"
    atomic_json(path, payload)
    markdown.write_text(payload["plan_markdown"].rstrip() + "\n", encoding="utf-8")
    record_artifact(state, f"draft-3-{slot}-json", path)
    record_artifact(state, f"draft-3-{slot}-md", markdown)
    record_findings(state, payload["new_findings"], f"draft-03-{slot}")
    record_finding_aliases(state, payload["finding_aliases"], f"draft-03-{slot}")
    state["draft_rounds"]["3"][slot] = {"provider": provider, "path": str(path), "markdown": str(markdown)}
    state["status"] = "SYNTHESIS_REQUIRED" if set(state["draft_rounds"]["3"]) == {"A", "B"} else "DIVERGING"
    save_parallel_stage(state, "diverge")
    emit({"status": state["status"], "run_id": state["run_id"], "slot": slot, "draft": str(markdown)})


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
        "Read the frozen request, scope contract, causal analysis protocol, both investigations, every completed draft round, and reviews. Resolve claims by "
        "repository evidence rather than vote count. Preserve material disagreements and unresolved product "
        "choices. Scope-lock every item to the frozen objective, preserve priority order, and record each material "
        "finding as problem, evidence, root cause, affected surfaces, solution/tradeoffs, and verification. Produce "
        "`implementation_plan.md` and `batches.md`. The plan must distinguish proposed plan "
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
        "scope_contract": state["scope_contract"],
        "finding_ledger": state["finding_ledger_path"],
        "causal_analysis": str(CAUSAL_ANALYSIS),
        "investigations": {key: value["path"] for key, value in state["investigations"].items()},
        "drafts": {key: value["path"] for key, value in state["drafts"].items()},
        "draft_rounds": state["draft_rounds"],
        "cross_reviews": {key: value["path"] for key, value in state["cross_reviews"].items()},
        "decisions": decisions,
        "previous_final_reviews": previous_final_reviews,
        "previous_convergence_reviews": {key: value["path"] for key, value in state["convergence_reviews"].items()},
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
    diagnostics = synthesis_diagnostics(plan_text, batches_text, state.get("finding_ledger", {}))
    if "TARGET-001" not in plan_text + "\n" + batches_text:
        diagnostics["errors"].append("synthesis must map the candidate to frozen scope id TARGET-001")
    if diagnostics["errors"]:
        raise WorkflowError("; ".join(diagnostics["errors"]))
    number = state["synthesis_submissions"] + 1
    destination = Path(state["run_directory"]) / "synthesis" / f"round_{number}"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan_copy = destination / "implementation_plan.md"
    batches_copy = destination / "batches.md"
    # The host is TOLD to write these two names into this directory (SKILL.md, "Host synthesis"),
    # and then submitting them raised SameFileError -- following the documentation was the one
    # thing that could not work. Submitting the frozen copy in place is a no-op, not an error.
    for source, copy in ((plan, plan_copy), (batches, batches_copy)):
        if source.resolve() != copy.resolve():
            shutil.copyfile(source, copy)
    record_artifact(state, f"candidate-{number}-plan", plan_copy)
    record_artifact(state, f"candidate-{number}-batches", batches_copy)
    state["synthesis_submissions"] = number
    state["candidate"] = {
        "round": number, "plan": str(plan_copy), "batch_manifest": str(batches_copy),
        "plan_sha256": sha256_file(plan_copy), "batch_manifest_sha256": sha256_file(batches_copy),
        "diagnostics": diagnostics,
    }
    state["final_reviews"] = {}
    state["pending_decision"] = None
    state["status"] = (
        "CONVERGENCE_REVIEW_REQUIRED"
        if state["planning_depth"] == "deep" and not state["convergence_round_one_complete"]
        else "FINAL_REVIEW_REQUIRED"
    )
    if state["status"] == "CONVERGENCE_REVIEW_REQUIRED":
        # A prior candidate may have received only one convergence result before a typed user
        # decision. Neither that result nor a late peer may count for the replacement candidate.
        state["convergence_reviews"] = {}
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"], "candidate": state["candidate"],
          "next_action": next_action(state)})


def command_check_synthesis(args: argparse.Namespace) -> None:
    plan = Path(args.plan).expanduser().resolve()
    batches = Path(args.batch_manifest).expanduser().resolve()
    if not plan.is_file() or not batches.is_file():
        raise WorkflowError("both synthesized plan and batch manifest must exist")
    diagnostics = synthesis_diagnostics(
        plan.read_text(encoding="utf-8"), batches.read_text(encoding="utf-8"))
    emit({"status": "SYNTHESIS_VALID" if not diagnostics["errors"] else "SYNTHESIS_INVALID",
          **diagnostics}, code=0 if not diagnostics["errors"] else 2)


def command_convergence_review(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] not in {"CONVERGENCE_REVIEW_REQUIRED", "CONVERGENCE_REVIEWING"}:
        raise WorkflowError(f"convergence-review is not allowed from {state['status']}")
    slot = args.reviewer
    if slot not in {"A", "B"}:
        raise WorkflowError("convergence reviewer must be A or B")
    if slot in state["convergence_reviews"] and not args.dry_run:
        raise WorkflowError(f"convergence reviewer {slot} already completed round one")
    provider = assignment_provider(state, f"convergence-1-{slot}", state["planners"][slot])
    target = f"candidate-round-{state['candidate']['round']}"
    prompt = (
        "You are convergence reviewer ({slot}), round one of exactly two bounded convergence rounds. Read "
        "{context}/request.md, {context}/causal_analysis.md, the scope contract, synthesized plan, batches, and finding evidence. Inspect the repository at "
        "baseline {sha}. This is not a novelty round: reject new objectives. A new finding is admissible only when "
        "it is in scope, materially affects correctness/safety/verification, cites evidence, and states root cause or "
        "honest uncertainty. Check that high-priority findings have dispositions and that priority changes are "
        "evidence-backed. PASS means no supported P0/P1 defect remains; it does not claim consensus or certainty. "
        "Return schema JSON with provider={provider}, reviewer_slot={slot}, target={target}, baseline_sha={sha}."
        + DELIVERY_CONTRACT
    ).format(
        slot=slot, provider=provider, target=target, sha=state["baseline_sha"], context="{context}"
    )
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "scope_contract.json": Path(state["scope_contract"]),
        "causal_analysis.md": CAUSAL_ANALYSIS,
        "finding_ledger.json": Path(state["finding_ledger_path"]),
        "implementation_plan.md": Path(state["candidate"]["plan"]),
        "batches.md": Path(state["candidate"]["batch_manifest"]),
        "investigation_A.json": Path(state["investigations"]["A"]["path"]),
        "investigation_B.json": Path(state["investigations"]["B"]["path"]),
    }
    payload = invoke(state, f"convergence-1-{slot}", provider, slot, context_files,
                     REVIEW_SCHEMA, prompt, args.timeout, args.dry_run,
                     validator=lambda value: validate_review(value, state, provider, slot, target))
    if args.dry_run:
        emit({"status": "CONVERGENCE_REVIEW_DRY_RUN", **payload})
    validate_review(payload, state, provider, slot, target)
    candidate_round = state["candidate"]["round"]
    path = (
        Path(state["run_directory"]) / "convergence_reviews"
        / f"round_1_candidate_{candidate_round}_{slot}.json"
    )
    atomic_json(path, payload)
    record_artifact(state, f"convergence-1-{slot}", path)
    state["convergence_reviews"][slot] = {
        "provider": provider, "verdict": payload["verdict"], "path": str(path),
        "candidate_sha256": state["candidate"]["plan_sha256"],
    }
    if payload["verdict"] == "NEEDS_USER_DECISION":
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "CONVERGENCE_BOUNDARY", "source": f"convergence-1-{slot}",
            "candidate_sha256": state["candidate"]["plan_sha256"], "created_at": utc_now(),
        }
    else:
        state["status"] = "CONVERGENCE_REVIEWING"
    save_parallel_stage(state, "convergence-review")
    emit({"status": state["status"], "run_id": state["run_id"], "verdict": payload["verdict"],
          "review": str(path)})


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
        "{context}/implementation_plan.md, {context}/batches.md, and {context}/causal_analysis.md. Inspect the repository at baseline {sha}. "
        "Judge factual correctness, full request coverage, dependency order, budget bounds, batch scope, and "
        "whether every exit condition names a finite observation. The plan may choose between drafts only when "
        "the choice is supported by evidence; do not demand excluded work without identifying a request conflict. "
        "PASS is valid only when no supported P0 or P1 finding remains; if you emit any P0/P1 finding, verdict must "
        "be FAIL or NEEDS_USER_DECISION. "
        "Do not edit files. Return schema JSON with provider={provider}, reviewer_slot={slot}, target={target}, "
        "baseline_sha={sha}."
        + DELIVERY_CONTRACT
    ).format(slot=slot, provider=provider, target=target, sha=state["baseline_sha"], context="{context}")
    context_files = {
        "request.md": Path(state["request_snapshot"]),
        "scope_contract.json": Path(state["scope_contract"]),
        "causal_analysis.md": CAUSAL_ANALYSIS,
        "finding_ledger.json": Path(state["finding_ledger_path"]),
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
        validator=lambda value: validate_review(value, state, provider, slot, target),
    )
    if args.dry_run:
        emit({"status": "FINAL_REVIEW_DRY_RUN", **payload})
    validate_review(payload, state, provider, slot, target)
    path = Path(state["run_directory"]) / "final_reviews" / f"round_{state['candidate']['round']}_{slot}.json"
    atomic_json(path, payload)
    record_artifact(state, f"final-{state['candidate']['round']}-{slot}", path)
    state["final_reviews"][slot] = {
        "provider": provider,
        "verdict": payload["verdict"],
        "path": str(path),
        "candidate_sha256": state["candidate"]["plan_sha256"],
    }
    if payload["verdict"] == "NEEDS_USER_DECISION":
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "type": "FINAL_PLAN_BOUNDARY", "source": f"final-{state['candidate']['round']}-{slot}",
            "candidate_sha256": state["candidate"]["plan_sha256"], "created_at": utc_now(),
        }
    else:
        state["status"] = "FINAL_REVIEWING"
    save_parallel_stage(state, "final-review")
    emit({"status": state["status"], "run_id": state["run_id"], "verdict": payload["verdict"], "review": str(path)})


def command_adjudicate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    normalize_pending_decisions(state)
    if state["status"] != "NEEDS_USER_DECISION" or not state.get("pending_decisions"):
        raise WorkflowError("there is no pending planning decision")
    selected = state["pending_decisions"].get(args.decision_id)
    if selected is None:
        raise WorkflowError(
            f"pending decision {args.decision_id!r} no longer exists; refresh status before applying")
    state["pending_decision"] = selected
    preview = {
        "status": "DECISION_PREVIEW", "pending_decision": state["pending_decision"],
        "choice": args.choice, "decision": args.decision, "actor": args.actor,
    }
    decision_type = state["pending_decision"]["type"]
    allowed = {
        "PLANNING_BOUNDARY": {"RESOLVE_AND_CONTINUE", "ABANDON"},
        "CONVERGENCE_BOUNDARY": {"RESOLVE_AND_CONTINUE", "ABANDON"},
        "FINAL_PLAN_BOUNDARY": {"RESOLVE_AND_CONTINUE", "ABANDON"},
        "SYNTHESIS_BUDGET_EXHAUSTED": {"GRANT_ONE_SYNTHESIS", "ABANDON"},
        "FINAL_REVIEW_BUDGET_EXHAUSTED": {"GRANT_ONE_SYNTHESIS", "ABANDON"},
        # RESOLVE_AND_CONTINUE un-parks the run and grants nothing. It exists because a spent
        # budget can stop being the right answer without anyone granting anything: when the
        # material the attempts measured has been repaired, the assignment should simply be
        # allowed to ask again. Leaving the run parked made that unreachable -- the reset check
        # lives inside the invocation, and the status guard refused before it ran.
        #
        # Un-parking is free; an attempt still is not. The next invocation re-derives the context
        # digest, and if the material did NOT change it charges nothing and parks again
        # immediately. The user can clear the block; only the evidence can lift the count.
        "INVOCATION_BUDGET_EXHAUSTED": {
            "GRANT_ONE_INVOCATION", "REASSIGN_ASSIGNMENT", "RESOLVE_AND_CONTINUE", "ABANDON"},
        "PROVIDER_INFRASTRUCTURE_FAILURE": {
            "REASSIGN_ASSIGNMENT", "RESOLVE_AND_CONTINUE", "ABANDON"},
    }.get(decision_type, {"ABANDON"})
    if decision_type == "PROVIDER_INFRASTRUCTURE_FAILURE":
        allowed = set(state["pending_decision"].get("choices", allowed))
    if args.choice not in allowed:
        raise WorkflowError(f"choice {args.choice} is not allowed for {decision_type}: {','.join(sorted(allowed))}")
    pending = dict(state["pending_decision"])
    pending_key = pending_decision_identity(pending)
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
                        if name != current and available(name)]
        if not alternatives:
            raise WorkflowError(
                f"no other provider is installed to take over {assignment} from {current}")
        replacement = args.to_provider or alternatives[0]
        if replacement == current:
            raise WorkflowError("reassigning an assignment to the provider that already holds it "
                                "changes nothing")
        if not available(replacement):
            raise WorkflowError(f"provider is not installed: {replacement}")
        preview["reassignment"] = {"assignment": assignment, "from": current, "to": replacement}
        preview["independence_cost"] = (
            f"{assignment} will be served by {replacement}, which also serves other assignments in "
            f"this run. Provider and model diversity claims are downgraded; process, sandbox, "
            f"home and context isolation are unchanged."
        )
    if not args.apply:
        emit(preview)
    path = Path(state["run_directory"]) / "decisions" / f"decision_{len(list((Path(state['run_directory']) / 'decisions').glob('*.json'))) + 1:03d}.json"
    record = {**preview, "applied_at": utc_now()}
    atomic_json(path, record)
    record_artifact(state, f"decision-{path.stem}", path)
    state["pending_decision"] = None
    state.setdefault("pending_decisions", {}).pop(pending_key, None)
    if args.choice == "ABANDON":
        state["status"] = "ABANDONED"
        state["abandoned"] = {
            "reason": args.decision, "actor": args.actor, "at": utc_now(),
            "pending_decision": pending,
        }
        write_terminal_report(state, "ABANDONED")
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
        # Diversity is an independence claim about the planned two-slot topology, not a historical
        # inventory of every model that attempted a call. Once a slot is reassigned, that stronger
        # claim is no longer intact even if the original model delivered earlier artifacts.
        state["model_diversity"] = False
        state["status"] = record["pending_decision"].get("resume_status", "SYNTHESIS_REQUIRED")
    elif decision_type in {"INVOCATION_BUDGET_EXHAUSTED", "PROVIDER_INFRASTRUCTURE_FAILURE"}:
        # Back to where the assignment was, with the count untouched. See the allowed-choices note.
        state["status"] = record["pending_decision"].get("resume_status", "SYNTHESIS_REQUIRED")
    elif decision_type == "PLANNING_BOUNDARY" and set(state["cross_reviews"]) != {"A", "B"}:
        state["status"] = "CROSS_REVIEWING"
    else:
        state["status"] = "SYNTHESIS_REQUIRED"
    if args.choice != "ABANDON" and state.get("pending_decisions"):
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = state["pending_decisions"][
            sorted(state["pending_decisions"])[0]]
    save_state(state)
    emit({"status": state["status"], "decision_record": str(path),
          "pending_decision": state.get("pending_decision")})


def command_status(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    emit({
        "status": state["status"], "run_id": state["run_id"], "baseline_sha": state["baseline_sha"],
        "host_adapter": state.get("host_adapter"),
        "peer_reviewer": state.get("peer_reviewer"),
        "selection_checks": state.get("selection_checks", {}),
        "planners": state["planners"], "provider_diversity": state["provider_diversity"],
        "model_diversity": state.get("model_diversity", False),
        "agent_runtime": state.get("agent_runtime") or {},
        "runtime_warnings": state.get("runtime_warnings") or [],
        "assignment_providers": state.get("assignment_providers") or {},
        "slot_provider_overrides": state.get("slot_provider_overrides") or {},
        "automatic_fallbacks": state.get("automatic_fallbacks") or [],
        "independence_notes": state.get("independence_notes") or [],
        "final_reviewer": state["final_reviewer"], "drafts": state["drafts"],
        "cross_reviews": state["cross_reviews"], "synthesis_submissions": state["synthesis_submissions"],
        "final_reviews": state["final_reviews"], "pending_decision": state["pending_decision"],
        "pending_decisions": state.get("pending_decisions") or {},
        "investigations": state.get("investigations") or {},
        "draft_rounds": state.get("draft_rounds") or {},
        "convergence_reviews": state.get("convergence_reviews") or {},
        "abandoned": state.get("abandoned"),
        "active_invocations": active_invocation_records(state),
        "resource_usage": state.get("resource_usage") or {},
        "infrastructure_usage": state.get("infrastructure_usage") or {},
        "finding_aliases": state.get("finding_aliases") or {},
        "final": state.get("final"), "run_directory": state["run_directory"], "usage": state["usage"],
        "next_action": next_action(state),
    })


def command_next(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    emit({"status": state["status"], "run_id": state["run_id"],
          "next_action": next_action(state)})


def command_export(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_ready_invariants(state)
    emit({
        "status": "READY", "run_id": state["run_id"], "baseline_sha": state["baseline_sha"],
        "host_adapter": state.get("host_adapter"),
        "peer_reviewer": state.get("peer_reviewer"),
        "selection_checks": state.get("selection_checks", {}),
        "plan": state["final"]["plan"], "batch_manifest": state["final"]["batch_manifest"],
        "plan_sha256": state["final"]["plan_sha256"],
        "batch_manifest_sha256": state["final"]["batch_manifest_sha256"],
        "report": state["final"]["report"],
        "provider_diversity": state["provider_diversity"],
        "model_diversity": state.get("model_diversity", False),
        "agent_runtime": state.get("agent_runtime") or {},
        "runtime_warnings": state.get("runtime_warnings") or [],
        "assignment_providers": state.get("assignment_providers") or {},
        "slot_provider_overrides": state.get("slot_provider_overrides") or {},
        "automatic_fallbacks": state.get("automatic_fallbacks") or [],
        "independence_notes": state.get("independence_notes") or [],
        "handoff": "Use scripts/workflow.py init only after explicit user approval.",
    })


def command_audit_export(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] not in TERMINAL_STATUSES:
        raise WorkflowError("audit-export is allowed only after READY or ABANDONED")
    report = Path(state["run_directory"]) / "final_report.md"
    if not report.is_file():
        report = write_terminal_report(state, state["status"])
        save_state(state)
    emit({
        "status": state["status"], "run_id": state["run_id"], "report": str(report),
        "host_adapter": state.get("host_adapter"),
        "peer_reviewer": state.get("peer_reviewer"),
        "selection_checks": state.get("selection_checks", {}),
        "assignment_providers": state.get("assignment_providers") or {},
        "slot_provider_overrides": state.get("slot_provider_overrides") or {},
        "automatic_fallbacks": state.get("automatic_fallbacks") or [],
        "independence_notes": state.get("independence_notes") or [],
        "candidate": state.get("candidate"), "final_reviews": state.get("final_reviews") or {},
        "abandoned": state.get("abandoned"), "resource_usage": state.get("resource_usage") or {},
    })


def command_migrate_engine(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id, allow_engine_drift=True)
    frozen = state.get("engine_contract")
    current = engine_contract()
    active = active_invocation_records(state)
    if active:
        raise WorkflowError(
            "engine migration is refused while invocation records are RUNNING: "
            + ",".join(sorted(active)))
    if frozen == current:
        emit({"status": "ENGINE_CURRENT", "run_id": state["run_id"], "engine_contract": current})
    preview = {
        "status": "ENGINE_MIGRATION_PREVIEW", "run_id": state["run_id"],
        "from": frozen, "to": current, "reason": args.reason, "actor": args.actor,
        "warning": "This changes prompts and workflow semantics for an existing run; prior invocation records remain frozen.",
    }
    if not args.apply:
        emit(preview)
    decisions = Path(state["run_directory"]) / "decisions"
    path = decisions / f"engine_migration_{len(list(decisions.glob('engine_migration_*.json'))) + 1:03d}.json"
    atomic_json(path, {**preview, "applied_at": utc_now()})
    record_artifact(state, f"decision-{path.stem}", path)
    state["engine_contract"] = current
    state["engine_epoch"] = state.get("engine_epoch", 0) + 1
    state["software_version"] = VERSION
    save_state(state)
    emit({"status": state["status"], "run_id": state["run_id"],
          "engine_contract": current, "decision_record": str(path)})


def command_recover_invocation(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id, allow_engine_drift=True)
    preview = {
        "status": "INTERRUPTED_INVOCATION_RECOVERY_PREVIEW", "run_id": state["run_id"],
        "assignment": args.assignment, "actor": args.actor, "reason": args.reason,
        "active": active_invocation_records(state).get(args.assignment),
        "warning": "Apply only after the named assignment process has ended; its spent attempt remains charged.",
    }
    if not args.apply:
        emit(preview)
    recovered = recover_interrupted_assignment(
        state, args.assignment, args.actor, args.reason)
    if not recovered:
        raise WorkflowError(f"no RUNNING invocation exists for assignment {args.assignment!r}")
    save_state(state)
    emit({**preview, "status": state["status"], "recovered_invocations": recovered})


def command_abandon(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] in TERMINAL_STATUSES:
        raise WorkflowError(f"planning run is already terminal: {state['status']}")
    if not args.apply:
        emit({"status": "ABANDON_PREVIEW", "run_id": state["run_id"], "reason": args.reason})
    state["status"] = "ABANDONED"
    state["abandoned"] = {"reason": args.reason, "actor": args.actor, "at": utc_now()}
    write_terminal_report(state, "ABANDONED")
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

    def add_codex_selection(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--codex-model",
            help=f"override the default {DEFAULT_CODEX_MODEL!r}; use 'cli-default' to defer to Codex")
        command.add_argument(
            "--codex-model-provider",
            help="freeze and pass a configured Codex model_provider (for example a DeepSeek gateway)")
        command.add_argument(
            "--codex-profile",
            help="freeze and pass a CODEX_HOME profile name; its profile config is mounted read-only")

    def add_model_selection(command: argparse.ArgumentParser) -> None:
        add_codex_selection(command)
        command.add_argument(
            "--claude-model",
            help=f"override the default {DEFAULT_CLAUDE_MODEL!r}; use 'cli-default' to defer to Claude")
        command.add_argument(
            "--dsh-model",
            help="override the dsh model (read from ~/.dsh/settings.yaml when omitted); "
                 "use 'cli-default' to defer to the harness")
        command.add_argument(
            "--dsh-model-provider",
            help="override the dsh model provider (defaults to deepseek-official)")

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--project", required=True)
    preflight.add_argument(
        "--base-ref", default="HEAD",
        help="Git ref whose committed SHA will be frozen; source worktree changes are excluded",
    )
    preflight.add_argument("--backend", choices=["auto", *SUPPORTED_PROVIDERS], default="auto")
    preflight.add_argument(
        "--host-adapter", choices=SUPPORTED_PROVIDERS,
        help="freeze the current host's CLI adapter so auto can prefer an independent reviewer")
    preflight.add_argument(
        "--peer-reviewer", choices=["auto", *SUPPORTED_PROVIDERS], default="auto",
        help="override the host-aware non-host slot after a higher-priority probe fails")
    preflight.add_argument(
        "--probe", action="store_true",
        help="spend one small sandboxed call per provider to check it can authenticate, read a "
             "mounted file, and return a schema object; exits 2 if any cannot")
    add_model_selection(preflight)
    preflight.set_defaults(func=command_preflight)

    init = commands.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument("--request", required=True)
    init.add_argument(
        "--base-ref", default="HEAD",
        help="Git branch, tag, or commit to freeze; uncommitted source-worktree changes are ignored",
    )
    init.add_argument("--backend", choices=["auto", *SUPPORTED_PROVIDERS], default="auto")
    init.add_argument(
        "--host-adapter", choices=SUPPORTED_PROVIDERS,
        help="freeze the current host's CLI adapter for host-aware reviewer selection")
    init.add_argument(
        "--peer-reviewer", choices=["auto", *SUPPORTED_PROVIDERS], default="auto",
        help="freeze an explicit non-host slot after probing the preferred reviewer order")
    init.add_argument(
        "--final-reviewer", choices=["auto", "both", *SUPPORTED_PROVIDERS], default="auto",
        help="use one fresh reviewer for standard planning; reserve 'both' for deep or explicitly requested high assurance",
    )
    init.add_argument(
        "--planning-depth", choices=["standard", "deep"], default="standard",
        help="standard stops after draft_02; deep adds draft_03 and an extra convergence review")
    init.add_argument(
        "--research-policy", choices=["local-only", "authoritative-web"], default="local-only",
        help="allow investigation agents to consult only official docs/GitHub when local evidence is insufficient")
    add_model_selection(init)
    init.set_defaults(func=command_init)

    for name, function in (("investigate", command_investigate), ("draft", command_draft),
                           ("cross-review", command_cross_review), ("diverge", command_diverge)):
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

    check_synthesis = commands.add_parser(
        "check-synthesis", help="validate host-authored plan and batch documents before submission")
    check_synthesis.add_argument("--plan", required=True)
    check_synthesis.add_argument("--batch-manifest", required=True)
    check_synthesis.set_defaults(func=command_check_synthesis)

    final_review = commands.add_parser("final-review")
    final_review.add_argument("--project", required=True)
    final_review.add_argument("--run-id", required=True)
    final_review.add_argument("--reviewer", choices=["A", "B", "F"], required=True)
    final_review.add_argument("--timeout", type=int, default=1800)
    final_review.add_argument("--dry-run", action="store_true")
    final_review.set_defaults(func=command_final_review)

    convergence_review = commands.add_parser("convergence-review")
    convergence_review.add_argument("--project", required=True)
    convergence_review.add_argument("--run-id", required=True)
    convergence_review.add_argument("--reviewer", choices=["A", "B"], required=True)
    convergence_review.add_argument("--timeout", type=int, default=1800)
    convergence_review.add_argument("--dry-run", action="store_true")
    convergence_review.set_defaults(func=command_convergence_review)

    adjudicate = commands.add_parser("adjudicate")
    adjudicate.add_argument("--project", required=True)
    adjudicate.add_argument("--run-id", required=True)
    adjudicate.add_argument(
        "--decision-id", required=True,
        help="stable key from status.pending_decisions; binds preview and apply to one decision")
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

    for name, function in (("status", command_status), ("next", command_next),
                           ("export", command_export), ("audit-export", command_audit_export)):
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

    migrate_engine = commands.add_parser(
        "migrate-engine", help="preview or explicitly apply engine drift to an existing planning run")
    migrate_engine.add_argument("--project", required=True)
    migrate_engine.add_argument("--run-id", required=True)
    migrate_engine.add_argument("--reason", required=True)
    migrate_engine.add_argument("--actor", required=True)
    migrate_engine.add_argument("--apply", action="store_true")
    migrate_engine.set_defaults(func=command_migrate_engine)

    recover = commands.add_parser(
        "recover-invocation", help="audit and terminalize a controller-interrupted planning call")
    recover.add_argument("--project", required=True)
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--assignment", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--apply", action="store_true")
    recover.set_defaults(func=command_recover_invocation)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        parallel_commands = {
            "investigate", "draft", "cross-review", "diverge",
            "convergence-review", "final-review",
        }
        if args.command == "recover-invocation":
            project = resolve_project(args.project)
            with assignment_lock(project, args.run_id, args.assignment):
                args.func(args)
        elif args.command in parallel_commands:
            project = resolve_project(args.project)
            slot = getattr(args, "slot", None) or getattr(args, "reviewer", None)
            with assignment_lock(project, args.run_id, f"{args.command}-{slot}"):
                args.func(args)
        elif (hasattr(args, "run_id") and hasattr(args, "project")
                and args.command not in {"status", "next"}):
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
