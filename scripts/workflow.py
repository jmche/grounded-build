#!/usr/bin/env python3
"""Centralized, resumable implementation harness for grounded-build."""

from __future__ import annotations

import argparse
import copy
import fcntl
import functools
import hashlib
import json
import os
import platform
import re
import resource
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 7
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_REVIEW_ROUNDS = 4
DEFAULT_MAX_REVIEW_INVOCATIONS_PER_BATCH = 10
DEFAULT_MAX_CONTRACT_INVOCATIONS = 3
DEFAULT_MAX_VERIFICATION_ATTEMPTS = 3
DEFAULT_MAX_VERIFICATION_CYCLES_PER_ROUND = 2
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_VERIFICATION_TEMP_BYTES = 256 * 1024 * 1024
# Entry count is a proxy for "this command is running away"; bytes are the real guard. Measured
# on the repository this run verifies, the full suite writes 15_676 entries / 16.7 MB at the
# BASELINE commit and 16_565 / 19.0 MB at the batch head -- so at 10_000 the cap rejected a
# healthy suite as an infrastructure failure while byte usage sat at 7% of its own budget, and it
# would have done so for every batch of this run regardless of what the batch changed. A test
# suite that gives each of ~2_250 tests its own tmp_path directory reaches five figures of small
# files without anything being wrong. Raised to leave real headroom over that while still
# stopping a loop that creates files without end; MAX_VERIFICATION_TEMP_BYTES is unchanged.
MAX_VERIFICATION_TEMP_FILES = 200_000
MAX_VERIFICATION_PROCESSES = 64
MAX_VERIFICATION_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
ENVIRONMENT_FINGERPRINT_VERSION = 3
MAX_ENVIRONMENT_FILES = 200_000
MAX_ENVIRONMENT_BYTES = 16 * 1024 * 1024 * 1024
MAX_ENVIRONMENT_SCAN_SECONDS = 30.0
SUPPORTED_REVIEWERS = ("claude", "codex", "dsh")
DEFAULT_CLAUDE_REVIEWER_MODEL = "opus"
DEFAULT_CODEX_REVIEWER_MODEL = "gpt-5.6-sol"
TERMINAL_STATUSES = {"FINALIZED", "SUPERSEDED", "ABANDONED"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE = SKILL_ROOT / "references" / "reviewer_prompt.md"
CONTRACT_PROMPT_TEMPLATE = SKILL_ROOT / "references" / "contract_reviewer_prompt.md"

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reviewer": {"type": "string", "enum": list(SUPPORTED_REVIEWERS)},
        "reviewed_sha": {"type": "string"},
        "base_sha": {"type": "string"},
        "batch": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["PASS", "FAIL", "NEEDS_USER_DECISION", "NEEDS_VERIFICATION"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "fingerprint": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "novelty": {
                        "type": "string",
                        "enum": [
                            "INITIAL_REVIEW",
                            "INTRODUCED_BY_FIX",
                            "PREVIOUSLY_MASKED",
                            "PRE_EXISTING",
                            "UNRELATED",
                        ],
                    },
                    "why_not_detectable_earlier": {"type": "string"},
                    "introduced_by_sha": {"type": "string"},
                    "severity_change_justification": {"type": "string"},
                    "location": {"type": "string"},
                    "trigger": {"type": "string"},
                    "consequence": {"type": "string"},
                    "required_outcome": {"type": "string"},
                },
                "required": [
                    "id",
                    "fingerprint",
                    "severity",
                    "novelty",
                    "why_not_detectable_earlier",
                    "introduced_by_sha",
                    "severity_change_justification",
                    "location",
                    "trigger",
                    "consequence",
                    "required_outcome",
                ],
            },
        },
        "resolved_finding_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "verification_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "enum": ["repository"]},
                    "reason": {"type": "string"},
                    "expected_exit": {"type": "integer"},
                },
                "required": ["id", "argv", "cwd", "reason", "expected_exit"],
            },
        },
        "criterion_results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criterion_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASS", "FAIL", "NOT_APPLICABLE"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["criterion_id", "status", "evidence_ids", "rationale"],
            },
        },
    },
    "required": [
        "reviewer",
        "reviewed_sha",
        "base_sha",
        "batch",
        "verdict",
        "summary",
        "findings",
        "resolved_finding_ids",
        "verification_requests",
        "criterion_results",
    ],
}

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reviewer": {"type": "string", "enum": list(SUPPORTED_REVIEWERS)},
        "baseline_sha": {"type": "string"},
        "assessment": {"type": "string", "enum": ["READY", "NEEDS_USER_DECISION"]},
        "summary": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"}, "batch": {"type": "string"},
                    "source": {"type": "string", "enum": ["DECLARED", "REVIEWER_DERIVED"]},
                    "observation": {"type": "string"}, "expected_result": {"type": "string"},
                    "scope": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "evidence_kind": {
                        "type": "string",
                        "enum": ["COMMAND", "REPOSITORY_ASSERTION", "USER_BOUNDARY"],
                    },
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "expected_exit": {"type": "integer"},
                },
                # Every property, because strict structured output has no optional property.
                # A non-COMMAND criterion supplies evidence_kind, argv=[] and expected_exit=0;
                # normalize_contract_criteria already defaulted exactly those three values, so
                # requiring them changes nothing downstream.
                "required": [
                    "id", "batch", "source", "observation", "expected_result", "scope",
                    "rationale", "evidence_kind", "argv", "expected_exit",
                ],
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"}, "batch": {"type": "string"},
                    "problem": {"type": "string"}, "decision_required": {"type": "string"},
                    "proposed_observation": {"type": "string"},
                },
                "required": ["id", "batch", "problem", "decision_required", "proposed_observation"],
            },
        },
    },
    "required": ["reviewer", "baseline_sha", "assessment", "summary", "criteria", "issues"],
}


class WorkflowError(RuntimeError):
    """A state, Git, or infrastructure error—not a code-quality verdict."""


class EnvironmentDriftError(WorkflowError):
    """The shared read-only project environment changed after run initialization."""


class ReviewContractError(WorkflowError):
    """The reviewer returned output that cannot be applied to the recorded state."""


PORTABLE_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum"}
)


def unsupported_schema_keywords(schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return response-schema problems outside the portable Codex/Claude subset.

    Two rules, because the structured-output backends enforce two and rejecting one shape while
    permitting the other only moves the 400 to the next schema. A keyword outside the subset is
    refused (`uniqueItems`, `pattern`, `minLength`), and so is an object whose `required` does not
    list EVERY key in its `properties` -- strict mode has no notion of an optional property, so
    "optional" is expressed by the value the caller supplies, not by omission from `required`.
    """
    problems: list[str] = []
    for key, value in schema.items():
        if key not in PORTABLE_SCHEMA_KEYWORDS:
            problems.append(f"{path}.{key}")
            continue
        if key == "properties" and isinstance(value, dict):
            for property_name, child in value.items():
                if isinstance(child, dict):
                    problems.extend(
                        unsupported_schema_keywords(child, f"{path}.properties.{property_name}")
                    )
        elif key == "items" and isinstance(value, dict):
            problems.extend(unsupported_schema_keywords(value, f"{path}.items"))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        required = schema.get("required")
        if not isinstance(required, list):
            problems.append(f"{path}.required is missing")
        else:
            for name in properties:
                if name not in required:
                    problems.append(f"{path}.required omits {name!r}")
    return problems


def validate_portable_review_schema() -> None:
    problems = (
        [f"REVIEW_SCHEMA{item[1:]}" for item in unsupported_schema_keywords(REVIEW_SCHEMA)]
        + [f"CONTRACT_SCHEMA{item[1:]}" for item in unsupported_schema_keywords(CONTRACT_SCHEMA)]
    )
    if problems:
        raise WorkflowError(
            "response schema is not portable to the structured-output backends: "
            + ", ".join(problems)
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(f"command timed out after {timeout}s: {args[0]}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise WorkflowError(f"command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


@functools.lru_cache(maxsize=None)
def executable_version(name: str) -> str | None:
    if not shutil.which(name):
        return None
    result = run([name, "--version"], check=False, timeout=30)
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0][:200] if result.returncode == 0 and value else None


def controller_runtime() -> dict[str, Any]:
    """Describe the interpreter that owns workflow state transitions.

    ``python3`` is a PATH lookup, not an identity. Recording the resolved interpreter makes a
    run resumable without silently changing Python distributions between invocations.
    """
    invoked = Path(sys.executable).absolute()
    resolved = invoked.resolve()
    path_python = shutil.which("python3")
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "executable": str(invoked),
        "resolved_executable": str(resolved),
        "path_python3": str(Path(path_python).absolute()) if path_python else None,
        "path_python3_matches": bool(path_python and Path(path_python).resolve() == resolved),
        "supported": sys.version_info >= (3, 11),
        "command_prefix": [str(invoked), str(Path(__file__).resolve())],
    }


def validate_controller_runtime(state: dict[str, Any]) -> None:
    """Reject silent interpreter drift for runs new enough to record controller identity."""
    recorded = state.get("controller_runtime")
    if not isinstance(recorded, dict):
        return
    current = controller_runtime()
    identity = ("implementation", "version", "resolved_executable")
    if any(recorded.get(key) != current.get(key) for key in identity):
        prefix = recorded.get("command_prefix") or [recorded.get("executable"), "workflow.py"]
        raise WorkflowError(
            "workflow controller runtime changed after initialization; resume with the recorded "
            f"command prefix {json.dumps(prefix)} or explicitly supersede the run"
        )


def project_runtime_payload(project: Path) -> dict[str, Any]:
    """Report, but never provision, the project's optional verification environment."""
    launcher = project / ".venv" / "bin" / "python"
    available = launcher.is_file() and os.access(launcher, os.X_OK)
    uv_lock = project / "uv.lock"
    pyproject = project / "pyproject.toml"
    uv_path = shutil.which("uv")
    if available:
        guidance = (
            "Relative .venv commands are resolved from the original project and mounted read-only "
            "while the fixed-SHA worktree remains the verification cwd."
        )
    elif uv_path and (uv_lock.is_file() or pyproject.is_file()):
        guidance = (
            "No project .venv is available. Provision one explicitly with the repository's locked "
            "uv workflow before verification; grounded-build never installs dependencies silently."
        )
    else:
        guidance = (
            "No project .venv is available. Provision the repository's documented environment "
            "explicitly before requesting a relative .venv verification command."
        )
    return {
        "project_venv": {
            "launcher": str(launcher),
            "available": available,
            "resolved_launcher": str(launcher.resolve()) if available else None,
            "version_probe": "NOT_EXECUTED",
            "worktree_copy_expected": False,
            "verification_source": "ORIGINAL_PROJECT_READ_ONLY" if available else None,
        },
        "uv": {
            "available": bool(uv_path),
            "executable": uv_path,
            "version": executable_version("uv") if uv_path else None,
            "lockfile": str(uv_lock) if uv_lock.is_file() else None,
            "pyproject": str(pyproject) if pyproject.is_file() else None,
            "auto_provision": False,
        },
        "guidance": guidance,
    }


def dsh_reviewer_identity(model: str | None = None, model_provider: str | None = None) -> dict[str, Any]:
    """Freeze the non-secret ``dsh`` adapter identity from the harness settings file.

    ``dsh`` is the DeepSeek Harness CLI adapter, not a model family; its effective model/provider
    come from ``settings.yaml`` (the user's saved selection layered over the profile default), so
    the selection is read rather than invented. Explicit overrides win, matching claude/codex.
    """
    configured: dict[str, Any] = {}
    dsh_home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
    settings = dsh_home / "settings.yaml"
    if settings.is_file():
        try:
            import yaml  # dsh identity is the only YAML consumer; guarded to stay optional
        except ImportError:
            yaml = None
        if yaml is not None:
            try:
                document = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError):
                document = {}
            selection = document.get("agent-default-model") or {}
            if isinstance(selection, dict):
                if isinstance(selection.get("provider"), str):
                    configured["provider"] = selection["provider"].strip()
                if isinstance(selection.get("model"), str):
                    configured["model"] = selection["model"].strip()
    # The harness composition default (dsh-base/cordis.patch.yml `agent-default-model`) is
    # deepseek-v4-flash; report it honestly so an unpinned run does not claim "no model" while the
    # fast tier actually serves it.
    selected_model = model or configured.get("model") or "deepseek-v4-flash"
    selected_provider = model_provider or configured.get("provider") or "deepseek-official"
    lowered = " ".join(str(value).lower() for value in (selected_model, selected_provider) if value)
    family = "deepseek" if "deepseek" in lowered else "gpt" if any(
        marker in lowered for marker in ("gpt", "openai", "o1", "o3", "o4")
    ) else "unknown"
    return {"adapter": "dsh", "cli_version": executable_version("dsh"),
            "model": selected_model, "model_provider": selected_provider, "profile": None,
            "model_family": family, "identity_source": (
                "explicit_override" if any((model, model_provider)) else
                "dsh_settings" if configured else "harness_default")}


def reviewer_runtime(args: argparse.Namespace, reviewer: str) -> dict[str, Any]:
    """Freeze the selected reviewer model separately from its CLI adapter."""
    if reviewer == "claude":
        requested = getattr(args, "claude_model", None)
        model = DEFAULT_CLAUDE_REVIEWER_MODEL if requested is None else requested
        if model == "cli-default":
            model = None
        return {"adapter": "claude", "cli_version": executable_version("claude"),
                "model": model, "model_provider": "anthropic", "profile": None,
                "model_family": "claude", "identity_source": (
                    "cli_default" if model is None else
                    "explicit_override" if requested is not None else "grounded_build_default")}
    if reviewer == "dsh":
        model = getattr(args, "dsh_model", None)
        model_provider = getattr(args, "dsh_model_provider", None)
        for option, value in (("--dsh-model", model), ("--dsh-model-provider", model_provider)):
            if value and value != "cli-default" and (
                    len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_./:+-]+", value)):
                raise WorkflowError(f"{option} must be a simple non-secret selector")
        if model == "cli-default":
            model = None
        return dsh_reviewer_identity(model, model_provider)
    profile = getattr(args, "codex_profile", None)
    if profile and not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        raise WorkflowError("--codex-profile must be a simple profile name")
    explicit_model = getattr(args, "codex_model", None)
    explicit_provider = getattr(args, "codex_model_provider", None)
    for option, value in (("--codex-model", explicit_model),
                          ("--codex-model-provider", explicit_provider)):
        if value and value != "cli-default" and (
                len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_./:+-]+", value)):
            raise WorkflowError(f"{option} must be a simple non-secret selector")
    model = None
    provider = None
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
        configured_model = parsed.get("model") if isinstance(parsed.get("model"), str) else None
        configured_provider = (
            parsed.get("model_provider") if isinstance(parsed.get("model_provider"), str) else None)
        if configured_model:
            model = configured_model
        if configured_provider:
            provider = configured_provider
    if not any((explicit_model, explicit_provider, profile)):
        model = DEFAULT_CODEX_REVIEWER_MODEL
    elif explicit_model:
        model = None if explicit_model == "cli-default" else explicit_model
    provider = explicit_provider or provider
    lowered = " ".join(str(value).lower() for value in (model, provider) if value)
    family = "deepseek" if "deepseek" in lowered else "gpt" if any(
        marker in lowered for marker in ("gpt", "openai", "o1", "o3", "o4")
    ) else "unknown"
    return {"adapter": "codex", "cli_version": executable_version("codex"),
            "model": model, "model_provider": provider, "profile": profile,
            "model_family": family,
            "identity_source": "explicit_override" if any((getattr(args, "codex_model", None),
                                                              getattr(args, "codex_model_provider", None),
                                                              profile)) else "grounded_build_default"}


def git(path: Path, *args: str, check: bool = True) -> str:
    return run(("git", "-C", str(path), *args), check=check).stdout.strip()


def resolve_project(value: str) -> Path:
    project = Path(value).expanduser().resolve()
    if not project.is_dir():
        raise WorkflowError(f"project directory does not exist: {project}")
    root = Path(git(project, "rev-parse", "--show-toplevel")).resolve()
    if root != project:
        raise WorkflowError(f"--project must be the Git root: expected {root}, got {project}")
    return project


def state_home() -> Path:
    configured = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".grounded-build" / "implementation"
    )
    return root.resolve()


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def common_git_dir(project: Path) -> Path:
    raw = git(project, "rev-parse", "--git-common-dir")
    candidate = Path(raw)
    return (candidate if candidate.is_absolute() else project / candidate).resolve()


def origin_url(project: Path) -> str:
    result = run(("git", "-C", str(project), "remote", "get-url", "origin"), check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def slug(value: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return (cleaned[:limit] or "item")


def project_identity(project: Path) -> tuple[str, str]:
    # The local Git common directory identifies a clone without changing when
    # its remote is renamed or replaced. The origin remains registry metadata.
    material = str(common_git_dir(project))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{slug(project.name, 36)}-{digest}", digest


def project_directory(project: Path) -> Path:
    key, _ = project_identity(project)
    return state_home() / "projects" / key


def runs_directory(project: Path) -> Path:
    return project_directory(project) / "runs"


def run_directory(project: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise WorkflowError(f"unsafe run identifier: {run_id!r}")
    return runs_directory(project) / run_id


def state_path(project: Path, run_id: str) -> Path:
    return run_directory(project, run_id) / "workflow.json"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    secure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"{label} must contain a JSON object: {path}")
    return payload


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def append_event(state: dict[str, Any], event_type: str, details: dict[str, Any]) -> dict[str, Any]:
    """Stage one immutable, hash-linked workflow event for the next atomic state save."""
    events_dir = Path(state["run_directory"]) / "events"
    secure_directory(events_dir)
    sequence = len(state.setdefault("events", [])) + 1
    previous_hash = state["events"][-1]["event_hash"] if state["events"] else None
    body = {
        "sequence": sequence,
        "type": event_type,
        "created_at": utc_now(),
        "previous_event_hash": previous_hash,
        "details": details,
    }
    event_hash = sha256_bytes(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    event = {**body, "event_hash": event_hash}
    path = events_dir / f"{sequence:06d}-{slug(event_type, 40)}.json"
    if path.exists():
        raise WorkflowError(f"event path already exists: {path}")
    state["events"].append({
        "sequence": sequence, "type": event_type, "event_hash": event_hash, "path": str(path)
    })
    state.setdefault("_pending_event_payloads", []).append({"path": str(path), "event": event})
    return event


def recover_interrupted_state_save(path: Path) -> None:
    transaction_path = path.parent / "state_save_transaction.json"
    if not transaction_path.is_file():
        return
    transaction = read_json_object(transaction_path, "state save transaction")
    supplied_digest = transaction.pop("transaction_digest", None)
    calculated_digest = sha256_bytes(
        json.dumps(transaction, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if supplied_digest != calculated_digest:
        raise WorkflowError("interrupted state-save transaction digest mismatch")
    intended_state = transaction.get("state")
    pending_events = transaction.get("events")
    if not isinstance(intended_state, dict) or not isinstance(pending_events, list):
        raise WorkflowError("interrupted state-save transaction is malformed")
    events_root = (path.parent / "events").resolve()
    for item in pending_events:
        if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
            raise WorkflowError("interrupted state-save event is malformed")
        event_path = Path(str(item.get("path", ""))).resolve()
        if not event_path.is_relative_to(events_root):
            raise WorkflowError("interrupted state-save event escapes the run event directory")
        atomic_json(event_path, item["event"])
    atomic_json(path, intended_state)
    transaction_path.unlink()


def validate_event_chain(state: dict[str, Any]) -> None:
    expected_previous: str | None = None
    events_root = (Path(state["run_directory"]) / "events").resolve()
    referenced_paths = {
        str(Path(reference.get("path", "")).resolve()) for reference in state.get("events", [])
    }
    disk_paths = {str(path.resolve()) for path in events_root.glob("*.json")} if events_root.is_dir() else set()
    if referenced_paths != disk_paths:
        raise WorkflowError("workflow event directory and state index differ; recovery is required")
    for expected_sequence, reference in enumerate(state.get("events", []), start=1):
        path = Path(reference.get("path", "")).resolve()
        if not path.is_relative_to(events_root) or not path.is_file():
            raise WorkflowError(f"workflow event is missing or outside the run: {path}")
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"invalid workflow event {path}: {exc}") from exc
        event_hash = event.pop("event_hash", None)
        calculated = sha256_bytes(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if (
            event.get("sequence") != expected_sequence
            or event.get("previous_event_hash") != expected_previous
            or event_hash != calculated
            or reference.get("event_hash") != calculated
        ):
            raise WorkflowError(f"workflow event chain validation failed at {path}")
        expected_previous = calculated


def state_authority_digest(state: dict[str, Any]) -> str:
    authoritative = {
        key: value for key, value in state.items()
        if key not in {"events", "updated_at"} and not key.startswith("_")
    }
    return sha256_bytes(
        json.dumps(authoritative, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def validate_state_checkpoint(state: dict[str, Any]) -> None:
    events = state.get("events", [])
    if not events:
        raise WorkflowError("current-schema state has no checkpoint event")
    last_path = Path(events[-1]["path"])
    last_event = read_json_object(last_path, "state checkpoint event")
    if last_event.get("type") != "STATE_CHECKPOINT":
        raise WorkflowError("workflow state is not sealed by a final checkpoint event")
    expected = last_event.get("details", {}).get("state_digest")
    actual = state_authority_digest(state)
    if expected != actual:
        raise WorkflowError("workflow state checkpoint digest mismatch")


def validate_artifact_manifests(state: dict[str, Any]) -> None:
    run_root = Path(state["run_directory"]).resolve()
    manifests: list[dict[str, Any]] = []
    for collection_name in ("review_invocations", "contract_review_invocations"):
        for invocation in state.get(collection_name, []):
            manifests.extend(invocation.get("artifacts", []))
    for evidence in state.get("verification_evidence", []):
        manifests.extend(evidence.get("manifest", []))
    for item in manifests:
        path = Path(item.get("path", "")).resolve()
        if not path.is_relative_to(run_root) or not path.is_file():
            raise WorkflowError(f"audited artifact is missing or outside the run: {path}")
        if path.stat().st_size != item.get("bytes") or sha256_file(path) != item.get("sha256"):
            raise WorkflowError(f"audited artifact digest mismatch: {path}")


def artifact_manifest(paths: Sequence[Path]) -> list[dict[str, Any]]:
    return [
        {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in paths if path.is_file()
    ]


def next_invocation(
    state: dict[str, Any], kind: str, *, batch: str | None = None, round_number: int | None = None
) -> tuple[str, Path, dict[str, Any]]:
    if kind == "contract":
        state["usage"]["contract_review_invocations"] += 1
        number = state["usage"]["contract_review_invocations"]
        if number > state["budgets"]["max_contract_review_invocations"]:
            raise WorkflowError("contract reviewer invocation budget exhausted")
        root = Path(state["run_directory"]) / "contracts" / f"invocation_{number:03d}"
        invocation_id = f"contract-{number:03d}"
        collection = state["contract_review_invocations"]
    elif kind == "review" and batch is not None and round_number is not None:
        counts = state["usage"]["review_invocations_by_batch"]
        counts[batch] = int(counts.get(batch, 0)) + 1
        number = counts[batch]
        # Must honour the same grant the typed decision hands out, or an approved extra
        # invocation would be refused here and the grant would buy nothing.
        if number > int(state["budgets"]["max_review_invocations_per_batch"]) + int(
            state.get("extra_review_invocations_granted", {}).get(batch, 0)
        ):
            raise WorkflowError(f"reviewer invocation budget exhausted for batch {batch}")
        root = (
            Path(state["run_directory"]) / "reviews" / f"batch_{slug(batch)}"
            / f"round_{round_number:02d}" / f"invocation_{number:03d}"
        )
        invocation_id = f"review-{slug(batch)}-{number:03d}"
        collection = state["review_invocations"]
    else:
        raise WorkflowError(f"unsupported invocation kind: {kind}")
    if root.exists():
        raise WorkflowError(f"invocation directory already exists: {root}")
    secure_directory(root)
    record = {
        "invocation_id": invocation_id, "kind": kind, "batch": batch,
        "round": round_number, "status": "STARTED", "started_at": utc_now(),
        "directory": str(root), "reviewer": state["reviewer"],
    }
    collection.append(record)
    append_event(state, "INVOCATION_STARTED", record)
    save_state(state)
    return invocation_id, root, record


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    secure_directory(path.parent)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowError(f"another workflow command is active: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_registry(project: Path) -> None:
    root = state_home()
    secure_directory(root)
    registry_path = root / "registry.json"
    with file_lock(root / "registry.lock"):
        if registry_path.is_file():
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WorkflowError(f"invalid central registry: {exc}") from exc
        else:
            registry = {"schema_version": 1, "projects": {}}
        key, digest = project_identity(project)
        registry["projects"][key] = {
            "project_path": str(project),
            "git_common_dir": str(common_git_dir(project)),
            "origin_url": origin_url(project),
            "identity_hash": digest,
            "updated_at": utc_now(),
        }
        atomic_json(registry_path, registry)


def active_run_ids(project: Path) -> list[str]:
    """Return every nonterminal run; no mutable pointer owns project execution."""
    root = runs_directory(project)
    active: list[str] = []
    if not root.is_dir():
        return active
    for child in sorted(root.iterdir()):
        path = child / "workflow.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") not in TERMINAL_STATUSES:
            active.append(str(payload.get("run_id") or child.name))
    return active


def resolve_run_id(project: Path, requested: str | None) -> str:
    if requested:
        return requested
    active = active_run_ids(project)
    if not active:
        raise WorkflowError("no active run; provide --run-id or initialize a workflow")
    if len(active) > 1:
        raise WorkflowError(
            "multiple active runs; provide --run-id. Candidates: " + ", ".join(active)
        )
    return active[0]


def command_lock_path(
    project: Path, project_root: Path, args: argparse.Namespace,
) -> Path:
    """Select the narrowest lock that protects the command's mutable authority."""
    if args.command in {"init", "preflight", "list"}:
        return project_root / "coordination.lock"
    run_id = resolve_run_id(project, getattr(args, "run_id", None))
    run_root = run_directory(project, run_id)
    if not state_path(project, run_id).is_file():
        raise WorkflowError(f"workflow state not found: {state_path(project, run_id)}")
    args.run_id = run_id
    return run_root / "workflow.lock"


def default_budgets() -> dict[str, int]:
    return {
        "max_quality_rounds_per_batch": MAX_REVIEW_ROUNDS,
        "max_review_invocations_per_batch": DEFAULT_MAX_REVIEW_INVOCATIONS_PER_BATCH,
        "max_contract_review_invocations": DEFAULT_MAX_CONTRACT_INVOCATIONS,
        "max_verification_attempts_per_request": DEFAULT_MAX_VERIFICATION_ATTEMPTS,
        "max_verification_cycles_per_round": DEFAULT_MAX_VERIFICATION_CYCLES_PER_ROUND,
    }


def default_usage() -> dict[str, Any]:
    return {
        "contract_review_invocations": 0,
        "review_invocations_by_batch": {},
        "verification_attempts": 0,
        "verification_cycles_by_round": {},
    }


def validate_legacy_state_integrity(state: dict[str, Any], schema: int) -> None:
    """Authenticate a checkpointed legacy state before translating its shape."""
    if not state.get("events"):
        raise WorkflowError(
            f"legacy schema {schema} has no verifiable checkpoint; automatic migration is refused"
        )
    validate_event_chain(state)
    validate_state_checkpoint(state)
    validate_artifact_manifests(state)


def load_state(project: Path, requested: str | None = None) -> dict[str, Any]:
    run_id = resolve_run_id(project, requested)
    path = state_path(project, run_id)
    recover_interrupted_state_save(path)
    if not path.is_file():
        raise WorkflowError(f"workflow state not found: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"invalid workflow state: {path}: {exc}") from exc
    loaded_schema = state.get("schema_version")
    if (
        Path(state.get("project", "")).resolve() != project
        or state.get("run_id") != run_id
        or Path(state.get("run_directory", "")).resolve() != path.parent.resolve()
    ):
        raise WorkflowError("workflow state identity does not match the requested project/run")
    if loaded_schema in {2, 3, 4, 5, 6}:
        validate_legacy_state_integrity(state, int(loaded_schema))
        state["schema_version"] = SCHEMA_VERSION
        state["_requires_migration"] = True
        state.setdefault("finding_ledger", {})
        state.setdefault("batch_convergence", {})
        state.setdefault("contract_reviews", [])
        state.setdefault("acceptance_contract", {
            "status": "LEGACY_IMPLICIT", "criteria": [], "digest": None,
        })
        state.setdefault("decisions", [])
        state.setdefault("verification_requests", [])
        state.setdefault("verification_evidence", [])
        state.setdefault("verification_worktrees", [])
        state.setdefault("review_invocations", [])
        state.setdefault("contract_review_invocations", [])
        state.setdefault("verification_attempts", [])
        state.setdefault("events", [])
        state.setdefault("budgets", default_budgets())
        state.setdefault("usage", default_usage())
        state.setdefault("pending_decision", None)
        state.setdefault("extra_review_rounds_granted", {})
        state.setdefault("extra_review_invocations_granted", {})
        state.setdefault("extra_verification_attempts_granted", {})
        state.setdefault("host_network_authorizations", {})
        state.setdefault("final_verification", None)
        state.setdefault("integration_transaction", None)
        state.setdefault("integration", {
            "status": "PENDING", "target_branch": state.get("target_branch"),
            "baseline_sha": state.get("baseline_sha"), "attempts": [],
        })
        if "environment_contract" not in state:
            state["environment_contract"] = project_environment_contract(project)
        state.setdefault("implementer", "legacy-unknown")
        state.setdefault("reviewer_history", [{
            "reviewer": state.get("reviewer", "legacy-unknown"),
            "selected_at": state.get("created_at"), "source": "LEGACY_STATE",
        }])
        state.setdefault("migration", {
            "from_schema": loaded_schema,
            "mode": "LEGACY_COMPATIBILITY",
            "loaded_at": utc_now(),
        })
        snapshot_value = state.get("plan_snapshot")
        if isinstance(snapshot_value, str) and Path(snapshot_value).is_file():
            observed_snapshot_digest = sha256_file(Path(snapshot_value))
            legacy_plan_digest = state.get("plan_digest")
            if isinstance(legacy_plan_digest, str) and observed_snapshot_digest != legacy_plan_digest:
                state["_migration_snapshot_mismatch"] = {
                    "expected": legacy_plan_digest, "actual": observed_snapshot_digest,
                }
            else:
                state.setdefault("plan_snapshot_digest", observed_snapshot_digest)
    elif state.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(
            f"run {run_id} uses unsupported schema {state.get('schema_version')}; "
            "the previous skill version cannot be resumed automatically"
        )
    if not state.get("_requires_migration"):
        validate_event_chain(state)
        validate_state_checkpoint(state)
        validate_artifact_manifests(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    if state.get("_requires_migration"):
        raise WorkflowError("legacy run requires explicit migrate --apply before mutation")
    state["updated_at"] = utc_now()
    append_event(state, "STATE_CHECKPOINT", {"state_digest": state_authority_digest(state)})
    pending_events = copy.deepcopy(state.get("_pending_event_payloads", []))
    intended_state = copy.deepcopy(state)
    intended_state.pop("_pending_event_payloads", None)
    transaction = {"state": intended_state, "events": pending_events}
    transaction["transaction_digest"] = sha256_bytes(
        json.dumps(transaction, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    run_root = Path(state["run_directory"])
    transaction_path = run_root / "state_save_transaction.json"
    atomic_json(transaction_path, transaction)
    for item in pending_events:
        atomic_json(Path(item["path"]), item["event"])
    atomic_json(run_root / "workflow.json", intended_state)
    transaction_path.unlink()
    state.pop("_pending_event_payloads", None)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_clean(path: Path, label: str) -> None:
    dirty = git(path, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise WorkflowError(f"{label} worktree is not clean:\n{dirty}")


def git_operation_markers(path: Path) -> list[str]:
    git_dir_raw = git(path, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = path / git_dir
    markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
    return [name for name in markers if (git_dir / name).exists()]


def ensure_no_git_operation(path: Path) -> None:
    active = git_operation_markers(path)
    if active:
        raise WorkflowError(f"{path} has an in-progress Git operation: {', '.join(active)}")


def registered_worktrees(project: Path) -> list[dict[str, str | None]]:
    """Parse porcelain records exactly; branch-name prefixes must never match each other."""
    records: list[dict[str, str | None]] = []
    current: dict[str, str | None] | None = None
    for line in git(project, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if current:
                records.append(current)
            current = {"path": line[len("worktree "):], "branch": None}
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[len("branch "):]
    if current:
        records.append(current)
    return records


def parse_batches(value: str) -> list[str]:
    batches = [item.strip() for item in value.split(",") if item.strip()]
    if not batches or len(set(batches)) != len(batches):
        raise WorkflowError("batch identifiers must be nonempty and unique")
    for batch in batches:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", batch):
            raise WorkflowError(f"unsafe batch identifier: {batch!r}")
    return batches


def current_batch(state: dict[str, Any]) -> str | None:
    accepted = set(state["accepted_batches"])
    return next((item for item in state["batches"] if item not in accepted), None)


def upstream_info(project: Path, target_branch: str) -> dict[str, Any]:
    upstream_result = run(
        ("git", "-C", str(project), "rev-parse", "--abbrev-ref", f"{target_branch}@{{upstream}}"),
        check=False,
    )
    if upstream_result.returncode != 0:
        return {"upstream": None, "upstream_sha": None, "ahead": None, "behind": None}
    upstream = upstream_result.stdout.strip()
    upstream_sha = git(project, "rev-parse", upstream)
    counts = git(project, "rev-list", "--left-right", "--count", f"{target_branch}...{upstream}")
    ahead_text, behind_text = counts.split()
    return {
        "upstream": upstream,
        "upstream_sha": upstream_sha,
        "ahead": int(ahead_text),
        "behind": int(behind_text),
    }


def preflight_payload(project: Path, target_branch: str) -> dict[str, Any]:
    target_sha = git(project, "rev-parse", target_branch)
    current_branch = git(project, "branch", "--show-current")
    dirty = git(project, "status", "--porcelain", "--untracked-files=all").splitlines()
    return {
        "status": "PREFLIGHT",
        "project": str(project),
        "project_key": project_identity(project)[0],
        "state_home": str(state_home()),
        "current_branch": current_branch or None,
        "current_sha": git(project, "rev-parse", "HEAD"),
        "target_branch": target_branch,
        "target_sha": target_sha,
        "working_tree_clean": not dirty,
        "working_tree_changes": dirty,
        "controller_runtime": controller_runtime(),
        "project_runtime": project_runtime_payload(project),
        **upstream_info(project, target_branch),
    }


def validate_plan_unchanged(state: dict[str, Any]) -> None:
    snapshot = Path(state["plan_snapshot"])
    expected = state.get("plan_snapshot_digest") or state.get("plan_digest")
    if not snapshot.is_file() or not isinstance(expected, str) or sha256_file(snapshot) != expected:
        raise WorkflowError(
            "the authoritative plan snapshot changed or disappeared after initialization: "
            f"restore {snapshot} to its frozen content, or supersede this run"
        )
    manifest_value = state.get("batch_manifest_snapshot")
    manifest_digest = state.get("batch_manifest_snapshot_digest")
    if manifest_value is not None:
        manifest = Path(str(manifest_value))
        if (
            not manifest.is_file()
            or not isinstance(manifest_digest, str)
            or sha256_file(manifest) != manifest_digest
        ):
            raise WorkflowError("the authoritative batch manifest changed or disappeared after initialization")


def _environment_file_digest(
    path: Path, observed: os.stat_result, deadline: float, relative: str,
) -> str:
    """Hash one already-lstat'd file without following a replacement symlink."""
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise WorkflowError(f"cannot open project .venv entry {relative}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise WorkflowError(f"project .venv entry changed before fingerprint: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise WorkflowError("project .venv fingerprint exceeded its time budget")
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != observed.st_size
            or after.st_mtime_ns != observed.st_mtime_ns
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(observed.st_mode)
        ):
            raise WorkflowError(f"project .venv entry changed during fingerprint: {relative}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def project_environment_contract(project: Path, *, include_content: bool = True) -> dict[str, Any]:
    """Fingerprint a project venv without executing code or following symlink targets."""
    root = project / ".venv"
    launcher = root / "bin" / "python"
    if root.is_symlink():
        raise WorkflowError("project .venv must not be a symlink")
    if not os.path.lexists(launcher):
        payload: dict[str, Any] = {
            "status": "ABSENT", "path": str(root.absolute()),
            "metadata_sha256": None, "content_sha256": None,
            "file_count": 0, "byte_count": 0, "symlink_count": 0,
            "external_runtime_policy": "HOST_TRUST_ROOT_NOT_FINGERPRINTED",
        }
    else:
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise WorkflowError(f"cannot inspect project .venv: {exc}") from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise WorkflowError("project .venv must be a real directory")
        metadata = hashlib.sha256()
        content = hashlib.sha256() if include_content else None
        file_count = 0
        byte_count = 0
        symlink_count = 0
        started = time.monotonic()
        deadline = started + MAX_ENVIRONMENT_SCAN_SECONDS
        pyvenv_digest: str | None = None
        def walk_error(exc: OSError) -> None:
            raise WorkflowError(f"cannot walk project .venv: {exc}") from exc

        for current_text, directories, files in os.walk(
            root, followlinks=False, onerror=walk_error,
        ):
            current = Path(current_text)
            directories[:] = sorted(
                name for name in directories
                if not (current / name).is_symlink()
            )
            try:
                symlinks = [item.name for item in current.iterdir() if item.is_symlink()]
            except OSError as exc:
                raise WorkflowError(f"cannot walk project .venv: {exc}") from exc
            entries = sorted(set(directories + files + symlinks))
            for name in entries:
                path = current / name
                relative = path.relative_to(root).as_posix()
                if time.monotonic() - started > MAX_ENVIRONMENT_SCAN_SECONDS:
                    raise WorkflowError("project .venv fingerprint exceeded its time budget")
                try:
                    observed = path.lstat()
                except OSError as exc:
                    raise WorkflowError(f"cannot inspect project .venv entry {relative}: {exc}") from exc
                mode = stat.S_IMODE(observed.st_mode)
                prefix = relative.encode("utf-8", errors="surrogateescape") + b"\0"
                if stat.S_ISLNK(observed.st_mode):
                    target = os.readlink(path)
                    record = (
                        prefix + b"L\0" + str(mode).encode() + b"\0"
                        + target.encode("utf-8", errors="surrogateescape") + b"\0"
                    )
                    metadata.update(record)
                    if content is not None:
                        content.update(record)
                    file_count += 1
                    symlink_count += 1
                elif stat.S_ISREG(observed.st_mode):
                    size = observed.st_size
                    if file_count + 1 > MAX_ENVIRONMENT_FILES or byte_count + size > MAX_ENVIRONMENT_BYTES:
                        raise WorkflowError(
                            "project .venv fingerprint exceeded its file or byte budget: "
                            f"{file_count + 1} files, {byte_count + size} bytes"
                        )
                    record = (
                        prefix + b"F\0" + str(mode).encode() + b"\0"
                        + str(size).encode() + b"\0" + str(observed.st_mtime_ns).encode() + b"\0"
                    )
                    metadata.update(record)
                    if content is not None:
                        digest = _environment_file_digest(path, observed, deadline, relative)
                        content.update(record + digest.encode() + b"\0")
                        if relative == "pyvenv.cfg":
                            pyvenv_digest = digest
                    file_count += 1
                    byte_count += size
                elif stat.S_ISDIR(observed.st_mode):
                    record = prefix + b"D\0" + str(mode).encode() + b"\0"
                    metadata.update(record)
                    if content is not None:
                        content.update(record)
                else:
                    raise WorkflowError(
                        f"project .venv contains unsupported special file: {relative}"
                    )
                if file_count > MAX_ENVIRONMENT_FILES or byte_count > MAX_ENVIRONMENT_BYTES:
                    raise WorkflowError(
                        "project .venv fingerprint exceeded its file or byte budget: "
                        f"{file_count} files, {byte_count} bytes"
                    )
        payload = {
            "status": "PRESENT", "path": str(root.absolute()),
            "launcher": str(launcher.absolute()),
            "pyvenv_sha256": pyvenv_digest,
            # Package metadata is already covered as ordinary in-root content. Do not perform a
            # second glob pass: a symlinked parent could make globbing cross the venv boundary.
            "packages": [], "metadata_sha256": metadata.hexdigest(),
            "content_sha256": content.hexdigest() if content is not None else None,
            "file_count": file_count, "byte_count": byte_count, "symlink_count": symlink_count,
            "probe_policy": "STATIC_FILESYSTEM_ONLY",
            "external_runtime_policy": "HOST_TRUST_ROOT_NOT_FINGERPRINTED",
        }
    payload["fingerprint_version"] = ENVIRONMENT_FINGERPRINT_VERSION
    payload["validation_level"] = "FULL_CONTENT" if include_content else "METADATA_ONLY"
    payload["digest"] = sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    return payload


def validate_environment_contract(
    project: Path, state: dict[str, Any], *, full: bool = False,
) -> None:
    expected = state.get("environment_contract")
    if isinstance(expected, dict):
        if expected.get("fingerprint_version") != ENVIRONMENT_FINGERPRINT_VERSION:
            raise WorkflowError("environment fingerprint requires explicit schema migration")
        current = project_environment_contract(project, include_content=full)
        key = "digest" if full else "metadata_sha256"
        if current.get(key) != expected.get(key):
            raise EnvironmentDriftError(
                "shared project environment drifted from the frozen fingerprint; "
                "restore it or supersede and initialize a new run"
            )


def environment_status(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    recorded = state.get("environment_contract")
    current = project_environment_contract(project, include_content=False)
    recorded_digest = recorded.get("metadata_sha256") if isinstance(recorded, dict) else None
    return {
        "recorded_digest": recorded_digest,
        "current_digest": current.get("metadata_sha256"),
        "recorded_status": recorded.get("status") if isinstance(recorded, dict) else None,
        "current_status": current.get("status"),
        "drifted": bool(
            isinstance(recorded, dict)
            and (
                recorded.get("status") != current.get("status")
                or recorded_digest != current.get("metadata_sha256")
            )
        ),
        "probe_policy": current.get("probe_policy", "STATIC_FILESYSTEM_ONLY"),
        "validation_level": "METADATA_ONLY",
        "full_content_required_before_evidence": True,
    }


def validate_batch_manifest(path: Path, batches: list[str]) -> None:
    if not path.is_file():
        raise WorkflowError(f"batch manifest does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise WorkflowError("batch manifest must not be empty")
    missing = [
        batch for batch in batches
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(batch)}(?![A-Za-z0-9])", text)
    ]
    if missing:
        raise WorkflowError("batch manifest does not declare every --batches ID: " + ", ".join(missing))


def copy_batch_manifest_context(state: dict[str, Any], destination: Path) -> None:
    snapshot_value = state.get("batch_manifest_snapshot")
    if isinstance(snapshot_value, str) and Path(snapshot_value).is_file():
        shutil.copyfile(Path(snapshot_value), destination)
    else:
        destination.write_text(
            "# Legacy batch scope unavailable\n\n"
            f"Declared batch identifiers: {', '.join(state['batches'])}\n\n"
            "This run predates the frozen batch-manifest contract. No authoritative mapping from "
            "batch identifiers to plan scope is available. Do not infer that the run covers the "
            "entire plan. Use an adjudicated acceptance contract when present; otherwise return "
            "NEEDS_USER_DECISION if batch-scoped acceptance depends on the missing boundary.\n",
            encoding="utf-8",
        )
    destination.chmod(0o600)


def target_staleness(project: Path, state: dict[str, Any]) -> tuple[bool, str]:
    current = git(project, "rev-parse", state["target_branch"])
    return current != state["baseline_sha"], current


def validate_active_state(project: Path, state: dict[str, Any]) -> None:
    if state.get("_requires_migration"):
        raise WorkflowError("legacy run requires explicit migrate --apply before mutation")
    if state["status"] in TERMINAL_STATUSES:
        raise WorkflowError(f"run is terminal: {state['status']}")
    validate_controller_runtime(state)
    validate_environment_contract(project, state)


def validate_implementation(state: dict[str, Any]) -> Path:
    implementation = Path(state["implementation_worktree"])
    if not implementation.is_dir():
        raise WorkflowError(f"implementation worktree is missing: {implementation}")
    branch = git(implementation, "branch", "--show-current")
    if branch != state["implementation_branch"]:
        raise WorkflowError(
            f"expected implementation branch {state['implementation_branch']!r}, found {branch!r}"
        )
    ensure_no_git_operation(implementation)
    return implementation


def command_preflight(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    target = args.target_branch or git(project, "branch", "--show-current")
    if not target:
        raise WorkflowError("detached HEAD requires --target-branch")
    emit(preflight_payload(project, target))


def command_list(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    runs_root = runs_directory(project)
    entries: list[dict[str, Any]] = []
    if runs_root.is_dir():
        for child in sorted(runs_root.iterdir(), reverse=True):
            path = child / "workflow.json"
            if not path.is_file():
                continue
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                entries.append(
                    {
                        "run_id": state.get("run_id", child.name),
                        "status": state.get("status", "UNKNOWN"),
                        "target_branch": state.get("target_branch"),
                        "baseline_sha": state.get("baseline_sha"),
                        "plan_snapshot": state.get("plan_snapshot"),
                        "created_at": state.get("created_at"),
                    }
                )
            except json.JSONDecodeError:
                entries.append({"run_id": child.name, "status": "CORRUPT"})
    active = active_run_ids(project)
    emit(
        {
            "status": "RUN_LIST",
            "project": str(project),
            "project_key": project_identity(project)[0],
            "active_run": active[0] if len(active) == 1 else None,
            "active_runs": active,
            "runs": entries,
        }
    )


def command_init(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    plan = Path(args.plan).expanduser().resolve()
    if not plan.is_file():
        raise WorkflowError(f"plan file does not exist: {plan}")
    batch_manifest = Path(args.batch_manifest).expanduser().resolve()
    if not shutil.which(args.reviewer):
        raise WorkflowError(f"reviewer CLI is not available on PATH: {args.reviewer}")
    runtime = reviewer_runtime(args, args.reviewer)
    ensure_no_git_operation(project)
    ensure_clean(project, "original project")
    target = args.target_branch or git(project, "branch", "--show-current")
    if not target:
        raise WorkflowError("detached HEAD requires --target-branch")
    baseline = git(project, "rev-parse", target)

    batches = parse_batches(args.batches)
    validate_batch_manifest(batch_manifest, batches)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{timestamp}-{slug(plan.stem, 32)}-{secrets.token_hex(3)}"
    run_root = run_directory(project, run_id)
    implementation = run_root / "worktrees" / "implementation"
    reviewer_path = run_root / "worktrees" / "reviewer"
    plan_dir = run_root / "plan"
    for directory in (
        run_root,
        plan_dir,
        run_root / "worktrees",
        run_root / "reviews",
        run_root / "logs",
        run_root / "tests",
        run_root / "contracts",
        run_root / "decisions",
    ):
        secure_directory(directory)
    snapshot = plan_dir / "original.md"
    manifest_snapshot = plan_dir / "batches.md"
    shutil.copy2(plan, snapshot)
    shutil.copy2(batch_manifest, manifest_snapshot)
    snapshot.chmod(0o600)
    manifest_snapshot.chmod(0o600)
    branch = f"workflow/{slug(plan.stem, 28)}-{timestamp}-{secrets.token_hex(2)}"

    try:
        git(project, "worktree", "add", "-b", branch, str(implementation), baseline)
        git(project, "worktree", "add", "--detach", str(reviewer_path), baseline)
    except Exception:
        # Keep any successfully registered worktree visible for explicit diagnosis/cleanup.
        raise

    current_branch = git(project, "branch", "--show-current")
    current_head = git(project, "rev-parse", "HEAD")
    before = preflight_payload(project, target)
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "AWAITING_CONTRACT_REVIEW",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "project": str(project),
        "project_key": project_identity(project)[0],
        "run_directory": str(run_root),
        "implementation_worktree": str(implementation),
        "reviewer_worktree": str(reviewer_path),
        "plan_original": str(plan),
        "plan_snapshot": str(snapshot),
        "plan_digest": sha256_file(plan),
        "plan_snapshot_digest": sha256_file(snapshot),
        "batch_manifest_original": str(batch_manifest),
        "batch_manifest_snapshot": str(manifest_snapshot),
        "batch_manifest_digest": sha256_file(batch_manifest),
        "batch_manifest_snapshot_digest": sha256_file(manifest_snapshot),
        "reviewer": args.reviewer,
        "reviewer_runtime": runtime,
        "controller_runtime": controller_runtime(),
        "project_runtime_at_start": before["project_runtime"],
        "environment_contract": project_environment_contract(project),
        "reviewer_history": [{
            "reviewer": args.reviewer, "selected_at": utc_now(), "source": "RUN_INITIALIZED",
            "actor": args.implementer,
        }],
        "implementer": args.implementer,
        "fix_policy": args.fix_policy,
        "target_branch": target,
        "implementation_branch": branch,
        "baseline_sha": baseline,
        "upstream_at_start": before["upstream"],
        "upstream_sha_at_start": before["upstream_sha"],
        "original_branch_at_start": current_branch or None,
        "original_head_at_start": current_head,
        "batches": batches,
        "accepted_batches": [],
        "accepted_shas": {},
        "reviews": [],
        "finding_ledger": {},
        "batch_convergence": {},
        "contract_reviews": [],
        "acceptance_contract": None,
        "decisions": [],
        "verification_requests": [],
        "verification_evidence": [],
        "verification_worktrees": [],
        "review_invocations": [],
        "contract_review_invocations": [],
        "verification_attempts": [],
        "events": [],
        "budgets": default_budgets(),
        "usage": default_usage(),
        "pending_decision": None,
        "extra_review_rounds_granted": {},
        "extra_review_invocations_granted": {},
        "extra_verification_attempts_granted": {},
        "host_network_authorizations": {},
        "final_verification": None,
        "integration_transaction": None,
        "integration": {
            "status": "PENDING", "target_branch": target,
            "baseline_sha": baseline, "attempts": [],
        },
        "final_sha": None,
        "finalized": False,
        "worktrees_removed": False,
    }
    append_event(state, "RUN_CREATED", {
        "baseline_sha": baseline,
        "plan_snapshot_sha256": state["plan_snapshot_digest"],
        "batch_manifest_snapshot_sha256": state["batch_manifest_snapshot_digest"],
        "batches": batches,
        "budgets": state["budgets"],
        "controller_runtime": state["controller_runtime"],
        "project_runtime_at_start": state["project_runtime_at_start"],
    })
    save_state(state)
    project_meta = {
        "schema_version": 1,
        "project": str(project),
        "project_key": state["project_key"],
        "git_common_dir": str(common_git_dir(project)),
        "origin_url": origin_url(project),
        "updated_at": utc_now(),
    }
    atomic_json(project_directory(project) / "project.json", project_meta)
    write_registry(project)
    emit(
        {
            "status": "AWAITING_CONTRACT_REVIEW",
            "run_id": run_id,
            "project_key": state["project_key"],
            "run_directory": str(run_root),
            "implementation_worktree": str(implementation),
            "reviewer_worktree": str(reviewer_path),
            "implementation_branch": branch,
            "target_branch": target,
            "baseline_sha": baseline,
            "original_branch_unchanged": git(project, "branch", "--show-current") == current_branch,
            "original_head_unchanged": git(project, "rev-parse", "HEAD") == current_head,
            "plan_digest": state["plan_digest"],
            "batch_manifest_digest": state["batch_manifest_digest"],
            "batches": batches,
            "next_batch": batches[0],
            "reviewer_runtime": runtime,
            "controller_runtime": state["controller_runtime"],
            "project_runtime_at_start": state["project_runtime_at_start"],
        }
    )


def build_prompt(
    state: dict[str, Any],
    batch: str,
    round_number: int,
    base: str,
    head: str,
    plan_path: Path,
    batch_manifest_path: Path,
    assignment_path: Path,
    ledger_path: Path,
    legacy_path: Path | None,
    evidence_path: Path | None,
    contract_path: Path,
) -> str:
    contract = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    cumulative_final_review = batch == state["batches"][-1]
    round_scope = (
        "This is round 1: perform the complete discovery review for this batch."
        if round_number == 1
        else (
            "This is a bounded re-review. Verify prior findings, inspect regressions introduced by "
            "the fixes, and report only genuinely new P0/P1 findings that were introduced by a fix "
            "or were previously masked. Do not restart an unlimited architecture review."
        )
    )
    return (
        f"{contract}\n\n## Review assignment\n\n"
        f"- Repository worktree: `{state['reviewer_worktree']}`\n"
        f"- Plan snapshot: `{plan_path}`\n"
        f"- Authoritative run scope and batch manifest: `{batch_manifest_path}`\n"
        f"- Assignment metadata and user decisions: `{assignment_path}`\n"
        f"- Run ID: `{state['run_id']}`\n"
        f"- Batch: `{batch}`\n"
        f"- Cumulative final review: `{'yes' if cumulative_final_review else 'no'}`\n"
        f"- Review round: `{round_number}`\n"
        f"- Base SHA: `{base}`\n"
        f"- Head SHA: `{head}`\n"
        f"- Diff range: `{base}..{head}`\n\n"
        f"- Finding ledger: `{ledger_path}`\n"
        f"- Acceptance contract: `{contract_path}`\n"
        + (f"- Legacy recovery findings: `{legacy_path}`\n" if legacy_path else "")
        + (f"- Fixed-SHA verification evidence: `{evidence_path}`\n" if evidence_path else "")
        + "\n"
        f"{round_scope}\n\n"
        "Read the plan snapshot, frozen batch manifest, assignment decisions, and repository "
        "instructions. Treat the manifest as authoritative for this run's included/excluded scope "
        "and batch mapping. Do not silently expand the run to the rest of the plan; if the declared "
        "boundary is unsound, return NEEDS_USER_DECISION. Review the declared batch and all "
        "affected consumers. Treat the supplied ledger as the authority for resolvable IDs. In a "
        "legacy recovery round, the explicitly supplied legacy recovery file is an additional "
        "authority, and every listed blocking ID must be either resolved or reported again. When "
        "both sources are empty, resolved_finding_ids must be empty even if historical IDs appear "
        "in source comments or other text. Reuse the existing finding ID and fingerprint for the same defect. "
        "List verified prior IDs in resolved_finding_ids. Return JSON matching the supplied schema, "
        "and return exactly one criterion_results entry for every acceptance-contract criterion "
        + ("across all batches because this is the cumulative final review. " if cumulative_final_review else "in this batch. ")
        + "Set reviewer, reviewed_sha, base_sha, and batch exactly as assigned. Do not wrap JSON in Markdown.\n"
    )


def prepare_review_context(
    state: dict[str, Any], batch: str, round_number: int, base: str, head: str,
    invocation_id: str,
) -> tuple[Path, Path, Path, Path, Path, Path | None, Path | None, Path]:
    """Create the minimal explicit context granted to the reviewer CLI."""
    context = (
        Path(state["run_directory"])
        / "review_context"
        / f"batch_{slug(batch)}"
        / f"round_{round_number:02d}"
        / slug(invocation_id)
    )
    secure_directory(context)
    plan_path = context / "plan.md"
    batch_manifest_path = context / "batch_manifest.md"
    ledger_path = context / "finding_ledger.json"
    assignment_path = context / "assignment.json"
    contract_path = context / "acceptance_contract.json"
    legacy_path: Path | None = None
    evidence_path: Path | None = None
    shutil.copyfile(Path(state["plan_snapshot"]), plan_path)
    plan_path.chmod(0o600)
    copy_batch_manifest_context(state, batch_manifest_path)
    atomic_json(ledger_path, state.get("finding_ledger", {}))
    atomic_json(contract_path, state["acceptance_contract"])
    atomic_json(
        assignment_path,
        {
            "run_id": state["run_id"],
            "batch": batch,
            "round": round_number,
            "base_sha": base,
            "reviewed_sha": head,
            "user_decisions": state.get("decisions", []),
        },
    )
    legacy_ids = legacy_recovery_open_ids(state, batch)
    if legacy_ids and legacy_recovery_round_allowed(state, batch, round_number):
        legacy_path = context / "legacy_recovery_findings.json"
        atomic_json(
            legacy_path,
            {
                "batch": batch,
                "blocking_finding_ids": legacy_ids,
                "rule": "Each ID must be resolved or reported again in this recovery review.",
            },
        )
    evidence = [
        copy.deepcopy(item) for item in state.get("verification_evidence", [])
        if item.get("batch") == batch and item.get("reviewed_sha") == head
    ]
    rejected = [
        item for item in state.get("verification_requests", [])
        if item.get("batch") == batch and item.get("reviewed_sha") == head and item.get("status") == "REJECTED"
    ]
    if evidence or rejected:
        evidence_dir = context / "evidence"
        secure_directory(evidence_dir)
        for item in evidence:
            evidence_id = item.get("evidence_id") or slug(item.get("request_key", "legacy"))
            for field in ("stdout_path", "stderr_path", "evidence_path"):
                raw = item.get(field)
                source = Path(raw) if isinstance(raw, str) else None
                if source is not None and source.is_file():
                    destination = evidence_dir / f"{slug(str(evidence_id))}-{source.name}"
                    shutil.copyfile(source, destination)
                    destination.chmod(0o600)
                    item[field] = str(destination)
        evidence_path = context / "verification_evidence.json"
        atomic_json(evidence_path, {"evidence": evidence, "rejected_requests": rejected})
    return (
        context, plan_path, batch_manifest_path, assignment_path, ledger_path,
        legacy_path, evidence_path, contract_path,
    )


def reviewer_command(
    reviewer: str,
    reviewer_path: Path,
    context_dir: Path,
    schema_path: Path,
    raw_path: Path,
    prompt: str,
    schema: dict[str, Any] = REVIEW_SCHEMA,
    runtime: dict[str, Any] | None = None,
) -> list[str]:
    if reviewer == "dsh":
        # dsh-headless has no structured-output flag, so the schema travels IN the prompt and the
        # host parses the printed final message. DSH_PERMISSION_MODE=read-only is set inline via
        # `env` because the implementation reviewer is not bubblewrap-wrapped (matching the
        # existing claude/codex implementation-reviewer isolation, which relies on CLI flags).
        schema_text = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        dsh_prompt = (
            prompt
            + "\n\nOUTPUT CONTRACT. Working directory for all file writes is the current working "
            + "directory. PERSIST YOUR REVIEW INCREMENTALLY — never rely on a single final "
            + "message, which may be lost. Use the Bash tool as you go:\n"
            + "1. As soon as you FINISH judging one acceptance-contract criterion, APPEND one JSON "
            + "object per line to `.review-out/criteria.jsonl` with exactly the keys `criterion_id`, "
            + "`status`, `evidence_ids`, `rationale` (printf '%s\\n' ... >> .review-out/criteria.jsonl, "
            + "or `cat >>` with one object per line). Never rewrite the file; append only.\n"
            + "2. As soon as you CONFIRM a finding, append its complete finding object to "
            + "`.review-out/findings.jsonl`, one per line (append only).\n"
            + "3. Only after every criterion and finding above is persisted, write "
            + "`.review-out/meta.json` with `{\"verdict\": ..., \"summary\": ..., "
            + "\"resolved_finding_ids\": [...], \"verification_requests\": [...]}` (empty arrays "
            + "when none).\n"
            + "4. Then write the COMPLETE assembled review object (the full schema below) to "
            + "`.review-out/review.json`.\n"
            + "5. Your FINAL assistant message must be ONLY that same complete JSON object — no "
            + "prose, no fences. If anything stops you before step 5, the files from steps 1-4 "
            + "already carry the review; the task fails only if neither the files nor the message "
            + "delivers it. Verify JSON is complete and valid before stopping.\n"
            + schema_text
        )
        return ["env", "DSH_PERMISSION_MODE=workspace-write", "dsh", "--profile", "headless", dsh_prompt]
    if reviewer == "codex":
        runtime = runtime or {}
        selection: list[str] = []
        if runtime.get("profile"):
            selection.extend(["-p", str(runtime["profile"])])
        if runtime.get("model"):
            selection.extend(["-m", str(runtime["model"])])
        if runtime.get("model_provider"):
            selection.extend(["-c", f'model_provider={json.dumps(runtime["model_provider"])}'])
        return [
            "codex", "-a", "never", "exec", "--ephemeral", "-s", "read-only",
            *selection,
            "-C", str(reviewer_path), "--add-dir", str(context_dir),
            "--output-schema", str(schema_path), "-o", str(raw_path), prompt,
        ]
    allowed = ",".join(
        [
            "Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)",
            "Bash(git show *)", "Bash(git status *)", "Bash(git grep *)", "Bash(rg *)",
        ]
    )
    return [
        "claude", "-p", *(["--model", str((runtime or {}).get("model"))]
                            if (runtime or {}).get("model") else []),
        "--output-format", "json", "--json-schema",
        json.dumps(schema, separators=(",", ":")), "--permission-mode", "dontAsk",
        "--allowedTools", allowed, "--disallowedTools", "Edit,Write,NotebookEdit",
        "--add-dir", str(context_dir), "--no-session-persistence", prompt,
    ]


def extract_review(reviewer: str, stdout: str, raw_path: Path) -> dict[str, Any]:
    if reviewer == "dsh":
        text = stdout.strip()
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
        raise WorkflowError("dsh output did not contain a structured review result")
    if reviewer == "codex":
        source = raw_path.read_text(encoding="utf-8") if raw_path.is_file() else stdout
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"Codex returned invalid structured output: {exc}") from exc
        return payload
    try:
        wrapper = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Claude returned invalid JSON wrapper: {exc}") from exc
    structured = wrapper.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = wrapper.get("result")
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"Claude result was not structured JSON: {exc}") from exc
        if isinstance(payload, dict):
            return payload
    raise WorkflowError("Claude output did not contain a structured review result")


def _read_json_objects(path: Path) -> list[dict[str, Any]]:
    """Read a file of JSON objects (one per line, or adjacent), tolerantly.

    The reviewer writes incrementally; tolerate pretty-printed or multi-line objects by
    decoding consecutive JSON values and skipping stray non-JSON fragments.
    """
    if not path.is_file():
        return []
    decoder = json.JSONDecoder()
    text = path.read_text(encoding="utf-8", errors="replace")
    items: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            newline = text.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        if isinstance(obj, dict):
            items.append(obj)
        index = end
    return items


def read_dsh_review(reviewer_path: Path) -> dict[str, Any] | None:
    """Read the review the dsh reviewer wrote to its ``.review-out`` channel.

    The reviewer persists its work INCREMENTALLY (.review-out/criteria.jsonl and
    findings.jsonl) so a stopped, truncated, or timed-out run does not lose what it had
    already decided, then writes the assembled ``review.json`` (and ``meta.json``) when
    complete. The complete object wins when present; otherwise the fragments are assembled
    into a review payload with whatever was recorded before the run stopped.
    """
    out = Path(reviewer_path) / ".review-out"
    if not out.is_dir():
        return None
    final = out / "review.json"
    if final.is_file():
        try:
            payload = json.loads(final.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
    criteria = _read_json_objects(out / "criteria.jsonl")
    findings = _read_json_objects(out / "findings.jsonl")
    if not criteria and not findings:
        return None
    try:
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = None
    payload = dict(meta) if isinstance(meta, dict) else {}
    payload["criterion_results"] = criteria
    payload["findings"] = findings
    return payload


def contract_digest(criteria: list[dict[str, Any]]) -> str:
    encoded = json.dumps(criteria, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_contract_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for source in criteria:
        if not isinstance(source, dict):
            raise ReviewContractError("contract criteria must contain objects")
        criterion = dict(source)
        criterion.setdefault("evidence_kind", "REPOSITORY_ASSERTION")
        criterion.setdefault("argv", [])
        criterion.setdefault("expected_exit", 0)
        normalized.append(criterion)
    return normalized


def bare_interpreter_name(executable: str) -> bool:
    """True when executable is a bare python/pypy name (no path separators).

    The verification sandbox resolves a bare interpreter through its system PATH to an
    interpreter that never sees the project environment, so a COMMAND criterion relying on a
    project dependency is guaranteed to fail with a module-missing error. A `.venv/`-relative
    launcher is the sanctioned bridge (mounted read-only by _sandbox_command) and an explicit
    absolute interpreter path is the operator's declared choice; only the ambiguous bare form is
    refused at entry.
    """
    return bool(re.fullmatch(r"(?:python|python[23](?:\.[0-9]+)*|pypy[0-9]*)", executable))


def non_venv_python(executable: str) -> bool:
    """True when the executable's basename is a python/pypy interpreter NOT under the project .venv.

    Used by verification to recognize the documented environment trap: a python interpreter with
    none of the project's dependencies. `.venv/` launchers are exempt because the sandbox mounts
    that environment read-only.
    """
    return normalized_venv_executable(executable) is None and bool(
        re.fullmatch(r"(?:python|python[23](?:\.[0-9]+)*|pypy[0-9]*)", Path(executable).name)
    )


def normalized_venv_executable(executable: str) -> str | None:
    """Return the canonical project-relative `.venv` path, without resolving symlinks.

    Verification accepts argv, not shell text. Lexical normalization therefore closes spelling
    variants such as `./.venv/bin/python` without following the launcher out of the environment.
    Absolute paths and relative paths that normalize outside `.venv` are not venv launchers.
    """
    path = Path(executable)
    if path.is_absolute():
        return None
    normalized = Path(os.path.normpath(executable))
    if normalized == Path(".venv") or not normalized.is_relative_to(Path(".venv")):
        return None
    return normalized.as_posix()


def reject_bare_interpreter(executable: str) -> None:
    if not bare_interpreter_name(executable):
        return
    raise ReviewContractError(
        f"executable {executable!r} is a bare interpreter that the verification sandbox resolves "
        "to a system interpreter without the project environment. Use `.venv/bin/python ...` "
        "(resolved from the original project and mounted read-only) or an explicit absolute "
        "runtime path."
    )


def validate_contract_payload(payload: dict[str, Any], state: dict[str, Any]) -> None:
    if payload.get("reviewer") != state["reviewer"]:
        raise ReviewContractError("contract reviewer identity mismatch")
    if payload.get("baseline_sha") != state["baseline_sha"]:
        raise ReviewContractError("contract review baseline SHA mismatch")
    if payload.get("assessment") not in {"READY", "NEEDS_USER_DECISION"}:
        raise ReviewContractError("invalid contract assessment")
    criteria = payload.get("criteria")
    issues = payload.get("issues")
    if not isinstance(criteria, list) or not isinstance(issues, list):
        raise ReviewContractError("contract criteria and issues must be lists")
    criteria[:] = normalize_contract_criteria(criteria)
    ids: set[str] = set()
    required = {"id", "batch", "source", "observation", "expected_result", "scope", "rationale"}
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict) or not required.issubset(criterion):
            raise ReviewContractError(f"contract criterion {index} is incomplete")
        if not isinstance(criterion["id"], str) or criterion["id"] in ids or not criterion["id"].strip():
            raise ReviewContractError(f"duplicate or empty contract criterion ID: {criterion['id']!r}")
        ids.add(criterion["id"])
        if criterion["batch"] not in state["batches"]:
            raise ReviewContractError(f"contract criterion uses unknown batch: {criterion['batch']}")
        if not isinstance(criterion["observation"], str) or not isinstance(criterion["expected_result"], str) or not criterion["observation"].strip() or not criterion["expected_result"].strip():
            raise ReviewContractError(f"contract criterion {criterion['id']} is not decidable")
        if not isinstance(criterion["scope"], list) or not criterion["scope"]:
            raise ReviewContractError(f"contract criterion {criterion['id']} has no explicit scope")
        if not all(isinstance(item, str) and item.strip() for item in criterion["scope"]):
            raise ReviewContractError(f"contract criterion {criterion['id']} has invalid scope entries")
        if criterion["evidence_kind"] not in {"COMMAND", "REPOSITORY_ASSERTION", "USER_BOUNDARY"}:
            raise ReviewContractError(f"contract criterion {criterion['id']} has invalid evidence_kind")
        if not isinstance(criterion["argv"], list) or not all(
            isinstance(item, str) and item for item in criterion["argv"]
        ):
            raise ReviewContractError(f"contract criterion {criterion['id']} has invalid argv")
        if criterion["evidence_kind"] == "COMMAND" and not criterion["argv"]:
            raise ReviewContractError(f"COMMAND criterion {criterion['id']} requires argv")
        if criterion["evidence_kind"] == "COMMAND":
            reject_bare_interpreter(criterion["argv"][0])
        if not isinstance(criterion["expected_exit"], int) or not 0 <= criterion["expected_exit"] <= 255:
            raise ReviewContractError(f"contract criterion {criterion['id']} has invalid expected_exit")
    if payload["assessment"] == "READY" and (issues or not criteria):
        raise ReviewContractError("READY contract requires criteria and no unresolved issues")
    if payload["assessment"] == "READY":
        boundary_ids = [
            criterion["id"] for criterion in criteria
            if criterion.get("evidence_kind") == "USER_BOUNDARY"
        ]
        if boundary_ids:
            raise ReviewContractError(
                "READY contract cannot contain unresolved USER_BOUNDARY criteria: "
                + ", ".join(boundary_ids)
            )
        covered = {criterion["batch"] for criterion in criteria}
        missing = sorted(set(state["batches"]) - covered)
        if missing:
            raise ReviewContractError(
                "READY contract does not cover every batch: " + ", ".join(missing)
            )
    if payload["assessment"] == "NEEDS_USER_DECISION" and not issues:
        raise ReviewContractError("NEEDS_USER_DECISION contract requires at least one issue")


def command_contract_review(args: argparse.Namespace) -> None:
    validate_portable_review_schema()
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if state.get("status") != "AWAITING_CONTRACT_REVIEW":
        raise WorkflowError(f"contract-review is not allowed from workflow status {state.get('status')}")
    validate_plan_unchanged(state)
    if state.get("acceptance_contract") and state["acceptance_contract"].get("status") in {"READY", "ADJUDICATED", "LEGACY_IMPLICIT"}:
        emit({"status": "CONTRACT_ALREADY_READY", "run_id": state["run_id"], "acceptance_contract": state["acceptance_contract"]})
    reviewer_path = Path(state["reviewer_worktree"])
    ensure_clean(reviewer_path, "reviewer")
    git(reviewer_path, "switch", "--detach", state["baseline_sha"])
    if (
        not args.dry_run
        and state["usage"]["contract_review_invocations"]
        >= state["budgets"]["max_contract_review_invocations"]
    ):
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": f"contract-budget-{len(state['decisions']) + 1:03d}",
            "type": "CONTRACT_REVIEW_BUDGET_EXHAUSTED",
            "allowed_choices": ["ADJUDICATE_CONTRACT", "ABORT_RUN"],
            "created_at": utc_now(),
        }
        append_event(state, "DECISION_REQUIRED", state["pending_decision"])
        save_state(state)
        emit({"status": "NEEDS_USER_DECISION", "reason": "CONTRACT_REVIEW_BUDGET_EXHAUSTED", "pending_decision": state["pending_decision"]}, 3)
    if args.dry_run:
        invocation_id = "preview"
        root = Path(state["run_directory"]) / "contracts" / f"preview_{secrets.token_hex(3)}"
        invocation: dict[str, Any] = {}
    else:
        invocation_id, root, invocation = next_invocation(state, "contract")
    context = root / "context"
    secure_directory(root)
    secure_directory(context)
    plan_copy = context / "plan.md"
    batch_manifest_copy = context / "batch_manifest.md"
    shutil.copyfile(Path(state["plan_snapshot"]), plan_copy)
    plan_copy.chmod(0o600)
    copy_batch_manifest_context(state, batch_manifest_copy)
    prompt = (
        CONTRACT_PROMPT_TEMPLATE.read_text(encoding="utf-8")
        + "\n\n## Contract review assignment\n\n"
        + f"- Plan snapshot: `{plan_copy}`\n- Baseline SHA: `{state['baseline_sha']}`\n"
        + f"- Authoritative run scope and batch manifest: `{batch_manifest_copy}`\n"
        + f"- Batches: `{','.join(state['batches'])}`\n"
        + "Treat the frozen manifest as authoritative for this run's included/excluded scope and "
        + "batch mapping. Do not silently reinterpret this run as covering the whole plan. If the "
        + "manifest conflicts with an indivisible requirement or omits a boundary needed for a "
        + "decidable criterion, return NEEDS_USER_DECISION and propose the change.\n"
        + "Return only JSON matching the supplied schema.\n"
    )
    prompt_path = root / "prompt.md"
    raw_path = root / "raw.json"
    report_path = root / "report.json"
    stderr_path = root / "stderr.log"
    schema_path = root / "schema.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    # Bind `batch` to the declared identifiers for THIS run. Left as a free string, a reviewer can
    # answer "B01,B02,B03,B04" for one criterion -- which the validator then rejects, after the
    # call has already been paid for. An enum makes the structured-output backend refuse the
    # malformed value while the answer is still being generated.
    contract_schema = copy.deepcopy(CONTRACT_SCHEMA)
    for section in ("criteria", "issues"):
        contract_schema["properties"][section]["items"]["properties"]["batch"] = {
            "type": "string", "enum": list(state["batches"]),
        }
    atomic_json(schema_path, contract_schema)
    command = reviewer_command(
        state["reviewer"], reviewer_path, context, schema_path, raw_path, prompt,
        contract_schema, state.get("reviewer_runtime"))
    if args.dry_run:
        emit({"status": "CONTRACT_REVIEW_DRY_RUN", "run_id": state["run_id"], "command": command[:-1] + ["<PROMPT>"], "prompt_path": str(prompt_path)})
    try:
        result = run(command, cwd=reviewer_path, check=False, timeout=args.timeout)
    except WorkflowError as exc:
        invocation.update({"status": "INFRA_ERROR", "error": str(exc), "completed_at": utc_now()})
        append_event(state, "INVOCATION_INFRA_ERROR", {"invocation_id": invocation_id, "error": str(exc)})
        save_state(state)
        emit({"status": "REVIEWER_ERROR", "reason": str(exc), "run_id": state["run_id"]}, 4)
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if state["reviewer"] == "claude":
        raw_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        invocation.update({"status": "INFRA_ERROR", "returncode": result.returncode, "completed_at": utc_now()})
        append_event(state, "INVOCATION_INFRA_ERROR", {"invocation_id": invocation_id, "returncode": result.returncode})
        save_state(state)
        emit({"status": "REVIEWER_ERROR", "reason": "CLI_EXIT_NONZERO", "returncode": result.returncode, "run_id": state["run_id"], "stderr_path": str(stderr_path)}, 4)
    try:
        payload = extract_review(state["reviewer"], result.stdout, raw_path)
        validate_contract_payload(payload, state)
    except WorkflowError as exc:
        invocation.update({"status": "CONTRACT_ERROR", "error": str(exc), "completed_at": utc_now()})
        append_event(state, "INVOCATION_CONTRACT_ERROR", {"invocation_id": invocation_id, "error": str(exc)})
        save_state(state)
        emit({"status": "REVIEWER_ERROR", "reason": str(exc), "run_id": state["run_id"], "raw_output_path": str(raw_path)}, 4)
    document = {**payload, "contract_digest": contract_digest(payload["criteria"])}
    atomic_json(report_path, document)
    invocation.update({
        "status": "VALID", "assessment": payload["assessment"], "completed_at": utc_now(),
        "artifacts": artifact_manifest([
            prompt_path, raw_path, stderr_path, report_path,
            *sorted(path for path in context.rglob("*") if path.is_file()),
        ]),
    })
    state["contract_reviews"].append({
        "assessment": payload["assessment"], "report_path": str(report_path),
        "baseline_sha": state["baseline_sha"], "invocation_id": invocation_id,
        "reviewer": state["reviewer"],
    })
    if payload["assessment"] == "READY":
        state["acceptance_contract"] = {"status": "READY", "criteria": payload["criteria"], "digest": document["contract_digest"], "source_report": str(report_path)}
        state["status"] = "IMPLEMENTING"
        status = "CONTRACT_READY"
        code = 0
    else:
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": f"contract-{invocation_id}", "type": "CONTRACT_BOUNDARY",
            "allowed_choices": ["ADJUDICATE_CONTRACT", "ABORT_RUN"],
            "created_at": utc_now(), "issues": payload["issues"],
        }
        status = "NEEDS_USER_DECISION"
        code = 3
    append_event(state, "CONTRACT_REVIEW_COMPLETED", {
        "invocation_id": invocation_id, "assessment": payload["assessment"],
        "contract_digest": document["contract_digest"],
    })
    save_state(state)
    emit({"status": status, "run_id": state["run_id"], "assessment": payload["assessment"], "summary": payload["summary"], "criteria": payload["criteria"], "issues": payload["issues"], "report_path": str(report_path)}, code)


def command_contract_adjudicate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if (
        state["status"] != "NEEDS_USER_DECISION"
        or state.get("acceptance_contract") is not None
        or (state.get("pending_decision") or {}).get("type") not in {
            "CONTRACT_BOUNDARY", "CONTRACT_REVIEW_BUDGET_EXHAUSTED"
        }
    ):
        raise WorkflowError("contract adjudication requires a pending contract decision")
    decision_path = Path(args.decision_file).expanduser().resolve()
    payload = read_json_object(decision_path, "contract decision file")
    criteria = payload.get("criteria")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError("contract adjudication requires a nonempty reason")
    synthetic = {"reviewer": state["reviewer"], "baseline_sha": state["baseline_sha"], "assessment": "READY", "summary": str(reason or ""), "criteria": criteria, "issues": []}
    validate_contract_payload(synthetic, state)
    decision = {"id": f"decision-{len(state['decisions']) + 1:03d}", "type": "CONTRACT_ADJUDICATION", "reason": reason, "criteria": criteria, "baseline_sha": state["baseline_sha"], "created_at": utc_now()}
    if not args.apply:
        emit({"status": "CONTRACT_ADJUDICATION_PREVIEW", "run_id": state["run_id"], "decision": decision})
    decision_file = Path(state["run_directory"]) / "decisions" / f"{decision['id']}.json"
    atomic_json(decision_file, decision)
    state["decisions"].append({**decision, "path": str(decision_file)})
    state["acceptance_contract"] = {"status": "ADJUDICATED", "criteria": criteria, "digest": contract_digest(criteria), "decision_path": str(decision_file)}
    state["status"] = "IMPLEMENTING"
    state["pending_decision"] = None
    append_event(state, "CONTRACT_ADJUDICATED", {
        "decision_id": decision["id"], "actor": payload.get("actor", "user"),
        "contract_digest": state["acceptance_contract"]["digest"],
    })
    save_state(state)
    emit({"status": "CONTRACT_ADJUDICATED", "run_id": state["run_id"], "acceptance_contract": state["acceptance_contract"]})


def validate_review_payload(
    payload: dict[str, Any], reviewer: str, batch: str, base: str, head: str,
    state: dict[str, Any] | None = None,
) -> None:
    for field, expected in {
        "reviewer": reviewer,
        "batch": batch,
        "base_sha": base,
        "reviewed_sha": head,
    }.items():
        if payload.get(field) != expected:
            raise WorkflowError(
                f"review result {field} mismatch: expected {expected!r}, got {payload.get(field)!r}"
            )
    if payload.get("verdict") not in {"PASS", "FAIL", "NEEDS_USER_DECISION", "NEEDS_VERIFICATION"}:
        raise WorkflowError(f"invalid review verdict: {payload.get('verdict')!r}")
    findings = payload.get("findings")
    required = {
        "id", "fingerprint", "severity", "novelty", "why_not_detectable_earlier",
        "introduced_by_sha", "severity_change_justification", "location", "trigger",
        "consequence", "required_outcome",
    }
    if not isinstance(findings, list):
        raise WorkflowError("review findings must be a list")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or not required.issubset(finding):
            raise WorkflowError(f"finding {index} is missing required fields")
        if finding["severity"] not in {"P0", "P1", "P2"}:
            raise WorkflowError(f"finding {index} has invalid severity")
        if not finding["fingerprint"].strip():
            raise WorkflowError(f"finding {index} has an empty fingerprint")
        if finding["novelty"] == "PREVIOUSLY_MASKED" and not finding[
            "why_not_detectable_earlier"
        ].strip():
            raise WorkflowError(f"finding {index} claims PREVIOUSLY_MASKED without explanation")
        if finding["novelty"] == "INTRODUCED_BY_FIX" and not re.fullmatch(
            r"[0-9a-f]{7,40}", finding["introduced_by_sha"]
        ):
            raise WorkflowError(f"finding {index} lacks a valid introduced_by_sha")
    resolved = payload.get("resolved_finding_ids")
    if not isinstance(resolved, list) or not all(isinstance(item, str) for item in resolved):
        raise WorkflowError("resolved_finding_ids must be a list of strings")
    if len(resolved) != len(set(resolved)):
        raise WorkflowError("resolved_finding_ids must not contain duplicates")
    requests = payload.get("verification_requests")
    if not isinstance(requests, list):
        raise WorkflowError("verification_requests must be a list")
    request_ids: set[str] = set()
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            raise WorkflowError(f"verification request {index} must be an object")
        if not isinstance(request.get("id"), str) or not request["id"].strip():
            raise WorkflowError(f"verification request {index} has no ID")
        if request["id"] in request_ids:
            raise WorkflowError(f"duplicate verification request ID: {request['id']}")
        request_ids.add(request["id"])
        argv = request.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise WorkflowError(f"verification request {request['id']} has invalid argv")
        reject_bare_interpreter(argv[0])
        if (
            request.get("cwd") != "repository"
            or not isinstance(request.get("expected_exit"), int)
            or not 0 <= request["expected_exit"] <= 255
        ):
            raise WorkflowError(f"verification request {request['id']} has invalid execution contract")
    if payload["verdict"] == "NEEDS_VERIFICATION" and not requests:
        raise WorkflowError("NEEDS_VERIFICATION requires at least one verification request")
    if payload["verdict"] != "NEEDS_VERIFICATION" and requests:
        raise WorkflowError("verification requests require verdict NEEDS_VERIFICATION")
    results = payload.get("criterion_results")
    if not isinstance(results, list):
        raise WorkflowError("criterion_results must be a list")
    result_ids: set[str] = set()
    for index, criterion_result in enumerate(results):
        if not isinstance(criterion_result, dict):
            raise WorkflowError(f"criterion result {index} must be an object")
        criterion_id = criterion_result.get("criterion_id")
        if not isinstance(criterion_id, str) or not criterion_id or criterion_id in result_ids:
            raise WorkflowError(f"criterion result {index} has duplicate or empty ID")
        result_ids.add(criterion_id)
        if criterion_result.get("status") not in {"PASS", "FAIL", "NOT_APPLICABLE"}:
            raise WorkflowError(f"criterion result {criterion_id} has invalid status")
        if not isinstance(criterion_result.get("evidence_ids"), list):
            raise WorkflowError(f"criterion result {criterion_id} has invalid evidence_ids")
    if state is not None and payload["verdict"] != "NEEDS_VERIFICATION":
        cumulative = bool(state.get("batches")) and batch == state["batches"][-1]
        criteria = [
            item for item in state.get("acceptance_contract", {}).get("criteria", [])
            if cumulative or item.get("batch") == batch
        ]
        expected_ids = {item["id"] for item in criteria}
        if result_ids != expected_ids:
            missing = sorted(expected_ids - result_ids)
            extra = sorted(result_ids - expected_ids)
            raise WorkflowError(f"criterion result coverage mismatch; missing={missing}, extra={extra}")
        by_id = {item["criterion_id"]: item for item in results}
        if payload["verdict"] == "PASS" and any(
            by_id[criterion_id]["status"] != "PASS" for criterion_id in expected_ids
        ):
            raise WorkflowError("PASS requires every batch criterion to PASS")
        evidence_by_id = {
            item.get("evidence_id"): item
            for item in state.get("verification_evidence", [])
            if item.get("reviewed_sha") == head and item.get("status") == "PASS"
        }
        for criterion in criteria:
            if criterion.get("evidence_kind") != "COMMAND":
                continue
            refs = by_id[criterion["id"]].get("evidence_ids", [])
            if not refs or any(
                ref not in evidence_by_id
                or evidence_by_id[ref].get("criterion_id") != criterion["id"]
                for ref in refs
            ):
                raise WorkflowError(
                    f"COMMAND criterion {criterion['id']} lacks current-SHA PASS evidence"
                )


def legacy_untracked_finding_evidence(
    state: dict[str, Any], batch: str
) -> dict[str, list[str]]:
    """Return IDs proved by pre-ledger formal reports without inventing ledger entries."""
    run_value = state.get("run_directory")
    if not isinstance(run_value, str):
        return {}
    reviews_root = (Path(run_value) / "reviews").resolve()
    ledger_ids = {
        item.get("id")
        for item in state.get("finding_ledger", {}).values()
        if item.get("batch") == batch
    }
    evidence: dict[str, list[str]] = {}
    for review in state.get("reviews", []):
        if review.get("batch") != batch:
            continue
        raw_path = review.get("report_path")
        if not isinstance(raw_path, str):
            continue
        report_path = Path(raw_path).resolve()
        if not report_path.is_relative_to(reviews_root) or not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or report.get("batch") != batch:
            continue
        # Policy-bearing reports were created after ledger tracking existed. Only
        # older formal reports qualify for this narrowly scoped compatibility path.
        if "policy" in report or "effective_verdict" in report:
            continue
        findings = report.get("findings")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            finding_id = finding.get("id") if isinstance(finding, dict) else None
            if isinstance(finding_id, str) and finding_id and finding_id not in ledger_ids:
                evidence.setdefault(finding_id, []).append(str(report_path))
    return evidence


def legacy_recovery_round_allowed(
    state: dict[str, Any], batch: str, round_number: int
) -> bool:
    """Allow one extra valid round only for a batch stranded before ledger tracking."""
    prior_count = sum(1 for item in state.get("reviews", []) if item.get("batch") == batch)
    return (
        round_number == MAX_REVIEW_ROUNDS + 1
        and prior_count == MAX_REVIEW_ROUNDS
        and bool(legacy_untracked_finding_evidence(state, batch))
    )


def legacy_recovery_open_ids(state: dict[str, Any], batch: str) -> list[str]:
    """Return blocking IDs from the latest same-batch pre-policy formal report."""
    run_value = state.get("run_directory")
    if not isinstance(run_value, str):
        return []
    reviews_root = (Path(run_value) / "reviews").resolve()
    for review in reversed(state.get("reviews", [])):
        if review.get("batch") != batch or not isinstance(review.get("report_path"), str):
            continue
        report_path = Path(review["report_path"]).resolve()
        if not report_path.is_relative_to(reviews_root) or not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(report, dict)
            or report.get("batch") != batch
            or "policy" in report
            or "effective_verdict" in report
        ):
            continue
        findings = report.get("findings")
        if not isinstance(findings, list):
            continue
        return sorted(
            {
                finding["id"]
                for finding in findings
                if isinstance(finding, dict)
                and isinstance(finding.get("id"), str)
                and finding.get("severity") in {"P0", "P1"}
            }
        )
    return []


def _normalized_text(value: Any) -> str:
    """Whitespace- and case-insensitive form used for deterministic obligation comparison."""
    return " ".join(str(value or "").split()).casefold()


def _obligation_paths(location: Any) -> frozenset[str]:
    """File paths named by a finding location, ignoring line numbers.

    Line numbers move with every fix commit, so comparing them would report drift for an
    obligation that never changed. The set of files a defect lives in is the stable part.
    """
    return frozenset(
        match.group(1)
        for match in re.finditer(r"([\w./\\-]+\.[A-Za-z0-9_]+)(?::\d+)?", str(location or ""))
    )


def apply_convergence_policy(
    state: dict[str, Any], payload: dict[str, Any], batch: str, round_number: int, head: str
) -> dict[str, Any]:
    """Update the finding ledger and derive the effective, bounded verdict."""
    ledger: dict[str, dict[str, Any]] = copy.deepcopy(state.get("finding_ledger", {}))
    convergence_by_batch: dict[str, dict[str, Any]] = copy.deepcopy(
        state.get("batch_convergence", {})
    )
    batch_items = {fp: item for fp, item in ledger.items() if item["batch"] == batch}
    id_to_fp = {item["id"]: fp for fp, item in batch_items.items()}
    all_id_to_fp = {item["id"]: fp for fp, item in ledger.items()}
    legacy_evidence = legacy_untracked_finding_evidence(state, batch)
    seen: set[str] = set()
    decision_reasons: list[str] = []
    warnings: list[dict[str, Any]] = []

    if legacy_recovery_round_allowed(state, batch, round_number):
        required_legacy_ids = set(legacy_recovery_open_ids(state, batch))
        accounted_ids = set(payload["resolved_finding_ids"]) | {
            finding["id"] for finding in payload["findings"]
        }
        omitted_ids = sorted(required_legacy_ids - accounted_ids)
        if omitted_ids:
            raise ReviewContractError(
                "legacy recovery omitted prior blocking finding IDs: "
                + ", ".join(omitted_ids)
            )

    for finding_id in payload["resolved_finding_ids"]:
        fingerprint = id_to_fp.get(finding_id)
        if not fingerprint:
            other_fingerprint = all_id_to_fp.get(finding_id)
            if other_fingerprint:
                # The cumulative final review's base is the run baseline, so it is required to
                # judge every batch -- and will therefore confirm fixes that an earlier,
                # already-accepted batch owns. Rejecting the whole payload for that returned no
                # verdict at all and burned 49.8 minutes across two adapters. Record it and leave
                # authority with the owning batch: confirming another batch's fix is not this
                # review's transition to make.
                warnings.append(
                    {
                        "code": "CROSS_BATCH_RESOLUTION",
                        "finding_id": finding_id,
                        "owning_batch": ledger[other_fingerprint]["batch"],
                    }
                )
                continue
            sources = legacy_evidence.get(finding_id)
            if sources:
                warnings.append(
                    {
                        "code": "LEGACY_UNTRACKED_RESOLUTION",
                        "finding_id": finding_id,
                        "source_reports": sources,
                    }
                )
                continue
            raise ReviewContractError(f"resolved finding is unknown in this batch: {finding_id}")
        item = ledger[fingerprint]
        # OPEN is the ordinary case. DEFERRED is admitted because a non-blocking finding the host
        # fixed anyway is the GOOD outcome, and refusing to record it made a deterministic stall:
        # the reviewer, reading its own prior report, correctly names the P2 it just verified as
        # fixed, and the whole payload is then rejected -- every round, with no way for either
        # side to break the loop. VERIFIED is idempotent for the same reason: confirming a fix a
        # second time is not an error to fail a review over.
        if item["status"] == "VERIFIED":
            warnings.append({"code": "ALREADY_VERIFIED_RESOLUTION", "finding_id": finding_id})
            continue
        if item["status"] not in {"OPEN", "DEFERRED"}:
            raise ReviewContractError(
                f"resolved finding is {item['status'].lower()}, not open or deferred: {finding_id}")
        item["resolved_from"] = item["status"]
        item["status"] = "VERIFIED"
        item["resolved_round"] = round_number
        item["resolved_sha"] = head

    for finding in payload["findings"]:
        fingerprint = finding["fingerprint"]
        if fingerprint in seen:
            raise ReviewContractError(f"duplicate finding fingerprint in one review: {fingerprint}")
        seen.add(fingerprint)
        existing = ledger.get(fingerprint)
        if finding["id"] in payload["resolved_finding_ids"]:
            raise ReviewContractError(f"finding cannot be resolved and present in the same review: {finding['id']}")
        if not existing and finding["id"] in id_to_fp:
            raise ReviewContractError(
                f"finding ID {finding['id']} is already bound to fingerprint {id_to_fp[finding['id']]}"
            )
        owner_fingerprint = fingerprint if existing else all_id_to_fp.get(finding["id"])
        owner = ledger.get(owner_fingerprint) if owner_fingerprint else None
        if owner is not None and owner["batch"] != batch:
            # Same reason as CROSS_BATCH_RESOLUTION: the reviewer re-observed a defect an earlier
            # batch owns. Record the observation, leave that batch's status, severity and blocking
            # flag alone, and route only a catastrophic one to the user. P0 always blocks; a P1 or
            # P2 outside this batch's authority is a warning, not a reason to stall the run.
            owner["last_seen_round"] = round_number
            owner["last_seen_sha"] = head
            owner["occurrences"] = owner.get("occurrences", 1) + 1
            warnings.append(
                {
                    "code": "CROSS_BATCH_FINDING",
                    "finding_id": finding["id"],
                    "fingerprint": fingerprint,
                    "owning_batch": owner["batch"],
                    "severity": finding["severity"],
                }
            )
            if finding["severity"] == "P0":
                decision_reasons.append(f"CROSS_BATCH_MATERIAL_FINDING:{finding['id']}")
            continue
        if existing:
            if existing["id"] != finding["id"]:
                raise ReviewContractError(
                    f"finding changed stable ID for {fingerprint}: {existing['id']} -> {finding['id']}"
                )
            if existing["status"] == "VERIFIED":
                existing["status"] = "OPEN" if finding["severity"] in {"P0", "P1"} else "DEFERRED"
                existing["reopened_round"] = round_number
            old_severity = existing["severity"]
            new_severity = finding["severity"]
            if old_severity != new_severity:
                if not finding["severity_change_justification"].strip():
                    raise ReviewContractError(
                        f"severity changed without justification for {finding['id']}: "
                        f"{old_severity} -> {new_severity}"
                    )
                if old_severity == "P2" and new_severity in {"P0", "P1"}:
                    decision_reasons.append(f"SEVERITY_UPGRADE:{finding['id']}")
            # `required_outcome` is what "fixed" means for this finding, and it used to be
            # overwritten silently every round. Measured on five stuck findings, one ID absorbed a
            # whole class of defects: F-B05-002 was restated at five locations across three files
            # over five rounds, each restatement looking narrow on its own. The host satisfied the
            # named site, the obligation moved, `occurrences` grew, and the finding never closed --
            # slipping past the round-2+ rule that would otherwise have deferred a late
            # INITIAL_REVIEW discovery. Line numbers are excluded from the comparison because they
            # move with every commit; the files a defect lives in are the stable part.
            previous_paths = _obligation_paths(existing.get("location"))
            current_paths = _obligation_paths(finding["location"])
            obligation_changed = (
                _normalized_text(existing.get("required_outcome"))
                != _normalized_text(finding["required_outcome"])
                or previous_paths != current_paths
            )
            revisions = list(existing.get("obligation_revisions", []))
            if obligation_changed:
                revisions.append(
                    {
                        "round": round_number,
                        "from_required_outcome": existing.get("required_outcome"),
                        "to_required_outcome": finding["required_outcome"],
                        "from_paths": sorted(previous_paths),
                        "to_paths": sorted(current_paths),
                    }
                )
                # Only a round that moves the obligation again can force the decision. A finding
                # whose obligation has stabilised must be allowed to converge.
                if len(revisions) >= 2:
                    decision_reasons.append(f"OBLIGATION_DRIFT:{finding['id']}")
            existing.setdefault("original_required_outcome", existing.get("required_outcome"))
            existing.setdefault("original_location", existing.get("location"))
            existing.update(
                {
                    "severity": new_severity,
                    "location": finding["location"],
                    "trigger": finding["trigger"],
                    "consequence": finding["consequence"],
                    "required_outcome": finding["required_outcome"],
                    "obligation_revisions": revisions,
                    "last_seen_round": round_number,
                    "last_seen_sha": head,
                    "occurrences": existing.get("occurrences", 1) + 1,
                    "severity_change_justification": finding["severity_change_justification"],
                }
            )
            if new_severity == "P0":
                # P0 is categorically blocking. In particular, a finding previously deferred as
                # P1 must not retain DEFERRED merely because its old status predates the upgrade.
                existing["status"] = "OPEN"
            elif new_severity == "P2":
                existing["status"] = "DEFERRED"
            elif existing["status"] != "DEFERRED":
                existing["status"] = "OPEN"
            continue

        severity = finding["severity"]
        novelty = finding["novelty"]
        if severity == "P0":
            blocking = True
        elif round_number == 1:
            blocking = severity == "P1" and novelty not in {"PRE_EXISTING", "UNRELATED"}
        elif severity == "P1":
            blocking = novelty in {"INTRODUCED_BY_FIX", "PREVIOUSLY_MASKED"}
        else:
            blocking = False
        deferred_reason = None
        if not blocking:
            if severity == "P2":
                deferred_reason = "NON_BLOCKING_P2"
            elif novelty in {"PRE_EXISTING", "UNRELATED"}:
                deferred_reason = novelty
            elif round_number > 1:
                deferred_reason = "LATE_NONQUALIFYING_DISCOVERY"
        ledger[fingerprint] = {
            **finding,
            "batch": batch,
            "status": "OPEN" if blocking else "DEFERRED",
            "blocking": blocking,
            "deferred_reason": deferred_reason,
            "first_seen_round": round_number,
            "first_seen_sha": head,
            "last_seen_round": round_number,
            "last_seen_sha": head,
            "occurrences": 1,
        }

    open_blocking = sorted(
        fp
        for fp, item in ledger.items()
        if item["batch"] == batch and item["status"] == "OPEN" and item["severity"] in {"P0", "P1"}
    )
    convergence = convergence_by_batch.setdefault(
        batch, {"last_blocking_count": None, "no_progress_streak": 0, "history": []}
    )
    previous_count = convergence["last_blocking_count"]
    if previous_count is not None and len(open_blocking) >= previous_count:
        convergence["no_progress_streak"] += 1
    else:
        convergence["no_progress_streak"] = 0
    convergence["last_blocking_count"] = len(open_blocking)
    convergence["history"].append(
        {
            "round": round_number,
            "reported_verdict": payload["verdict"],
            "blocking_count": len(open_blocking),
            "blocking_fingerprints": open_blocking,
        }
    )

    if payload["verdict"] == "NEEDS_USER_DECISION":
        decision_reasons.append("REVIEWER_REQUESTED_DECISION")
    if open_blocking and round_number >= MAX_REVIEW_ROUNDS:
        decision_reasons.append("FINAL_REVIEW_ROUND_REACHED")
    if open_blocking and convergence["no_progress_streak"] >= 2:
        decision_reasons.append("NO_PROGRESS_FOR_TWO_ROUNDS")
    if decision_reasons:
        effective_verdict = "NEEDS_USER_DECISION"
    elif open_blocking:
        effective_verdict = "FAIL"
    else:
        effective_verdict = "PASS"
    result = {
        "effective_verdict": effective_verdict,
        "reported_verdict": payload["verdict"],
        "blocking_fingerprints": open_blocking,
        "blocking_count": len(open_blocking),
        "no_progress_streak": convergence["no_progress_streak"],
        "decision_reasons": decision_reasons,
        "warnings": warnings,
        "deferred_findings": [
            item
            for item in ledger.values()
            if item["batch"] == batch and item["status"] == "DEFERRED"
        ],
    }
    state["finding_ledger"] = ledger
    state["batch_convergence"] = convergence_by_batch
    return result


def register_verification_requests(
    state: dict[str, Any], payload: dict[str, Any], batch: str, round_number: int,
    base: str, head: str, report_path: Path,
) -> list[dict[str, Any]]:
    registered: list[dict[str, Any]] = []
    existing_keys = {
        (item["batch"], item["reviewed_sha"], item["request"]["id"])
        for item in state.get("verification_requests", [])
        if "request" in item
    }
    for request in payload["verification_requests"]:
        key = (batch, head, request["id"])
        if key in existing_keys:
            raise ReviewContractError(f"duplicate verification request for this SHA: {request['id']}")
        key_digest = hashlib.sha256(request["id"].encode("utf-8")).hexdigest()[:10]
        sequence = len(state.get("verification_requests", [])) + 1
        record = {
            "request_key": f"vr-{slug(batch)}-{sequence:04d}-{key_digest}",
            "batch": batch, "round": round_number, "base_sha": base,
            "reviewed_sha": head, "request": request, "status": "PENDING",
            "requested_at": utc_now(), "report_path": str(report_path),
            "source": "REVIEWER",
        }
        state["verification_requests"].append(record)
        registered.append(record)
    return registered


def register_missing_contract_verification(
    state: dict[str, Any], batch: str, round_number: int, base: str, head: str
) -> list[dict[str, Any]]:
    cumulative = batch == state["batches"][-1]
    criteria = [
        item for item in state.get("acceptance_contract", {}).get("criteria", [])
        if (cumulative or item.get("batch") == batch) and item.get("evidence_kind") == "COMMAND"
    ]
    existing = {
        item.get("criterion_id") for item in state.get("verification_requests", [])
        if item.get("batch") == batch and item.get("reviewed_sha") == head
    }
    registered: list[dict[str, Any]] = []
    for criterion in criteria:
        if criterion["id"] in existing:
            continue
        request = {
            "id": f"criterion-{criterion['id']}", "argv": criterion["argv"],
            "cwd": "repository", "reason": criterion["rationale"],
            "expected_exit": criterion["expected_exit"],
        }
        key_digest = hashlib.sha256(criterion["id"].encode("utf-8")).hexdigest()[:10]
        sequence = len(state.get("verification_requests", [])) + 1
        record = {
            "request_key": f"vr-{slug(batch)}-{sequence:04d}-{key_digest}",
            "criterion_id": criterion["id"], "source": "ACCEPTANCE_CONTRACT",
            "batch": batch, "round": round_number, "base_sha": base,
            "reviewed_sha": head, "request": request, "status": "PENDING",
            "requested_at": utc_now(), "report_path": state["acceptance_contract"].get("source_report"),
        }
        state["verification_requests"].append(record)
        registered.append(record)
    return registered


def register_final_contract_verification(
    state: dict[str, Any], head: str
) -> list[dict[str, Any]]:
    criteria = [
        item for item in state.get("acceptance_contract", {}).get("criteria", [])
        if item.get("evidence_kind") == "COMMAND"
    ]
    registered: list[dict[str, Any]] = []
    for criterion in criteria:
        sequence = len(state.get("verification_requests", [])) + 1
        key_digest = hashlib.sha256(("final:" + criterion["id"]).encode()).hexdigest()[:10]
        request = {
            "id": f"final-{criterion['id']}", "argv": criterion["argv"],
            "cwd": "repository", "reason": f"final cumulative verification: {criterion['rationale']}",
            "expected_exit": criterion["expected_exit"],
        }
        record = {
            "request_key": f"vr-final-{sequence:04d}-{key_digest}",
            "criterion_id": criterion["id"], "source": "FINAL_CONTRACT",
            "batch": "__FINAL__", "round": 0, "base_sha": state["baseline_sha"],
            "reviewed_sha": head, "request": request, "status": "PENDING",
            "requested_at": utc_now(), "report_path": None,
        }
        state["verification_requests"].append(record)
        registered.append(record)
    return registered


def _verification_environment() -> dict[str, str]:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": str(Path.home()),
        "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }
    return environment


def _sandbox_command(
    bwrap: str, command: list[str], project: Path, worktree: Path, temp_dir: Path,
    network_policy: str,
) -> tuple[list[str], list[str]]:
    runtime_mounts: list[str] = []
    executable = command[0]
    venv_executable = normalized_venv_executable(executable)
    relative_venv_executable = venv_executable is not None
    if relative_venv_executable:
        executable_path = (project / venv_executable).resolve()
    elif Path(executable).is_absolute():
        executable_path = Path(executable).resolve()
    else:
        resolved = shutil.which(executable, path="/usr/local/bin:/usr/bin:/bin")
        executable_path = Path(resolved).resolve() if resolved else Path(executable)
    home = Path.home().resolve()
    if relative_venv_executable:
        # A virtualenv launcher must NOT be followed to its target. `.venv/bin/python` is a
        # symlink to the interpreter the venv was built from, so resolving it hands the sandbox a
        # BARE interpreter: sys.prefix becomes the interpreter's own prefix, the venv's
        # site-packages are never on the path, and every `.venv/bin/python -m pytest` criterion
        # fails with "No module named pytest" -- a contract full of unsatisfiable obligations.
        # Mount the venv at its real path and the interpreter tree it points at, both read-only
        # and both narrow, and invoke the launcher itself so it computes its own prefix.
        venv_root = (project / ".venv").resolve()
        launcher = project / venv_executable
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            runtime = project_runtime_payload(project)
            raise WorkflowError(
                f"verification requests {executable!r}, but the original project has no executable "
                f"launcher at {launcher}; Git-ignored virtual environments do not follow worktrees. "
                f"{runtime['guidance']}"
            )
        runtime_mounts.extend(["--ro-bind", str(venv_root), str(venv_root)])
        # Walk the launcher's symlink chain and mount EVERY prefix on it. Mounting only the fully
        # resolved interpreter is not enough: uv points the venv at an unversioned directory
        # symlink (cpython-3.11-… -> cpython-3.11.15-…), so the path the launcher actually names
        # would not exist inside the sandbox and execvp fails with ENOENT.
        mounted: set[str] = {str(venv_root)}
        current = launcher
        for _hop in range(8):
            if not current.is_symlink():
                break
            target = Path(os.readlink(current))
            if not target.is_absolute():
                target = current.parent / target
            target = Path(os.path.abspath(target))
            prefix = target.parent.parent
            resolved_prefix = prefix.resolve()
            system_roots = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))
            resolved_target = target.resolve()
            if any(
                resolved_target == root or resolved_target.is_relative_to(root)
                for root in system_roots
            ):
                current = target
                continue
            if resolved_prefix == Path("/"):
                # `/bin/python` makes parent.parent `/`. The runtime is already supplied by the
                # narrow system mounts below. A non-system target resolving through a directory
                # symlink to `/` is refused rather than exposing the entire host filesystem.
                raise WorkflowError(
                    f"refusing to expose broad runtime prefix {resolved_prefix} to verification"
                )
            if resolved_prefix in {Path("/etc"), Path("/var"), Path("/opt"), Path("/home")}:
                raise WorkflowError(
                    f"refusing to expose broad runtime prefix {resolved_prefix} to verification"
                )
            if resolved_prefix.is_relative_to(home):
                if resolved_prefix == home or resolved_prefix.name in {
                    ".local", ".config", ".cache", ".ssh", ".agents", ".codex",
                }:
                    raise WorkflowError(
                        "refusing to expose a broad or sensitive home runtime to verification"
                    )
            mount_key = f"{resolved_prefix}->{prefix}"
            if mount_key not in mounted and resolved_prefix.exists():
                mounted.add(mount_key)
                # Bind the resolved source at the lexical destination. This preserves a launcher's
                # expected path without letting bwrap resolve a source-directory symlink to a
                # broader tree than the policy inspected.
                runtime_mounts.extend(["--ro-bind", str(resolved_prefix), str(prefix)])
            current = target
        command[0] = str(launcher)
    elif executable_path.is_absolute() and executable_path.is_relative_to(home):
        if executable_path.parent.name != "bin":
            raise WorkflowError(
                "home-scoped verification executables must live in a dedicated runtime bin directory"
            )
        prefix = executable_path.parent.parent
        if prefix == home or prefix.name in {".local", ".config", ".cache", ".ssh", ".agents", ".codex"}:
            raise WorkflowError("refusing to expose a broad or sensitive home runtime to verification")
        runtime_mounts.extend(["--ro-bind", str(prefix), "/opt"])
        command[0] = str(Path("/opt") / executable_path.relative_to(prefix))
    git_dir_raw = git(worktree, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    git_common = common_git_dir(project)
    system_mounts: list[str] = []
    for source in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        if source.exists():
            system_mounts.extend(["--ro-bind", str(source), str(source)])
    etc_mounts: list[str] = ["--dir", "/etc"]
    for source in (
        Path("/etc/ssl"), Path("/etc/ca-certificates"), Path("/etc/localtime"),
        Path("/etc/nsswitch.conf"), Path("/etc/hosts"), Path("/etc/resolv.conf"),
        # A process that cannot look up its own uid cannot run. Python's `pwd.getpwuid` raises
        # KeyError without these, and this repository resolves the real home that way at module
        # import, so every command that touches it dies before main(). Both files are
        # world-readable and carry no secret.
        Path("/etc/passwd"), Path("/etc/group"),
    ):
        if source.exists():
            etc_mounts.extend(["--ro-bind", str(source), str(source)])
    sandbox_home = temp_dir / "home"
    secure_directory(sandbox_home)
    sandboxed = [
        bwrap, "--die-with-parent", "--new-session",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        *system_mounts, *etc_mounts,
        "--ro-bind", str(worktree), "/mnt",
        "--bind", str(temp_dir), "/tmp",
        "--dir", "/home", "--bind", str(sandbox_home), str(home),
        *runtime_mounts,
        # The repository's git metadata at its REAL paths, so `git` simply works from /mnt with no
        # environment override. Ordering matters twice: these come after the $HOME bind, which
        # would otherwise mask anything mounted under it, and the common dir is bound before the
        # worktree gitdir it contains, so the parent cannot shadow the child.
        "--ro-bind", str(git_common), str(git_common),
        "--ro-bind", str(git_dir), str(git_dir),
        "--proc", "/proc", "--dev", "/dev", "--chdir", "/mnt",
    ]
    if network_policy == "offline":
        sandboxed.append("--unshare-net")
    sandboxed.extend(["--clearenv"])
    for key, value in _verification_environment().items():
        sandboxed.extend(["--setenv", key, value])
    # GIT_DIR / GIT_COMMON_DIR / GIT_WORK_TREE are deliberately NOT exported. They are absolute
    # overrides that apply to EVERY git process in the sandbox, so a verification that creates its
    # own throwaway repository -- `git init` in a temporary directory, which this plan's Authority
    # Git and agent-Git batches do constantly -- is redirected into the run's own read-only git
    # directory and dies with "cannot copy ... Read-only file system". Mounting the metadata at its
    # real paths above gives repository access from /mnt without hijacking anything else.
    sandboxed.extend(["--", *command])
    return sandboxed, command


def _directory_usage(path: Path) -> tuple[int, int]:
    total_bytes = 0
    entry_count = 0
    for root, directories, files in os.walk(path):
        entry_count += len(directories)
        if entry_count > MAX_VERIFICATION_TEMP_FILES:
            return total_bytes, entry_count
        for name in files:
            entry_count += 1
            try:
                total_bytes += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass
            if total_bytes > MAX_VERIFICATION_TEMP_BYTES or entry_count > MAX_VERIFICATION_TEMP_FILES:
                return total_bytes, entry_count
    return total_bytes, entry_count


def _process_tree_usage(root_pid: int) -> tuple[int, int]:
    """Return aggregate process count and RSS for a live subprocess tree."""
    records: dict[int, tuple[int, int]] = {}
    page_size = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2:].split()
            records[int(entry.name)] = (int(fields[1]), int(fields[21]) * page_size)
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _) in records.items():
            if pid not in descendants and parent in descendants:
                descendants.add(pid)
                changed = True
    return len(descendants), sum(records.get(pid, (0, 0))[1] for pid in descendants)


def _run_logged(
    command: Sequence[str], stdout_path: Path, stderr_path: Path, timeout: int,
    monitored_directory: Path,
) -> tuple[int, bool, str | None]:
    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        # RLIMIT_NPROC is deliberately NOT set here. It counts processes per UID, not per
        # process tree, so a budget of MAX_VERIFICATION_PROCESSES means "this user may have
        # that many processes ANYWHERE on the machine" -- an editor and a browser already
        # exhaust it, and the child's first clone() fails with EAGAIN, surfacing as
        # "bwrap: Creating new namespace failed: Resource temporarily unavailable". The same
        # verification would then pass on a fresh login and fail with a session open, which
        # is an infrastructure error produced by unrelated activity. The per-tree budget this
        # limit was meant to express is already enforced below, where _process_tree_usage
        # measures the actual descendants against MAX_VERIFICATION_PROCESSES.
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        cpu_seconds = max(1, timeout + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        affinity = os.sched_getaffinity(0)
        if affinity:
            os.sched_setaffinity(0, {min(affinity)})
        os.nice(10)

    timed_out = False
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            list(command), stdout=stdout_handle, stderr=stderr_handle,
            env=_verification_environment(), preexec_fn=limits, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        resource_error: str | None = None
        while process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            used_bytes, used_entries = _directory_usage(monitored_directory)
            if used_bytes > MAX_VERIFICATION_TEMP_BYTES or used_entries > MAX_VERIFICATION_TEMP_FILES:
                resource_error = (
                    "verification temporary-output budget exceeded: "
                    f"{used_bytes} bytes across {used_entries} filesystem entries"
                )
                break
            process_count, memory_bytes = _process_tree_usage(process.pid)
            if process_count > MAX_VERIFICATION_PROCESSES or memory_bytes > MAX_VERIFICATION_MEMORY_BYTES:
                resource_error = (
                    "verification aggregate process budget exceeded: "
                    f"{process_count} processes using {memory_bytes} RSS bytes"
                )
                break
            time.sleep(0.02)
        if timed_out or resource_error:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.wait()
        returncode = int(process.returncode)
    stdout_path.chmod(0o600)
    stderr_path.chmod(0o600)
    return returncode, timed_out, resource_error


def command_verify(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    # CHANGES_REQUESTED is admitted like review (see the review guard below) so that after one
    # request FAILs, the remaining PENDING/APPROVED requests for the same batch/SHA can still be
    # verified instead of leaving the operator with no executable move. A FAILed request itself
    # stays non-retryable; this admission is about its siblings, not a FAIL retry.
    if state.get("status") not in {"AWAITING_VERIFICATION", "FINAL_VERIFICATION_REQUIRED", "IMPLEMENTING", "CHANGES_REQUESTED"}:
        raise WorkflowError(f"verify is not allowed from workflow status {state.get('status')}")
    # Status and ordinary transitions use a bounded metadata observation. Evidence-producing
    # verification additionally proves all in-venv regular-file content before and after execution.
    validate_environment_contract(project, state, full=True)
    matches = [item for item in state.get("verification_requests", []) if item.get("request_key") == args.request_id]
    if len(matches) != 1:
        raise WorkflowError(f"verification request not found: {args.request_id}")
    record = matches[0]
    if record["status"] not in {"PENDING", "APPROVED"}:
        raise WorkflowError(f"verification request is not retryable: {record['status']}")
    if args.reject_reason:
        preview = {"status": "VERIFICATION_REJECTION_PREVIEW", "request_id": args.request_id, "reason": args.reject_reason}
        if not args.apply:
            emit(preview)
        if record.get("source") == "FINAL_CONTRACT":
            state["status"] = "NEEDS_USER_DECISION"
            state["pending_decision"] = {
                "decision_id": f"final-verification-{len(state['decisions']) + 1:03d}",
                "type": "FINAL_VERIFICATION_REJECTED", "request_key": args.request_id,
                "allowed_choices": ["SUPERSEDE_RUN", "ABORT_RUN"],
                "previous_status": "FINAL_VERIFICATION_REQUIRED", "created_at": utc_now(),
                "reason": args.reject_reason,
            }
            append_event(state, "DECISION_REQUIRED", state["pending_decision"])
            save_state(state)
            emit({
                "status": "NEEDS_USER_DECISION", "reason": "FINAL_VERIFICATION_REJECTED",
                "pending_decision": state["pending_decision"],
            }, 3)
        record["status"] = "REJECTED"
        record["rejection_reason"] = args.reject_reason
        record["resolved_at"] = utc_now()
        remaining = [
            item for item in state.get("verification_requests", [])
            if item is not record and item.get("batch") == record.get("batch")
            and item.get("reviewed_sha") == record.get("reviewed_sha")
            and item.get("status") in {"PENDING", "APPROVED", "RUNNING"}
        ]
        state["status"] = "AWAITING_VERIFICATION" if remaining else "IMPLEMENTING"
        append_event(state, "VERIFICATION_REJECTED", {"request_id": args.request_id, "reason": args.reject_reason})
        save_state(state)
        emit({**preview, "status": "VERIFICATION_REQUEST_REJECTED"})
    request = record["request"]
    command = list(request["argv"])
    prior_attempts = [
        item for item in state.get("verification_attempts", [])
        if item.get("request_key") == args.request_id
    ]
    attempt_number = len(prior_attempts) + 1
    maximum = int(state["budgets"]["max_verification_attempts_per_request"]) + int(
        state["extra_verification_attempts_granted"].get(args.request_id, 0)
    )
    host_authorized = args.request_id in state["host_network_authorizations"]
    preview = {
        "status": "VERIFICATION_PREVIEW", "run_id": state["run_id"],
        "request_id": args.request_id, "reviewed_sha": record["reviewed_sha"],
        "argv": command, "expected_exit": request["expected_exit"],
        "attempt": attempt_number, "max_attempts": maximum,
        "isolated_worktree": True, "filesystem_sandbox": "bwrap",
        "network_policy": args.network_policy, "environment_policy": "allowlist",
        "host_network_authorized": host_authorized,
    }
    if attempt_number > maximum:
        allowed = ["GRANT_ONE_VERIFICATION_ATTEMPT", "ABORT_RUN"]
        if record.get("source") != "FINAL_CONTRACT":
            allowed.insert(1, "REJECT_VERIFICATION_REQUEST")
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": f"verification-attempt-{len(state['decisions']) + 1:03d}",
            "type": "VERIFICATION_ATTEMPT_BUDGET_EXHAUSTED",
            "request_key": args.request_id, "allowed_choices": allowed,
            "previous_status": "FINAL_VERIFICATION_REQUIRED" if record.get("source") == "FINAL_CONTRACT" else "AWAITING_VERIFICATION",
            "created_at": utc_now(),
        }
        append_event(state, "DECISION_REQUIRED", state["pending_decision"])
        save_state(state)
        emit({"status": "NEEDS_USER_DECISION", "reason": "VERIFICATION_ATTEMPT_BUDGET_EXHAUSTED", "pending_decision": state["pending_decision"]}, 3)
    if not args.apply:
        emit(preview)
    if args.network_policy == "host" and not host_authorized:
        if args.network_reason and args.actor:
            authorization = {
                "id": f"decision-{len(state['decisions']) + 1:03d}",
                "type": "VERIFICATION_NETWORK_ACCESS", "choice": "AUTHORIZE_HOST_NETWORK",
                "request_key": args.request_id, "reason": args.network_reason,
                "actor": args.actor, "created_at": utc_now(),
            }
            state["host_network_authorizations"][args.request_id] = authorization
            state["decisions"].append(authorization)
            append_event(state, "HOST_NETWORK_AUTHORIZED", authorization)
            save_state(state)
        else:
            previous_status = state.get("status")
            state["status"] = "NEEDS_USER_DECISION"
            state["pending_decision"] = {
                "decision_id": f"network-{len(state['decisions']) + 1:03d}",
                "type": "VERIFICATION_NETWORK_ACCESS", "request_key": args.request_id,
                "allowed_choices": ["AUTHORIZE_HOST_NETWORK", "ABORT_RUN"],
                "previous_status": previous_status, "created_at": utc_now(),
            }
            append_event(state, "DECISION_REQUIRED", state["pending_decision"])
            save_state(state)
            emit({"status": "NEEDS_USER_DECISION", "reason": "HOST_NETWORK_REQUIRES_AUTHORIZATION", "pending_decision": state["pending_decision"]}, 3)
    bwrap = shutil.which("bwrap")
    attempt_root = (
        Path(state["run_directory"]) / "verification_runs" / args.request_id
        / f"attempt_{attempt_number:03d}"
    )
    secure_directory(attempt_root)
    attempt = {
        "attempt_id": f"{args.request_id}-attempt-{attempt_number:03d}",
        "request_key": args.request_id, "attempt": attempt_number,
        "status": "STARTED", "network_policy": args.network_policy,
        "started_at": utc_now(), "directory": str(attempt_root),
    }
    state["verification_attempts"].append(attempt)
    state["usage"]["verification_attempts"] += 1
    record["status"] = "APPROVED"
    append_event(state, "VERIFICATION_ATTEMPT_STARTED", attempt)
    save_state(state)
    if not bwrap:
        attempt.update({"status": "INFRA_ERROR", "error": "bwrap is required", "completed_at": utc_now()})
        record["status"] = "PENDING"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": attempt["error"], "retryable": True}, 4)
    run_root = Path(state["run_directory"])
    worktree = _shared_verification_worktree(run_root, record["reviewed_sha"])
    temp_dir = attempt_root / "tmp"
    logs = attempt_root / "logs"
    secure_directory(temp_dir)
    secure_directory(logs)
    try:
        _ensure_verification_worktree(project, worktree, record["reviewed_sha"])
        if str(worktree) not in state["verification_worktrees"]:
            state["verification_worktrees"].append(str(worktree))
        # Persist the record BEFORE the sandboxed command runs. The append alone lives in memory
        # until some later branch saves, and every one of those branches is reached by RETURNING
        # from _run_logged. A process killed while that command runs -- a tool timeout, an
        # operator stop -- takes the record with it and strands the worktree: four survived the
        # run this line exists because of. cleanup no longer depends on this list being complete
        # (see _registered_worktrees_under), but a durable record is what lets anything else
        # reason about what this run created.
        save_state(state)
        sandboxed_command, executed_command = _sandbox_command(
            bwrap, command, project, worktree, temp_dir, args.network_policy
        )
        sandbox_manifest = {
            "profile": "strict-offline" if args.network_policy == "offline" else "strict-host-network",
            "network_isolation": args.network_policy == "offline",
            "environment_keys": sorted(_verification_environment()),
            "host_home_hidden": True, "workspace_destination": "/mnt",
            "command": executed_command,
        }
        atomic_json(attempt_root / "sandbox_manifest.json", sandbox_manifest)
        stdout_path = logs / "stdout.log"
        stderr_path = logs / "stderr.log"
        started = time.monotonic()
        returncode, timed_out, resource_error = _run_logged(
            sandboxed_command, stdout_path, stderr_path, args.timeout, temp_dir
        )
        # A read-only bind is not a snapshot: host-side changes remain visible while the command
        # runs. Recheck before accepting evidence so mid-verification drift is infrastructure.
        validate_environment_contract(project, state, full=True)
    except (WorkflowError, OSError) as exc:
        attempt.update({"status": "INFRA_ERROR", "error": str(exc), "completed_at": utc_now()})
        record["status"] = "PENDING"
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": str(exc), "retryable": True}, 4)
    duration = round(time.monotonic() - started, 3)
    if timed_out:
        attempt.update({"status": "INFRA_ERROR", "error": f"timeout after {args.timeout}s", "completed_at": utc_now()})
        record["status"] = "PENDING"
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": attempt["error"], "retryable": True}, 4)
    if resource_error:
        attempt.update({"status": "INFRA_ERROR", "error": resource_error, "completed_at": utc_now()})
        record["status"] = "PENDING"
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": resource_error, "retryable": True}, 4)
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    if returncode != request["expected_exit"] and stderr_text.lstrip().startswith("bwrap:"):
        attempt.update({"status": "INFRA_ERROR", "error": stderr_text.strip(), "completed_at": utc_now()})
        record["status"] = "PENDING"
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": attempt["error"], "retryable": True}, 4)
    # A python interpreter outside the project .venv carries none of the project environment by
    # contract, so a module-missing failure from one is an environment mismatch, not a code
    # failure: classify it as retryable infrastructure exactly like a timeout or resource breach.
    # `.venv/` launchers are the sanctioned environment and keep normal FAIL semantics for a
    # genuine red test.
    requested_executable = request.get("argv")[0] if request.get("argv") else ""
    if (
        returncode != request["expected_exit"]
        and non_venv_python(requested_executable)
        and re.search(r"(No module named|ModuleNotFoundError|ImportError:\s*No module)", stderr_text)
    ):
        attempt.update({"status": "INFRA_ERROR", "error": stderr_text.strip(), "completed_at": utc_now()})
        record["status"] = "PENDING"
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "VERIFICATION_INFRA_ERROR", attempt)
        save_state(state)
        emit({**preview, "status": "VERIFICATION_ERROR", "reason": attempt["error"], "retryable": True}, 4)
    evidence_id = f"ev-{hashlib.sha256((args.request_id + str(attempt_number)).encode()).hexdigest()[:12]}"
    status = "PASS" if returncode == request["expected_exit"] else "FAIL"
    evidence = {
        "evidence_id": evidence_id, "request_key": args.request_id,
        "criterion_id": record.get("criterion_id"), "batch": record["batch"],
        "reviewed_sha": record["reviewed_sha"], "argv": executed_command,
        "runner": "bwrap", "network_isolation": args.network_policy == "offline",
        "expected_exit": request["expected_exit"], "actual_exit": returncode,
        "status": status, "duration_seconds": duration,
        "stdout_path": str(stdout_path), "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path), "stderr_sha256": sha256_file(stderr_path),
        "output_limit_bytes": MAX_CAPTURE_BYTES,
        "worktree": str(worktree),
        # Proves the read-only mount held, not that this command behaved. /mnt is `--ro-bind`, so
        # a non-empty list here means the isolation itself broke. Measured across 223
        # verifications of one run: empty every time.
        "worktree_changes": git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines(),
        # A passing verification's scratch is removed below; the evidence says so rather than
        # leaving a reader to wonder whether the directory was lost.
        "scratch_removed": status == "PASS",
        "completed_at": utc_now(),
    }
    evidence_path = attempt_root / "evidence.json"
    atomic_json(evidence_path, evidence)
    if status == "PASS":
        # Everything worth keeping is already captured and hashed: stdout/stderr under logs/, the
        # exit code, the argv, the mount profile. The scratch is TMPDIR and $HOME for a command
        # that has now exited. Kept on FAIL and on every error path above, because that is when
        # somebody needs to look at what the command left behind.
        shutil.rmtree(temp_dir, ignore_errors=True)
    evidence["evidence_path"] = str(evidence_path)
    evidence["manifest"] = artifact_manifest([
        evidence_path, stdout_path, stderr_path, attempt_root / "sandbox_manifest.json"
    ])
    state["verification_evidence"].append(evidence)
    attempt.update({"status": status, "evidence_id": evidence_id, "completed_at": utc_now()})
    record["status"] = status
    record["evidence_path"] = str(evidence_path)
    record["resolved_at"] = utc_now()
    if record.get("source") == "FINAL_CONTRACT":
        final_requests = [
            item for item in state["verification_requests"]
            if item.get("source") == "FINAL_CONTRACT" and item.get("reviewed_sha") == record["reviewed_sha"]
        ]
        if status == "FAIL":
            last_batch = state["accepted_batches"].pop() if state["accepted_batches"] else None
            if last_batch:
                state["accepted_shas"].pop(last_batch, None)
            state["final_verification"] = {
                "status": "FAIL", "reviewed_sha": record["reviewed_sha"],
                "failed_request": args.request_id,
            }
            state["status"] = "CHANGES_REQUESTED"
        elif all(item.get("status") == "PASS" for item in final_requests):
            state["final_verification"] = {
                "status": "PASS", "reviewed_sha": record["reviewed_sha"],
                "evidence_ids": [
                    item.get("evidence_path") for item in final_requests
                ],
            }
            state["status"] = "READY_TO_FINALIZE"
        else:
            state["status"] = "FINAL_VERIFICATION_REQUIRED"
    else:
        remaining = [
            item for item in state.get("verification_requests", [])
            if item is not record and item.get("batch") == record.get("batch")
            and item.get("reviewed_sha") == record.get("reviewed_sha")
            and item.get("status") in {"PENDING", "APPROVED", "RUNNING"}
        ]
        state["status"] = (
            "AWAITING_VERIFICATION" if status == "PASS" and remaining
            else ("IMPLEMENTING" if status == "PASS" else "CHANGES_REQUESTED")
        )
    append_event(state, "VERIFICATION_COMPLETED", {
        "request_key": args.request_id, "attempt": attempt_number,
        "evidence_id": evidence_id, "status": status, "reviewed_sha": record["reviewed_sha"],
    })
    save_state(state)
    emit({**preview, "status": f"VERIFICATION_{status}", "evidence": evidence}, 0 if status == "PASS" else 2)


def require_target_unchanged(project: Path, state: dict[str, Any]) -> None:
    """Observe integration drift without invalidating fixed-baseline execution."""
    stale, target_head = target_staleness(project, state)
    integration = state.setdefault("integration", {
        "target_branch": state["target_branch"], "baseline_sha": state["baseline_sha"],
        "attempts": [],
    })
    integration["observed_target_sha"] = target_head
    if not state.get("finalized"):
        integration["status"] = "DIVERGED" if stale else "FAST_FORWARD_READY"


def command_review(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    validate_plan_unchanged(state)
    if not state.get("acceptance_contract"):
        raise WorkflowError("acceptance contract is not ready; run contract-review first")
    # AWAITING_ACCEPTANCE is admitted because a PASS describes one exact SHA, not the batch: a
    # commit made after that PASS leaves the run with no forward move at all -- `accept` refuses
    # because the report no longer authorizes HEAD, and `review` used to refuse on status. The
    # batch is not accepted yet, so new work still belongs to it and earns a review round. The
    # guard below keeps a PASS that still describes HEAD from buying a second paid review.
    if state.get("status") not in {"IMPLEMENTING", "CHANGES_REQUESTED", "AWAITING_ACCEPTANCE"}:
        raise WorkflowError(f"review is not allowed from workflow status {state.get('status')}")
    require_target_unchanged(project, state)
    implementation = validate_implementation(state)
    ensure_clean(implementation, "implementation")
    pending = current_batch(state)
    if args.batch != pending:
        raise WorkflowError(f"expected next batch {pending!r}, got {args.batch!r}")
    prior = [item for item in state["reviews"] if item["batch"] == args.batch]
    round_number = len(prior) + 1
    legacy_recovery_round = legacy_recovery_round_allowed(
        state, args.batch, round_number
    )
    round_limit = int(state["budgets"]["max_quality_rounds_per_batch"]) + int(
        state["extra_review_rounds_granted"].get(args.batch, 0)
    )
    if round_number > round_limit and not legacy_recovery_round:
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": f"decision-{slug(args.batch)}-{len(state['decisions']) + 1:03d}",
            "type": "REVIEW_BUDGET_EXHAUSTED", "batch": args.batch,
            "allowed_choices": ["GRANT_ONE_REVIEW", "DEFER_ELIGIBLE_P1", "ABORT_RUN"],
            "created_at": utc_now(),
        }
        append_event(state, "DECISION_REQUIRED", state["pending_decision"])
        save_state(state)
        emit(
            {
                "status": "NEEDS_USER_DECISION",
                "reason": "MAX_REVIEW_ROUNDS_EXCEEDED",
                "run_id": state["run_id"],
                "batch": args.batch,
            },
            3,
        )
    reviewer_path = Path(state["reviewer_worktree"])
    if not reviewer_path.is_dir():
        raise WorkflowError(f"reviewer worktree is missing: {reviewer_path}")
    ensure_clean(reviewer_path, "reviewer")
    if state["reviewer"] == "dsh":
        # The dsh reviewer writes the review to `.review-out/review.json` via its Bash tool
        # (workspace-write permission). `core.excludesFile` hides that directory from
        # `git status`, so the post-review cleanliness check still passes with the file present.
        dsh_review_out = reviewer_path / ".review-out"
        shutil.rmtree(dsh_review_out, ignore_errors=True)
        dsh_review_out.mkdir(exist_ok=True)
        dsh_exclude = Path(state["run_directory"]) / "reviews" / "dsh_reviewer.exclude"
        dsh_exclude.write_text(".review-out/\n", encoding="utf-8")
        run(["git", "-C", str(reviewer_path), "config", "core.excludesFile", str(dsh_exclude)],
            check=False)
    head = git(implementation, "rev-parse", "HEAD")
    previous = state["accepted_batches"][-1] if state["accepted_batches"] else None
    previous_sha = state["accepted_shas"].get(previous, state["baseline_sha"])
    if run(("git", "-C", str(implementation), "merge-base", "--is-ancestor", previous_sha, head), check=False).returncode != 0:
        raise WorkflowError("current implementation HEAD no longer descends from the previously accepted SHA")
    cumulative_final_review = args.batch == state["batches"][-1]
    base = state["baseline_sha"] if cumulative_final_review else previous_sha
    if args.batch.startswith("INTEGRATION_"):
        base = str((state.get("integration") or {}).get("approved_target_sha") or base)
    if head == base:
        raise WorkflowError(f"batch {args.batch!r} has no committed changes relative to its base")
    if prior and prior[-1]["verdict"] == "FAIL" and prior[-1]["reviewed_sha"] == head:
        raise WorkflowError("review requested changes but implementation HEAD has no new fix commit")
    if prior and prior[-1]["verdict"] == "PASS" and prior[-1]["reviewed_sha"] == head:
        raise WorkflowError(
            f"{head[:12]} already holds a PASS review; run accept for batch {args.batch!r} "
            "instead of paying for another review"
        )
    contract_requests = register_missing_contract_verification(
        state, args.batch, round_number, base, head
    )
    if contract_requests:
        state["status"] = "AWAITING_VERIFICATION"
        append_event(state, "CONTRACT_VERIFICATION_REQUIRED", {
            "batch": args.batch, "round": round_number, "reviewed_sha": head,
            "request_keys": [item["request_key"] for item in contract_requests],
        })
        save_state(state)
        emit({
            "status": "VERIFICATION_REQUIRED", "run_id": state["run_id"],
            "batch": args.batch, "round": round_number, "reviewed_sha": head,
            "requests": contract_requests, "review_round_consumed": False,
            "source": "ACCEPTANCE_CONTRACT",
        }, 3)
    unresolved_requests = [
        item for item in state.get("verification_requests", [])
        if item.get("batch") == args.batch and item.get("reviewed_sha") == head
        and item.get("status") in {"PENDING", "APPROVED", "RUNNING"}
    ]
    if unresolved_requests:
        raise WorkflowError("review is blocked by unresolved fixed-SHA verification requests")
    git(reviewer_path, "switch", "--detach", head)
    ensure_clean(reviewer_path, "reviewer")

    # An invocation budget is a bounded resource like the round budget beside it, and running
    # out of one is a fact that a single grant can change -- yet this decision used to offer only
    # ABORT_RUN and DEFER_ELIGIBLE_P1, and DEFER refuses outright when the batch holds no eligible
    # OPEN P1, leaving abandonment as the only move. It now mirrors REVIEW_BUDGET_EXHAUSTED: one
    # explicit extra invocation, once per batch, restoring the status the run parked from.
    if (
        not args.dry_run
        and int(state["usage"]["review_invocations_by_batch"].get(args.batch, 0))
        >= int(state["budgets"]["max_review_invocations_per_batch"])
        + int(state["extra_review_invocations_granted"].get(args.batch, 0))
    ):
        previous_status = state["status"]
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": f"review-call-budget-{slug(args.batch)}-{len(state['decisions']) + 1:03d}",
            "type": "REVIEW_INVOCATION_BUDGET_EXHAUSTED", "batch": args.batch,
            "allowed_choices": [
                "GRANT_ONE_REVIEW_INVOCATION", "DEFER_ELIGIBLE_P1", "ABORT_RUN",
            ],
            "previous_status": previous_status,
            "created_at": utc_now(),
        }
        append_event(state, "DECISION_REQUIRED", state["pending_decision"])
        save_state(state)
        emit({"status": "NEEDS_USER_DECISION", "reason": "REVIEW_INVOCATION_BUDGET_EXHAUSTED", "pending_decision": state["pending_decision"]}, 3)
    if args.dry_run:
        invocation_id = "preview"
        invocation_dir = (
            Path(state["run_directory"]) / "reviews" / f"batch_{slug(args.batch)}"
            / f"round_{round_number:02d}" / f"preview_{secrets.token_hex(3)}"
        )
        invocation: dict[str, Any] = {}
        secure_directory(invocation_dir)
    else:
        invocation_id, invocation_dir, invocation = next_invocation(
            state, "review", batch=args.batch, round_number=round_number
        )
    batch_dir = invocation_dir
    prompt_path = batch_dir / "prompt.md"
    report_path = batch_dir / "report.json"
    raw_path = batch_dir / "raw.json"
    stderr_path = batch_dir / "stderr.log"
    metadata_path = batch_dir / "metadata.json"
    schema_path = Path(state["run_directory"]) / "review_schema.json"
    validate_portable_review_schema()
    atomic_json(schema_path, REVIEW_SCHEMA)
    atomic_json(Path(state["run_directory"]) / "finding_ledger.json", state["finding_ledger"])
    (
        context_dir, context_plan, context_batch_manifest, context_assignment,
        context_ledger, context_legacy, context_evidence, context_contract,
    ) = prepare_review_context(
        state, args.batch, round_number, base, head, invocation_id
    )
    prompt = build_prompt(
        state, args.batch, round_number, base, head,
        context_plan, context_batch_manifest, context_assignment, context_ledger,
        context_legacy, context_evidence, context_contract,
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    prompt_path.chmod(0o600)
    command = reviewer_command(
        state["reviewer"], reviewer_path, context_dir,
        schema_path, raw_path, prompt, runtime=state.get("reviewer_runtime"),
    )
    if args.dry_run:
        emit(
            {
                "status": "DRY_RUN",
                "run_id": state["run_id"],
                "batch": args.batch,
                "round": round_number,
                "base_sha": base,
                "reviewed_sha": head,
                "reviewer": state["reviewer"],
                "command": command[:-1] + ["<PROMPT>"],
                "prompt_path": str(prompt_path),
            }
        )
    started = time.monotonic()
    try:
        result = run(command, cwd=reviewer_path, check=False, timeout=args.timeout)
        timed_out = False
    except WorkflowError as exc:
        timed_out = True
        result = None
    # dsh reviewer: the OUTPUT CONTRACT persists the review incrementally under
    # .review-out/ (criteria.jsonl / findings.jsonl / meta.json / review.json), so a truncated,
    # stopped, or timed-out final stream still yields whatever was already decided. Read the
    # channel only AFTER the run: a file present before the run would be a stale artifact.
    file_payload = read_dsh_review(reviewer_path) if state["reviewer"] == "dsh" else None
    if timed_out and file_payload is None:
        invocation.update({"status": "INFRA_ERROR", "error": str(exc), "completed_at": utc_now()})
        append_event(state, "INVOCATION_INFRA_ERROR", {"invocation_id": invocation_id, "error": str(exc)})
        save_state(state)
        emit({
            "status": "REVIEWER_ERROR", "reason": str(exc), "run_id": state["run_id"],
            "batch": args.batch, "round": round_number,
        }, 4)
    if result is not None:
        duration = round(time.monotonic() - started, 3)
        stderr_path.write_text(result.stderr, encoding="utf-8")
        stderr_path.chmod(0o600)
        if state["reviewer"] in {"claude", "dsh"}:
            raw_path.write_text(result.stdout, encoding="utf-8")
            raw_path.chmod(0o600)
        atomic_json(
            metadata_path,
            {
                "reviewer": state["reviewer"], "batch": args.batch, "round": round_number,
                "base_sha": base, "reviewed_sha": head, "returncode": result.returncode,
                "duration_seconds": duration, "started_at": utc_now(),
            },
        )
        if result.returncode != 0 and file_payload is None:
            invocation.update({"status": "INFRA_ERROR", "returncode": result.returncode, "completed_at": utc_now()})
            append_event(state, "INVOCATION_INFRA_ERROR", {
                "invocation_id": invocation_id, "returncode": result.returncode,
            })
            save_state(state)
            emit(
                {
                    "status": "REVIEWER_ERROR", "reason": "CLI_EXIT_NONZERO",
                    "returncode": result.returncode, "run_id": state["run_id"],
                    "batch": args.batch, "round": round_number,
                    "stderr_path": str(stderr_path), "raw_output_path": str(raw_path),
                },
                4,
            )
    else:
        atomic_json(metadata_path, {
            "reviewer": state["reviewer"], "batch": args.batch, "round": round_number,
            "base_sha": base, "reviewed_sha": head, "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "started_at": utc_now(), "file_channel": True,
        })
    try:
        payload = file_payload if file_payload is not None else extract_review(
            state["reviewer"], result.stdout, raw_path)
        validate_review_payload(payload, state["reviewer"], args.batch, base, head, state)
        ensure_clean(reviewer_path, "reviewer")
        if payload["verdict"] == "NEEDS_VERIFICATION":
            request_report = batch_dir / "verification_request.json"
            atomic_json(request_report, payload)
            cycle_key = f"{args.batch}:{round_number}:{head}"
            cycles = state["usage"].setdefault("verification_cycles_by_round", {})
            cycles[cycle_key] = int(cycles.get(cycle_key, 0)) + 1
            if cycles[cycle_key] > state["budgets"]["max_verification_cycles_per_round"]:
                invocation.update({
                    "status": "VALID_NEEDS_DECISION", "reported_verdict": payload["verdict"],
                    "completed_at": utc_now(),
                    "artifacts": artifact_manifest([
                        prompt_path, raw_path, stderr_path, metadata_path, request_report,
                        *sorted(path for path in context_dir.rglob("*") if path.is_file()),
                    ]),
                })
                state["status"] = "NEEDS_USER_DECISION"
                state["pending_decision"] = {
                    "decision_id": f"verification-cycle-{slug(args.batch)}-{len(state['decisions']) + 1:03d}",
                    "type": "VERIFICATION_CYCLE_BUDGET_EXHAUSTED", "batch": args.batch,
                    "allowed_choices": ["RESUME_WITH_DECISION", "ABORT_RUN"],
                    "created_at": utc_now(), "cycle_key": cycle_key,
                }
                append_event(state, "DECISION_REQUIRED", state["pending_decision"])
                save_state(state)
                emit({
                    "status": "NEEDS_USER_DECISION", "reason": "VERIFICATION_CYCLE_BUDGET_EXHAUSTED",
                    "pending_decision": state["pending_decision"], "review_round_consumed": False,
                }, 3)
            registered = register_verification_requests(
                state, payload, args.batch, round_number, base, head, request_report
            )
            invocation.update({
                "status": "VALID_NEEDS_VERIFICATION", "reported_verdict": payload["verdict"],
                "completed_at": utc_now(),
                "artifacts": artifact_manifest([
                    prompt_path, raw_path, stderr_path, metadata_path, request_report,
                    *sorted(path for path in context_dir.rglob("*") if path.is_file()),
                ]),
            })
            state["status"] = "AWAITING_VERIFICATION"
            append_event(state, "VERIFICATION_REQUESTED", {
                "invocation_id": invocation_id, "batch": args.batch, "round": round_number,
                "cycle": cycles[cycle_key], "request_keys": [item["request_key"] for item in registered],
            })
            save_state(state)
            emit({
                "status": "VERIFICATION_REQUIRED", "run_id": state["run_id"],
                "batch": args.batch, "round": round_number, "reviewed_sha": head,
                "requests": registered, "report_path": str(request_report),
                "review_round_consumed": False,
            }, 3)
        policy = apply_convergence_policy(state, payload, args.batch, round_number, head)
    except WorkflowError as exc:
        invocation.update({"status": "CONTRACT_ERROR", "error": str(exc), "completed_at": utc_now()})
        append_event(state, "INVOCATION_CONTRACT_ERROR", {
            "invocation_id": invocation_id, "error": str(exc),
        })
        save_state(state)
        emit(
            {
                "status": "REVIEWER_ERROR", "reason": str(exc),
                "run_id": state["run_id"], "batch": args.batch,
                "round": round_number, "raw_output_path": str(raw_path),
            },
            4,
        )
    report_document = {**payload, "effective_verdict": policy["effective_verdict"], "policy": policy}
    atomic_json(report_path, report_document)
    atomic_json(Path(state["run_directory"]) / "finding_ledger.json", state["finding_ledger"])
    invocation.update({
        "status": "VALID", "reported_verdict": payload["verdict"],
        "effective_verdict": policy["effective_verdict"], "completed_at": utc_now(),
        "artifacts": artifact_manifest([
            prompt_path, raw_path, stderr_path, metadata_path, report_path,
            *sorted(path for path in context_dir.rglob("*") if path.is_file()),
        ]),
    })
    state["reviews"].append(
        {
            "batch": args.batch, "round": round_number, "base_sha": base,
            "reviewed_sha": head, "verdict": policy["effective_verdict"],
            "reported_verdict": payload["verdict"],
            "blocking_count": policy["blocking_count"],
            "no_progress_streak": policy["no_progress_streak"],
            "report_path": str(report_path), "metadata_path": str(metadata_path),
            "invocation_id": invocation_id, "reviewer": state["reviewer"],
        }
    )
    state["status"] = {
        "PASS": "AWAITING_ACCEPTANCE",
        "FAIL": "CHANGES_REQUESTED",
        "NEEDS_USER_DECISION": "NEEDS_USER_DECISION",
    }[policy["effective_verdict"]]
    if policy["effective_verdict"] == "NEEDS_USER_DECISION":
        allowed = ["ABORT_RUN"]
        if any(
            reason == "NO_PROGRESS_FOR_TWO_ROUNDS"
            or reason.startswith("SEVERITY_UPGRADE:")
            or reason.startswith("OBLIGATION_DRIFT:")
            for reason in policy["decision_reasons"]
        ):
            allowed.insert(0, "RETURN_TO_FIX")
        if "REVIEWER_REQUESTED_DECISION" in policy["decision_reasons"] or any(
            reason.startswith("CROSS_BATCH_MATERIAL_FINDING:")
            for reason in policy["decision_reasons"]
        ):
            allowed.insert(0, "RESUME_WITH_DECISION")
        maximum = state["budgets"]["max_quality_rounds_per_batch"]
        granted = int(state["extra_review_rounds_granted"].get(args.batch, 0))
        if round_number >= maximum + granted and granted < 1:
            allowed.insert(0, "GRANT_ONE_REVIEW")
        if any(
            item["batch"] == args.batch and item["status"] == "OPEN" and item["severity"] == "P1"
            for item in state["finding_ledger"].values()
        ):
            allowed.insert(0, "DEFER_ELIGIBLE_P1")
        state["pending_decision"] = {
            "decision_id": f"decision-{slug(args.batch)}-{len(state['decisions']) + 1:03d}",
            "type": "REVIEW_CONVERGENCE", "batch": args.batch,
            "allowed_choices": allowed, "created_at": utc_now(),
            "reasons": policy["decision_reasons"],
        }
    append_event(state, "QUALITY_REVIEW_COMPLETED", {
        "invocation_id": invocation_id, "batch": args.batch, "round": round_number,
        "effective_verdict": policy["effective_verdict"], "reviewed_sha": head,
    })
    save_state(state)
    status = {
        "PASS": "REVIEW_PASS", "FAIL": "REVIEW_FAIL",
        "NEEDS_USER_DECISION": "NEEDS_USER_DECISION",
    }[policy["effective_verdict"]]
    emit(
        {
            "status": status, "run_id": state["run_id"], "batch": args.batch,
            "round": round_number, "base_sha": base, "reviewed_sha": head,
            "verdict": policy["effective_verdict"],
            "reported_verdict": payload["verdict"], "summary": payload["summary"],
            "findings": payload["findings"], "report_path": str(report_path),
            "blocking_fingerprints": policy["blocking_fingerprints"],
            "blocking_count": policy["blocking_count"],
            "deferred_findings": policy["deferred_findings"],
            "no_progress_streak": policy["no_progress_streak"],
            "decision_reasons": policy["decision_reasons"],
            "warnings": policy["warnings"],
            "fix_policy": state["fix_policy"],
            "final_review_round": round_number >= MAX_REVIEW_ROUNDS,
            "legacy_recovery_round": legacy_recovery_round,
        },
        0 if policy["effective_verdict"] == "PASS" else (3 if policy["effective_verdict"] == "NEEDS_USER_DECISION" else 2),
    )


def command_accept(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if state.get("status") != "AWAITING_ACCEPTANCE":
        raise WorkflowError(f"accept is not allowed from workflow status {state.get('status')}")
    validate_plan_unchanged(state)
    require_target_unchanged(project, state)
    implementation = validate_implementation(state)
    ensure_clean(implementation, "implementation")
    pending = current_batch(state)
    if args.batch != pending:
        raise WorkflowError(f"expected pending batch {pending!r}, got {args.batch!r}")
    report = Path(args.review_file).expanduser().resolve()
    reviews_root = (Path(state["run_directory"]) / "reviews").resolve()
    if reviews_root not in report.parents or not report.is_file():
        raise WorkflowError("--review-file must be an existing report inside this run")
    payload = read_json_object(report, "review report")
    head = git(implementation, "rev-parse", "HEAD")
    unresolved_verification = [
        item for item in state.get("verification_requests", [])
        if item.get("batch") == args.batch and item.get("reviewed_sha") == head
        and item.get("status") not in {"PASS", "REJECTED"}
    ]
    if unresolved_verification:
        raise WorkflowError("cannot accept while fixed-SHA verification is unresolved or failing")
    if payload.get("effective_verdict") != "PASS":
        raise WorkflowError("only a PASS review can accept a batch")
    cumulative = args.batch == state["batches"][-1]
    criteria = [
        item for item in state.get("acceptance_contract", {}).get("criteria", [])
        if cumulative or item.get("batch") == args.batch
    ]
    results = payload.get("criterion_results", [])
    by_id = {item.get("criterion_id"): item for item in results if isinstance(item, dict)}
    if {item["id"] for item in criteria} != set(by_id) or any(
        by_id[item["id"]].get("status") != "PASS" for item in criteria
    ):
        raise WorkflowError("PASS report does not satisfy every acceptance-contract criterion")
    if payload.get("batch") != args.batch or payload.get("reviewed_sha") != head:
        raise WorkflowError("review report does not authorize the current batch and HEAD")
    batch_reviews = [item for item in state["reviews"] if item["batch"] == args.batch]
    if not batch_reviews or batch_reviews[-1]["report_path"] != str(report):
        raise WorkflowError("only the latest registered review can authorize acceptance")
    state["accepted_batches"].append(args.batch)
    state["accepted_shas"][args.batch] = head
    next_batch = current_batch(state)
    final_requests: list[dict[str, Any]] = []
    if next_batch:
        state["status"] = "IMPLEMENTING"
    else:
        final_requests = register_final_contract_verification(state, head)
        if final_requests:
            state["status"] = "FINAL_VERIFICATION_REQUIRED"
            state["final_verification"] = {
                "status": "PENDING", "reviewed_sha": head,
                "request_keys": [item["request_key"] for item in final_requests],
            }
        else:
            state["status"] = "READY_TO_FINALIZE"
            state["final_verification"] = {
                "status": "NOT_APPLICABLE", "reviewed_sha": head,
                "reason": "acceptance contract contains no executable COMMAND criteria",
            }
    append_event(state, "BATCH_ACCEPTED", {
        "batch": args.batch, "accepted_sha": head, "next_batch": next_batch,
        "final_verification_requests": [item["request_key"] for item in final_requests],
    })
    save_state(state)
    emit(
        {
            "status": "BATCH_ACCEPTED", "run_id": state["run_id"],
            "batch": args.batch, "accepted_sha": head, "next_batch": next_batch,
            "all_batches_accepted": next_batch is None,
            "final_verification_requests": final_requests,
        }
    )


def command_adjudicate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    pending = state.get("pending_decision")
    if not isinstance(pending, dict) or state.get("status") != "NEEDS_USER_DECISION":
        raise WorkflowError("no typed workflow decision is pending")
    if args.decision_id != pending.get("decision_id"):
        raise WorkflowError("decision ID does not match the pending workflow decision")
    if args.choice not in pending.get("allowed_choices", []):
        raise WorkflowError(f"choice is not allowed for this decision: {args.choice}")
    if not args.reason.strip() or not args.actor.strip():
        raise WorkflowError("adjudication requires nonempty --reason and --actor")
    finding_ids = [item for item in (args.finding_ids or "").split(",") if item]
    decision = {
        "id": f"decision-{len(state['decisions']) + 1:03d}",
        "pending_decision_id": args.decision_id, "type": pending["type"],
        "choice": args.choice, "reason": args.reason, "actor": args.actor,
        "finding_ids": finding_ids, "created_at": utc_now(),
    }
    if args.choice == "GRANT_ONE_REVIEW":
        batch = pending.get("batch")
        if not isinstance(batch, str):
            raise WorkflowError("review grant requires a batch-scoped decision")
        if int(state["extra_review_rounds_granted"].get(batch, 0)) >= 1:
            raise WorkflowError("the single extra review grant was already used for this batch")
    elif args.choice == "GRANT_ONE_REVIEW_INVOCATION":
        batch = pending.get("batch")
        if not isinstance(batch, str):
            raise WorkflowError("invocation grant requires a batch-scoped decision")
        if int(state.get("extra_review_invocations_granted", {}).get(batch, 0)) >= 1:
            raise WorkflowError(
                "the single extra review invocation grant was already used for this batch")
    elif args.choice == "DEFER_ELIGIBLE_P1":
        if not finding_ids:
            raise WorkflowError("DEFER_ELIGIBLE_P1 requires --finding-ids")
        ledger_by_id = {item["id"]: item for item in state["finding_ledger"].values()}
        for finding_id in finding_ids:
            finding = ledger_by_id.get(finding_id)
            if not finding or finding.get("status") != "OPEN" or finding.get("severity") != "P1":
                raise WorkflowError(f"finding is not an eligible OPEN P1: {finding_id}")
    elif args.choice == "GRANT_ONE_VERIFICATION_ATTEMPT":
        request_key = pending.get("request_key")
        if not isinstance(request_key, str):
            raise WorkflowError("verification grant requires a request-scoped decision")
        if int(state["extra_verification_attempts_granted"].get(request_key, 0)) >= 1:
            raise WorkflowError("the single extra verification attempt was already granted")
    elif args.choice in {"REJECT_VERIFICATION_REQUEST", "AUTHORIZE_HOST_NETWORK"}:
        request_key = pending.get("request_key")
        if not isinstance(request_key, str):
            raise WorkflowError(f"{args.choice} requires a request-scoped decision")
        records = [
            item for item in state.get("verification_requests", [])
            if item.get("request_key") == request_key
        ]
        if len(records) != 1 or records[0].get("status") not in {"PENDING", "APPROVED"}:
            raise WorkflowError("the verification request is not pending and retryable")
    if not args.apply:
        emit({"status": "ADJUDICATION_PREVIEW", "run_id": state["run_id"], "decision": decision})
    if args.choice == "GRANT_ONE_REVIEW":
        batch = str(pending["batch"])
        state["extra_review_rounds_granted"][batch] = 1
        state["status"] = "CHANGES_REQUESTED"
    elif args.choice == "GRANT_ONE_REVIEW_INVOCATION":
        state.setdefault("extra_review_invocations_granted", {})[str(pending["batch"])] = 1
        state["status"] = str(pending.get("previous_status", "CHANGES_REQUESTED"))
    elif args.choice == "DEFER_ELIGIBLE_P1":
        ledger_by_id = {item["id"]: item for item in state["finding_ledger"].values()}
        for finding_id in finding_ids:
            finding = ledger_by_id[finding_id]
            finding["status"] = "DEFERRED"
            finding["deferred_reason"] = f"USER_ADJUDICATED:{args.reason}"
        state["status"] = "IMPLEMENTING"
    elif args.choice == "GRANT_ONE_VERIFICATION_ATTEMPT":
        request_key = str(pending["request_key"])
        state["extra_verification_attempts_granted"][request_key] = 1
        state["status"] = str(pending.get("previous_status", "AWAITING_VERIFICATION"))
    elif args.choice == "REJECT_VERIFICATION_REQUEST":
        request_key = str(pending["request_key"])
        record = next(
            item for item in state["verification_requests"]
            if item.get("request_key") == request_key
        )
        if record.get("source") == "FINAL_CONTRACT":
            raise WorkflowError("a required final contract verification cannot be waived")
        record["status"] = "REJECTED"
        record["rejection_reason"] = f"USER_ADJUDICATED:{args.reason}"
        record["resolved_at"] = utc_now()
        state["status"] = "IMPLEMENTING"
    elif args.choice == "AUTHORIZE_HOST_NETWORK":
        request_key = str(pending["request_key"])
        state["host_network_authorizations"][request_key] = decision
        state["status"] = str(pending.get("previous_status", "AWAITING_VERIFICATION"))
    elif args.choice == "RETURN_TO_FIX":
        state["status"] = "CHANGES_REQUESTED"
    elif args.choice == "SUPERSEDE_RUN":
        state["status"] = "SUPERSEDED"
        state["superseded_at"] = utc_now()
    elif args.choice == "ABORT_RUN":
        state["status"] = "ABANDONED"
        state["abandoned_at"] = utc_now()
    elif args.choice == "RESUME_WITH_DECISION":
        # Restore the status the run parked from (decisions that carry previous_status), falling
        # back to IMPLEMENTING for decisions that predate it. The next command re-observes the
        # fact the decision was about (e.g. target staleness) and re-parks if it still holds.
        state["status"] = str(pending.get("previous_status", "IMPLEMENTING"))
    else:
        raise WorkflowError(f"choice requires its dedicated contract command: {args.choice}")
    state["pending_decision"] = None
    state["decisions"].append(decision)
    append_event(state, "USER_ADJUDICATED", decision)
    save_state(state)
    emit({"status": "ADJUDICATED", "run_id": state["run_id"], "decision": decision, "run_status": state["status"]})


def status_payload(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    target_sha = git(project, "rev-parse", state["target_branch"])
    current_controller = controller_runtime()
    recorded_controller = state.get("controller_runtime")
    controller_drift = bool(
        isinstance(recorded_controller, dict)
        and any(
            recorded_controller.get(key) != current_controller.get(key)
            for key in ("implementation", "version", "resolved_executable")
        )
    )
    stale = not state["finalized"] and target_sha != state["baseline_sha"]
    implementation = Path(state["implementation_worktree"])
    implementation_head = git(implementation, "rev-parse", "HEAD") if implementation.is_dir() else None
    effective_status = "MIGRATION_REQUIRED" if state.get("_requires_migration") else state["status"]
    integration = copy.deepcopy(state.get("integration") or {})
    integration.update({
        "target_branch": state["target_branch"], "baseline_sha": state["baseline_sha"],
        "observed_target_sha": target_sha,
    })
    if state.get("finalized"):
        integration["status"] = "INTEGRATED"
    elif integration.get("status") not in {"PREPARING", "RECONCILING", "REVIEW_REQUIRED", "APPROVED"}:
        integration["status"] = "DIVERGED" if stale else "FAST_FORWARD_READY"
    source_plan = Path(state.get("plan_original", ""))
    source_plan_changed = (
        not source_plan.is_file()
        or sha256_file(source_plan) != state.get("plan_digest")
    )
    source_manifest_value = state.get("batch_manifest_original")
    source_manifest = Path(source_manifest_value) if isinstance(source_manifest_value, str) else None
    source_batch_manifest_changed = (
        source_manifest is not None
        and (
            not source_manifest.is_file()
            or sha256_file(source_manifest) != state.get("batch_manifest_digest")
        )
    )
    return {
        "status": effective_status, "recorded_status": state["status"],
        "project": str(project), "project_key": state["project_key"],
        "run_id": state["run_id"], "run_directory": state["run_directory"],
        "target_branch": state["target_branch"], "target_sha": target_sha,
        "baseline_sha": state["baseline_sha"], "target_advanced": stale,
        "integration": integration,
        "implementation_branch": state["implementation_branch"],
        "implementation_worktree": state["implementation_worktree"],
        "implementation_sha": implementation_head,
        "reviewer": state["reviewer"], "reviewer_worktree": state["reviewer_worktree"],
        "reviewer_runtime": state.get("reviewer_runtime") or {},
        "controller_runtime": recorded_controller,
        "current_controller_runtime": current_controller,
        "controller_runtime_drift": controller_drift,
        "project_runtime_at_start": state.get("project_runtime_at_start"),
        "environment": environment_status(project, state),
        "reviewer_history": state.get("reviewer_history", []),
        "implementer": state.get("implementer", "legacy-unknown"),
        "fix_policy": state["fix_policy"], "batches": state["batches"],
        "accepted_batches": state["accepted_batches"], "next_batch": current_batch(state),
        "review_count": len(state["reviews"]), "finalized": state["finalized"],
        "open_blocking_findings": [
            item for item in state["finding_ledger"].values() if item["status"] == "OPEN"
        ],
        "deferred_findings": [
            item for item in state["finding_ledger"].values() if item["status"] == "DEFERRED"
        ],
        "batch_convergence": state["batch_convergence"],
        "acceptance_contract": state.get("acceptance_contract"),
        "contract_reviews": state.get("contract_reviews", []),
        "decisions": state.get("decisions", []),
        "verification_requests": state.get("verification_requests", []),
        "verification_evidence": state.get("verification_evidence", []),
        "verification_attempts": state.get("verification_attempts", []),
        "review_invocations": state.get("review_invocations", []),
        "contract_review_invocations": state.get("contract_review_invocations", []),
        "budgets": state.get("budgets", {}), "usage": state.get("usage", {}),
        "pending_decision": state.get("pending_decision"),
        "final_verification": state.get("final_verification"),
        "final_sha": state["final_sha"], "plan_digest": state["plan_digest"],
        "plan_snapshot": state["plan_snapshot"],
        "plan_snapshot_digest": state.get("plan_snapshot_digest"),
        "source_plan_changed": source_plan_changed,
        "batch_manifest_snapshot": state.get("batch_manifest_snapshot"),
        "batch_manifest_snapshot_digest": state.get("batch_manifest_snapshot_digest"),
        "source_batch_manifest_changed": source_batch_manifest_changed,
        "worktrees_removed": state["worktrees_removed"],
    }


def command_status(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    emit(status_payload(project, load_state(project, args.run_id)))


def legacy_pending_decision(state: dict[str, Any]) -> dict[str, Any] | None:
    """The typed decision a legacy run parked at NEEDS_USER_DECISION never got.

    A run stopped at NEEDS_USER_DECISION before typed decisions existed carries no
    ``pending_decision``, and every exit is closed against it: ``contract-review``, ``review`` and
    ``accept`` refuse the status, while ``adjudicate`` refuses because nothing is pending. Data
    migration alone does not reach this -- it translates the state's SHAPE, and the run is stuck on
    its POSITION in the state machine.

    Returns the decision to raise, or ``None`` when the run needs no repair. This synthesizes a
    QUESTION, never an answer: the choices are the ones already implemented for a batch parked
    after review, and resolving it still requires ``--reason`` and ``--actor`` like any other
    adjudication.
    """
    if state.get("status") != "NEEDS_USER_DECISION":
        return None
    if isinstance(state.get("pending_decision"), dict):
        return None
    batch = current_batch(state)
    if not batch:
        return None
    return {
        "decision_id": f"legacy-parked-{slug(batch)}-{len(state['decisions']) + 1:03d}",
        "type": "LEGACY_PARKED_WITHOUT_TYPED_DECISION",
        "batch": batch,
        "allowed_choices": [
            "GRANT_ONE_REVIEW", "DEFER_ELIGIBLE_P1", "ABORT_RUN", "SUPERSEDE_RUN",
        ],
        "created_at": utc_now(),
        "reason": (
            "This run was parked at NEEDS_USER_DECISION under an earlier schema, before workflow "
            "decisions were typed, so no pending decision was recorded and no command could act "
            "on the run. The original stop is preserved in reviews and batch_convergence; this "
            "entry only makes it adjudicable."
        ),
    }


def command_migrate(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    schema_pending = bool(state.get("_requires_migration"))
    decision_repair = legacy_pending_decision(state)
    if not schema_pending and not decision_repair:
        emit({"status": "MIGRATION_NOT_REQUIRED", "run_id": state["run_id"], "schema_version": SCHEMA_VERSION})
    migration = state.get("migration", {})
    if state.get("_migration_snapshot_mismatch"):
        raise WorkflowError(
            "legacy plan snapshot digest does not match the recorded plan digest; migration is blocked"
        )
    preview = {
        "status": "MIGRATION_PREVIEW", "run_id": state["run_id"],
        "from_schema": migration.get("from_schema") if schema_pending else SCHEMA_VERSION,
        "to_schema": SCHEMA_VERSION,
        "mode": migration.get("mode") if schema_pending else "PARKED_DECISION_REPAIR",
        "legacy_acceptance_contract": state.get("acceptance_contract", {}).get("status"),
        "plan_snapshot_digest": state.get("plan_snapshot_digest"),
        "pending_decision_repair": decision_repair,
        "environment_fingerprint_migration": (
            isinstance(state.get("environment_contract"), dict)
            and state["environment_contract"].get("fingerprint_version")
            != ENVIRONMENT_FINGERPRINT_VERSION
        ),
    }
    if not args.apply:
        emit(preview)
    backup: Path | None = None
    backup_reused = False
    if schema_pending:
        path = state_path(project, state["run_id"])
        backup = path.with_name(f"workflow.schema{migration.get('from_schema', 'legacy')}.json")
        if backup.exists():
            if (
                backup.is_symlink()
                or not stat.S_ISREG(backup.lstat().st_mode)
                or sha256_file(backup) != sha256_file(path)
            ):
                raise WorkflowError(f"migration backup exists but does not match legacy state: {backup}")
            backup.chmod(0o600)
            backup_reused = True
        else:
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        state.pop("_requires_migration", None)
        state.pop("_migration_snapshot_mismatch", None)
        state["migration"] = {
            **migration, "status": "APPLIED", "applied_at": utc_now(),
            "backup_path": str(backup), "backup_sha256": sha256_file(backup),
        }
        append_event(state, "SCHEMA_MIGRATED", state["migration"])
        old_environment = state.get("environment_contract")
        if (
            not isinstance(old_environment, dict)
            or old_environment.get("fingerprint_version") != ENVIRONMENT_FINGERPRINT_VERSION
        ):
            old_version = old_environment.get("fingerprint_version") if isinstance(old_environment, dict) else None
            state["environment_contract"] = project_environment_contract(project, include_content=True)
            append_event(state, "ENVIRONMENT_FINGERPRINT_MIGRATED", {
                "from_version": old_version,
                "to_version": ENVIRONMENT_FINGERPRINT_VERSION,
                "basis": "MIGRATION_TIME_STATIC_FILESYSTEM_OBSERVATION",
            })
        integration = state.setdefault("integration", {})
        attempts = integration.setdefault("attempts", [])
        active_attempts = [
            item for item in attempts
            if item.get("status") in {"PREPARING", "READY_FOR_COMMIT", "CONFLICT"}
        ]
        if len(active_attempts) > 1:
            current = active_attempts[-1]
            for item in active_attempts[:-1]:
                item["status"] = "LEGACY_SUPERSEDED"
                item["superseded_at"] = utc_now()
                item["superseded_by_attempt"] = current.get("number")
            integration["current_attempt"] = current.get("number")
            append_event(state, "LEGACY_RECONCILIATION_ATTEMPTS_NORMALIZED", {
                "preserved_attempts": [item.get("number") for item in active_attempts],
                "current_attempt": current.get("number"),
            })
        pending = state.get("pending_decision")
        if isinstance(pending, dict) and pending.get("type") == "TARGET_ADVANCED":
            state["status"] = str(pending.get("previous_status", "IMPLEMENTING"))
            state["pending_decision"] = None
            state.setdefault("integration", {})["status"] = "DIVERGED"
            append_event(state, "TARGET_DECOUPLED", {
                "legacy_decision_id": pending.get("decision_id"),
                "restored_status": state["status"],
            })
    # Re-derived after the schema step so one invocation can repair a run that needed both.
    decision_repair = legacy_pending_decision(state)
    if decision_repair:
        state["pending_decision"] = decision_repair
        append_event(state, "DECISION_REQUIRED", decision_repair)
    save_state(state)
    emit({
        **preview, "status": "MIGRATED",
        "backup_path": str(backup) if backup else None,
        "backup_reused": backup_reused,
        "pending_decision_repair": decision_repair,
    })


def write_final_report(state: dict[str, Any], integrated: bool) -> Path:
    report = Path(state["run_directory"]) / "final_report.md"
    review_lines = [
        f"- Batch `{item['batch']}` round {item['round']}: **{item['verdict']}** "
        f"at `{item['reviewed_sha']}` by `{item.get('reviewer', 'legacy-unknown')}`"
        for item in state["reviews"]
    ]
    deferred_lines = [
        f"- `{item['id']}` ({item['severity']}): {item['consequence']} "
        f"[{item.get('deferred_reason') or 'deferred'}]"
        for item in state["finding_ledger"].values()
        if item["status"] == "DEFERRED"
    ]
    contract = state.get("acceptance_contract") or {}
    decision_lines = [
        f"- `{item['id']}` {item['type']}: {item['reason']}"
        for item in state.get("decisions", [])
    ]
    verification_lines = [
        f"- `{item['request_key']}`: **{item['status']}** at `{item['reviewed_sha']}` "
        f"(exit {item.get('actual_exit')}, evidence `{item.get('evidence_path')}`)"
        for item in state.get("verification_evidence", [])
    ]
    report.write_text(
        "# Implementation workflow report\n\n"
        f"- Project: `{state['project']}`\n- Project key: `{state['project_key']}`\n"
        f"- Run ID: `{state['run_id']}`\n- Plan: `{state['plan_snapshot']}`\n"
        f"- Plan SHA-256: `{state['plan_digest']}`\n- Reviewer: `{state['reviewer']}`\n"
        f"- Reviewer history: `{json.dumps(state.get('reviewer_history', []), ensure_ascii=False, sort_keys=True)}`\n"
        f"- Implementer: `{state.get('implementer', 'legacy-unknown')}`\n"
        f"- Controller runtime: `{json.dumps(state.get('controller_runtime'), ensure_ascii=False, sort_keys=True)}`\n"
        f"- Project runtime at start: `{json.dumps(state.get('project_runtime_at_start'), ensure_ascii=False, sort_keys=True)}`\n"
        f"- Acceptance contract: `{contract.get('status')}`\n"
        f"- Acceptance contract digest: `{contract.get('digest')}`\n"
        f"- Plan snapshot SHA-256: `{state.get('plan_snapshot_digest')}`\n"
        f"- Batch manifest: `{state.get('batch_manifest_snapshot') or 'LEGACY_UNAVAILABLE'}`\n"
        f"- Batch manifest SHA-256: `{state.get('batch_manifest_snapshot_digest')}`\n"
        f"- Target branch: `{state['target_branch']}`\n"
        f"- Implementation branch: `{state['implementation_branch']}`\n"
        f"- Baseline SHA: `{state['baseline_sha']}`\n"
        f"- Final reviewed SHA: `{state['accepted_shas'].get(state['batches'][-1])}`\n"
        f"- Accepted batches: {', '.join(state['accepted_batches'])}\n"
        f"- Integrated: {'yes' if integrated else 'not yet'}\n\n## Review history\n\n"
        + ("\n".join(review_lines) if review_lines else "- No reviews recorded")
        + "\n\n## Human decisions\n\n"
        + ("\n".join(decision_lines) if decision_lines else "- None")
        + "\n\n## Fixed-SHA verification evidence\n\n"
        + ("\n".join(verification_lines) if verification_lines else "- None")
        + "\n\n## Final verification\n\n"
        + f"- `{json.dumps(state.get('final_verification'), ensure_ascii=False, sort_keys=True)}`"
        + "\n\n## Operational budgets and usage\n\n"
        + f"- Budgets: `{json.dumps(state.get('budgets', {}), sort_keys=True)}`\n"
        + f"- Usage: `{json.dumps(state.get('usage', {}), sort_keys=True)}`\n"
        + f"- Review invocations: {len(state.get('review_invocations', []))}\n"
        + f"- Contract invocations: {len(state.get('contract_review_invocations', []))}\n"
        + f"- Verification attempts: {len(state.get('verification_attempts', []))}"
        + "\n\n## Deferred findings\n\n"
        + ("\n".join(deferred_lines) if deferred_lines else "- None") + "\n",
        encoding="utf-8",
    )
    report.chmod(0o600)
    return report


def command_finalize(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["finalized"]:
        report = write_final_report(state, integrated=True)
        emit({**status_payload(project, state), "status": "ALREADY_FINALIZED", "final_report": str(report)})
    validate_active_state(project, state)
    if state.get("status") not in {"READY_TO_FINALIZE", "FINALIZING"}:
        raise WorkflowError(f"finalize is not allowed from workflow status {state.get('status')}")
    if current_batch(state) is not None:
        raise WorkflowError(f"cannot finalize before accepting batch {current_batch(state)!r}")
    validate_plan_unchanged(state)
    implementation = validate_implementation(state)
    ensure_clean(implementation, "implementation")
    final_head = git(implementation, "rev-parse", "HEAD")
    accepted_final = state["accepted_shas"].get(state["batches"][-1])
    if accepted_final != final_head:
        # Commits made after acceptance are outside the batch contract, so refusing is correct --
        # but the operator still has to be told which two SHAs disagree and what the legal moves are.
        raise WorkflowError(
            f"implementation HEAD is {final_head[:12]} but the accepted final SHA is "
            f"{str(accepted_final)[:12]}; commits made after acceptance are outside this batch's "
            "contract. Reset the implementation worktree to the accepted SHA to finalize it, or "
            "supersede this run and review the extra commits in a new one."
        )
    final_verification = state.get("final_verification")
    if not isinstance(final_verification, dict) or final_verification.get("status") not in {"PASS", "NOT_APPLICABLE"}:
        raise WorkflowError("final fixed-SHA verification has not completed")
    if final_verification.get("reviewed_sha") != final_head:
        raise WorkflowError("final verification does not belong to the final accepted SHA")
    target_sha = git(project, "rev-parse", state["target_branch"])
    integration = state.get("integration") or {}
    expected_target = integration.get("approved_target_sha") or state["baseline_sha"]
    transaction = state.get("integration_transaction")
    if state.get("status") == "FINALIZING":
        if not isinstance(transaction, dict) or transaction.get("final_sha") != final_head:
            raise WorkflowError("FINALIZING state lacks a valid integration transaction")
        if target_sha == final_head:
            recovery = {
                "status": "FINALIZE_RECOVERY_PREVIEW", "run_id": state["run_id"],
                "target_branch": state["target_branch"], "final_sha": final_head,
                "prepared_at": transaction.get("prepared_at"),
            }
            if not args.apply:
                emit(recovery)
            state["status"] = "FINALIZED"
            state["finalized"] = True
            state["final_sha"] = final_head
            transaction["status"] = "COMMITTED"
            transaction["committed_at"] = utc_now()
            transaction["recovered"] = True
            report = write_final_report(state, integrated=True)
            append_event(state, "INTEGRATION_RECOVERED", transaction)
            save_state(state)
            emit({**recovery, "status": "FINALIZED", "final_report": str(report)})
        prepared_target = transaction.get("expected_target_sha") or expected_target
        if target_sha != prepared_target:
            recovery = {
                "status": "FINALIZE_DIVERGENCE_RECOVERY_PREVIEW",
                "run_id": state["run_id"], "target_branch": state["target_branch"],
                "prepared_target_sha": prepared_target, "current_target_sha": target_sha,
                "final_sha": final_head,
            }
            if not args.apply:
                emit(recovery, 3)
            transaction.update({
                "status": "ABORTED_TARGET_MOVED", "aborted_at": utc_now(),
                "observed_target_sha": target_sha,
            })
            state["status"] = "READY_TO_FINALIZE"
            state.setdefault("integration", {}).update({
                "status": "DIVERGED", "observed_target_sha": target_sha,
            })
            append_event(state, "INTEGRATION_PREPARATION_ABORTED", transaction)
            save_state(state)
            emit({
                **recovery, "status": "RECONCILIATION_REQUIRED",
                "next_command": "reconcile",
            }, 3)
    can_ff = run(
        ("git", "-C", str(project), "merge-base", "--is-ancestor", target_sha, final_head),
        check=False,
    ).returncode == 0
    unchanged = target_sha == expected_target
    report = write_final_report(state, integrated=False)
    preview = {
        "status": "FINALIZE_PREVIEW", "run_id": state["run_id"],
        "target_branch": state["target_branch"], "target_sha": target_sha,
        "baseline_sha": state["baseline_sha"], "expected_target_sha": expected_target,
        "target_unchanged": unchanged,
        "implementation_branch": state["implementation_branch"],
        "final_reviewed_sha": final_head, "fast_forward_possible": can_ff,
        "original_project_untouched_until_apply": True,
        "commits": git(project, "log", "--oneline", f"{target_sha}..{final_head}").splitlines(),
        "final_report": str(report),
    }
    if not unchanged or not can_ff:
        emit({
            **preview, "status": "RECONCILIATION_REQUIRED",
            "reason": "TARGET_ADVANCED" if not unchanged else "INTEGRATION_NOT_FAST_FORWARD",
            "next_command": "reconcile",
        }, 3)
    if not args.apply:
        emit(preview)
    ensure_no_git_operation(project)
    current_branch = git(project, "branch", "--show-current")
    target_ref = f"refs/heads/{state['target_branch']}"
    if current_branch == state["target_branch"]:
        ensure_clean(project, "original project")
    else:
        checked_out = {
            record["branch"] for record in registered_worktrees(project)
            if Path(str(record["path"])).resolve() != project
        }
        if target_ref in checked_out:
            raise WorkflowError(
                f"target branch {state['target_branch']!r} is checked out in another worktree"
            )
    if state.get("status") == "READY_TO_FINALIZE":
        state["status"] = "FINALIZING"
        state["integration_transaction"] = {
            "status": "PREPARED", "target_branch": state["target_branch"],
            "baseline_sha": state["baseline_sha"],
            "expected_target_sha": expected_target, "final_sha": final_head,
            "prepared_at": utc_now(),
        }
        append_event(state, "INTEGRATION_PREPARED", state["integration_transaction"])
        save_state(state)
    if current_branch == state["target_branch"]:
        git(project, "merge", "--ff-only", state["implementation_branch"])
        merged = git(project, "rev-parse", "HEAD")
    else:
        git(project, "update-ref", target_ref, final_head, target_sha)
        merged = git(project, "rev-parse", target_ref)
    if merged != final_head:
        raise WorkflowError("merged SHA does not match the reviewed SHA")
    state["status"] = "FINALIZED"
    state["finalized"] = True
    state["final_sha"] = merged
    state["integration_transaction"]["status"] = "COMMITTED"
    state["integration_transaction"]["committed_at"] = utc_now()
    report = write_final_report(state, integrated=True)
    append_event(state, "INTEGRATION_COMMITTED", state["integration_transaction"])
    save_state(state)
    emit({**preview, "status": "FINALIZED", "final_sha": merged, "final_report": str(report)})


def observe_reconciliation_attempt(project: Path, attempt: dict[str, Any]) -> str:
    """Revalidate the Git resources behind an idempotent reconciliation response."""
    worktree = Path(str(attempt["worktree"]))
    records = {Path(str(item["path"])).resolve(): item for item in registered_worktrees(project)}
    registered = records.get(worktree.resolve())
    if not worktree.is_dir() or registered is None:
        raise WorkflowError(
            "recorded reconciliation worktree is missing or no longer registered; "
            "inspect it, then use abandon-reconciliation before creating another attempt"
        )
    expected_ref = f"refs/heads/{attempt['branch']}"
    if registered.get("branch") != expected_ref:
        raise WorkflowError("recorded reconciliation worktree is on an unexpected branch")
    markers = git_operation_markers(worktree)
    unexpected = [item for item in markers if item != "MERGE_HEAD"]
    if unexpected:
        raise WorkflowError(
            "recorded reconciliation worktree has an unexpected Git operation: "
            + ", ".join(unexpected)
        )
    head = git(worktree, "rev-parse", "HEAD")
    if "MERGE_HEAD" in markers:
        if head != attempt["target_sha"]:
            raise WorkflowError("uncommitted reconciliation no longer starts at its recorded target")
        merge_head = git(worktree, "rev-parse", "MERGE_HEAD")
        if merge_head != attempt["source_candidate_sha"]:
            raise WorkflowError("reconciliation MERGE_HEAD is not the recorded source candidate")
        unmerged = bool(git(worktree, "diff", "--name-only", "--diff-filter=U"))
        return "CONFLICT" if unmerged else "READY_FOR_COMMIT"
    if head == attempt["target_sha"]:
        raise WorkflowError("recorded reconciliation has neither a merge operation nor a merge commit")
    ensure_clean(worktree, "reconciliation")
    for ancestor, label in (
        (attempt["target_sha"], "target"),
        (attempt["source_candidate_sha"], "source candidate"),
    ):
        if run(
            ("git", "-C", str(worktree), "merge-base", "--is-ancestor", ancestor, head),
            check=False,
        ).returncode:
            raise WorkflowError(f"recorded reconciliation HEAD does not contain the {label} SHA")
    return "READY_FOR_COMMIT"


def command_reconcile(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if state.get("status") != "READY_TO_FINALIZE":
        raise WorkflowError(f"reconcile requires READY_TO_FINALIZE, found {state.get('status')}")
    implementation = validate_implementation(state)
    ensure_clean(implementation, "implementation")
    source = git(implementation, "rev-parse", "HEAD")
    accepted = state.get("accepted_shas", {}).get(state["batches"][-1])
    final_verification = state.get("final_verification") or {}
    if source != accepted or final_verification.get("reviewed_sha") != source:
        raise WorkflowError("reconciliation source is not the final accepted and verified SHA")
    target_branch = args.target_branch or state["target_branch"]
    if target_branch != state["target_branch"]:
        raise WorkflowError(
            f"reconciliation target {target_branch!r} does not match the frozen target "
            f"{state['target_branch']!r}"
        )
    integration = state.setdefault("integration", {})
    attempts = integration.setdefault("attempts", [])
    active_attempts = [
        item for item in attempts
        if item.get("status") in {"PREPARING", "READY_FOR_COMMIT", "CONFLICT"}
    ]
    if len(active_attempts) > 1:
        raise WorkflowError(
            "multiple active reconciliation attempts require explicit schema migration or abandonment"
        )
    current_number = integration.get("current_attempt")
    if active_attempts and current_number != active_attempts[0].get("number"):
        raise WorkflowError("integration.current_attempt does not identify the active reconciliation attempt")
    existing = active_attempts[0] if active_attempts else None
    if existing and existing.get("status") in {"READY_FOR_COMMIT", "CONFLICT"}:
        observed_status = observe_reconciliation_attempt(project, existing)
        existing_status = (
            "RECONCILIATION_CONFLICT"
            if observed_status == "CONFLICT"
            else "RECONCILIATION_READY_FOR_COMMIT"
        )
        emit({
            "status": existing_status, "run_id": state["run_id"], "existing": True,
            "target_branch": existing["target_branch"], "target_sha": existing["target_sha"],
            "source_candidate_sha": existing["source_candidate_sha"],
            "worktree": existing["worktree"], "branch": existing["branch"],
            "attempt_number": existing["number"],
        }, 3 if observed_status == "CONFLICT" else 0)
    preparing = existing if existing and existing.get("status") == "PREPARING" else None
    new_preparation = preparing is None
    if preparing:
        number = int(preparing["number"])
        target_sha = str(preparing["target_sha"])
        worktree = Path(str(preparing["worktree"]))
        branch = str(preparing["branch"])
        if preparing.get("source_candidate_sha") != source or preparing.get("target_branch") != target_branch:
            raise WorkflowError("recorded reconciliation preparation does not match current authority")
    else:
        target_sha = git(project, "rev-parse", target_branch)
        number = len(attempts) + 1
        root = Path(state["run_directory"])
        worktree = root / "worktrees" / f"reconciliation_{number:02d}"
        branch = f"{state['implementation_branch']}-reconcile-{number:02d}"
        preparing = {
            "number": number, "target_branch": target_branch, "target_sha": target_sha,
            "source_candidate_sha": source, "worktree": str(worktree), "branch": branch,
            "status": "PREPARING", "created_at": utc_now(),
        }
    preview = {
        "status": "RECONCILE_PREVIEW", "run_id": state["run_id"],
        "target_branch": target_branch, "target_sha": target_sha,
        "source_candidate_sha": source, "worktree": str(worktree), "branch": branch,
        "attempt_number": number,
    }
    if not args.apply:
        emit(preview)
    if new_preparation:
        attempts.append(preparing)
        integration.update({
            "status": "PREPARING", "target_branch": target_branch,
            "observed_target_sha": target_sha, "current_attempt": number,
        })
        append_event(state, "RECONCILIATION_PREPARING", preparing)
        save_state(state)
    secure_directory(worktree.parent)
    records = {Path(str(item["path"])).resolve(): item for item in registered_worktrees(project)}
    registered = records.get(worktree.resolve())
    if registered:
        expected_ref = f"refs/heads/{branch}"
        if registered.get("branch") != expected_ref:
            raise WorkflowError("reconciliation worktree is registered on an unexpected branch")
    elif worktree.exists():
        raise WorkflowError("reconciliation path exists but is not a registered Git worktree")
    else:
        branch_exists = run(
            ("git", "-C", str(project), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
            check=False,
        ).returncode == 0
        if branch_exists:
            branch_head = git(project, "rev-parse", branch)
            checked_out = {item.get("branch") for item in registered_worktrees(project)}
            if branch_head != target_sha or f"refs/heads/{branch}" in checked_out:
                raise WorkflowError("reconciliation branch exists with unexpected authority")
            git(project, "worktree", "add", str(worktree), branch)
        else:
            git(project, "worktree", "add", "-b", branch, str(worktree), target_sha)
    markers = git_operation_markers(worktree)
    if "MERGE_HEAD" in markers:
        unmerged = bool(git(worktree, "diff", "--name-only", "--diff-filter=U"))
        merge_status = "CONFLICT" if unmerged else "READY_FOR_COMMIT"
        git_output = "recovered existing merge operation"
    else:
        head = git(worktree, "rev-parse", "HEAD")
        if head != target_sha:
            raise WorkflowError("reconciliation worktree moved before its merge was recorded")
        result = run(("git", "-C", str(worktree), "merge", "--no-ff", "--no-commit", source), check=False)
        unmerged = bool(git(worktree, "diff", "--name-only", "--diff-filter=U"))
        if result.returncode and not unmerged:
            raise WorkflowError((result.stdout + result.stderr).strip() or "reconciliation merge failed")
        merge_status = "CONFLICT" if unmerged else "READY_FOR_COMMIT"
        git_output = (result.stdout + result.stderr).strip()
    preparing["status"] = merge_status
    preparing["prepared_at"] = utc_now()
    state["integration"].update({
        "status": "RECONCILING", "target_branch": target_branch,
        "observed_target_sha": target_sha, "current_attempt": number,
    })
    append_event(state, "RECONCILIATION_STARTED", preparing)
    save_state(state)
    emit({
        **preview,
        "status": "RECONCILIATION_CONFLICT" if merge_status == "CONFLICT" else "RECONCILIATION_READY_FOR_COMMIT",
        "git_output": git_output,
    }, 3 if merge_status == "CONFLICT" else 0)


def command_submit_reconciliation(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    integration = state.get("integration") or {}
    attempts = integration.get("attempts") or []
    if integration.get("status") != "RECONCILING" or not attempts:
        raise WorkflowError("no reconciliation attempt is ready for submission")
    requested_number = args.attempt_number or integration.get("current_attempt")
    matching = [item for item in attempts if item.get("number") == requested_number]
    if len(matching) != 1:
        raise WorkflowError(f"reconciliation attempt not found: {requested_number}")
    attempt = matching[0]
    if attempt.get("number") != integration.get("current_attempt"):
        raise WorkflowError("only integration.current_attempt may be submitted")
    if attempt.get("status") not in {"READY_FOR_COMMIT", "CONFLICT"}:
        raise WorkflowError(f"reconciliation attempt is not submittable: {attempt.get('status')}")
    worktree = Path(attempt["worktree"])
    ensure_clean(worktree, "reconciliation")
    head = git(worktree, "rev-parse", "HEAD")
    if head == attempt["target_sha"]:
        raise WorkflowError("reconciliation has no committed merge result")
    for ancestor, label in (
        (attempt["target_sha"], "target"), (attempt["source_candidate_sha"], "source candidate"),
    ):
        if run(("git", "-C", str(worktree), "merge-base", "--is-ancestor", ancestor, head), check=False).returncode:
            raise WorkflowError(f"reconciliation HEAD does not contain the {label} SHA")
    batch = f"INTEGRATION_{attempt['number']:02d}"
    state["integration"].update({
        "status": "REVIEW_REQUIRED", "approved_target_sha": attempt["target_sha"],
        "candidate_sha": head, "source_implementation_worktree": state["implementation_worktree"],
        "source_implementation_branch": state["implementation_branch"],
    })
    state["implementation_worktree"] = str(worktree)
    state["implementation_branch"] = attempt["branch"]
    state["batches"].append(batch)
    contract = state["acceptance_contract"]
    contract["criteria"].append({
        "id": f"{batch}-combined-behavior", "batch": batch,
        "source": "WORKFLOW_DERIVED", "observation": "combined target and candidate behavior",
        "expected_result": "the merge preserves accepted plan behavior without integration regressions",
        "scope": ["."], "rationale": "reconciliation changes the reviewed base",
        "evidence_kind": "REPOSITORY_ASSERTION", "argv": [], "expected_exit": 0,
    })
    contract["digest"] = contract_digest(contract["criteria"])
    state["final_verification"] = None
    state["status"] = "IMPLEMENTING"
    attempt.update({"status": "SUBMITTED", "submitted_sha": head, "batch": batch})
    append_event(state, "RECONCILIATION_SUBMITTED", attempt)
    save_state(state)
    emit({
        "status": "RECONCILIATION_REVIEW_REQUIRED", "run_id": state["run_id"],
        "batch": batch, "reviewed_sha": head,
        "next_command": f"review --batch {batch}",
    })


def command_abandon_reconciliation(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if state.get("status") != "READY_TO_FINALIZE":
        raise WorkflowError(
            f"abandon-reconciliation requires READY_TO_FINALIZE, found {state.get('status')}"
        )
    integration = state.get("integration") or {}
    attempts = integration.get("attempts") or []
    requested_number = args.attempt_number or integration.get("current_attempt")
    matching = [item for item in attempts if item.get("number") == requested_number]
    if len(matching) != 1:
        raise WorkflowError(f"reconciliation attempt not found: {requested_number}")
    attempt = matching[0]
    if attempt.get("number") != integration.get("current_attempt"):
        raise WorkflowError("only integration.current_attempt may be abandoned")
    if attempt.get("status") not in {"PREPARING", "READY_FOR_COMMIT", "CONFLICT"}:
        raise WorkflowError(f"reconciliation attempt is not active: {attempt.get('status')}")
    preview = {
        "status": "ABANDON_RECONCILIATION_PREVIEW", "run_id": state["run_id"],
        "attempt_number": attempt["number"], "worktree": attempt["worktree"],
        "branch": attempt["branch"], "reason": args.reason, "actor": args.actor,
        "resources_preserved": True,
    }
    if not args.apply:
        emit(preview)
    attempt.update({
        "status": "ABANDONED", "abandoned_at": utc_now(),
        "abandon_reason": args.reason, "abandoned_by": args.actor,
    })
    integration.update({"status": "DIVERGED", "current_attempt": None})
    append_event(state, "RECONCILIATION_ABANDONED", {
        "attempt_number": attempt["number"], "reason": args.reason,
        "actor": args.actor, "resources_preserved": True,
    })
    save_state(state)
    emit({**preview, "status": "RECONCILIATION_ABANDONED"})


def command_supersede(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] in TERMINAL_STATUSES:
        emit({"status": "ALREADY_TERMINAL", "run_id": state["run_id"], "run_status": state["status"]})
    if not args.apply:
        emit(
            {
                "status": "SUPERSEDE_PREVIEW", "run_id": state["run_id"],
                "implementation_branch": state["implementation_branch"],
                "implementation_worktree": state["implementation_worktree"],
                "reviewer_worktree": state["reviewer_worktree"],
                "preserved": True,
            }
        )
    state["status"] = "SUPERSEDED"
    state["superseded_at"] = utc_now()
    save_state(state)
    emit({"status": "SUPERSEDED", "run_id": state["run_id"], "artifacts_preserved": True})


def command_change_reviewer(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    validate_active_state(project, state)
    if state.get("status") in {"FINALIZING", "READY_TO_FINALIZE", "FINAL_VERIFICATION_REQUIRED"}:
        raise WorkflowError(f"reviewer change is not meaningful from workflow status {state.get('status')}")
    if not shutil.which(args.reviewer):
        raise WorkflowError(f"reviewer CLI is not available on PATH: {args.reviewer}")
    runtime = reviewer_runtime(args, args.reviewer)
    previous = state["reviewer"]
    previous_runtime = state.get("reviewer_runtime") or {}
    if previous == args.reviewer and previous_runtime == runtime:
        emit({
            "status": "REVIEWER_ALREADY_SELECTED", "run_id": state["run_id"],
            "reviewer": previous, "implementer": state.get("implementer"),
            "same_as_implementer_allowed": True,
        })
    if not args.reason.strip() or not args.actor.strip():
        raise WorkflowError("reviewer change requires nonempty --reason and --actor")
    change = {
        "id": f"decision-{len(state.get('decisions', [])) + 1:03d}",
        "type": "REVIEWER_RUNTIME_CHANGED" if previous == args.reviewer else "REVIEWER_CHANGED",
        "from_reviewer": previous,
        "to_reviewer": args.reviewer, "reason": args.reason.strip(),
        "actor": args.actor.strip(), "created_at": utc_now(),
        "quality_rounds_reset": False, "invocation_budgets_reset": False,
        "finding_ledger_reset": False,
        "reviewer_runtime": runtime,
        "from_reviewer_runtime": previous_runtime,
    }
    preview = {
        "status": "REVIEWER_CHANGE_PREVIEW", "run_id": state["run_id"],
        "change": change, "run_status_unchanged": state["status"],
    }
    if not args.apply:
        emit(preview)
    state["reviewer"] = args.reviewer
    state["reviewer_runtime"] = runtime
    history = state.setdefault("reviewer_history", [{
        "reviewer": previous, "selected_at": state.get("created_at"),
        "source": "LEGACY_STATE",
    }])
    history.append({
        "reviewer": args.reviewer, "selected_at": change["created_at"],
        "source": "USER_CHANGE", "decision_id": change["id"],
        "actor": change["actor"], "reason": change["reason"],
    })
    state.setdefault("decisions", []).append(change)
    append_event(state, "REVIEWER_CHANGED", change)
    save_state(state)
    emit({
        **preview, "status": "REVIEWER_CHANGED", "reviewer": args.reviewer,
        "implementer": state.get("implementer"),
        "same_as_implementer": args.reviewer == state.get("implementer"),
        "reviewer_runtime": runtime,
    })


def _shared_verification_worktree(run_root: Path, sha: str) -> Path:
    """Where verifications of ``sha`` read the repository from. One directory per SHA, shared.

    Every verification used to get its own checkout. It cannot need one: the sandbox mounts the
    tree ``--ro-bind`` at ``/mnt``, so two verifications of the same commit receive a
    byte-identical tree that neither of them is able to modify. Measured on the run this function
    comes from -- 223 verifications, 19 distinct SHAs, and ``worktree_changes`` empty 223 times
    out of 223 -- that was twelve checkouts made for every one that carried distinct content.

    Sharing here needs no reset between uses, and that is the point: a `git clean` between
    verifications would make isolation depend on the reset succeeding, where the read-only mount
    makes it a property of how the sandbox is built.
    """
    return run_root / "verification_worktrees" / sha[:12]


def _ensure_verification_worktree(project: Path, worktree: Path, sha: str) -> None:
    """Create the shared checkout, or confirm the one already there is the right commit.

    The path is keyed by SHA, so a mismatch means something outside this code moved it. Rebuilding
    rather than trusting the name keeps "the tree at /mnt is that commit" true by verification and
    not by convention.
    """
    if worktree.exists():
        try:
            if git(worktree, "rev-parse", "HEAD").strip() == sha:
                return
        except WorkflowError:
            pass
        git(project, "worktree", "remove", "--force", str(worktree))
    secure_directory(worktree.parent)
    git(project, "worktree", "add", "--detach", str(worktree), sha)


def _registered_worktrees_under(project: Path, root: Path) -> list[Path]:
    """Every worktree git has registered beneath ``root``, sorted deepest-first.

    The authority on which worktrees exist is git, not ``state["verification_worktrees"]``.
    A verification attempt appends its worktree to that list and the list reaches disk only when
    the attempt finishes; an attempt killed mid-run leaves the worktree registered and the record
    gone. Four such orphans survived the run this function exists because of -- cleanup walked its
    own bookkeeping, could not see them, and reported success. Asking git retires the whole class:
    no future gap in the record can strand a worktree, because the record is no longer what
    cleanup consults.
    """
    listing = git(project, "worktree", "list", "--porcelain")
    found: list[Path] = []
    root = root.resolve()
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line[len("worktree "):].strip()).resolve()
        if candidate == root:
            continue
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        found.append(candidate)
    return sorted(found, key=lambda path: len(path.parts), reverse=True)


def _disposable_temp_dirs(root: Path) -> list[Path]:
    """The per-attempt sandbox scratch directories under ``root``.

    ``TMPDIR`` for each sandboxed verification. Nothing reads them once the command exits -- the
    evidence is ``evidence.json``, ``logs/`` and ``sandbox_manifest.json`` beside them, and those
    stay. They were never cleaned by anything: one run of 223 verifications left 230 of these
    holding 7.7 GB, against 1.9 MB of actual evidence, because a full test suite writes its
    per-test temporary tree here every single time.
    """
    verification_root = root / "verification_runs"
    if not verification_root.is_dir():
        return []
    return sorted(
        path for path in verification_root.glob("*/attempt_*/tmp") if path.is_dir()
    )


def _directory_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def command_cleanup(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state = load_state(project, args.run_id)
    if state["status"] not in TERMINAL_STATUSES:
        raise WorkflowError("cleanup requires FINALIZED, SUPERSEDED, or ABANDONED status")
    run_root = Path(state["run_directory"]).resolve()
    primary = [Path(state["reviewer_worktree"]), Path(state["implementation_worktree"])]
    existing = [path for path in primary if path.exists()]
    primary_resolved = {path.resolve() for path in existing}
    # Ask git, not this run's own bookkeeping. See _registered_worktrees_under.
    existing_verification = [
        path for path in _registered_worktrees_under(project, run_root)
        if path not in primary_resolved
    ]
    temp_dirs = _disposable_temp_dirs(run_root)
    for path in existing:
        ensure_clean(path, path.name)
    if not args.apply:
        emit(
            {
                "status": "CLEANUP_PREVIEW", "run_id": state["run_id"],
                "worktrees": [str(path) for path in existing + existing_verification],
                "temp_directories": len(temp_dirs),
                "temp_bytes": sum(_directory_bytes(path) for path in temp_dirs),
                "run_artifacts_preserved": state["run_directory"],
            }
        )
    for path in existing:
        git(project, "worktree", "remove", str(path))
    for path in existing_verification:
        # Verification worktrees are disposable and may contain test-generated files.
        git(project, "worktree", "remove", "--force", str(path))
    removed_temp_bytes = 0
    for path in temp_dirs:
        removed_temp_bytes += _directory_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
    state["worktrees_removed"] = True
    save_state(state)
    emit(
        {
            "status": "WORKTREES_REMOVED", "run_id": state["run_id"],
            "removed": [str(path) for path in existing + existing_verification],
            "temp_directories_removed": len(temp_dirs),
            "temp_bytes_removed": removed_temp_bytes,
            "run_artifacts_preserved": state["run_directory"],
        }
    )


def add_project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)


def add_run_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add_codex_selection(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--codex-model",
            help=f"override reviewer default {DEFAULT_CODEX_REVIEWER_MODEL!r}; use 'cli-default' to defer")
        command.add_argument(
            "--codex-model-provider",
            help="configured Codex model_provider, including an OpenAI-compatible DeepSeek gateway")
        command.add_argument("--codex-profile", help="CODEX_HOME profile name")

    def add_reviewer_selection(command: argparse.ArgumentParser) -> None:
        add_codex_selection(command)
        command.add_argument(
            "--claude-model",
            help=f"override reviewer default {DEFAULT_CLAUDE_REVIEWER_MODEL!r}; use 'cli-default' to defer")
        command.add_argument(
            "--dsh-model",
            help="override the dsh reviewer model (read from ~/.dsh/settings.yaml when omitted); "
                 "use 'cli-default' to defer to the harness")
        command.add_argument(
            "--dsh-model-provider",
            help="override the dsh reviewer model provider (defaults to deepseek-official)")

    preflight = commands.add_parser("preflight", help="inspect local target and upstream refs")
    add_project_argument(preflight)
    preflight.add_argument("--target-branch")
    preflight.set_defaults(func=command_preflight)

    listing = commands.add_parser("list", help="list centralized runs for a project")
    add_project_argument(listing)
    listing.set_defaults(func=command_list)

    init = commands.add_parser("init", help="create an isolated implementation run")
    add_project_argument(init)
    init.add_argument("--plan", required=True)
    init.add_argument(
        "--batch-manifest", required=True,
        help="host-authored run scope and mapping for every declared batch",
    )
    init.add_argument("--reviewer", required=True, choices=SUPPORTED_REVIEWERS)
    init.add_argument("--implementer", default="current-host-agent")
    init.add_argument("--fix-policy", choices=("ask", "auto", "never"), default="ask")
    init.add_argument("--batches", required=True)
    init.add_argument("--target-branch")
    add_reviewer_selection(init)
    init.set_defaults(func=command_init)

    review = commands.add_parser("review", help="review the current committed batch SHA")
    add_project_argument(review)
    add_run_argument(review)
    review.add_argument("--batch", required=True)
    review.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    review.add_argument("--dry-run", action="store_true")
    review.set_defaults(func=command_review)

    contract_review = commands.add_parser("contract-review", help="independently normalize or challenge plan acceptance boundaries")
    add_project_argument(contract_review)
    add_run_argument(contract_review)
    contract_review.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    contract_review.add_argument("--dry-run", action="store_true")
    contract_review.set_defaults(func=command_contract_review)

    contract_adjudicate = commands.add_parser("contract-adjudicate", help="record a user-approved acceptance contract decision")
    add_project_argument(contract_adjudicate)
    add_run_argument(contract_adjudicate)
    contract_adjudicate.add_argument("--decision-file", required=True)
    contract_adjudicate.add_argument("--apply", action="store_true")
    contract_adjudicate.set_defaults(func=command_contract_adjudicate)

    verify = commands.add_parser("verify", help="preview or execute a reviewer-requested fixed-SHA command")
    add_project_argument(verify)
    add_run_argument(verify)
    verify.add_argument("--request-id", required=True)
    verify.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    verify.add_argument("--reject-reason")
    verify.add_argument("--network-policy", choices=("offline", "host"), default="offline")
    verify.add_argument("--network-reason", help="auditable reason for direct host-network authorization")
    verify.add_argument("--actor", help="identity authorizing direct host-network access")
    verify.add_argument("--apply", action="store_true")
    verify.set_defaults(func=command_verify)

    adjudicate = commands.add_parser("adjudicate", help="apply one typed pending workflow decision")
    add_project_argument(adjudicate)
    add_run_argument(adjudicate)
    adjudicate.add_argument("--decision-id", required=True)
    adjudicate.add_argument("--choice", required=True)
    adjudicate.add_argument("--reason", required=True)
    adjudicate.add_argument("--actor", required=True)
    adjudicate.add_argument("--finding-ids")
    adjudicate.add_argument("--apply", action="store_true")
    adjudicate.set_defaults(func=command_adjudicate)

    accept = commands.add_parser("accept", help="accept an exact PASS-reviewed batch SHA")
    add_project_argument(accept)
    add_run_argument(accept)
    accept.add_argument("--batch", required=True)
    accept.add_argument("--review-file", required=True)
    accept.set_defaults(func=command_accept)

    change_reviewer = commands.add_parser(
        "change-reviewer", help="auditably select a different isolated reviewer CLI",
    )
    add_project_argument(change_reviewer)
    add_run_argument(change_reviewer)
    change_reviewer.add_argument("--reviewer", required=True, choices=SUPPORTED_REVIEWERS)
    change_reviewer.add_argument("--reason", required=True)
    change_reviewer.add_argument("--actor", required=True)
    change_reviewer.add_argument("--apply", action="store_true")
    add_reviewer_selection(change_reviewer)
    change_reviewer.set_defaults(func=command_change_reviewer)

    status = commands.add_parser("status", help="show active or historical run state")
    add_project_argument(status)
    add_run_argument(status)
    status.set_defaults(func=command_status)

    migrate = commands.add_parser("migrate", help="preview or apply an explicit legacy-run schema migration")
    add_project_argument(migrate)
    add_run_argument(migrate)
    migrate.add_argument("--apply", action="store_true")
    migrate.set_defaults(func=command_migrate)

    finalize = commands.add_parser("finalize", help="preview or apply fast-forward integration")
    add_project_argument(finalize)
    add_run_argument(finalize)
    finalize.add_argument("--apply", action="store_true")
    finalize.set_defaults(func=command_finalize)

    reconcile = commands.add_parser(
        "reconcile", help="preview or create an isolated merge reconciliation worktree",
    )
    add_project_argument(reconcile)
    add_run_argument(reconcile)
    reconcile.add_argument("--target-branch")
    reconcile.add_argument("--apply", action="store_true")
    reconcile.set_defaults(func=command_reconcile)

    submit_reconciliation = commands.add_parser(
        "submit-reconciliation", help="freeze a committed reconciliation for normal review",
    )
    add_project_argument(submit_reconciliation)
    add_run_argument(submit_reconciliation)
    submit_reconciliation.add_argument("--attempt-number", type=int)
    submit_reconciliation.set_defaults(func=command_submit_reconciliation)

    abandon_reconciliation = commands.add_parser(
        "abandon-reconciliation", help="preserve and deactivate the current reconciliation attempt",
    )
    add_project_argument(abandon_reconciliation)
    add_run_argument(abandon_reconciliation)
    abandon_reconciliation.add_argument("--attempt-number", type=int)
    abandon_reconciliation.add_argument("--reason", required=True)
    abandon_reconciliation.add_argument("--actor", required=True)
    abandon_reconciliation.add_argument("--apply", action="store_true")
    abandon_reconciliation.set_defaults(func=command_abandon_reconciliation)

    supersede = commands.add_parser("supersede", help="preserve but deactivate an obsolete run")
    add_project_argument(supersede)
    add_run_argument(supersede)
    supersede.add_argument("--apply", action="store_true")
    supersede.set_defaults(func=command_supersede)

    cleanup = commands.add_parser("cleanup", help="unregister worktrees for a terminal run")
    add_project_argument(cleanup)
    add_run_argument(cleanup)
    cleanup.add_argument("--apply", action="store_true")
    cleanup.set_defaults(func=command_cleanup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = resolve_project(args.project)
        project_root = project_directory(project)
        secure_directory(state_home())
        secure_directory(state_home() / "projects")
        secure_directory(project_root)
        with file_lock(command_lock_path(project, project_root, args)):
            if args.command == "finalize" and args.apply:
                # Runs execute independently, but advancing repository refs is shared authority.
                with file_lock(project_root / "integration.lock"):
                    args.func(args)
            else:
                args.func(args)
    except EnvironmentDriftError as exc:
        print(json.dumps({"status": "ENVIRONMENT_DRIFT", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except WorkflowError as exc:
        print(json.dumps({"status": "WORKFLOW_ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
