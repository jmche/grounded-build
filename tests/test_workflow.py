from __future__ import annotations

import argparse
import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_ROOT / "scripts" / "workflow.py"
SPEC = importlib.util.spec_from_file_location("ipwr_workflow", WORKFLOW)
assert SPEC and SPEC.loader
WORKFLOW_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKFLOW_MODULE)


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ipwr-v2-test-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.plan = self.root / "plan.md"
        self.batch_manifest = self.root / "batches.md"
        self.bin_dir = self.root / "bin"
        self.state_home = self.root / "central-state"
        self.bin_dir.mkdir()
        self.plan.write_text(
            "# Plan\n\n## Batch 1\n\nChange tracked.txt and review it.\n",
            encoding="utf-8",
        )
        self.batch_manifest.write_text(
            "# Run scope\n\nIncluded: plan batch 1.\n\nExcluded: none.\n",
            encoding="utf-8",
        )
        self.run_command("git", "init", "-q", "-b", "main", str(self.project))
        self.run_command("git", "-C", str(self.project), "config", "user.email", "test@example.invalid")
        self.run_command("git", "-C", str(self.project), "config", "user.name", "Skill Test")
        (self.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "tracked.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "base")
        self.make_fake_reviewer("codex")
        self.make_fake_reviewer("claude")
        self.make_fake_reviewer("dsh")
        self.make_fake_reviewer("other")
        code_mode_host = self.bin_dir / "codex-code-mode-host"
        code_mode_host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        code_mode_host.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.bin_dir}{os.pathsep}{self.environment['PATH']}"
        self.environment["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        self.environment["GROUNDED_BUILD_TESTING"] = "1"
        self.environment["GROUNDED_BUILD_OTHER_COMMAND"] = str(self.bin_dir / "other")
        dsh_home = self.root / "dsh-home"
        dsh_home.mkdir()
        (dsh_home / "settings.yaml").write_text(
            "agent-default-model:\n  provider: deepseek-official\n"
            "  model: deepseek-v4-flash\n", encoding="utf-8")
        self.environment["DSH_HOME"] = str(dsh_home)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def run_command(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, text=True, capture_output=True, check=True)

    def make_fake_reviewer(self, name: str) -> None:
        path = self.bin_dir / name
        path.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                import time

                if "--version" in sys.argv:
                    print("fake-{name} 1.0")
                    raise SystemExit(0)
                if len(sys.argv) > 1 and sys.argv[1] == "capabilities":
                    print(json.dumps({{"protocol": "grounded-build-other-v1", "read_only": True,
                                      "structured_output": True, "fresh_process": True}}))
                    raise SystemExit(0)
                if "--help" in sys.argv:
                    print("--profile headless --patch --dump-config --json-schema --output-format "
                          "--permission-mode --no-session-persistence --output-schema "
                          "--output-last-message --ephemeral --sandbox --config")
                    raise SystemExit(0)
                bridge = len(sys.argv) > 1 and sys.argv[1] == "run"
                prompt = (open(sys.argv[sys.argv.index("--prompt") + 1], encoding="utf-8").read()
                          if bridge else sys.argv[-1])
                if "This is a capability check." in prompt:
                    probe_path = re.search(r"Read (/.+?/probe-input\\.txt)\\.", prompt).group(1)
                    with open(probe_path, encoding="utf-8") as handle:
                        observed = handle.read().rstrip("\\n")
                    payload = {{"provider": "{name}", "ready": True, "observed": observed}}
                    if bridge or "{name}" == "codex":
                        flag = "--output" if bridge else "-o"
                        with open(sys.argv[sys.argv.index(flag) + 1], "w", encoding="utf-8") as handle:
                            json.dump(payload, handle)
                    elif "{name}" == "dsh":
                        print(json.dumps(payload))
                    else:
                        print(json.dumps({{"structured_output": payload}}))
                    raise SystemExit(0)
                if "{name}" == "codex" and os.environ.get("FAKE_CODEX_ROUTER_ERROR") == "1":
                    print(
                        "2026-09-09T00:00:00Z ERROR codex_core::tools::router: "
                        "error=failed to spawn code-mode host /opt/codex-code-mode-host: "
                        "No such file or directory (os error 2)",
                        file=sys.stderr,
                    )
                if os.environ.get("FAKE_{name.upper()}_SLEEP"):
                    time.sleep(float(os.environ["FAKE_{name.upper()}_SLEEP"]))
                if "{name}" == "claude" and os.environ.get("FAKE_CLAUDE_QUOTA_WRAPPER") == "1":
                    print(json.dumps({{"is_error": True, "api_error_status": 429,
                                      "result": "five-hour token quota exhausted"}}))
                    raise SystemExit(0)
                if os.environ.get("FAKE_{name.upper()}_QUOTA") == "1":
                    print("429 rate limit: five-hour token quota exhausted", file=sys.stderr)
                    raise SystemExit(42)
                if os.environ.get("FAKE_{name.upper()}_NON_RATE") == "1":
                    print("configuration error: unknown field quota_policy; "
                          "request id 429 failed validation", file=sys.stderr)
                    raise SystemExit(42)
                if os.environ.get("FAKE_{name.upper()}_EXIT") == "1":
                    raise SystemExit(42)
                def field(label):
                    match = re.search(rf"- {{label}}: `([^`]+)`", prompt)
                    if not match:
                        raise SystemExit(f"missing {{label}}")
                    return match.group(1)

                if "## Contract review assignment" in prompt:
                    batches = field("Batches").split(",")
                    with open(field("Authoritative run scope and batch manifest"), encoding="utf-8") as handle:
                        manifest_text = handle.read()
                    if any(batch not in manifest_text for batch in batches):
                        raise SystemExit("batch manifest omitted a declared batch")
                    needs_decision = os.environ.get("FAKE_CONTRACT_NEEDS_DECISION") == "1"
                    command_contract = os.environ.get("FAKE_CONTRACT_COMMAND") == "1"
                    payload = {{
                        "reviewer": "{name}",
                        "baseline_sha": field("Baseline SHA"),
                        "assessment": "NEEDS_USER_DECISION" if needs_decision else "READY",
                        "summary": "fixture contract ready",
                        "criteria": [] if needs_decision else [{{
                            "id": f"{{batch}}-exit", "batch": batch,
                            "source": "REVIEWER_DERIVED",
                            "observation": "tracked.txt has implemented content",
                            "expected_result": "the named behavior is present",
                            "scope": ["tracked.txt"],
                            "rationale": "finite fixture observation",
                            "evidence_kind": "COMMAND" if command_contract else "REPOSITORY_ASSERTION",
                            "argv": [sys.executable, "-c", "print('contract verified')"] if command_contract else [],
                            "expected_exit": 0,
                        }} for batch in batches],
                        "issues": [{{
                            "id": "boundary-1", "batch": batches[0],
                            "problem": "fixture boundary is ambiguous",
                            "decision_required": "choose the observable boundary",
                            "proposed_observation": "tracked.txt equals implemented",
                        }}] if needs_decision else [],
                    }}
                else:
                    requests = json.loads(os.environ.get("FAKE_VERIFICATION_REQUESTS", "[]"))
                    injected_findings = json.loads(os.environ.get("FAKE_FINDINGS", "[]"))
                    needs_review_decision = os.environ.get("FAKE_REVIEW_NEEDS_DECISION") == "1"
                    evidence_ids = []
                    evidence_match = re.search(r"- Fixed-SHA verification evidence: `([^`]+)`", prompt)
                    if evidence_match:
                        with open(evidence_match.group(1), encoding="utf-8") as handle:
                            evidence_ids = [item["evidence_id"] for item in json.load(handle)["evidence"]]
                    with open(field("Acceptance contract"), encoding="utf-8") as handle:
                        contract_criteria = json.load(handle)["criteria"]
                    cumulative = field("Cumulative final review") == "yes"
                    selected_criteria = [
                        item for item in contract_criteria
                        if cumulative or item["batch"] == field("Batch")
                    ]
                    payload = {{
                        "reviewer": "{name}",
                        "reviewed_sha": field("Head SHA"),
                        "base_sha": field("Base SHA"),
                        "batch": field("Batch"),
                        "verdict": "NEEDS_VERIFICATION" if requests else ("NEEDS_USER_DECISION" if needs_review_decision else ("FAIL" if injected_findings else "PASS")),
                        "summary": "fixture review",
                        "findings": injected_findings,
                        "resolved_finding_ids": json.loads(
                            os.environ.get("FAKE_RESOLVED_FINDING_IDS", "[]")
                        ),
                        "verification_requests": requests,
                        "criterion_results": [] if requests else [{{
                            "criterion_id": item["id"],
                            "status": "PASS", "evidence_ids": evidence_ids if item.get("evidence_kind") == "COMMAND" else [],
                            "rationale": "fixture repository assertion passed",
                        }} for item in selected_criteria],
                    }}
                if bridge or "{name}" == "codex":
                    flag = "--output" if bridge else "-o"
                    output = sys.argv[sys.argv.index(flag) + 1]
                    with open(output, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                elif "{name}" == "dsh":
                    print(json.dumps(payload))
                else:
                    print(json.dumps({{"structured_output": payload}}))
                """
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)

    def workflow(self, *args: str, expected: int = 0) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(WORKFLOW), *args],
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, expected, msg=result.stderr + result.stdout)
        return json.loads(result.stdout)

    def initialize(
        self, reviewer: str = "codex", implementer: str = "current-host-agent"
    ) -> dict[str, object]:
        initialized = self.initialize_raw(reviewer, implementer)
        self.assertEqual(initialized["status"], "AWAITING_CONTRACT_REVIEW")
        contract = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(contract["status"], "CONTRACT_READY")
        return initialized

    def initialize_raw(
        self, reviewer: str = "codex", implementer: str = "current-host-agent"
    ) -> dict[str, object]:
        return self.workflow(
            "init",
            "--project", str(self.project),
            "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest),
            "--reviewer", reviewer,
            "--implementer", implementer,
            "--fix-policy", "ask",
            "--batches", "1",
            "--target-branch", "main",
        )

    def write_authenticated_legacy_state(
        self, state_path: Path, state: dict[str, object], schema: int,
    ) -> None:
        """Reseal a fixture as a genuine checkpointed legacy state."""
        state["schema_version"] = schema
        events = state["events"]
        assert isinstance(events, list) and events
        reference = events[-1]
        assert isinstance(reference, dict)
        event_path = Path(str(reference["path"]))
        event = json.loads(event_path.read_text(encoding="utf-8"))
        event["details"]["state_digest"] = WORKFLOW_MODULE.state_authority_digest(state)
        event.pop("event_hash", None)
        event_hash = WORKFLOW_MODULE.sha256_bytes(
            json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        )
        event["event_hash"] = event_hash
        reference["event_hash"] = event_hash
        event_path.write_text(json.dumps(event), encoding="utf-8")
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def reseal_state_checkpoint(self, state_path: Path, state: dict[str, object]) -> None:
        events = state["events"]
        assert isinstance(events, list) and events
        reference = events[-1]
        assert isinstance(reference, dict)
        event_path = Path(str(reference["path"]))
        event = json.loads(event_path.read_text(encoding="utf-8"))
        event["details"]["state_digest"] = WORKFLOW_MODULE.state_authority_digest(state)
        event.pop("event_hash", None)
        event_hash = WORKFLOW_MODULE.sha256_bytes(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        event["event_hash"] = event_hash
        reference["event_hash"] = event_hash
        event_path.write_text(json.dumps(event), encoding="utf-8")
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_preflight_and_init_record_controller_and_project_runtime(self) -> None:
        preflight = self.workflow(
            "preflight", "--project", str(self.project), "--target-branch", "main"
        )
        controller = preflight["controller_runtime"]
        self.assertEqual(controller["resolved_executable"], str(Path(sys.executable).resolve()))
        self.assertTrue(controller["supported"])
        self.assertEqual(
            controller["command_prefix"],
            [str(Path(sys.executable).absolute()), str(WORKFLOW.resolve())],
        )
        project_runtime = preflight["project_runtime"]
        self.assertFalse(project_runtime["project_venv"]["available"])
        self.assertFalse(project_runtime["project_venv"]["worktree_copy_expected"])
        self.assertFalse(project_runtime["uv"]["auto_provision"])

        initialized = self.initialize_raw()
        self.assertEqual(initialized["controller_runtime"], controller)
        self.assertEqual(initialized["project_runtime_at_start"], project_runtime)
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertFalse(status["controller_runtime_drift"])
        self.assertEqual(status["controller_runtime"], controller)

    def test_controller_runtime_drift_is_rejected_but_legacy_runs_remain_usable(self) -> None:
        current = WORKFLOW_MODULE.controller_runtime()
        changed = {**current, "version": "0.0-different"}
        with self.assertRaisesRegex(
            WORKFLOW_MODULE.WorkflowError, "controller runtime changed"
        ):
            WORKFLOW_MODULE.validate_controller_runtime({"controller_runtime": changed})
        WORKFLOW_MODULE.validate_controller_runtime({})

    def test_engine_migration_is_preview_first_and_restores_an_active_run(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["controller_runtime"]["engine_files"]["workflow.py"] = "old-engine"
        self.reseal_state_checkpoint(state_path, state)
        preview = self.workflow(
            "migrate-engine", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--reason", "apply reviewed skill update", "--actor", "test",
        )
        self.assertEqual(preview["status"], "ENGINE_MIGRATION_PREVIEW")
        applied = self.workflow(
            "migrate-engine", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--reason", "apply reviewed skill update", "--actor", "test", "--apply",
        )
        self.assertEqual(applied["status"], "IMPLEMENTING")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertIsNone(status["engine_drift"])

    def test_engine_migration_revalidates_a_current_sha_pass_before_acceptance(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        first = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(first["status"], "REVIEW_PASS")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["controller_runtime"]["engine_files"]["workflow.py"] = "old-engine"
        self.reseal_state_checkpoint(state_path, state)
        migrated = self.workflow(
            "migrate-engine", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--reason", "revalidate current pass", "--actor", "test", "--apply",
        )
        self.assertTrue(migrated["revalidation"]["required"])
        second = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(second["status"], "REVIEW_PASS")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertIsNone(status["engine_revalidation"])

    def commit_batch_change(self, implementation: Path, content: str = "implemented\n") -> str:
        (implementation / "tracked.txt").write_text(content, encoding="utf-8")
        self.run_command("git", "-C", str(implementation), "add", "tracked.txt")
        self.run_command("git", "-C", str(implementation), "commit", "-qm", "implement batch 1")
        return self.run_command("git", "-C", str(implementation), "rev-parse", "HEAD").stdout.strip()

    def test_same_agent_family_can_be_isolated_implementer_and_reviewer(self) -> None:
        initialized = self.initialize("claude", implementer="claude")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["implementer"], "claude")
        self.assertEqual(status["reviewer"], "claude")
        self.assertEqual(status["reviewer_history"][0]["source"], "RUN_INITIALIZED")

    def test_auto_reviewer_prefers_codex_for_non_codex_host(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["reviewer"], "codex")
        self.assertEqual(initialized["host_adapter"], "dsh")
        self.assertTrue(initialized["reviewer_selection_checks"]["codex"]["ok"])

    def test_invalid_environment_is_rejected_before_automatic_reviewer_probe(self) -> None:
        launcher = self.project / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        os.mkfifo(self.project / ".venv" / "unsupported-fifo")
        args = argparse.Namespace(
            project=str(self.project), plan=str(self.plan),
            batch_manifest=str(self.batch_manifest), target_branch="main", batches="1",
            instruction_file=[], reviewer="auto", host_adapter="dsh", implementer="dsh",
            fix_policy="ask",
        )
        with mock.patch.object(
            WORKFLOW_MODULE, "select_implementation_reviewer",
        ) as selection:
            with self.assertRaisesRegex(
                WORKFLOW_MODULE.WorkflowError, "unsupported special file",
            ):
                WORKFLOW_MODULE.command_init(args)
        selection.assert_not_called()

    def test_auto_reviewer_falls_back_to_host_when_codex_read_host_is_missing(self) -> None:
        (self.bin_dir / "codex-code-mode-host").unlink()
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["reviewer"], "dsh")
        self.assertFalse(initialized["reviewer_selection_checks"]["codex"]["ok"])
        self.assertTrue(initialized["reviewer_selection_checks"]["dsh"]["ok"])

    def test_codex_host_auto_reviewer_prefers_claude_then_dsh(self) -> None:
        common = (
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "codex", "--implementer", "codex", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        preferred = self.workflow(*common)
        self.assertEqual(preferred["reviewer"], "claude")

        claude = self.bin_dir / "claude"
        claude.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        claude.chmod(0o755)
        fallback = self.workflow(*common)
        self.assertEqual(fallback["reviewer"], "dsh")
        self.assertFalse(fallback["reviewer_selection_checks"]["claude"]["ok"])
        self.assertTrue(fallback["reviewer_selection_checks"]["dsh"]["ok"])

    def test_other_host_auto_reviewer_prefers_codex(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "other", "--implementer", "other", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["reviewer"], "codex")
        self.assertTrue(initialized["reviewer_selection_checks"]["codex"]["ok"])

    def test_auto_reviewer_rate_limit_falls_back_to_host_for_contract_review(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "other", "--implementer", "other", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.environment["FAKE_CODEX_QUOTA"] = "1"
        fallback = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CODEX_QUOTA")
        self.assertEqual(fallback["status"], "REVIEWER_AUTO_FALLBACK")
        self.assertEqual(fallback["fallback"]["to_reviewer"], "other")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer"], "other")
        self.assertEqual(status["reviewer_requested"], "auto")
        self.assertEqual(status["automatic_reviewer_fallbacks"][0]["to_reviewer"], "other")
        self.assertEqual(status["host_reviewer_runtime"]["adapter"], "other")
        self.assertEqual(status["recorded_status"], "AWAITING_CONTRACT_REVIEW")
        contract = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(contract["status"], "CONTRACT_READY")

    def test_explicit_reviewer_rate_limit_does_not_change_authority(self) -> None:
        initialized = self.initialize_raw("codex", implementer="dsh")
        self.environment["FAKE_CODEX_QUOTA"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CODEX_QUOTA")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer"], "codex")
        self.assertEqual(status["recorded_status"], "AWAITING_CONTRACT_REVIEW")

    def test_user_changed_reviewer_rate_limit_does_not_change_authority(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.workflow(
            "change-reviewer", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--reviewer", "claude",
            "--reason", "User-selected reviewer", "--actor", "test-user", "--apply",
        )
        self.environment["FAKE_CLAUDE_QUOTA"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CLAUDE_QUOTA")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer"], "claude")
        self.assertEqual(status["reviewer_requested"], "claude")
        self.assertEqual(status["automatic_reviewer_fallbacks"], [])

    def test_successful_claude_quota_wrapper_falls_back_to_codex_host(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "codex", "--implementer", "codex", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["reviewer"], "claude")
        self.environment["FAKE_CLAUDE_QUOTA_WRAPPER"] = "1"
        fallback = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CLAUDE_QUOTA_WRAPPER")
        self.assertEqual(fallback["status"], "REVIEWER_AUTO_FALLBACK")
        self.assertEqual(fallback["fallback"]["to_reviewer"], "codex")

    def test_rate_limit_classifier_does_not_match_incidental_rate_text(self) -> None:
        classifier = WORKFLOW_MODULE.planning_isolation_module().is_rate_limit_failure
        self.assertTrue(classifier("429 too many requests"))
        self.assertTrue(classifier("five-hour usage limit reached"))
        self.assertFalse(classifier("separate transport failure"))
        self.assertFalse(classifier("configuration error: unknown field quota_policy"))
        self.assertFalse(classifier("invalid value request_quota in schema"))
        self.assertFalse(classifier("request id 429 failed validation"))

    def test_auto_reviewer_non_rate_failure_does_not_fall_back(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.environment["FAKE_CODEX_NON_RATE"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CODEX_NON_RATE")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer"], "codex")
        self.assertEqual(status["automatic_reviewer_fallbacks"], [])

    def test_auto_reviewer_rate_limit_stops_when_host_is_unavailable(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        host = self.bin_dir / "dsh"
        host.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        host.chmod(0o755)
        self.environment["FAKE_CODEX_QUOTA"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CODEX_QUOTA")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer"], "codex")

    def test_auto_reviewer_rate_limit_falls_back_for_batch_review(self) -> None:
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "dsh", "--implementer", "dsh", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        implementation = Path(str(initialized["implementation_worktree"]))
        reviewed_sha = self.commit_batch_change(implementation)
        self.environment["FAKE_CODEX_QUOTA"] = "1"
        fallback = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=4,
        )
        self.environment.pop("FAKE_CODEX_QUOTA")
        self.assertEqual(fallback["status"], "REVIEWER_AUTO_FALLBACK")
        self.assertEqual(fallback["fallback"]["to_reviewer"], "dsh")
        review = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["reviewed_sha"], reviewed_sha)

    def test_other_only_installation_uses_other_as_implementation_reviewer(self) -> None:
        for reviewer in ("codex", "claude", "dsh"):
            path = self.bin_dir / reviewer
            path.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            path.chmod(0o755)
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "auto",
            "--host-adapter", "other", "--implementer", "other", "--fix-policy", "ask",
            "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["reviewer"], "other")
        self.assertTrue(initialized["reviewer_selection_checks"]["other"]["ok"])

    def test_explicit_other_reviewer_handles_contract_and_fixed_sha_review(self) -> None:
        initialized = self.initialize("other", implementer="other")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["reviewer_runtime"]["adapter"], "other")
        self.assertEqual(status["reviewer_runtime"]["protocol"], "grounded-build-other-v1")
        implementation = Path(str(initialized["implementation_worktree"]))
        reviewed_sha = self.commit_batch_change(implementation)
        review = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["reviewed_sha"], reviewed_sha)
        report = json.loads(Path(str(review["report_path"])).read_text(encoding="utf-8"))
        self.assertEqual(report["reviewer"], "other")

    def test_dsh_reviews_the_contract_and_fixed_sha_batch(self) -> None:
        initialized = self.initialize("dsh")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["reviewer_runtime"]["adapter"], "dsh")
        self.assertEqual(status["reviewer_runtime"]["model"], "deepseek-v4-flash")
        implementation = Path(str(initialized["implementation_worktree"]))
        reviewed_sha = self.commit_batch_change(implementation)
        review = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["reviewed_sha"], reviewed_sha)
        report = json.loads(Path(str(review["report_path"])).read_text(encoding="utf-8"))
        self.assertEqual(report["reviewer"], "dsh")

    def test_dsh_review_does_not_mutate_shared_git_configuration(self) -> None:
        initialized = self.initialize("dsh")
        implementation = Path(str(initialized["implementation_worktree"]))
        before = self.run_command(
            "git", "-C", str(self.project), "config", "--local", "--list"
        ).stdout
        self.commit_batch_change(implementation)
        self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        after = self.run_command(
            "git", "-C", str(self.project), "config", "--local", "--list"
        ).stdout
        self.assertEqual(after, before)
        self.assertNotIn("core.excludesfile", after.lower())

    def test_review_timeout_is_recorded_as_infrastructure_error(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_CODEX_SLEEP"] = "2"
        failed = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
            "--timeout", "1", expected=4,
        )
        self.environment.pop("FAKE_CODEX_SLEEP")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        self.assertIn("timed out", str(failed["reason"]))
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(state["review_invocations"][-1]["status"], "INFRA_ERROR")
        self.assertIn("timed out", state["review_invocations"][-1]["error"])

    def test_zero_exit_codex_tool_host_failure_cannot_approve_contract(self) -> None:
        initialized = self.initialize_raw("codex")
        self.environment["FAKE_CODEX_ROUTER_ERROR"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.environment.pop("FAKE_CODEX_ROUTER_ERROR")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        self.assertIn("TOOL_HOST_STARTUP", str(failed["reason"]))
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertIsNone(state["acceptance_contract"])
        self.assertEqual(state["contract_review_invocations"][-1]["status"], "INFRA_ERROR")

    def test_zero_exit_codex_tool_host_failure_cannot_pass_batch_review(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_CODEX_ROUTER_ERROR"] = "1"
        failed = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
        )
        self.environment.pop("FAKE_CODEX_ROUTER_ERROR")
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        self.assertIn("TOOL_HOST_STARTUP", str(failed["reason"]))
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(state["reviews"], [])
        self.assertEqual(state["review_invocations"][-1]["status"], "INFRA_ERROR")

    def test_reviewer_switch_is_audited_without_resetting_state_or_budget(self) -> None:
        initialized = self.initialize_raw("codex", implementer="claude")
        self.environment["FAKE_CODEX_EXIT"] = "1"
        failed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=4,
        )
        self.assertEqual(failed["status"], "REVIEWER_ERROR")
        self.environment.pop("FAKE_CODEX_EXIT")
        before = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        preview = self.workflow(
            "change-reviewer", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--reviewer", "claude",
            "--reason", "Codex quota exhausted", "--actor", "test-user",
        )
        self.assertEqual(preview["status"], "REVIEWER_CHANGE_PREVIEW")
        unchanged = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(unchanged["reviewer"], "codex")
        changed = self.workflow(
            "change-reviewer", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--reviewer", "claude",
            "--reason", "Codex quota exhausted", "--actor", "test-user", "--apply",
        )
        self.assertEqual(changed["status"], "REVIEWER_CHANGED")
        self.assertTrue(changed["same_as_implementer"])
        after = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(after["recorded_status"], before["recorded_status"])
        self.assertEqual(after["usage"], before["usage"])
        self.assertEqual(after["budgets"], before["budgets"])
        self.assertEqual(after["reviewer"], "claude")
        self.assertEqual(after["decisions"][-1]["type"], "REVIEWER_CHANGED")
        self.assertEqual(after["usage"]["contract_review_invocations"], 1)
        contract = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(contract["status"], "CONTRACT_READY")
        final_status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(final_status["contract_review_invocations"][0]["reviewer"], "codex")
        self.assertEqual(final_status["contract_review_invocations"][0]["status"], "INFRA_ERROR")
        self.assertEqual(final_status["contract_review_invocations"][1]["reviewer"], "claude")

    def review_accept(self, initialized: dict[str, object]) -> tuple[str, dict[str, object]]:
        implementation = Path(str(initialized["implementation_worktree"]))
        head = self.commit_batch_change(implementation)
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["reviewed_sha"], head)
        accepted = self.workflow(
            "accept", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
            "--review-file", str(review["report_path"]),
        )
        self.assertTrue(accepted["all_batches_accepted"])
        return head, review

    def test_original_project_is_untouched_until_finalize(self) -> None:
        original_branch = self.run_command(
            "git", "-C", str(self.project), "branch", "--show-current"
        ).stdout.strip()
        original_head = self.run_command(
            "git", "-C", str(self.project), "rev-parse", "HEAD"
        ).stdout.strip()
        initialized = self.initialize("codex")
        self.assertTrue(initialized["original_branch_unchanged"])
        self.assertTrue(initialized["original_head_unchanged"])
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "branch", "--show-current").stdout.strip(),
            original_branch,
        )
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "HEAD").stdout.strip(),
            original_head,
        )
        implementation = Path(str(initialized["implementation_worktree"]))
        reviewer = Path(str(initialized["reviewer_worktree"]))
        self.assertTrue(str(implementation).startswith(str(self.state_home)))
        self.assertTrue(str(reviewer).startswith(str(self.state_home)))
        self.assertNotEqual(implementation, reviewer)

    def test_full_codex_workflow_and_second_run_use_latest_head(self) -> None:
        initialized = self.initialize("codex")
        first_head, _ = self.review_accept(initialized)
        preview = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["status"], "FINALIZE_PREVIEW")
        finalized = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(finalized["final_sha"], first_head)
        self.assertIn("Integrated: yes", Path(str(finalized["final_report"])).read_text())

        (self.project / "tracked.txt").write_text("later external development\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "tracked.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "later development")
        latest = self.run_command("git", "-C", str(self.project), "rev-parse", "HEAD").stdout.strip()
        second = self.initialize("codex")
        self.assertNotEqual(second["run_id"], initialized["run_id"])
        self.assertEqual(second["baseline_sha"], latest)
        self.assertNotEqual(second["run_directory"], initialized["run_directory"])
        implementation_head = self.run_command(
            "git", "-C", str(second["implementation_worktree"]), "rev-parse", "HEAD"
        ).stdout.strip()
        self.assertEqual(implementation_head, latest)

    def test_finalize_still_refuses_dirty_checked_out_target(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "local-note.txt").write_text("preserve me\n", encoding="utf-8")
        preview = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertTrue(preview["target_checked_out_here"])
        self.assertFalse(preview["target_checkout_clean"])
        self.assertTrue(preview["apply_blocked_by_dirty_target"])
        self.assertIn("?? local-note.txt", preview["target_checkout_changes"])
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertTrue(status["finalize_apply_blocked_by_dirty_target"])
        rejected = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply", expected=1,
        )
        self.assertIn("original project worktree is not clean", str(rejected["error"]))
        self.assertEqual((self.project / "local-note.txt").read_text(encoding="utf-8"), "preserve me\n")

    def test_finalize_merges_the_reviewed_sha_when_implementation_branch_moves(self) -> None:
        initialized = self.initialize("codex")
        reviewed_sha, _ = self.review_accept(initialized)
        implementation = Path(str(initialized["implementation_worktree"]))
        tree = self.run_command("git", "-C", str(implementation), "write-tree").stdout.strip()
        unreviewed_sha = self.run_command(
            "git", "-C", str(implementation), "commit-tree", tree,
            "-p", reviewed_sha, "-m", "unreviewed race commit",
        ).stdout.strip()
        real_git = shutil.which("git")
        assert real_git
        wrapper = self.bin_dir / "git"
        wrapper.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import os
                import subprocess
                import sys
                args = sys.argv[1:]
                if "merge" in args and os.environ.get("GB_RACE_BRANCH"):
                    subprocess.run([
                        {real_git!r}, "-C", os.environ["GB_RACE_WORKTREE"], "update-ref",
                        "refs/heads/" + os.environ["GB_RACE_BRANCH"],
                        os.environ["GB_RACE_SHA"],
                    ], check=True)
                os.execv({real_git!r}, [{real_git!r}, *args])
                """
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.environment.update({
            "GB_RACE_BRANCH": str(initialized["implementation_branch"]),
            "GB_RACE_WORKTREE": str(implementation),
            "GB_RACE_SHA": unreviewed_sha,
        })
        finalized = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(finalized["final_sha"], reviewed_sha)
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "main").stdout.strip(),
            reviewed_sha,
        )
        self.assertEqual(
            self.run_command(
                "git", "-C", str(self.project), "rev-parse",
                str(initialized["implementation_branch"]),
            ).stdout.strip(),
            unreviewed_sha,
        )

    def test_finalize_recovers_after_merge_before_state_commit(self) -> None:
        initialized = self.initialize("codex")
        final_head, _ = self.review_accept(initialized)
        implementation = Path(str(initialized["implementation_worktree"]))
        tree = self.run_command("git", "-C", str(implementation), "write-tree").stdout.strip()
        later_sha = self.run_command(
            "git", "-C", str(implementation), "commit-tree", tree,
            "-p", final_head, "-m", "later unreviewed commit",
        ).stdout.strip()
        prior_home = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
        os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        try:
            state = WORKFLOW_MODULE.load_state(self.project, str(initialized["run_id"]))
            state["status"] = "FINALIZING"
            state["integration_transaction"] = {
                "status": "PREPARED", "target_branch": "main",
                "baseline_sha": state["baseline_sha"], "final_sha": final_head,
                "prepared_at": WORKFLOW_MODULE.utc_now(),
            }
            WORKFLOW_MODULE.append_event(
                state, "INTEGRATION_PREPARED", state["integration_transaction"]
            )
            WORKFLOW_MODULE.save_state(state)
        finally:
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_IMPLEMENT_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = prior_home
        self.run_command(
            "git", "-C", str(self.project), "merge", "--ff-only",
            str(initialized["implementation_branch"]),
        )
        self.run_command(
            "git", "-C", str(implementation), "update-ref",
            "refs/heads/" + str(initialized["implementation_branch"]), later_sha,
        )
        preview = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["status"], "FINALIZE_RECOVERY_PREVIEW")
        recovered = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(recovered["status"], "FINALIZED")
        self.assertEqual(recovered["final_sha"], final_head)

    def test_finalize_continues_after_prepared_implementation_worktree_disappears(self) -> None:
        initialized = self.initialize("codex")
        final_head, _ = self.review_accept(initialized)
        prior_home = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
        os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        try:
            state = WORKFLOW_MODULE.load_state(self.project, str(initialized["run_id"]))
            state["status"] = "FINALIZING"
            state["integration_transaction"] = {
                "status": "PREPARED", "target_branch": "main",
                "baseline_sha": state["baseline_sha"],
                "expected_target_sha": state["baseline_sha"],
                "final_sha": final_head, "prepared_at": WORKFLOW_MODULE.utc_now(),
            }
            WORKFLOW_MODULE.append_event(
                state, "INTEGRATION_PREPARED", state["integration_transaction"]
            )
            WORKFLOW_MODULE.save_state(state)
        finally:
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_IMPLEMENT_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = prior_home
        self.run_command(
            "git", "-C", str(self.project), "worktree", "remove",
            str(initialized["implementation_worktree"]),
        )
        recovered = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(recovered["status"], "FINALIZED")
        self.assertEqual(recovered["final_sha"], final_head)

    def test_two_runs_can_progress_independently_from_one_baseline(self) -> None:
        first = self.initialize("codex", "codex-host")
        second = self.initialize("claude", "claude-host")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["baseline_sha"], second["baseline_sha"])
        listing = self.workflow("list", "--project", str(self.project))
        self.assertEqual(
            set(listing["active_runs"]), {first["run_id"], second["run_id"]}
        )
        ambiguous = self.workflow(
            "status", "--project", str(self.project), expected=1,
        )
        self.assertEqual(ambiguous["status"], "WORKFLOW_ERROR")
        self.assertIn("multiple active runs", str(ambiguous["error"]))
        for initialized in (first, second):
            implementation = Path(str(initialized["implementation_worktree"]))
            self.commit_batch_change(implementation)
            reviewed = self.workflow(
                "review", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]), "--batch", "1",
            )
            self.assertEqual(reviewed["status"], "REVIEW_PASS")

    def test_a_busy_run_lock_does_not_block_another_run(self) -> None:
        first = self.initialize("codex", "codex-host")
        second = self.initialize("claude", "claude-host")
        first_lock = Path(str(first["run_directory"])) / "workflow.lock"
        with WORKFLOW_MODULE.file_lock(first_lock):
            available = self.workflow(
                "status", "--project", str(self.project),
                "--run-id", str(second["run_id"]),
            )
            self.assertEqual(available["run_id"], second["run_id"])
            busy = self.workflow(
                "status", "--project", str(self.project),
                "--run-id", str(first["run_id"]), expected=1,
            )
            self.assertIn("another workflow command is active", str(busy["error"]))

    def test_target_advance_is_integration_drift_not_execution_staleness(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "IMPLEMENTING")
        self.assertEqual(status["integration"]["status"], "DIVERGED")
        reviewed = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(reviewed["status"], "REVIEW_PASS")

    def test_diverged_target_reconciles_as_reviewed_synthetic_batch(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        blocked = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=3,
        )
        self.assertEqual(blocked["status"], "RECONCILIATION_REQUIRED")
        preview = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(preview["status"], "RECONCILE_PREVIEW")
        preview_state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(preview_state["integration"]["attempts"], [])
        reconciled = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(reconciled["status"], "RECONCILIATION_READY_FOR_COMMIT")
        worktree = Path(str(reconciled["worktree"]))
        self.run_command("git", "-C", str(worktree), "commit", "-qm", "merge reviewed candidate")
        submitted = self.workflow(
            "submit-reconciliation", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        batch = str(submitted["batch"])
        self.assertEqual(batch, "INTEGRATION_01")
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", batch,
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        accepted = self.workflow(
            "accept", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", batch,
            "--review-file", str(review["report_path"]),
        )
        self.assertTrue(accepted["all_batches_accepted"])
        final = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(final["status"], "FINALIZED")

    def test_interrupted_reconciliation_creation_is_resumable(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        prior_home = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
        os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        original_run = WORKFLOW_MODULE.run
        try:
            def interrupt_before_merge(command: object, **kwargs: object) -> object:
                if isinstance(command, (tuple, list)) and "merge" in command:
                    raise RuntimeError("simulated interruption before merge")
                return original_run(command, **kwargs)

            WORKFLOW_MODULE.run = interrupt_before_merge
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                WORKFLOW_MODULE.command_reconcile(argparse.Namespace(
                    project=str(self.project), run_id=str(initialized["run_id"]),
                    target_branch=None, apply=True,
                ))
        finally:
            WORKFLOW_MODULE.run = original_run
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_IMPLEMENT_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = prior_home
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["integration"]["attempts"][-1]["status"], "PREPARING")
        interrupted_status = self.workflow(
            "status", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(interrupted_status["integration"]["status"], "PREPARING")
        resumed = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(resumed["status"], "RECONCILIATION_READY_FOR_COMMIT")

    def test_reconciliation_conflict_preserves_target_and_reviewed_candidate(self) -> None:
        initialized = self.initialize("codex")
        candidate_sha, _ = self.review_accept(initialized)
        (self.project / "tracked.txt").write_text("conflicting target\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "tracked.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "conflicting target")
        target_sha = self.run_command(
            "git", "-C", str(self.project), "rev-parse", "main"
        ).stdout.strip()
        conflict = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply", expected=3,
        )
        self.assertEqual(conflict["status"], "RECONCILIATION_CONFLICT")
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "main").stdout.strip(),
            target_sha,
        )
        self.assertEqual(
            self.run_command(
                "git", "-C", str(initialized["implementation_worktree"]), "rev-parse", "HEAD"
            ).stdout.strip(),
            candidate_sha,
        )

    def test_reconciliation_is_idempotent_until_explicitly_abandoned(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        first = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        repeated = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertTrue(repeated["existing"])
        self.assertEqual(repeated["attempt_number"], first["attempt_number"])
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        self.assertEqual(len(json.loads(state_path.read_text())["integration"]["attempts"]), 1)
        wrong = self.workflow(
            "submit-reconciliation", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--attempt-number", "99", expected=1,
        )
        self.assertIn("attempt not found", str(wrong["error"]))
        preview = self.workflow(
            "abandon-reconciliation", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--reason", "retry with a new target merge",
            "--actor", "test-suite",
        )
        self.assertEqual(preview["status"], "ABANDON_RECONCILIATION_PREVIEW")
        abandoned = self.workflow(
            "abandon-reconciliation", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--reason", "retry with a new target merge",
            "--actor", "test-suite", "--apply",
        )
        self.assertEqual(abandoned["status"], "RECONCILIATION_ABANDONED")
        second = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(second["attempt_number"], 2)

    def test_reconciliation_does_not_report_ready_for_a_missing_worktree(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        reconciled = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.run_command(
            "git", "-C", str(self.project), "worktree", "remove", "--force",
            str(reconciled["worktree"]),
        )
        refused = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply", expected=1,
        )
        self.assertIn("recorded reconciliation worktree is missing", str(refused["error"]))

    def test_finalize_updates_target_ref_without_switching_original_checkout(self) -> None:
        initialized = self.initialize("codex")
        final_sha, _ = self.review_accept(initialized)
        self.run_command("git", "-C", str(self.project), "switch", "-qc", "ongoing-work")
        original_head = self.run_command(
            "git", "-C", str(self.project), "rev-parse", "HEAD"
        ).stdout.strip()
        (self.project / "ongoing-note.txt").write_text("keep local work\n", encoding="utf-8")
        finalized = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(finalized["status"], "FINALIZED")
        self.assertEqual(
            self.run_command(
                "git", "-C", str(self.project), "branch", "--show-current"
            ).stdout.strip(),
            "ongoing-work",
        )
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "HEAD").stdout.strip(),
            original_head,
        )
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "main").stdout.strip(),
            final_sha,
        )
        self.assertEqual(
            (self.project / "ongoing-note.txt").read_text(encoding="utf-8"), "keep local work\n"
        )

    def test_similarly_prefixed_worktree_branch_does_not_block_target_update(self) -> None:
        initialized = self.initialize("codex")
        final_sha, _ = self.review_accept(initialized)
        self.run_command("git", "-C", str(self.project), "switch", "-qc", "ongoing-work")
        sibling = self.root / "main-extra-worktree"
        self.run_command(
            "git", "-C", str(self.project), "worktree", "add", "-qb", "main-extra", str(sibling)
        )
        finalized = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply",
        )
        self.assertEqual(finalized["status"], "FINALIZED")
        self.assertEqual(
            self.run_command("git", "-C", str(self.project), "rev-parse", "main").stdout.strip(),
            final_sha,
        )

    def test_finalizing_target_move_recovers_to_reconciliation(self) -> None:
        initialized = self.initialize("codex")
        final_sha, _ = self.review_accept(initialized)
        prior_home = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
        os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        try:
            state = WORKFLOW_MODULE.load_state(self.project, str(initialized["run_id"]))
            state["status"] = "FINALIZING"
            state["integration_transaction"] = {
                "status": "PREPARED", "target_branch": "main",
                "baseline_sha": state["baseline_sha"],
                "expected_target_sha": state["baseline_sha"],
                "final_sha": final_sha, "prepared_at": WORKFLOW_MODULE.utc_now(),
            }
            WORKFLOW_MODULE.append_event(
                state, "INTEGRATION_PREPARED", state["integration_transaction"]
            )
            WORKFLOW_MODULE.save_state(state)
        finally:
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_IMPLEMENT_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = prior_home
        (self.project / "late-target.txt").write_text("late\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "late-target.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "late target move")
        recovered = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--apply", expected=3,
        )
        self.assertEqual(recovered["status"], "RECONCILIATION_REQUIRED")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY_TO_FINALIZE")
        self.assertEqual(state["integration_transaction"]["status"], "ABORTED_TARGET_MOVED")
        implementation = Path(str(initialized["implementation_worktree"]))
        tree = self.run_command("git", "-C", str(implementation), "write-tree").stdout.strip()
        later_sha = self.run_command(
            "git", "-C", str(implementation), "commit-tree", tree,
            "-p", final_sha, "-m", "later unreviewed commit",
        ).stdout.strip()
        self.run_command(
            "git", "-C", str(implementation), "update-ref",
            "refs/heads/" + str(initialized["implementation_branch"]), later_sha,
        )
        self.run_command(
            "git", "-C", str(self.project), "worktree", "remove", str(implementation),
        )
        preview = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(preview["status"], "RECONCILE_PREVIEW")
        self.assertEqual(preview["source_candidate_sha"], final_sha)

    def test_final_integration_lock_serializes_only_target_updates(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        project_state_root = Path(str(initialized["run_directory"])).parents[1]
        with WORKFLOW_MODULE.file_lock(project_state_root / "integration.lock"):
            blocked = self.workflow(
                "finalize", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]), "--apply", expected=1,
            )
            self.assertIn("another workflow command is active", str(blocked["error"]))
            status = self.workflow(
                "status", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]),
            )
            self.assertEqual(status["status"], "READY_TO_FINALIZE")

    def test_reconciliation_cannot_change_the_frozen_target_branch(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        self.run_command("git", "-C", str(self.project), "branch", "other-target")
        refused = self.workflow(
            "reconcile", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--target-branch", "other-target",
            expected=1,
        )
        self.assertIn("does not match the frozen target", str(refused["error"]))

    def test_shared_environment_drift_is_infrastructure_not_quality_failure(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        cfg = self.project / ".venv" / "pyvenv.cfg"
        cfg.write_text("home = fixture\n", encoding="utf-8")
        (self.project / ".git" / "info" / "exclude").write_text(".venv/\n", encoding="utf-8")
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        cfg.write_text("home = changed\n", encoding="utf-8")
        status = self.workflow(
            "status", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertTrue(status["environment"]["drifted"])
        drift = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=2,
        )
        self.assertEqual(drift["status"], "ENVIRONMENT_DRIFT")

    def test_environment_fingerprint_is_static_and_content_sensitive(self) -> None:
        venv = self.project / ".venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True, capture_output=True, text=True,
        )
        site_packages = next(venv.glob("lib/python*/site-packages"))
        marker = self.root / "pth-executed"
        (site_packages / "unsafe.pth").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        package_file = site_packages / "fixture_package.py"
        package_file.write_text("VALUE = 1\n", encoding="utf-8")
        first = WORKFLOW_MODULE.project_environment_contract(self.project)
        self.assertFalse(marker.exists(), "fingerprinting must never execute project environment code")
        package_file.write_text("VALUE = 2\n", encoding="utf-8")
        second = WORKFLOW_MODULE.project_environment_contract(self.project)
        self.assertNotEqual(first["digest"], second["digest"])

    def test_environment_fingerprint_never_reads_external_symlink_targets(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        secret = self.root / "external-secret"
        secret.write_text("first\n", encoding="utf-8")
        os.symlink(secret, self.project / ".venv" / "external-link")
        external_packages = self.root / "external-packages"
        metadata = external_packages / "fixture.dist-info" / "METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("Name: external\nVersion: 1\n", encoding="utf-8")
        python_lib = self.project / ".venv" / "lib" / "python3"
        python_lib.mkdir(parents=True)
        os.symlink(external_packages, python_lib / "site-packages")
        first = WORKFLOW_MODULE.project_environment_contract(self.project)
        secret.write_text("different external content\n", encoding="utf-8")
        metadata.write_text("Name: external\nVersion: 2\n", encoding="utf-8")
        second = WORKFLOW_MODULE.project_environment_contract(self.project)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["symlink_count"], 3)

    def test_environment_fingerprint_rejects_symlink_root_and_records_modes(self) -> None:
        external = self.root / "external-venv"
        (external / "bin").mkdir(parents=True)
        os.symlink(sys.executable, external / "bin" / "python")
        os.symlink(external, self.project / ".venv")
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "must not be a symlink"):
            WORKFLOW_MODULE.project_environment_contract(self.project)
        (self.project / ".venv").unlink()
        (self.project / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.project / ".venv" / "bin" / "python")
        fixture = self.project / ".venv" / "fixture"
        fixture.write_text("stable\n", encoding="utf-8")
        first = WORKFLOW_MODULE.project_environment_contract(self.project)
        fixture.chmod(0o755)
        second = WORKFLOW_MODULE.project_environment_contract(self.project)
        self.assertNotEqual(first["metadata_sha256"], second["metadata_sha256"])

    def test_environment_fingerprint_has_a_file_budget(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        (self.project / ".venv" / "extra").write_text("x", encoding="utf-8")
        original = WORKFLOW_MODULE.MAX_ENVIRONMENT_FILES
        original_bytes = WORKFLOW_MODULE.MAX_ENVIRONMENT_BYTES
        try:
            WORKFLOW_MODULE.MAX_ENVIRONMENT_FILES = 1
            with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "file or byte budget"):
                WORKFLOW_MODULE.project_environment_contract(self.project)
            WORKFLOW_MODULE.MAX_ENVIRONMENT_FILES = original
            WORKFLOW_MODULE.MAX_ENVIRONMENT_BYTES = 0
            with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "file or byte budget"):
                WORKFLOW_MODULE.project_environment_contract(self.project)
        finally:
            WORKFLOW_MODULE.MAX_ENVIRONMENT_FILES = original
            WORKFLOW_MODULE.MAX_ENVIRONMENT_BYTES = original_bytes

    def test_environment_fingerprint_covers_cache_named_directories(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        cache = self.project / ".venv" / ".pytest_cache"
        cache.mkdir()
        runtime = cache / "runtime.py"
        runtime.write_text("VALUE = 1\n", encoding="utf-8")
        first = WORKFLOW_MODULE.project_environment_contract(self.project)
        runtime.write_text("VALUE = 2\n", encoding="utf-8")
        second = WORKFLOW_MODULE.project_environment_contract(self.project)
        self.assertNotEqual(first["digest"], second["digest"])

    def test_environment_fingerprint_rejects_special_files_and_walk_errors(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        fifo = self.project / ".venv" / "runtime-input"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "unsupported special file"):
            WORKFLOW_MODULE.project_environment_contract(self.project)
        fifo.unlink()
        locked = self.project / ".venv" / "locked"
        locked.mkdir()
        (locked / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
        locked.chmod(0)
        try:
            with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "cannot walk project .venv"):
                WORKFLOW_MODULE.project_environment_contract(self.project)
        finally:
            locked.chmod(0o700)

    def test_environment_drift_during_verification_invalidates_evidence(self) -> None:
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        cfg = self.project / ".venv" / "pyvenv.cfg"
        cfg.write_text("home = fixture\n", encoding="utf-8")
        (self.project / ".git" / "info" / "exclude").write_text(".venv/\n", encoding="utf-8")
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "drift-smoke", "argv": ["true"], "cwd": "repository",
            "reason": "fixture", "expected_exit": 0,
        }])
        required = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )
        fake_bwrap = self.bin_dir / "bwrap"
        fake_bwrap.write_text(
            f"#!/bin/sh\nprintf 'home = changed\\n' > {str(cfg)!r}\nexit 0\n",
            encoding="utf-8",
        )
        fake_bwrap.chmod(0o755)
        failed = self.workflow(
            "verify", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
            "--request-id", str(required["requests"][0]["request_key"]),
            "--network-policy", "host", "--network-reason", "fixture",
            "--actor", "test-suite", "--apply", expected=4,
        )
        self.assertEqual(failed["status"], "VERIFICATION_ERROR")
        self.assertIn("environment drifted", str(failed["reason"]))

    def test_claude_adapter_uses_isolated_implementation(self) -> None:
        initialized = self.initialize("claude")
        implementation = Path(str(initialized["implementation_worktree"]))
        head = self.commit_batch_change(implementation)
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["reviewed_sha"], head)

    def test_supersede_releases_active_slot_without_deleting_history(self) -> None:
        first = self.initialize("codex")
        preview = self.workflow(
            "supersede", "--project", str(self.project),
            "--run-id", str(first["run_id"]),
        )
        self.assertEqual(preview["status"], "SUPERSEDE_PREVIEW")
        applied = self.workflow(
            "supersede", "--project", str(self.project),
            "--run-id", str(first["run_id"]), "--apply",
        )
        self.assertEqual(applied["status"], "SUPERSEDED")
        second = self.initialize("codex")
        self.assertNotEqual(second["run_id"], first["run_id"])
        listing = self.workflow("list", "--project", str(self.project))
        self.assertEqual(len(listing["runs"]), 2)
        self.assertEqual(listing["active_run"], second["run_id"])

    def test_snapshot_is_authority_and_dirty_original_is_excluded_from_baseline(self) -> None:
        (self.project / "tracked.txt").write_text("uncommitted tracked change\n", encoding="utf-8")
        (self.project / "dirty.txt").write_text("untracked change\n", encoding="utf-8")
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest),
            "--reviewer", "codex", "--batches", "1", "--target-branch", "main",
        )
        self.assertEqual(initialized["status"], "AWAITING_CONTRACT_REVIEW")
        self.assertFalse(initialized["original_worktree_clean_at_start"])
        self.assertTrue(initialized["original_worktree_changes_excluded_from_baseline"])
        self.assertIn(" M tracked.txt", initialized["original_worktree_changes_at_start"])
        self.assertIn("?? dirty.txt", initialized["original_worktree_changes_at_start"])
        implementation = Path(str(initialized["implementation_worktree"]))
        self.assertEqual((implementation / "tracked.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((implementation / "dirty.txt").exists())
        contract = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(contract["status"], "CONTRACT_READY")
        self.commit_batch_change(implementation)
        self.plan.write_text("# Changed plan\n", encoding="utf-8")
        self.batch_manifest.write_text("# Changed source manifest\nBatch 1\n", encoding="utf-8")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertTrue(status["source_plan_changed"])
        self.assertTrue(status["source_batch_manifest_changed"])
        changed = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(changed["status"], "REVIEW_PASS")

    def test_explicit_uncommitted_instruction_is_frozen_for_both_review_boundaries(self) -> None:
        instruction = self.project / "CLAUDE.md"
        instruction.write_text("# Local contract\n\nPreserve the public API.\n", encoding="utf-8")
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(instruction),
        )
        records = initialized["instruction_snapshots"]
        self.assertEqual(len(records), 1)
        frozen = Path(str(records[0]["snapshot"]))
        original_text = frozen.read_text(encoding="utf-8")
        instruction.write_text("# Changed later\n", encoding="utf-8")

        contract_preview = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--dry-run",
        )
        contract_prompt = Path(str(contract_preview["prompt_path"])).read_text(encoding="utf-8")
        contract_match = re.search(
            r"Frozen supplementary instruction manifest: `([^`]+)`", contract_prompt,
        )
        self.assertIsNotNone(contract_match)
        contract_manifest = json.loads(Path(str(contract_match.group(1))).read_text(encoding="utf-8"))
        contract_copy = Path(contract_manifest["instructions"][0]["snapshot"])
        self.assertEqual(contract_copy.read_text(encoding="utf-8"), original_text)

        contract = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(contract["status"], "CONTRACT_READY")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        review_preview = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", "--dry-run",
        )
        review_prompt = Path(str(review_preview["prompt_path"])).read_text(encoding="utf-8")
        review_match = re.search(
            r"Frozen supplementary instruction manifest: `([^`]+)`", review_prompt,
        )
        self.assertIsNotNone(review_match)
        review_manifest = json.loads(Path(str(review_match.group(1))).read_text(encoding="utf-8"))
        review_copy = Path(review_manifest["instructions"][0]["snapshot"])
        self.assertEqual(review_copy.read_text(encoding="utf-8"), original_text)

    def test_tampered_instruction_snapshot_fails_closed(self) -> None:
        instruction = self.root / "AGENTS.md"
        instruction.write_text("# Contract\n", encoding="utf-8")
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(instruction),
        )
        frozen = Path(str(initialized["instruction_snapshots"][0]["snapshot"]))
        frozen.write_text("tampered\n", encoding="utf-8")
        rejected = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=1,
        )
        self.assertIn("frozen instruction snapshot changed", str(rejected["error"]))
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertIn("frozen instruction snapshot changed", str(status["instruction_snapshot_error"]))

    def test_instruction_inputs_are_bounded_text_and_unique(self) -> None:
        instruction = self.root / "AGENTS.md"
        instruction.write_text("# Contract\n", encoding="utf-8")
        duplicate = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(instruction), "--instruction-file", str(instruction),
            expected=1,
        )
        self.assertIn("duplicate --instruction-file", str(duplicate["error"]))

        binary = self.root / "binary-contract"
        binary.write_bytes(b"\xff\xfe")
        rejected_binary = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(binary), expected=1,
        )
        self.assertIn("not UTF-8 text", str(rejected_binary["error"]))

        oversized = self.root / "oversized-contract.md"
        oversized.write_bytes(b"a" * (WORKFLOW_MODULE.MAX_INSTRUCTION_BYTES + 1))
        rejected_size = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(oversized), expected=1,
        )
        self.assertIn("instruction file exceeds", str(rejected_size["error"]))
        self.assertEqual(list(self.state_home.glob("projects/*/runs/*")), [])

    def test_redirected_instruction_directory_fails_closed(self) -> None:
        instruction = self.root / "AGENTS.md"
        instruction.write_text("# Contract\n", encoding="utf-8")
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1", "--target-branch", "main",
            "--instruction-file", str(instruction),
        )
        instruction_dir = Path(str(initialized["run_directory"])) / "instructions"
        outside = self.root / "redirected-instructions"
        instruction_dir.rename(outside)
        os.symlink(outside, instruction_dir)
        rejected = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=1,
        )
        self.assertIn("frozen instruction directory changed", str(rejected["error"]))

    def test_instruction_context_copy_rechecks_the_exact_bytes(self) -> None:
        run_root = self.root / "instruction-copy-run"
        instruction_dir = run_root / "instructions"
        context = run_root / "context"
        instruction_dir.mkdir(parents=True)
        context.mkdir()
        snapshot = instruction_dir / "01-contract.md"
        snapshot.write_text("trusted\n", encoding="utf-8")
        state = {
            "run_directory": str(run_root),
            "instruction_snapshots": [{
                "source": str(self.root / "AGENTS.md"),
                "snapshot": str(snapshot),
                "sha256": WORKFLOW_MODULE.sha256_file(snapshot),
                "size_bytes": snapshot.stat().st_size,
            }],
        }
        original_validate = WORKFLOW_MODULE.validate_instruction_snapshots

        def swap_after_validation(candidate: dict[str, object]) -> None:
            original_validate(candidate)
            snapshot.write_text("raced\n", encoding="utf-8")

        with mock.patch.object(
            WORKFLOW_MODULE, "validate_instruction_snapshots", side_effect=swap_after_validation,
        ):
            with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "changed while being read"):
                WORKFLOW_MODULE.copy_instruction_context(state, context)

    def test_atomic_file_publication_never_leaves_a_partial_authority_file(self) -> None:
        destination = self.root / "workflow.schema8.json"
        with mock.patch.object(os, "link", side_effect=OSError("simulated crash boundary")):
            with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "cannot create migration backup"):
                WORKFLOW_MODULE.create_file_atomically(
                    destination, b"authenticated state", "migration backup",
                )
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".workflow.schema8.json.*")), [])
        WORKFLOW_MODULE.create_file_atomically(
            destination, b"authenticated state", "migration backup",
        )
        self.assertEqual(destination.read_bytes(), b"authenticated state")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_batch_manifest_must_name_every_declared_batch(self) -> None:
        rejected = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--batches", "1,2", expected=1,
        )
        self.assertIn("does not declare every --batches ID: 2", str(rejected["error"]))

    def test_contract_reviewer_receives_frozen_batch_manifest(self) -> None:
        initialized = self.initialize_raw("codex")
        dry_run = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--dry-run",
        )
        prompt = Path(str(dry_run["prompt_path"])).read_text(encoding="utf-8")
        match = re.search(r"Authoritative run scope and batch manifest: `([^`]+)`", prompt)
        self.assertIsNotNone(match)
        context_manifest = Path(str(match.group(1)))
        self.assertEqual(context_manifest.read_text(encoding="utf-8"), self.batch_manifest.read_text(encoding="utf-8"))

    def test_frozen_batch_manifest_tampering_is_detected(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        frozen = Path(str(initialized["run_directory"])) / "plan" / "batches.md"
        frozen.write_text("tampered batch scope\n", encoding="utf-8")
        rejected = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=1,
        )
        self.assertIn("authoritative batch manifest changed", str(rejected["error"]))

    def test_unknown_resolution_is_reviewer_error_and_does_not_consume_round(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_RESOLVED_FINDING_IDS"] = '["invented-id"]'
        rejected = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
        )
        self.assertEqual(rejected["status"], "REVIEWER_ERROR")
        self.assertIn("unknown in this batch", str(rejected["reason"]))
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(state["reviews"], [])
        self.assertEqual(state["finding_ledger"], {})
        self.assertEqual(len(state["review_invocations"]), 1)
        self.assertEqual(state["review_invocations"][0]["status"], "CONTRACT_ERROR")
        rejected_again = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
        )
        self.assertEqual(rejected_again["round"], 1)
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(len(state["review_invocations"]), 2)
        self.assertNotEqual(
            state["review_invocations"][0]["directory"],
            state["review_invocations"][1]["directory"],
        )

    def test_reviewer_receives_minimal_round_context(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["review_mode"], "FULL")
        self.assertEqual(review["review_mode_reason"], "INITIAL_DISCOVERY")
        self.assertEqual(review["supplied_diff_base_sha"], review["base_sha"])
        run_dir = Path(str(initialized["run_directory"]))
        context = run_dir / "review_context" / "batch_1" / "round_01" / "review-1-001"
        self.assertEqual(
            {path.name for path in context.iterdir()},
            {
                "acceptance_contract.json", "assignment.json", "batch_manifest.md",
                "changes.patch", "finding_ledger.json", "plan.md", "review_schema.json",
            },
        )
        prompt = (
            run_dir / "reviews" / "batch_1" / "round_01" / "invocation_001" / "prompt.md"
        ).read_text()
        self.assertIn(str(context / "plan.md"), prompt)
        self.assertIn(str(context / "batch_manifest.md"), prompt)
        self.assertIn(str(context / "assignment.json"), prompt)
        self.assertIn(str(context / "changes.patch"), prompt)
        self.assertIn(str(context / "finding_ledger.json"), prompt)
        self.assertIn("implemented", (context / "changes.patch").read_text())
        assignment = json.loads((context / "assignment.json").read_text())
        self.assertEqual(assignment["review_mode"], "FULL")
        self.assertEqual(
            assignment["authoritative_coverage_base_sha"], review["base_sha"]
        )
        self.assertNotIn(str(run_dir / "reviews"), prompt)
        (context / "batch_manifest.md").write_text("tampered reviewer input\n", encoding="utf-8")
        tampered = self.workflow(
            "status", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=1,
        )
        self.assertIn("audited artifact digest mismatch", str(tampered["error"]))

    def test_new_run_requires_independent_contract_review(self) -> None:
        initialized = self.initialize_raw("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        blocked = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=1,
        )
        self.assertEqual(blocked["status"], "WORKFLOW_ERROR")
        self.assertIn("contract-review", str(blocked["error"]))

    def test_contract_decision_is_auditable_and_unblocks_implementation(self) -> None:
        self.environment["FAKE_CONTRACT_NEEDS_DECISION"] = "1"
        initialized = self.initialize_raw("codex")
        reviewed = self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=3,
        )
        self.assertEqual(reviewed["status"], "NEEDS_USER_DECISION")
        decision_file = self.root / "decision.json"
        decision_file.write_text(
            json.dumps({
                "reason": "user selected the finite fixture boundary",
                "criteria": [{
                    "id": "1-exit", "batch": "1", "source": "DECLARED",
                    "observation": "tracked.txt equals implemented",
                    "expected_result": "exact match", "scope": ["tracked.txt"],
                    "rationale": "explicit user boundary",
                }],
            }),
            encoding="utf-8",
        )
        preview = self.workflow(
            "contract-adjudicate", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
            "--decision-file", str(decision_file),
        )
        self.assertEqual(preview["status"], "CONTRACT_ADJUDICATION_PREVIEW")
        applied = self.workflow(
            "contract-adjudicate", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
            "--decision-file", str(decision_file), "--apply",
        )
        self.assertEqual(applied["status"], "CONTRACT_ADJUDICATED")
        self.assertTrue(Path(applied["acceptance_contract"]["decision_path"]).is_file())

    def test_verification_request_runs_at_fixed_sha_without_consuming_review_round(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        head = self.commit_batch_change(implementation)
        self.environment["IPWR_AUDIT_SECRET"] = "must-not-enter-sandbox"
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "python-smoke",
            "argv": [
                sys.executable, "-c",
                "import os,pathlib; assert os.getenv('IPWR_AUDIT_SECRET') is None; "
                "assert not (pathlib.Path.home()/'.ssh').exists(); print('verified')",
            ],
            "cwd": "repository", "reason": "need executable evidence", "expected_exit": 0,
        }])
        requested = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )
        self.assertEqual(requested["status"], "VERIFICATION_REQUIRED")
        self.assertFalse(requested["review_round_consumed"])
        request_id = requested["requests"][0]["request_key"]
        preview = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
        )
        self.assertEqual(preview["status"], "VERIFICATION_PREVIEW")
        verified = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply",
        )
        self.assertEqual(verified["status"], "VERIFICATION_PASS")
        self.assertEqual(verified["evidence"]["reviewed_sha"], head)
        self.environment.pop("FAKE_VERIFICATION_REQUESTS")
        self.environment.pop("IPWR_AUDIT_SECRET")
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")
        self.assertEqual(review["round"], 1)

    def test_command_contract_forces_batch_and_final_fixed_sha_verification(self) -> None:
        self.environment["FAKE_CONTRACT_COMMAND"] = "1"
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        head = self.commit_batch_change(implementation)
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        self.assertEqual(required["source"], "ACCEPTANCE_CONTRACT")
        request_id = required["requests"][0]["request_key"]
        self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply",
        )
        review = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        accepted = self.workflow(
            "accept", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", "--review-file", str(review["report_path"]),
        )
        self.assertEqual(len(accepted["final_verification_requests"]), 1)
        final_request = accepted["final_verification_requests"][0]["request_key"]
        completed = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", final_request, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply",
        )
        self.assertEqual(completed["status"], "VERIFICATION_PASS")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "READY_TO_FINALIZE")
        self.assertEqual(status["final_verification"]["status"], "PASS")
        self.assertEqual(status["final_verification"]["reviewed_sha"], head)

    def test_verification_infrastructure_error_is_retryable_and_audited(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "infra-smoke", "argv": ["true"], "cwd": "repository",
            "reason": "fixture", "expected_exit": 0,
        }])
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        request_id = required["requests"][0]["request_key"]
        fake_bwrap = self.bin_dir / "bwrap"
        fake_bwrap.write_text("#!/bin/sh\necho 'bwrap: fixture unavailable' >&2\nexit 1\n", encoding="utf-8")
        fake_bwrap.chmod(0o755)
        failed = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply", expected=4,
        )
        self.assertTrue(failed["retryable"])
        retry = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
        )
        self.assertEqual(retry["attempt"], 2)
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        request = next(item for item in state["verification_requests"] if item["request_key"] == request_id)
        self.assertEqual(request["status"], "PENDING")
        self.assertEqual(state["verification_attempts"][0]["status"], "INFRA_ERROR")

    def test_non_venv_python_module_missing_is_infrastructure_not_quality_fail(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "bare", "argv": ["/usr/bin/python3", "-m", "pytest", "tests/"],
            "cwd": "repository", "reason": "fixture", "expected_exit": 0,
        }])
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        request_id = required["requests"][0]["request_key"]
        fake_bwrap = self.bin_dir / "bwrap"
        fake_bwrap.write_text(
            "#!/bin/sh\necho '/usr/bin/python3: No module named pytest' >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake_bwrap.chmod(0o755)
        failed = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply", expected=4,
        )
        self.assertTrue(failed["retryable"])
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        request = next(item for item in state["verification_requests"] if item["request_key"] == request_id)
        self.assertEqual(request["status"], "PENDING")
        self.assertEqual(state["status"], "AWAITING_VERIFICATION")
        self.assertEqual(state["verification_attempts"][0]["status"], "INFRA_ERROR")

    def test_non_python_command_failure_stays_quality_fail_not_infra(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "nonpy", "argv": ["git", "grep", "-n", "state: warn", "HEAD"],
            "cwd": "repository", "reason": "fixture", "expected_exit": 0,
        }])
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        request_id = required["requests"][0]["request_key"]
        fake_bwrap = self.bin_dir / "bwrap"
        fake_bwrap.write_text(
            "#!/bin/sh\necho 'fatal: not a git repository' >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake_bwrap.chmod(0o755)
        failed = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply", expected=2,
        )
        self.assertEqual(failed["status"], "VERIFICATION_FAIL")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        request = next(item for item in state["verification_requests"] if item["request_key"] == request_id)
        self.assertEqual(request["status"], "FAIL")
        self.assertEqual(state["status"], "CHANGES_REQUESTED")

    def test_verify_from_changes_requested_unblocks_sibling_requests(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([
            {"id": "r1", "argv": ["git", "grep", "-n", "missing-token", "HEAD"],
             "cwd": "repository", "reason": "fixture", "expected_exit": 0},
            {"id": "r2", "argv": ["true"],
             "cwd": "repository", "reason": "fixture", "expected_exit": 0},
        ])
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        keys = [item["request_key"] for item in required["requests"]]
        fake_bwrap = self.bin_dir / "bwrap"
        # The sandbox spawn comes from a stripped environment, so the fake bwrap keys its
        # behavior on the request payload rather than on an environment variable.
        fake_bwrap.write_text(
            "#!/bin/sh\ncase \"$*\" in\n  *missing-token*)\n"
            "    echo 'fixture failure: not a code result' >&2\n    exit 1 ;;\nesac\nexit 0\n",
            encoding="utf-8",
        )
        fake_bwrap.chmod(0o755)
        first = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", keys[0], "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply", expected=2,
        )
        self.assertEqual(first["status"], "VERIFICATION_FAIL")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "CHANGES_REQUESTED")
        second = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", keys[1], "--network-policy", "host",
            "--network-reason", "test fixture", "--actor", "test-suite", "--apply",
        )
        self.assertEqual(second["status"], "VERIFICATION_PASS")

    def test_reviewer_cannot_smuggle_bare_python_verification_request(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "smuggle", "argv": ["python3", "-m", "pytest", "tests/"],
            "cwd": "repository", "reason": "fixture", "expected_exit": 0,
        }])
        refused = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=4,
        )
        self.assertEqual(refused["status"], "REVIEWER_ERROR")
        self.assertIn("bare interpreter", str(refused["reason"]))

    def test_host_network_requires_typed_exact_request_authorization(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_VERIFICATION_REQUESTS"] = json.dumps([{
            "id": "network-smoke", "argv": ["true"], "cwd": "repository",
            "reason": "fixture", "expected_exit": 0,
        }])
        required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        request_id = required["requests"][0]["request_key"]
        blocked = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host", "--apply", expected=3,
        )
        self.assertEqual(blocked["reason"], "HOST_NETWORK_REQUIRES_AUTHORIZATION")
        pending = blocked["pending_decision"]
        self.assertIn("AUTHORIZE_HOST_NETWORK", pending["allowed_choices"])
        adjudicated = self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--decision-id", pending["decision_id"], "--choice", "AUTHORIZE_HOST_NETWORK",
            "--reason", "fixture needs loopback", "--actor", "test-user", "--apply",
        )
        self.assertEqual(adjudicated["run_status"], "AWAITING_VERIFICATION")
        preview = self.workflow(
            "verify", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--request-id", request_id, "--network-policy", "host",
        )
        self.assertTrue(preview["host_network_authorized"])

    def test_finalize_target_advance_preview_does_not_park(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        preview = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            expected=3,
        )
        self.assertEqual(preview["status"], "RECONCILIATION_REQUIRED")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY_TO_FINALIZE")
        self.assertIsNone(state.get("pending_decision"))

    def test_finalize_target_advance_apply_does_not_park_execution(self) -> None:
        initialized = self.initialize("codex")
        self.review_accept(initialized)
        (self.project / "target-change.txt").write_text("advanced\n", encoding="utf-8")
        self.run_command("git", "-C", str(self.project), "add", "target-change.txt")
        self.run_command("git", "-C", str(self.project), "commit", "-qm", "advance target")
        parked = self.workflow(
            "finalize", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply", expected=3,
        )
        self.assertEqual(parked["status"], "RECONCILIATION_REQUIRED")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY_TO_FINALIZE")
        self.assertIsNone(state.get("pending_decision"))

    def test_contract_criterion_with_bare_python_argv_is_refused(self) -> None:
        state = {"reviewer": "codex", "baseline_sha": "a" * 40, "batches": ["B1"]}
        base = {
            "id": "c1", "batch": "B1", "source": "REVIEWER_DERIVED",
            "observation": "run the suite", "expected_result": "exit 0",
            "scope": ["tests/"], "rationale": "fixture",
            "evidence_kind": "COMMAND", "expected_exit": 0,
        }
        for argv in (
            ["python3", "-m", "pytest", "tests/"],
            ["python", "-c", "print(1)"],
        ):
            payload = {
                "reviewer": "codex", "baseline_sha": "a" * 40, "assessment": "READY",
                "summary": "s", "issues": [], "criteria": [{**base, "argv": argv}],
            }
            with self.assertRaises(WORKFLOW_MODULE.ReviewContractError):
                WORKFLOW_MODULE.validate_contract_payload(payload, state)
        for argv in (
            [".venv/bin/python", "-m", "pytest", "tests/"],
            ["/usr/bin/python3", "-c", "print('ok')"],
            ["git", "grep", "-n", "state: warn", "HEAD"],
        ):
            payload = {
                "reviewer": "codex", "baseline_sha": "a" * 40, "assessment": "READY",
                "summary": "s", "issues": [], "criteria": [{**base, "argv": argv}],
            }
            WORKFLOW_MODULE.validate_contract_payload(payload, state)

    def test_reviewer_verification_request_with_bare_python_argv_is_refused(self) -> None:
        payload = {
            "reviewer": "codex", "reviewed_sha": "b" * 40, "base_sha": "a" * 40,
            "batch": "B1", "verdict": "NEEDS_VERIFICATION", "summary": "s",
            "findings": [], "resolved_finding_ids": [],
            "verification_requests": [{
                "id": "v1", "argv": ["python3", "-m", "pytest", "tests/"],
                "cwd": "repository", "reason": "r", "expected_exit": 0,
            }],
            "criterion_results": [],
        }
        with self.assertRaises(WORKFLOW_MODULE.WorkflowError):
            WORKFLOW_MODULE.validate_review_payload(payload, "codex", "B1", "a" * 40, "b" * 40)
        payload["verification_requests"] = [{
            "id": "v2", "argv": [".venv/bin/python", "-m", "pytest", "tests/"],
            "cwd": "repository", "reason": "r", "expected_exit": 0,
        }]
        WORKFLOW_MODULE.validate_review_payload(payload, "codex", "B1", "a" * 40, "b" * 40)

    def test_implementation_review_identity_is_engine_bound(self) -> None:
        payload = {"reviewer": "dsh", "batch": "wrong", "base_sha": "x", "reviewed_sha": "y"}
        disclosures = WORKFLOW_MODULE.bind_review_identity(
            payload, reviewer="codex", batch="B1", base="a" * 40, head="b" * 40,
        )
        self.assertEqual(payload["reviewer"], "codex")
        self.assertEqual(payload["batch"], "B1")
        self.assertEqual(payload["base_sha"], "a" * 40)
        self.assertEqual(payload["reviewed_sha"], "b" * 40)
        self.assertEqual({item["field"] for item in disclosures},
                         {"reviewer", "batch", "base_sha", "reviewed_sha"})

    def test_implementation_json_parser_tolerates_only_raw_control_characters(self) -> None:
        raw = '{"summary":"line1\nline2"}'
        payload = WORKFLOW_MODULE.extract_review("dsh", raw, self.root / "missing.json")
        self.assertEqual(payload["summary"], "line1\nline2")
        with self.assertRaises(WORKFLOW_MODULE.WorkflowError):
            WORKFLOW_MODULE.extract_review("dsh", '{"summary":}', self.root / "missing.json")

    def test_review_semantic_prose_is_normalized_at_the_authority_boundary(self) -> None:
        payload = {
            "reviewer": "codex", "reviewed_sha": "b" * 40, "base_sha": "a" * 40,
            "batch": "B1", "verdict": "FAIL", "summary": "blocking defect",
            "findings": [{
                "id": "F1", "fingerprint": "f1", "severity": "P1", "novelty": "INITIAL_REVIEW",
                "details": "the blocking behavior is reproducible",
                "required_outcome": "the blocking behavior no longer reproduces",
            }],
        }
        disclosures = WORKFLOW_MODULE.normalize_review_payload(payload)
        WORKFLOW_MODULE.validate_review_payload(
            payload, "codex", "B1", "a" * 40, "b" * 40,
        )
        self.assertEqual(payload["findings"][0]["novelty"], "INITIAL_REVIEW")
        self.assertEqual(payload["findings"][0]["location"], "")
        self.assertEqual(payload["verification_requests"], [])
        self.assertIn(
            {"type": "OPTIONAL_FINDING_FIELD_DEFAULTED", "finding_index": 0, "field": "location"},
            disclosures,
        )
        # A missing close condition must neither be invented by the controller nor cost the
        # reviewer's other work: it is disclosed, kept empty, and warned about by the policy.
        without_close_condition = json.loads(json.dumps(payload))
        del without_close_condition["findings"][0]["required_outcome"]
        disclosures = WORKFLOW_MODULE.normalize_review_payload(without_close_condition)
        self.assertIn(
            {"type": "FINDING_CLOSE_CONDITION_MISSING", "finding_index": 0, "field": "required_outcome"},
            disclosures,
        )
        WORKFLOW_MODULE.validate_review_payload(
            without_close_condition, "codex", "B1", "a" * 40, "b" * 40,
        )
        state = {"finding_ledger": {}, "batch_convergence": {}}
        policy = WORKFLOW_MODULE.apply_convergence_policy(
            state, without_close_condition, "B1", 1, "b" * 40
        )
        self.assertIn(
            {"code": "FINDING_WITHOUT_CLOSE_CONDITION", "finding_id": "F1"}, policy["warnings"]
        )
        self.assertEqual(state["finding_ledger"]["f1"]["required_outcome"], "")
        # A finding with no substance at all is still a delivery error.
        without_details = json.loads(json.dumps(payload))
        without_details["findings"][0]["details"] = " "
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "empty details"):
            WORKFLOW_MODULE.validate_review_payload(
                without_details, "codex", "B1", "a" * 40, "b" * 40,
            )

    def test_provider_schema_excludes_engine_owned_identity(self) -> None:
        review = WORKFLOW_MODULE.producer_delivery_schema(
            WORKFLOW_MODULE.REVIEW_SCHEMA,
            {"reviewer", "reviewed_sha", "base_sha", "batch"},
        )
        self.assertNotIn("reviewer", review["properties"])
        self.assertNotIn("reviewed_sha", review["required"])
        self.assertIn("findings", review["properties"])
        finding_schema = review["properties"]["findings"]["items"]
        self.assertFalse(finding_schema["additionalProperties"])
        self.assertEqual(
            finding_schema["required"],
            ["id", "fingerprint", "severity", "novelty", "location", "required_outcome", "details"],
        )
        self.assertEqual(
            finding_schema,
            WORKFLOW_MODULE.REVIEW_SCHEMA["properties"]["findings"]["items"],
            "the wire finding shape is the internal finding shape, not a restatement",
        )
        self.assertIn("criterion_results", review["required"])
        self.assertIn("verification_requests", review["required"])
        self.assertIn("resolved_finding_ids", review["required"])
        self.assertEqual(WORKFLOW_MODULE.unsupported_schema_keywords(review), [])
        contract = WORKFLOW_MODULE.producer_delivery_schema(
            WORKFLOW_MODULE.CONTRACT_SCHEMA, {"reviewer", "baseline_sha"},
        )
        self.assertNotIn("baseline_sha", contract["properties"])
        self.assertIn("criteria", contract["properties"])

    def test_legacy_run_requires_explicit_migration(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        self.write_authenticated_legacy_state(state_path, state, 4)
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "MIGRATION_REQUIRED")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["status"], "MIGRATION_PREVIEW")
        interrupted_backup = state_path.with_name("workflow.schema4.json")
        interrupted_backup.write_bytes(state_path.read_bytes())
        interrupted_backup.chmod(0o600)
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        self.assertTrue(migrated["backup_reused"])
        self.assertTrue(Path(str(migrated["backup_path"])).is_file())

    def test_review_contract_snapshot_is_stable_when_installed_source_changes(self) -> None:
        run_root = self.root / "snapshot-run"
        (run_root / "contracts").mkdir(parents=True)
        source = self.root / "reviewer-source.md"
        source.write_text("frozen semantics\n", encoding="utf-8")
        original_sources = WORKFLOW_MODULE.REVIEW_CONTRACT_SOURCES
        try:
            WORKFLOW_MODULE.REVIEW_CONTRACT_SOURCES = {"batch_review": source}
            contract = WORKFLOW_MODULE.snapshot_review_contract(run_root)
            state = {"run_directory": str(run_root), "review_contract": contract}
            source.write_text("new installed semantics\n", encoding="utf-8")
            self.assertEqual(
                WORKFLOW_MODULE.read_review_contract(state, "batch_review"),
                "frozen semantics\n",
            )
        finally:
            WORKFLOW_MODULE.REVIEW_CONTRACT_SOURCES = original_sources

    def test_tampered_review_contract_snapshot_fails_closed(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        prompt_path = Path(state["review_contract"]["contract_review"]["path"])
        prompt_path.write_text("tampered\n", encoding="utf-8")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertIn("digest mismatch", str(status["review_contract_error"]))
        result = subprocess.run(
            [
                sys.executable, str(WORKFLOW), "contract-review",
                "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            ],
            text=True, capture_output=True, env=self.environment, check=False,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "WORKFLOW_ERROR")
        self.assertIn("frozen reviewer contract digest mismatch", payload["error"])

    def test_schema7_run_requires_explicit_review_contract_migration(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("review_contract")
        self.write_authenticated_legacy_state(state_path, state, 7)
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertTrue(preview["review_contract_migration"])
        self.assertEqual(
            set(preview["review_contract_candidate"]), {"batch_review", "contract_review"}
        )
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(current["schema_version"], WORKFLOW_MODULE.SCHEMA_VERSION)
        WORKFLOW_MODULE.validate_review_contract(current)
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema7"))

    def test_schema8_run_migrates_without_replacing_frozen_reviewer_contract(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        original_contract = json.loads(json.dumps(state["review_contract"]))
        state.pop("instruction_snapshots")
        state.pop("original_worktree_clean_at_start")
        state.pop("original_worktree_changes_at_start")
        self.write_authenticated_legacy_state(state_path, state, 8)

        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(preview["from_schema"], 8)
        self.assertFalse(preview["review_contract_migration"])
        self.assertIsNone(preview["review_contract_candidate"])
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(current["schema_version"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertEqual(current["review_contract"], original_contract)
        self.assertEqual(current["instruction_snapshots"], [])
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema8"))

    def test_schema9_run_requires_explicit_review_transport_migration(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        original_contract = json.loads(json.dumps(state["review_contract"]))
        self.write_authenticated_legacy_state(state_path, state, 9)

        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "MIGRATION_REQUIRED")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["from_schema"], 9)
        self.assertEqual(preview["to_schema"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertFalse(preview["review_contract_migration"])
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(current["schema_version"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertEqual(current["review_contract"], original_contract)
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema9"))

    def test_schema10_run_requires_explicit_closeout_recovery_migration(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        original_contract = json.loads(json.dumps(state["review_contract"]))
        state.pop("closeout_review_grants")
        self.write_authenticated_legacy_state(state_path, state, 10)

        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "MIGRATION_REQUIRED")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["from_schema"], 10)
        self.assertEqual(preview["to_schema"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertFalse(preview["review_contract_migration"])
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(current["schema_version"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertEqual(current["review_contract"], original_contract)
        self.assertEqual(current["closeout_review_grants"], {})
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema10"))

    def test_sequential_migration_uses_the_actual_loaded_schema(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("review_contract")
        state["migration"] = {
            "from_schema": 6,
            "mode": "LEGACY_COMPATIBILITY",
            "status": "APPLIED",
            "backup_path": str(state_path.with_name("workflow.schema6.json")),
            "backup_sha256": "legacy-digest",
        }
        self.write_authenticated_legacy_state(state_path, state, 7)
        old_backup = state_path.with_name("workflow.schema6.json")
        old_backup.write_text("older schema backup\n", encoding="utf-8")
        old_backup.chmod(0o600)

        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(preview["from_schema"], 7)
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema7"))
        current = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(current["migration"]["from_schema"], 7)
        self.assertEqual(current["migration_history"][0]["from_schema"], 6)
        self.assertEqual(old_backup.read_text(encoding="utf-8"), "older schema backup\n")

    def test_legacy_migration_recreates_missing_contracts_parent(self) -> None:
        initialized = self.initialize_raw("codex")
        run_root = Path(str(initialized["run_directory"]))
        state_path = run_root / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("review_contract")
        state["environment_contract"] = None
        self.write_authenticated_legacy_state(state_path, state, 7)
        shutil.rmtree(run_root / "contracts")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertTrue(preview["environment_fingerprint_migration"])
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        current = json.loads(state_path.read_text(encoding="utf-8"))
        WORKFLOW_MODULE.validate_review_contract(current)

    def test_invalid_legacy_migration_history_fails_as_workflow_error(self) -> None:
        initialized = self.initialize_raw("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["migration_history"] = {"invalid": True}
        self.write_authenticated_legacy_state(state_path, state, 7)
        refused = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            expected=1,
        )
        self.assertIn("migration_history must be a list", str(refused["error"]))

    def test_schema6_environment_fingerprint_requires_explicit_rebase(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state["environment_contract"]["fingerprint_version"] = 2
        state["integration"].update({
            "status": "RECONCILING", "current_attempt": 2,
            "attempts": [
                {"number": 1, "status": "READY_FOR_COMMIT"},
                {"number": 2, "status": "READY_FOR_COMMIT"},
            ],
        })
        self.write_authenticated_legacy_state(state_path, state, 6)
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "MIGRATION_REQUIRED")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertTrue(preview["environment_fingerprint_migration"])
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        current = json.loads(state_path.read_text())
        self.assertEqual(current["schema_version"], WORKFLOW_MODULE.SCHEMA_VERSION)
        self.assertEqual(current["environment_contract"]["fingerprint_version"], 3)
        self.assertEqual(current["integration"]["attempts"][0]["status"], "LEGACY_SUPERSEDED")
        self.assertEqual(current["integration"]["current_attempt"], 2)
        self.assertTrue(Path(str(migrated["backup_path"])).name.startswith("workflow.schema6"))

    def test_schema5_target_advance_parking_migrates_to_integration_drift(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": "legacy-target", "type": "TARGET_ADVANCED",
            "previous_status": "IMPLEMENTING", "allowed_choices": ["ABORT_RUN"],
        }
        state.pop("integration", None)
        state.pop("environment_contract", None)
        self.write_authenticated_legacy_state(state_path, state, 5)
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
        )
        self.assertEqual(status["status"], "IMPLEMENTING")
        self.assertIsNone(status["pending_decision"])

    def test_schema_downgrade_cannot_launder_forged_completion(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state.update({
            "schema_version": 6, "status": "FINALIZED", "finalized": True,
            "final_sha": "f" * 40,
        })
        state_path.write_text(json.dumps(state), encoding="utf-8")
        refused = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply", expected=1,
        )
        self.assertIn("checkpoint digest mismatch", str(refused["error"]))

    def test_migration_refuses_a_symlinked_backup(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        self.write_authenticated_legacy_state(state_path, state, 4)
        backup = state_path.with_name("workflow.schema4.json")
        os.symlink(state_path, backup)
        refused = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply", expected=1,
        )
        self.assertIn("backup exists but does not match", str(refused["error"]))

    def test_migration_refuses_a_dangling_symlinked_backup(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        self.write_authenticated_legacy_state(state_path, state, 4)
        backup = state_path.with_name("workflow.schema4.json")
        target = self.root / "must-not-be-created.json"
        os.symlink(target, backup)
        refused = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply", expected=1,
        )
        self.assertIn("backup exists but does not match", str(refused["error"]))
        self.assertFalse(target.exists())

    def test_event_tampering_is_detected(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        first_event = Path(state["events"][0]["path"])
        event = json.loads(first_event.read_text())
        event["details"]["baseline_sha"] = "0" * 40
        first_event.write_text(json.dumps(event), encoding="utf-8")
        result = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            expected=1,
        )
        self.assertIn("event chain validation failed", str(result["error"]))

    def test_workflow_state_tampering_is_detected(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state["budgets"]["max_review_invocations_per_batch"] = 999
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            expected=1,
        )
        self.assertIn("checkpoint digest mismatch", str(result["error"]))

    def test_interrupted_cross_file_state_save_is_recovered(self) -> None:
        initialized = self.initialize("codex")
        prior_home = os.environ.get("GROUNDED_BUILD_IMPLEMENT_HOME")
        os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)
        original_atomic_json = WORKFLOW_MODULE.atomic_json
        try:
            state = WORKFLOW_MODULE.load_state(self.project, str(initialized["run_id"]))
            WORKFLOW_MODULE.append_event(state, "TEST_PENDING_EVENT", {"fixture": True})

            def fail_workflow_write(path: Path, payload: dict[str, object]) -> None:
                if path.name == "workflow.json":
                    raise OSError("simulated crash before workflow commit")
                original_atomic_json(path, payload)

            WORKFLOW_MODULE.atomic_json = fail_workflow_write
            with self.assertRaisesRegex(OSError, "simulated crash"):
                WORKFLOW_MODULE.save_state(state)
        finally:
            WORKFLOW_MODULE.atomic_json = original_atomic_json
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_IMPLEMENT_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_IMPLEMENT_HOME"] = prior_home
        transaction = Path(str(initialized["run_directory"])) / "state_save_transaction.json"
        self.assertTrue(transaction.is_file())
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "IMPLEMENTING")
        self.assertFalse(transaction.exists())

    def test_typed_review_decision_has_audited_resume_path(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        self.environment["FAKE_REVIEW_NEEDS_DECISION"] = "1"
        decision_required = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1", expected=3,
        )
        self.assertEqual(decision_required["status"], "NEEDS_USER_DECISION")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        decision_id = status["pending_decision"]["decision_id"]
        preview = self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--decision-id", decision_id, "--choice", "RESUME_WITH_DECISION",
            "--reason", "retain the declared fixture boundary", "--actor", "test-user",
        )
        self.assertEqual(preview["status"], "ADJUDICATION_PREVIEW")
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--decision-id", decision_id, "--choice", "RESUME_WITH_DECISION",
            "--reason", "retain the declared fixture boundary", "--actor", "test-user", "--apply",
        )
        self.environment.pop("FAKE_REVIEW_NEEDS_DECISION")
        reviewed = self.workflow(
            "review", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--batch", "1",
        )
        self.assertEqual(reviewed["status"], "REVIEW_PASS")
        self.assertEqual(reviewed["review_mode"], "FULL")
        self.assertEqual(reviewed["review_mode_reason"], "SAME_SHA_AFTER_USER_DECISION")
        self.assertEqual(reviewed["supplied_diff_base_sha"], reviewed["base_sha"])
        run_dir = Path(str(initialized["run_directory"]))
        context = run_dir / "review_context" / "batch_1" / "round_02" / "review-1-002"
        self.assertIn("implemented", (context / "changes.patch").read_text(encoding="utf-8"))

    def _pass_then_commit(self) -> tuple[dict, dict, str]:
        """Reach a PASS, then commit again -- the state B10 wedged in."""
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        first = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(first["status"], "REVIEW_PASS")
        later_head = self.commit_batch_change(implementation, "implemented once more\n")
        return initialized, first, later_head

    def _reach_exhausted_review_decision(
        self, reviewer_decision_at_final: bool = False,
    ) -> tuple[dict, Path, dict]:
        """Reach the final ordinary review with one stable OPEN P1."""
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        run_id = str(initialized["run_id"])
        finding = {
            "id": "F-1-001", "fingerprint": "persistent-closeout-defect",
            "severity": "P1", "novelty": "INITIAL_REVIEW",
            "location": "tracked.txt:1",
            "details": "the fixture content is reviewed and the required content remains incorrect",
            "required_outcome": "make the fixture content correct",
        }
        self.environment["FAKE_FINDINGS"] = json.dumps([finding])
        for round_number in range(1, 4):
            self.commit_batch_change(implementation, f"unfixed round {round_number}\n")
            result = self.workflow(
                "review", "--project", str(self.project), "--run-id", run_id,
                "--batch", "1", expected=3 if round_number == 3 else 2,
            )
            if round_number == 3:
                self.assertEqual(result["status"], "NEEDS_USER_DECISION")
                pending = self.workflow(
                    "status", "--project", str(self.project), "--run-id", run_id,
                )["pending_decision"]
                self.workflow(
                    "adjudicate", "--project", str(self.project), "--run-id", run_id,
                    "--decision-id", str(pending["decision_id"]),
                    "--choice", "RETURN_TO_FIX", "--reason", "complete the bounded repair",
                    "--actor", "test-user", "--apply",
                )
        self.commit_batch_change(implementation, "unfixed round 4\n")
        if reviewer_decision_at_final:
            self.environment["FAKE_REVIEW_NEEDS_DECISION"] = "1"
        final = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=3,
        )
        self.environment.pop("FAKE_REVIEW_NEEDS_DECISION", None)
        self.assertEqual(final["status"], "NEEDS_USER_DECISION")
        return initialized, implementation, finding

    def test_exhausted_return_to_fix_gets_one_sha_bound_closeout_and_can_accept(self) -> None:
        initialized, implementation, finding = self._reach_exhausted_review_decision()
        run_id = str(initialized["run_id"])
        pending = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )["pending_decision"]
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(pending["decision_id"]), "--choice", "RETURN_TO_FIX",
            "--reason", "apply the final bounded repair", "--actor", "test-user", "--apply",
        )
        without_commit = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=1,
        )
        self.assertIn("requires a new fix commit", without_commit["error"])

        closeout_head = self.commit_batch_change(implementation, "fixed for closeout\n")
        self.environment["FAKE_CODEX_EXIT"] = "1"
        infrastructure = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=4,
        )
        self.assertEqual(infrastructure["status"], "REVIEWER_ERROR")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        grant = json.loads(state_path.read_text(encoding="utf-8"))["closeout_review_grants"]["1"]
        self.assertEqual(grant["bound_sha"], closeout_head)
        self.assertFalse(grant["used"])

        self.environment.pop("FAKE_CODEX_EXIT")
        self.environment["FAKE_FINDINGS"] = "[]"
        self.environment["FAKE_RESOLVED_FINDING_IDS"] = json.dumps([finding["id"]])
        closeout = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(closeout["status"], "REVIEW_PASS")
        self.assertTrue(closeout["closeout_review"])
        self.assertEqual(closeout["reviewed_sha"], closeout_head)
        accepted = self.workflow(
            "accept", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
            "--review-file", str(closeout["report_path"]),
        )
        self.assertEqual(accepted["status"], "BATCH_ACCEPTED")
        self.environment.pop("FAKE_FINDINGS")
        self.environment.pop("FAKE_RESOLVED_FINDING_IDS")

    def test_exhausted_deferral_gets_same_sha_closeout(self) -> None:
        initialized, _, finding = self._reach_exhausted_review_decision()
        run_id = str(initialized["run_id"])
        status = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        head = status["implementation_sha"]
        pending = status["pending_decision"]
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(pending["decision_id"]), "--choice", "DEFER_ELIGIBLE_P1",
            "--finding-ids", str(finding["id"]), "--reason", "defer the eligible P1",
            "--actor", "test-user", "--apply",
        )
        self.environment["FAKE_FINDINGS"] = "[]"
        closeout = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(closeout["status"], "REVIEW_PASS")
        self.assertTrue(closeout["closeout_review"])
        self.assertEqual(closeout["reviewed_sha"], head)
        self.assertEqual(closeout["review_mode_reason"], "SAME_SHA_AFTER_USER_DECISION")
        self.environment.pop("FAKE_FINDINGS")

    def test_nonpassing_closeout_is_terminal_for_the_repair_loop(self) -> None:
        initialized, implementation, _ = self._reach_exhausted_review_decision()
        run_id = str(initialized["run_id"])
        pending = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )["pending_decision"]
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(pending["decision_id"]), "--choice", "RETURN_TO_FIX",
            "--reason", "attempt the final bounded repair", "--actor", "test-user", "--apply",
        )
        self.commit_batch_change(implementation, "still unfixed at closeout\n")
        closeout = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=3,
        )
        self.assertEqual(closeout["status"], "NEEDS_USER_DECISION")
        self.assertIn("CLOSEOUT_REVIEW_DID_NOT_PASS", closeout["decision_reasons"])
        terminal = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )["pending_decision"]
        self.assertEqual(terminal["type"], "CLOSEOUT_REVIEW_DID_NOT_PASS")
        self.assertEqual(terminal["allowed_choices"], ["SUPERSEDE_RUN", "ABORT_RUN"])
        self.environment.pop("FAKE_FINDINGS")

    def test_closeout_invocation_exhaustion_has_one_grant_then_terminal_choices(self) -> None:
        initialized, implementation, _ = self._reach_exhausted_review_decision()
        run_id = str(initialized["run_id"])
        pending = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )["pending_decision"]
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(pending["decision_id"]), "--choice", "RETURN_TO_FIX",
            "--reason", "attempt the final bounded repair", "--actor", "test-user", "--apply",
        )
        self.commit_batch_change(implementation, "fixed for invocation closeout\n")
        self.environment["FAKE_CODEX_EXIT"] = "1"
        # Four valid reviews already consumed four invocations. Six infrastructure failures bind
        # but do not consume the closeout and bring this batch to its normal invocation cap.
        for _ in range(6):
            self.workflow(
                "review", "--project", str(self.project), "--run-id", run_id,
                "--batch", "1", expected=4,
            )
        first_park = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=3,
        )["pending_decision"]
        self.assertEqual(
            first_park["allowed_choices"],
            ["GRANT_ONE_REVIEW_INVOCATION", "SUPERSEDE_RUN", "ABORT_RUN"],
        )
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(first_park["decision_id"]),
            "--choice", "GRANT_ONE_REVIEW_INVOCATION", "--reason", "one final call",
            "--actor", "test-user", "--apply",
        )
        self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=4,
        )
        second_park = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id,
            "--batch", "1", expected=3,
        )["pending_decision"]
        self.assertEqual(second_park["allowed_choices"], ["SUPERSEDE_RUN", "ABORT_RUN"])
        state = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        self.assertFalse(state["closeout_review_grants"]["1"]["used"])
        for choice in second_park["allowed_choices"]:
            preview = self.workflow(
                "adjudicate", "--project", str(self.project), "--run-id", run_id,
                "--decision-id", str(second_park["decision_id"]), "--choice", choice,
                "--reason", "terminal closeout choice", "--actor", "test-user",
            )
            self.assertEqual(preview["status"], "ADJUDICATION_PREVIEW")
        abandoned = self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(second_park["decision_id"]), "--choice", "ABORT_RUN",
            "--reason", "closeout calls are exhausted", "--actor", "test-user", "--apply",
        )
        self.assertEqual(abandoned["run_status"], "ABANDONED")
        self.environment.pop("FAKE_CODEX_EXIT")
        self.environment.pop("FAKE_FINDINGS")

    def test_exhausted_decision_only_resume_gets_same_sha_closeout(self) -> None:
        initialized, _, finding = self._reach_exhausted_review_decision(
            reviewer_decision_at_final=True
        )
        run_id = str(initialized["run_id"])
        status = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        pending = status["pending_decision"]
        self.assertIn("RESUME_WITH_DECISION", pending["allowed_choices"])
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(pending["decision_id"]), "--choice", "RESUME_WITH_DECISION",
            "--reason", "retain the declared authority", "--actor", "test-user", "--apply",
        )
        self.environment["FAKE_FINDINGS"] = "[]"
        self.environment["FAKE_RESOLVED_FINDING_IDS"] = json.dumps([finding["id"]])
        closeout = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(closeout["status"], "REVIEW_PASS")
        self.assertTrue(closeout["closeout_review"])
        self.assertEqual(closeout["reviewed_sha"], status["implementation_sha"])
        self.environment.pop("FAKE_FINDINGS")
        self.environment.pop("FAKE_RESOLVED_FINDING_IDS")

    def test_review_findings_are_returned_in_declared_severity_order(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation)
        findings = []
        for severity in ("P2", "P0", "P1"):
            findings.append({
                "id": f"F-{severity}", "fingerprint": f"fixture-{severity.lower()}",
                "severity": severity, "novelty": "INITIAL_REVIEW",
                "location": "tracked.txt:1",
                "details": f"the fixture is reviewed: {severity} consequence",
                "required_outcome": f"resolve the {severity} consequence",
            })
        self.environment["FAKE_FINDINGS"] = json.dumps(findings)
        reviewed = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=2,
        )
        self.assertEqual(
            [finding["severity"] for finding in reviewed["findings"]], ["P0", "P1", "P2"]
        )
        self.environment.pop("FAKE_FINDINGS")

    def test_a_commit_after_pass_is_reviewable_instead_of_wedging_the_run(self) -> None:
        self.plan.write_text(
            "# Plan\n\n## Batch 1\n\nChange tracked.txt.\n\n## Batch 2\n\nClose out.\n",
            encoding="utf-8",
        )
        self.batch_manifest.write_text(
            "# Run scope\n\nIncluded: plan batches 1 and 2.\n\nExcluded: none.\n",
            encoding="utf-8",
        )
        initialized = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest), "--reviewer", "codex",
            "--implementer", "current-host-agent", "--fix-policy", "ask",
            "--batches", "1,2", "--target-branch", "main",
        )
        self.workflow(
            "contract-review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
        )
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        first = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        later_head = self.commit_batch_change(implementation, "implemented once more\n")
        second = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(second["status"], "REVIEW_PASS")
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["reviewed_sha"], later_head)
        self.assertEqual(second["review_mode"], "DELTA")
        self.assertEqual(second["review_mode_reason"], "FOLLOW_UP_FROM_PRIOR_REVIEWED_SHA")
        self.assertEqual(second["supplied_diff_base_sha"], first["reviewed_sha"])
        run_dir = Path(str(initialized["run_directory"]))
        context = run_dir / "review_context" / "batch_1" / "round_02" / "review-1-002"
        assignment = json.loads((context / "assignment.json").read_text())
        self.assertEqual(assignment["authoritative_coverage_base_sha"], second["base_sha"])
        self.assertEqual(assignment["supplied_diff_base_sha"], first["reviewed_sha"])
        expected = self.run_command(
            "git", "-C", str(initialized["implementation_worktree"]),
            "diff", "--no-ext-diff", "--no-textconv", "--binary",
            str(first["reviewed_sha"]), later_head, "--",
        ).stdout
        self.assertEqual((context / "changes.patch").read_text(), expected)
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["review_performance"]["full_reviews"], 1)
        self.assertEqual(status["review_performance"]["delta_reviews"], 1)
        self.assertEqual(status["review_performance"]["legacy_unclassified_reviews"], 0)
        self.assertGreaterEqual(
            status["review_performance"]["valid_review_wall_seconds"], 0
        )
        self.assertGreater(
            status["review_performance"]["valid_review_supplied_diff_bytes"], 0
        )
        self.assertEqual(
            status["review_performance"]["latest_review"]["supplied_diff_base_sha"],
            first["reviewed_sha"],
        )

    def test_exactly_one_post_pass_review_can_cross_the_round_limit(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        run_id = str(initialized["run_id"])
        for round_number in range(1, 5):
            self.commit_batch_change(implementation, f"implemented round {round_number}\n")
            review = self.workflow(
                "review", "--project", str(self.project),
                "--run-id", run_id, "--batch", "1",
            )
            self.assertEqual(review["status"], "REVIEW_PASS")
            self.assertEqual(review["round"], round_number)

        exempt_head = self.commit_batch_change(implementation, "post-pass correction\n")
        exempt = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(exempt["status"], "REVIEW_PASS")
        self.assertEqual(exempt["round"], 5)
        self.assertEqual(exempt["reviewed_sha"], exempt_head)

        self.commit_batch_change(implementation, "another post-pass change\n")
        parked = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.assertEqual(parked["reason"], "MAX_REVIEW_ROUNDS_EXCEEDED")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(state["post_pass_review_exemptions_used"]["1"]["round"], 5)

    def test_convergence_and_round_budget_grants_are_independent(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        run_id = str(initialized["run_id"])
        for round_number in range(1, 4):
            self.commit_batch_change(implementation, f"implemented round {round_number}\n")
            self.workflow(
                "review", "--project", str(self.project),
                "--run-id", run_id, "--batch", "1",
            )

        self.commit_batch_change(implementation, "round four needs a decision\n")
        self.environment["FAKE_REVIEW_NEEDS_DECISION"] = "1"
        convergence = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.environment.pop("FAKE_REVIEW_NEEDS_DECISION")
        convergence_status = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )
        convergence_decision = convergence_status["pending_decision"]
        self.assertIn("GRANT_ONE_REVIEW", convergence_decision["allowed_choices"])
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(convergence_decision["decision_id"]),
            "--choice", "GRANT_ONE_REVIEW", "--reason", "resolve convergence",
            "--actor", "test-user", "--apply",
        )
        self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1",
        )
        self.commit_batch_change(implementation, "post-pass correction\n")
        self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1",
        )
        self.commit_batch_change(implementation, "budget-granted correction\n")
        budget = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1", expected=3,
        )
        budget_status = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )
        budget_decision = budget_status["pending_decision"]
        self.assertEqual(budget_decision["type"], "REVIEW_BUDGET_EXHAUSTED")
        self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", str(budget_decision["decision_id"]),
            "--choice", "GRANT_ONE_REVIEW", "--reason", "one budget extension",
            "--actor", "test-user", "--apply",
        )
        final = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(final["status"], "REVIEW_PASS")
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text()
        )
        self.assertEqual(state["extra_review_rounds_granted"]["1"], 1)
        self.assertEqual(state["budget_review_rounds_granted"]["1"], 1)
        self.commit_batch_change(implementation, "after every review extension\n")
        exhausted_again = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.assertEqual(exhausted_again["reason"], "MAX_REVIEW_ROUNDS_EXCEEDED")
        decision = self.workflow(
            "status", "--project", str(self.project), "--run-id", run_id,
        )["pending_decision"]
        self.assertNotIn("GRANT_ONE_REVIEW", decision["allowed_choices"])

    def test_pass_without_a_new_commit_refuses_a_second_paid_review(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        first = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(first["status"], "REVIEW_PASS")
        refused = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=1,
        )
        self.assertEqual(refused["status"], "WORKFLOW_ERROR")
        self.assertIn("already holds a PASS", refused["error"])
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(len(state["review_invocations"]), 1)

    def test_accept_binds_to_the_head_reviewed_after_the_post_pass_commit(self) -> None:
        initialized, _, later_head = self._pass_then_commit()
        second = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(second["review_mode"], "FULL")
        self.assertEqual(second["review_mode_reason"], "CUMULATIVE_FINAL_REVIEW")
        self.assertEqual(second["supplied_diff_base_sha"], second["base_sha"])
        accepted = self.workflow(
            "accept", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
            "--review-file", str(second["report_path"]),
        )
        self.assertTrue(accepted["all_batches_accepted"])
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["accepted_shas"]["1"], later_head)

    def test_the_superseded_pass_report_cannot_authorize_the_new_head(self) -> None:
        initialized, first, _ = self._pass_then_commit()
        self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        refused = self.workflow(
            "accept", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
            "--review-file", str(first["report_path"]), expected=1,
        )
        self.assertEqual(refused["status"], "WORKFLOW_ERROR")
        self.assertIn("does not authorize the current batch and HEAD", refused["error"])

    def test_finalize_names_both_shas_when_head_moved_past_acceptance(self) -> None:
        initialized = self.initialize("codex")
        accepted_head, _ = self.review_accept(initialized)
        implementation = Path(str(initialized["implementation_worktree"]))
        stray = self.commit_batch_change(implementation, "an extra commit after acceptance\n")
        refused = self.workflow(
            "finalize", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), expected=1,
        )
        self.assertIn(stray[:12], refused["error"])
        self.assertIn(accepted_head[:12], refused["error"])
        self.assertIn("supersede", refused["error"])

    def test_accept_is_still_refused_before_any_passing_review(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        refused = self.workflow(
            "accept", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
            "--review-file", str(Path(str(initialized["run_directory"])) / "workflow.json"),
            expected=1,
        )
        self.assertIn("accept is not allowed from workflow status", refused["error"])

    def _exhaust_review_invocations(self, initialized: dict) -> None:
        """Burn the batch's invocation budget on reviewer errors, which consume no round."""
        self.environment["FAKE_CODEX_EXIT"] = "1"
        for _ in range(10):
            self.workflow(
                "review", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
            )
        self.environment.pop("FAKE_CODEX_EXIT")

    def test_invocation_budget_exhaustion_offers_one_explicit_grant(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        self._exhaust_review_invocations(initialized)
        parked = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )
        self.assertEqual(parked["reason"], "REVIEW_INVOCATION_BUDGET_EXHAUSTED")
        decision = parked["pending_decision"]
        self.assertEqual(
            decision["allowed_choices"],
            ["GRANT_ONE_REVIEW_INVOCATION", "SUPERSEDE_RUN", "ABORT_RUN"],
        )
        self.assertIn("previous_status", decision)

    def test_granted_invocation_restores_the_parked_status_and_is_usable(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        self._exhaust_review_invocations(initialized)
        parked = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )
        decision_id = parked["pending_decision"]["decision_id"]
        previous = parked["pending_decision"]["previous_status"]
        adjudicated = self.workflow(
            "adjudicate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--decision-id", decision_id, "--choice", "GRANT_ONE_REVIEW_INVOCATION",
            "--reason", "one more call to finish the batch", "--actor", "test-user", "--apply",
        )
        self.assertEqual(adjudicated["run_status"], previous)
        # The grant must satisfy both budget guards, so the extra call really runs.
        review = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(review["status"], "REVIEW_PASS")

    def test_only_one_invocation_grant_is_available_per_batch(self) -> None:
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
        self.commit_batch_change(implementation, "implemented\n")
        self._exhaust_review_invocations(initialized)
        parked = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )
        decision_id = parked["pending_decision"]["decision_id"]
        self.workflow(
            "adjudicate", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--decision-id", decision_id,
            "--choice", "GRANT_ONE_REVIEW_INVOCATION", "--reason", "one final call",
            "--actor", "test-user", "--apply",
        )
        self.environment["FAKE_CODEX_EXIT"] = "1"
        self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
        )
        self.environment.pop("FAKE_CODEX_EXIT")
        exhausted = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
        )["pending_decision"]
        self.assertEqual(exhausted["allowed_choices"], ["SUPERSEDE_RUN", "ABORT_RUN"])
        for choice in exhausted["allowed_choices"]:
            preview = self.workflow(
                "adjudicate", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]),
                "--decision-id", str(exhausted["decision_id"]), "--choice", choice,
                "--reason", "terminal invocation-budget choice", "--actor", "test-user",
            )
            self.assertEqual(preview["status"], "ADJUDICATION_PREVIEW")
        superseded = self.workflow(
            "adjudicate", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]),
            "--decision-id", str(exhausted["decision_id"]), "--choice", "SUPERSEDE_RUN",
            "--reason", "ordinary review calls are exhausted", "--actor", "test-user", "--apply",
        )
        self.assertEqual(superseded["run_status"], "SUPERSEDED")

    def test_restated_obligation_stays_frozen_and_reaches_a_typed_decision(self) -> None:
        """A finding restated every round keeps its first close condition on the real path.

        The reviewer may reword or relocate the obligation; the ledger records each restatement
        and keeps judging the finding against the frozen text, so the stop comes from the
        no-progress rule rather than from a machine judgement about the wording.
        """
        initialized = self.initialize("codex")
        run_id = str(initialized["run_id"])
        implementation = Path(str(initialized["implementation_worktree"]))

        def drifting(location: str, outcome: str) -> str:
            return json.dumps([{
                "id": "F-1-001", "fingerprint": "drifting-obligation", "severity": "P1",
                "novelty": "INITIAL_REVIEW", "location": location,
                "details": "the declared input produces the declared failure",
                "required_outcome": outcome,
            }])

        self.commit_batch_change(implementation, "implemented round 1\n")
        self.environment["FAKE_FINDINGS"] = drifting("one.py:10", "enforce it at the write boundary")
        first = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1", expected=2,
        )
        self.assertEqual(first["status"], "REVIEW_FAIL")

        self.commit_batch_change(implementation, "implemented round 2\n")
        self.environment["FAKE_FINDINGS"] = drifting("two.py:20", "reject every produced filename")
        second = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1", expected=2,
        )
        self.assertEqual(second["status"], "REVIEW_FAIL")
        self.assertEqual(second["decision_reasons"], [])
        emitted = second["findings"][0]
        overlay = set(emitted) - {"id", "fingerprint", "severity", "novelty", "location", "details"}
        self.assertTrue(overlay <= set(WORKFLOW_MODULE.LEDGER_BOUND_FINDING_FIELDS), overlay)
        self.assertTrue(
            {"status", "deferred_reason", "required_outcome", "latest_required_outcome",
             "obligation_revisions"} <= overlay,
            overlay,
        )
        self.assertEqual(emitted["required_outcome"], "enforce it at the write boundary")
        self.assertEqual(emitted["latest_required_outcome"], "reject every produced filename")
        self.assertEqual(emitted["status"], "OPEN")

        self.commit_batch_change(implementation, "implemented round 3\n")
        self.environment["FAKE_FINDINGS"] = drifting(
            "three.py:30", "reject every produced path or directory component"
        )
        third = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.assertEqual(third["status"], "NEEDS_USER_DECISION")
        self.assertEqual(third["decision_reasons"], ["NO_PROGRESS_FOR_TWO_ROUNDS"])
        status = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        allowed = status["pending_decision"]["allowed_choices"]
        self.assertIn("RETURN_TO_FIX", allowed)
        self.assertIn("DEFER_ELIGIBLE_P1", allowed)
        entry = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )["finding_ledger"]["drifting-obligation"]
        self.assertEqual(entry["required_outcome"], "enforce it at the write boundary")
        self.assertEqual(
            entry["latest_required_outcome"], "reject every produced path or directory component"
        )
        self.assertEqual(entry["location"], "three.py:30")
        self.assertEqual(
            [item["stated_required_outcome"] for item in entry["obligation_revisions"]],
            ["reject every produced filename", "reject every produced path or directory component"],
        )
        self.environment.pop("FAKE_FINDINGS")

    def test_upgraded_deferred_p2_offers_return_to_fix_on_the_real_path(self) -> None:
        initialized = self.initialize("codex")
        run_id = str(initialized["run_id"])
        implementation = Path(str(initialized["implementation_worktree"]))

        def graded(severity: str) -> str:
            return json.dumps([{
                "id": "F-1-001", "fingerprint": "graded-defect", "severity": severity,
                "novelty": "INITIAL_REVIEW", "location": "tracked.txt:1",
                "details": "the fixture content is wrong; severity re-assessed on new evidence",
                "required_outcome": "make the fixture content correct",
            }])

        self.commit_batch_change(implementation, "round one\n")
        self.environment["FAKE_FINDINGS"] = graded("P2")
        first = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(first["status"], "REVIEW_PASS")
        self.commit_batch_change(implementation, "round two\n")
        self.environment["FAKE_FINDINGS"] = graded("P1")
        second = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.assertEqual(second["status"], "NEEDS_USER_DECISION")
        self.assertEqual(second["decision_reasons"], ["SEVERITY_UPGRADE:F-1-001"])
        status = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        self.assertIn("RETURN_TO_FIX", status["pending_decision"]["allowed_choices"])
        self.environment.pop("FAKE_FINDINGS")

    def test_deferred_p1_rereported_as_p0_blocks_acceptance(self) -> None:
        initialized = self.initialize("codex")
        run_id = str(initialized["run_id"])
        implementation = Path(str(initialized["implementation_worktree"]))

        def finding(
            finding_id: str, fingerprint: str, severity: str, justification: str = "",
        ) -> dict[str, str]:
            return {
                "id": finding_id, "fingerprint": fingerprint, "severity": severity,
                "novelty": "INITIAL_REVIEW", "location": "module.py:10",
                "details": "the declared input produces the declared failure. " + justification,
                "required_outcome": "preserve the declared authority boundary",
            }

        self.commit_batch_change(implementation, "round one\n")
        first_finding = finding("F-1-001", "round-one-blocker", "P1")
        self.environment["FAKE_FINDINGS"] = json.dumps([first_finding])
        first = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
            expected=2,
        )
        self.assertEqual(first["status"], "REVIEW_FAIL")

        self.commit_batch_change(implementation, "round two\n")
        deferred = finding("F-1-002", "deferred-upgrade", "P1")
        self.environment["FAKE_FINDINGS"] = json.dumps([deferred])
        self.environment["FAKE_RESOLVED_FINDING_IDS"] = json.dumps(["F-1-001"])
        second = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
        )
        self.assertEqual(second["status"], "REVIEW_PASS")
        second_report = second["report_path"]

        self.commit_batch_change(implementation, "round three\n")
        upgraded = finding(
            "F-1-002", "deferred-upgrade", "P0", "new evidence proves an authority bypass",
        )
        self.environment["FAKE_FINDINGS"] = json.dumps([upgraded])
        self.environment["FAKE_RESOLVED_FINDING_IDS"] = "[]"
        third = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
            expected=2,
        )
        self.assertEqual(third["status"], "REVIEW_FAIL")
        self.assertEqual(third["blocking_count"], 1)

        refused = self.workflow(
            "accept", "--project", str(self.project), "--run-id", run_id, "--batch", "1",
            "--review-file", str(second_report), expected=1,
        )
        self.assertIn("accept is not allowed", refused["error"])
        state = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["finding_ledger"]["deferred-upgrade"]["status"], "OPEN")
        self.environment.pop("FAKE_FINDINGS")
        self.environment.pop("FAKE_RESOLVED_FINDING_IDS")


class ConvergencePolicyTests(unittest.TestCase):
    @staticmethod
    def state() -> dict[str, object]:
        return {"finding_ledger": {}, "batch_convergence": {}}

    @staticmethod
    def finding(
        finding_id: str,
        fingerprint: str,
        severity: str,
        novelty: str = "INITIAL_REVIEW",
    ) -> dict[str, str]:
        return {
            "id": finding_id,
            "fingerprint": fingerprint,
            "severity": severity,
            "novelty": novelty,
            "location": "module.py:10",
            "details": "specific input causes an observable failure"
            + (" (hidden by the prior control flow)" if novelty == "PREVIOUSLY_MASKED" else ""),
            "required_outcome": "preserve the required behavior",
        }

    def payload(
        self,
        verdict: str,
        findings: list[dict[str, str]],
        resolved: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "verdict": verdict,
            "findings": findings,
            "resolved_finding_ids": resolved or [],
        }

    def apply(
        self,
        state: dict[str, object],
        payload: dict[str, object],
        round_number: int,
    ) -> dict[str, object]:
        return WORKFLOW_MODULE.apply_convergence_policy(
            state, payload, "1", round_number, f"{round_number:040x}"
        )

    def test_p2_is_deferred_and_does_not_block_pass(self) -> None:
        state = self.state()
        result = self.apply(
            state,
            self.payload("PASS", [self.finding("B1-P2-001", "diagnostic-message", "P2")]),
            1,
        )
        self.assertEqual(result["effective_verdict"], "PASS")
        self.assertEqual(result["blocking_count"], 0)
        self.assertEqual(len(result["deferred_findings"]), 1)

    def test_p0_always_blocks_even_when_reported_pre_existing(self) -> None:
        state = self.state()
        finding = self.finding("B1-P0-001", "authority-bypass", "P0", "PRE_EXISTING")
        result = self.apply(state, self.payload("FAIL", [finding]), 1)
        self.assertEqual(result["effective_verdict"], "FAIL")
        self.assertEqual(result["blocking_count"], 1)

    def test_deferred_p1_reclassified_as_p0_reopens_and_blocks(self) -> None:
        state = self.state()
        deferred = self.finding("B1-P1-001", "deferred-upgrade", "P1")
        first = self.apply(state, self.payload("PASS", [deferred]), 2)
        self.assertEqual(first["blocking_count"], 0)
        self.assertEqual(state["finding_ledger"]["deferred-upgrade"]["status"], "DEFERRED")

        upgraded = self.finding("B1-P1-001", "deferred-upgrade", "P0")
        upgraded["details"] += "; new evidence proves authority bypass"
        second = self.apply(state, self.payload("FAIL", [upgraded]), 3)
        self.assertEqual(second["effective_verdict"], "FAIL")
        self.assertEqual(second["blocking_count"], 1)
        self.assertEqual(state["finding_ledger"]["deferred-upgrade"]["status"], "OPEN")

    def test_resolved_p1_converges_to_pass(self) -> None:
        state = self.state()
        first = self.apply(
            state,
            self.payload("FAIL", [self.finding("B1-P1-001", "budget-routing", "P1")]),
            1,
        )
        self.assertEqual(first["effective_verdict"], "FAIL")
        second = self.apply(
            state,
            self.payload("PASS", [], ["B1-P1-001"]),
            2,
        )
        self.assertEqual(second["effective_verdict"], "PASS")
        self.assertEqual(state["finding_ledger"]["budget-routing"]["status"], "VERIFIED")

    def test_late_nonqualifying_p1_is_deferred(self) -> None:
        state = self.state()
        self.apply(state, self.payload("PASS", []), 1)
        result = self.apply(
            state,
            self.payload("PASS", [self.finding("B1-P1-002", "late-idea", "P1")]),
            2,
        )
        self.assertEqual(result["effective_verdict"], "PASS")
        self.assertEqual(
            state["finding_ledger"]["late-idea"]["deferred_reason"],
            "LATE_NONQUALIFYING_DISCOVERY",
        )

    def test_p1_introduced_by_fix_blocks_on_later_round(self) -> None:
        state = self.state()
        self.apply(state, self.payload("PASS", []), 1)
        finding = self.finding(
            "B1-P1-003", "repair-regression", "P1", "INTRODUCED_BY_FIX"
        )
        result = self.apply(state, self.payload("FAIL", [finding]), 2)
        self.assertEqual(result["effective_verdict"], "FAIL")
        self.assertEqual(result["blocking_count"], 1)

    def test_two_no_progress_rounds_require_user_decision(self) -> None:
        state = self.state()
        finding = self.finding("B1-P1-001", "persistent-error", "P1")
        self.assertEqual(self.apply(state, self.payload("FAIL", [finding]), 1)["effective_verdict"], "FAIL")
        self.assertEqual(self.apply(state, self.payload("FAIL", [finding]), 2)["effective_verdict"], "FAIL")
        third = self.apply(state, self.payload("FAIL", [finding]), 3)
        self.assertEqual(third["effective_verdict"], "NEEDS_USER_DECISION")
        self.assertIn("NO_PROGRESS_FOR_TWO_ROUNDS", third["decision_reasons"])

    def test_round_four_is_final_when_blocking_remains(self) -> None:
        state = self.state()
        finding = self.finding("B1-P1-001", "still-blocking", "P1")
        state["finding_ledger"]["still-blocking"] = {
            **finding,
            "batch": "1",
            "status": "OPEN",
            "blocking": True,
            "first_seen_round": 1,
            "last_seen_round": 3,
            "occurrences": 3,
        }
        state["batch_convergence"]["1"] = {
            "last_blocking_count": 2,
            "no_progress_streak": 0,
            "history": [],
        }
        result = self.apply(state, self.payload("FAIL", [finding]), 4)
        self.assertEqual(result["effective_verdict"], "NEEDS_USER_DECISION")
        self.assertIn("FINAL_REVIEW_ROUND_REACHED", result["decision_reasons"])

    def test_policy_failure_is_atomic(self) -> None:
        state = self.state()
        finding = self.finding("B1-P1-001", "known-fingerprint", "P1")
        self.apply(state, self.payload("FAIL", [finding]), 1)
        before = json.loads(json.dumps(state))
        with self.assertRaisesRegex(
            WORKFLOW_MODULE.ReviewContractError, "unknown in this batch"
        ):
            self.apply(
                state,
                self.payload("PASS", [], ["B1-P1-001", "invented-id"]),
                2,
            )
        self.assertEqual(state, before)

    def test_legacy_resolution_uses_historical_evidence_without_fabricating_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ipwr-legacy-") as temporary:
            run_dir = Path(temporary)
            report_path = run_dir / "reviews" / "batch_1" / "round_01.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "batch": "1",
                        "verdict": "FAIL",
                        "findings": [{"id": "legacy-P1-001", "severity": "P1"}],
                    }
                ),
                encoding="utf-8",
            )
            state = {
                **self.state(),
                "run_directory": str(run_dir),
                "reviews": [{"batch": "1", "report_path": str(report_path)}],
            }
            result = self.apply(
                state, self.payload("PASS", [], ["legacy-P1-001"]), 2
            )
            self.assertEqual(result["effective_verdict"], "PASS")
            self.assertEqual(
                result["warnings"][0]["code"], "LEGACY_UNTRACKED_RESOLUTION"
            )
            self.assertEqual(state["finding_ledger"], {})

    def test_policy_era_report_does_not_authorize_unknown_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ipwr-modern-") as temporary:
            run_dir = Path(temporary)
            report_path = run_dir / "reviews" / "batch_1" / "round_01.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "batch": "1",
                        "effective_verdict": "FAIL",
                        "policy": {},
                        "findings": [{"id": "not-legacy"}],
                    }
                ),
                encoding="utf-8",
            )
            state = {
                **self.state(),
                "run_directory": str(run_dir),
                "reviews": [{"batch": "1", "report_path": str(report_path)}],
            }
            with self.assertRaises(WORKFLOW_MODULE.ReviewContractError):
                self.apply(state, self.payload("PASS", [], ["not-legacy"]), 2)

    def test_only_one_fifth_round_is_allowed_for_stranded_legacy_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ipwr-recovery-") as temporary:
            run_dir = Path(temporary)
            report_path = run_dir / "reviews" / "batch_1" / "round_04.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {"batch": "1", "findings": [{"id": "legacy-id", "severity": "P1"}]}
                ),
                encoding="utf-8",
            )
            state = {
                **self.state(),
                "run_directory": str(run_dir),
                "reviews": [
                    {"batch": "1", "report_path": str(report_path)} for _ in range(4)
                ],
            }
            self.assertTrue(
                WORKFLOW_MODULE.legacy_recovery_round_allowed(state, "1", 5)
            )
            self.assertFalse(
                WORKFLOW_MODULE.legacy_recovery_round_allowed(state, "1", 6)
            )
            report_path.write_text(
                json.dumps(
                    {
                        "batch": "1",
                        "effective_verdict": "FAIL",
                        "policy": {},
                        "findings": [{"id": "legacy-id"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                WORKFLOW_MODULE.legacy_recovery_round_allowed(state, "1", 5)
            )

    def test_legacy_recovery_cannot_silently_drop_prior_blocking_findings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ipwr-recovery-gate-") as temporary:
            run_dir = Path(temporary)
            report_path = run_dir / "reviews" / "batch_1" / "round_04.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "batch": "1",
                        "findings": [
                            {"id": "old-blocker", "severity": "P1"},
                            {"id": "old-p2", "severity": "P2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = {
                **self.state(),
                "run_directory": str(run_dir),
                "reviews": [
                    {"batch": "1", "report_path": str(report_path)} for _ in range(4)
                ],
            }
            with self.assertRaisesRegex(
                WORKFLOW_MODULE.ReviewContractError, "old-blocker"
            ):
                self.apply(state, self.payload("PASS", []), 5)


class ConvergencePolicyFindingBuilder(unittest.TestCase):
    """Shared construction helpers for the cross-batch and obligation-drift suites."""

    BATCH = "1"

    @staticmethod
    def state(ledger: dict | None = None) -> dict:
        return {"finding_ledger": ledger or {}, "batch_convergence": {}}

    @staticmethod
    def finding(
        finding_id: str,
        fingerprint: str,
        severity: str = "P1",
        novelty: str = "INITIAL_REVIEW",
        location: str = "pkg/module.py:10",
        required_outcome: str = "preserve the required behavior",
    ) -> dict:
        return {
            "id": finding_id,
            "fingerprint": fingerprint,
            "severity": severity,
            "novelty": novelty,
            "location": location,
            "details": "specific input causes an observable failure",
            "required_outcome": required_outcome,
        }

    @staticmethod
    def owned_entry(finding_id: str, batch: str, severity: str = "P1") -> dict:
        return {
            "id": finding_id,
            "fingerprint": "owned-elsewhere",
            "batch": batch,
            "severity": severity,
            "novelty": "INITIAL_REVIEW",
            "status": "VERIFIED",
            "blocking": severity != "P2",
            "deferred_reason": None,
            "location": "other/module.py:5",
            "required_outcome": "the other batch's obligation",
            "first_seen_round": 1,
            "first_seen_sha": "b" * 40,
            "last_seen_round": 1,
            "last_seen_sha": "b" * 40,
            "occurrences": 1,
        }

    def apply(self, state: dict, payload: dict, round_number: int) -> dict:
        return WORKFLOW_MODULE.apply_convergence_policy(
            state, payload, self.BATCH, round_number, f"{round_number:040x}"
        )

    @staticmethod
    def payload(verdict: str, findings: list, resolved: list | None = None) -> dict:
        return {
            "verdict": verdict,
            "findings": findings,
            "resolved_finding_ids": resolved or [],
        }

    def runTest(self) -> None:  # pragma: no cover - container only
        pass


class CrossBatchFindingTests(ConvergencePolicyFindingBuilder):
    """A review may reference an earlier batch's finding without losing the whole payload."""

    def test_cross_batch_resolution_is_warned_not_rejected(self) -> None:
        ledger = {"owned-elsewhere": self.owned_entry("F-B03-004", "0")}
        ledger["owned-elsewhere"]["status"] = "OPEN"
        state = self.state(ledger)
        result = self.apply(state, self.payload("PASS", [], ["F-B03-004"]), 1)
        self.assertEqual(result["effective_verdict"], "PASS")
        codes = [item["code"] for item in result["warnings"]]
        self.assertIn("CROSS_BATCH_RESOLUTION", codes)
        # Authority stays with the owning batch: its status is untouched.
        self.assertEqual(state["finding_ledger"]["owned-elsewhere"]["status"], "OPEN")

    def test_unknown_resolution_outside_any_batch_is_still_rejected(self) -> None:
        state = self.state()
        with self.assertRaises(WORKFLOW_MODULE.ReviewContractError):
            self.apply(state, self.payload("PASS", [], ["F-NOWHERE-001"]), 1)

    def test_cross_batch_p2_finding_is_warned_and_does_not_block(self) -> None:
        state = self.state({"owned-elsewhere": self.owned_entry("F-B03-004", "0", "P2")})
        finding = self.finding("F-B03-004", "owned-elsewhere", "P2")
        result = self.apply(state, self.payload("PASS", [finding]), 1)
        self.assertEqual(result["effective_verdict"], "PASS")
        warning = next(w for w in result["warnings"] if w["code"] == "CROSS_BATCH_FINDING")
        self.assertEqual(warning["owning_batch"], "0")
        owner = state["finding_ledger"]["owned-elsewhere"]
        self.assertEqual(owner["occurrences"], 2)
        self.assertEqual(owner["status"], "VERIFIED")
        self.assertEqual(owner["batch"], "0")

    def test_cross_batch_p1_finding_records_but_does_not_stall_the_run(self) -> None:
        state = self.state({"owned-elsewhere": self.owned_entry("F-B03-004", "0")})
        finding = self.finding("F-B03-004", "owned-elsewhere", "P1")
        result = self.apply(state, self.payload("PASS", [finding]), 1)
        self.assertEqual(result["effective_verdict"], "PASS")
        self.assertEqual(result["decision_reasons"], [])

    def test_cross_batch_p0_finding_requires_a_user_decision(self) -> None:
        state = self.state({"owned-elsewhere": self.owned_entry("F-B03-004", "0")})
        finding = self.finding("F-B03-004", "owned-elsewhere", "P0")
        result = self.apply(state, self.payload("PASS", [finding]), 1)
        self.assertEqual(result["effective_verdict"], "NEEDS_USER_DECISION")
        self.assertIn("CROSS_BATCH_MATERIAL_FINDING:F-B03-004", result["decision_reasons"])

    def test_cross_batch_id_reuse_with_new_fingerprint_does_not_duplicate_the_id(self) -> None:
        state = self.state({"owned-elsewhere": self.owned_entry("F-B03-004", "0")})
        finding = self.finding("F-B03-004", "a-brand-new-fingerprint", "P2")
        result = self.apply(state, self.payload("PASS", [finding]), 1)
        self.assertNotIn("a-brand-new-fingerprint", state["finding_ledger"])
        self.assertIn("CROSS_BATCH_FINDING", [w["code"] for w in result["warnings"]])

    def test_same_batch_id_rebinding_is_still_rejected(self) -> None:
        state = self.state()
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "first-fp")]), 1)
        with self.assertRaises(WORKFLOW_MODULE.ReviewContractError):
            self.apply(state, self.payload("FAIL", [self.finding("F-1", "second-fp")]), 2)


class ObligationDriftTests(ConvergencePolicyFindingBuilder):
    """The close condition is frozen at first statement; restatements are recorded, not judged."""

    def test_unchanged_obligation_records_no_revision(self) -> None:
        state = self.state()
        finding = self.finding("F-1", "stable")
        self.apply(state, self.payload("FAIL", [finding]), 1)
        result = self.apply(state, self.payload("FAIL", [finding]), 2)
        entry = state["finding_ledger"]["stable"]
        self.assertEqual(entry.get("obligation_revisions"), [])
        self.assertEqual(entry["occurrences"], 2)
        self.assertEqual(result["decision_reasons"], [])

    def test_rewording_or_relocation_never_produces_a_machine_decision(self) -> None:
        """Whether a restatement widens the obligation is a semantic judgement, so no reason fires."""
        state = self.state()
        for round_number, location, outcome in (
            (1, "a/one.py:1", "preserve the required behavior"),
            (2, "b/two.py:2", "preserve the  required behaviour everywhere"),
            (3, "c/three.py:3", "reject every produced path"),
        ):
            result = self.apply(
                state,
                self.payload(
                    "FAIL",
                    [self.finding("F-1", "drifting", location=location, required_outcome=outcome)],
                ),
                round_number,
            )
            self.assertEqual(
                [reason for reason in result["decision_reasons"] if reason != "NO_PROGRESS_FOR_TWO_ROUNDS"],
                [],
            )

    def test_close_condition_is_frozen_and_restatements_are_kept_for_audit(self) -> None:
        state = self.state()
        self.apply(
            state,
            self.payload("FAIL", [self.finding("F-1", "drifting", required_outcome="the first ask")]),
            1,
        )
        self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "drifting", location="b/two.py:2", required_outcome="a much wider ask")],
            ),
            2,
        )
        self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "drifting", location="b/two.py:9", required_outcome="THE  first ask")],
            ),
            3,
        )
        entry = state["finding_ledger"]["drifting"]
        self.assertEqual(entry["required_outcome"], "the first ask")
        self.assertEqual(entry["latest_required_outcome"], "THE  first ask")
        self.assertEqual(entry["location"], "b/two.py:9")
        self.assertEqual(
            entry["obligation_revisions"],
            [{"round": 2, "stated_required_outcome": "a much wider ask"}],
        )

    def test_recorded_five_round_restatement_stops_on_no_progress(self) -> None:
        """Replays F-B05-002, which was restated at five locations over five rounds.

        The stop now comes from the no-progress rule on the frozen obligation, and the ledger
        shows the four restatements the adjudicating user needs to see.
        """
        observed = [
            ("da-evaluator/scripts/evaluator_synthesizer_agent.py:338", "persist a durable typed park record"),
            ("scripts/provider_park.py:97", "persist a durable typed park record"),
            ("da-evaluator/scripts/run_evaluator_fanout.py:1974", "consult the park after the launch that observed the refusal"),
            ("da-evaluator/scripts/run_evaluator_fanout.py:1606", "every operation recording a park must have a later reader"),
            ("scripts/run_fanout.py:433", "give run_fanout.run its own park consult and typed return"),
        ]
        state = self.state()
        fired_at = None
        for index, (location, outcome) in enumerate(observed, start=1):
            result = self.apply(
                state,
                self.payload(
                    "FAIL",
                    [
                        self.finding(
                            "F-B05-002", "provider-park",
                            location=location, required_outcome=outcome,
                        )
                    ],
                ),
                index,
            )
            if fired_at is None and result["effective_verdict"] == "NEEDS_USER_DECISION":
                fired_at = index
                self.assertEqual(result["decision_reasons"], ["NO_PROGRESS_FOR_TWO_ROUNDS"])
        self.assertEqual(fired_at, 3)
        entry = state["finding_ledger"]["provider-park"]
        self.assertEqual(entry["required_outcome"], "persist a durable typed park record")
        self.assertEqual(len(entry["obligation_revisions"]), 3)

    def test_severity_change_is_recorded_without_a_justification_field(self) -> None:
        state = self.state()
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "graded", severity="P1")]), 1)
        result = self.apply(
            state, self.payload("FAIL", [self.finding("F-1", "graded", severity="P0")]), 2
        )
        entry = state["finding_ledger"]["graded"]
        self.assertEqual(entry["severity_history"], [{"round": 2, "from": "P1", "to": "P0"}])
        self.assertEqual(entry["status"], "OPEN")
        self.assertEqual(result["effective_verdict"], "FAIL")

    def test_reopened_finding_carries_no_stale_deferred_reason(self) -> None:
        state = self.state()
        self.apply(state, self.payload("PASS", [self.finding("F-1", "graded", severity="P2")]), 1)
        self.assertEqual(state["finding_ledger"]["graded"]["deferred_reason"], "NON_BLOCKING_P2")
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "graded", severity="P0")]), 2)
        entry = state["finding_ledger"]["graded"]
        self.assertEqual(entry["status"], "OPEN")
        self.assertIsNone(entry["deferred_reason"])
        self.assertEqual(entry["latest_required_outcome"], "preserve the required behavior")

    def test_downgrade_to_p2_unblocks_with_a_recorded_reason_and_a_warning(self) -> None:
        state = self.state()
        first = self.apply(state, self.payload("FAIL", [self.finding("F-1", "graded", severity="P0")]), 1)
        self.assertEqual(first["effective_verdict"], "FAIL")
        second = self.apply(
            state, self.payload("PASS", [self.finding("F-1", "graded", severity="P2")]), 2
        )
        entry = state["finding_ledger"]["graded"]
        self.assertEqual(second["effective_verdict"], "PASS")
        self.assertEqual(entry["status"], "DEFERRED")
        self.assertFalse(entry["blocking"])
        self.assertEqual(entry["deferred_reason"], "SEVERITY_DOWNGRADE:P0->P2")
        self.assertIn(
            {"code": "SEVERITY_DOWNGRADE", "finding_id": "F-1", "from": "P0", "to": "P2"},
            second["warnings"],
        )

    def test_upgrade_of_a_deferred_p2_escalates_to_the_user(self) -> None:
        state = self.state()
        self.apply(state, self.payload("PASS", [self.finding("F-1", "graded", severity="P2")]), 1)
        self.assertEqual(state["finding_ledger"]["graded"]["status"], "DEFERRED")
        second = self.apply(
            state, self.payload("FAIL", [self.finding("F-1", "graded", severity="P1")]), 2
        )
        self.assertEqual(second["effective_verdict"], "NEEDS_USER_DECISION")
        self.assertEqual(second["decision_reasons"], ["SEVERITY_UPGRADE:F-1"])
        entry = state["finding_ledger"]["graded"]
        # The upgrade is the user's call (RETURN_TO_FIX or DEFER_ELIGIBLE_P1): the entry stays
        # deferred until that decision rather than reopening on the reviewer's authority alone.
        self.assertEqual(entry["status"], "DEFERRED")
        self.assertFalse(entry["blocking"])
        self.assertEqual(entry["severity"], "P1")
        self.assertEqual(entry["severity_history"], [{"round": 2, "from": "P2", "to": "P1"}])

    def test_ledger_entry_written_before_this_change_still_converges(self) -> None:
        """A run created with the retired prose fields must review and converge unchanged."""
        legacy = {
            "id": "F-1", "fingerprint": "pre-existing-entry", "batch": self.BATCH,
            "severity": "P1", "novelty": "INITIAL_REVIEW", "status": "OPEN", "blocking": True,
            "deferred_reason": None, "location": "pkg/module.py:10",
            "trigger": "specific input", "consequence": "observable failure",
            "severity_change_justification": "", "introduced_by_sha": "",
            "required_outcome": "preserve the required behavior",
            "original_required_outcome": "preserve the required behavior",
            "obligation_revisions": [{
                "round": 1, "from_required_outcome": "x", "to_required_outcome": "y",
                "from_paths": [], "to_paths": [],
            }],
            "first_seen_round": 1, "first_seen_sha": "c" * 40,
            "last_seen_round": 1, "last_seen_sha": "c" * 40, "occurrences": 1,
        }
        state = self.state({"pre-existing-entry": legacy})
        state["batch_convergence"] = {self.BATCH: {
            "last_blocking_count": 1, "no_progress_streak": 0, "history": [],
        }}
        result = self.apply(
            state, self.payload("FAIL", [self.finding("F-1", "pre-existing-entry")]), 2
        )
        entry = state["finding_ledger"]["pre-existing-entry"]
        self.assertEqual(len(entry["obligation_revisions"]), 1)
        self.assertEqual(entry["required_outcome"], "preserve the required behavior")
        self.assertEqual(entry["details"], "specific input causes an observable failure")
        self.assertEqual(result["effective_verdict"], "FAIL")
        resolved = self.apply(state, self.payload("PASS", [], ["F-1"]), 3)
        self.assertEqual(resolved["effective_verdict"], "PASS")

    def test_cross_batch_ownership_is_decided_before_obligation_bookkeeping(self) -> None:
        state = self.state({"owned-elsewhere": self.owned_entry("F-B03-004", "0")})
        finding = self.finding(
            "F-B03-004", "owned-elsewhere", "P2",
            location="somewhere/else.py:1", required_outcome="a different ask",
        )
        result = self.apply(state, self.payload("PASS", [finding]), 2)
        self.assertIn("CROSS_BATCH_FINDING", [w["code"] for w in result["warnings"]])
        self.assertNotIn("obligation_revisions", state["finding_ledger"]["owned-elsewhere"])


class ReviewerSchemaCompatibilityTests(unittest.TestCase):
    def test_ready_contract_must_cover_every_batch(self) -> None:
        state = {"reviewer": "codex", "baseline_sha": "a" * 40, "batches": ["B01", "B02"]}
        payload = {
            "reviewer": "codex", "baseline_sha": "a" * 40, "assessment": "READY",
            "summary": "incomplete", "issues": [],
            "criteria": [{
                "id": "C1", "batch": "B01", "source": "DECLARED",
                "observation": "run test", "expected_result": "exit 0",
                "scope": ["tests"], "rationale": "fixture",
            }],
        }
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "does not cover every batch"):
            WORKFLOW_MODULE.validate_contract_payload(payload, state)

    def test_verification_request_keys_do_not_collide_after_slugging(self) -> None:
        state = {"verification_requests": []}
        payload = {"verification_requests": [
            {"id": "test/a", "argv": ["true"], "cwd": "repository", "reason": "a", "expected_exit": 0},
            {"id": "test a", "argv": ["true"], "cwd": "repository", "reason": "b", "expected_exit": 0},
        ]}
        records = WORKFLOW_MODULE.register_verification_requests(
            state, payload, "B01", 1, "a" * 40, "b" * 40, Path("/tmp/report.json")
        )
        self.assertEqual(len({item["request_key"] for item in records}), 2)

    def test_ready_contract_rejects_unresolved_user_boundary(self) -> None:
        state = {"reviewer": "codex", "baseline_sha": "a" * 40, "batches": ["B01"]}
        payload = {
            "reviewer": "codex", "baseline_sha": "a" * 40, "assessment": "READY",
            "summary": "not actually ready", "issues": [],
            "criteria": [{
                "id": "C1", "batch": "B01", "source": "REVIEWER_DERIVED",
                "observation": "choose behavior", "expected_result": "user choice",
                "scope": ["product"], "rationale": "boundary",
                "evidence_kind": "USER_BOUNDARY", "argv": [], "expected_exit": 0,
            }],
        }
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "cannot contain unresolved USER_BOUNDARY"):
            WORKFLOW_MODULE.validate_contract_payload(payload, state)

    def test_command_criterion_requires_its_own_evidence(self) -> None:
        criterion = lambda identifier: {
            "id": identifier, "batch": "B01", "evidence_kind": "COMMAND",
        }
        state = {
            "acceptance_contract": {"criteria": [criterion("C1"), criterion("C2")]},
            "verification_evidence": [{
                "evidence_id": "EV1", "criterion_id": "C1", "reviewed_sha": "b" * 40,
                "status": "PASS",
            }],
        }
        payload = {
            "reviewer": "codex", "reviewed_sha": "b" * 40, "base_sha": "a" * 40,
            "batch": "B01", "verdict": "PASS", "summary": "wrong reuse",
            "findings": [], "resolved_finding_ids": [], "verification_requests": [],
            "criterion_results": [
                {"criterion_id": "C1", "status": "PASS", "evidence_ids": ["EV1"], "rationale": "ok"},
                {"criterion_id": "C2", "status": "PASS", "evidence_ids": ["EV1"], "rationale": "reused"},
            ],
        }
        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "C2 lacks current-SHA PASS evidence"):
            WORKFLOW_MODULE.validate_review_payload(
                payload, "codex", "B01", "a" * 40, "b" * 40, state
            )

    def test_review_schema_uses_portable_keyword_subset(self) -> None:
        self.assertEqual(WORKFLOW_MODULE.MAX_REVIEW_ROUNDS, 4)
        self.assertEqual(
            WORKFLOW_MODULE.unsupported_schema_keywords(WORKFLOW_MODULE.REVIEW_SCHEMA), []
        )
        self.assertNotIn("uniqueItems", json.dumps(WORKFLOW_MODULE.REVIEW_SCHEMA))
        self.assertNotIn("minLength", json.dumps(WORKFLOW_MODULE.REVIEW_SCHEMA))
        self.assertNotIn("pattern", json.dumps(WORKFLOW_MODULE.REVIEW_SCHEMA))

    @staticmethod
    def _conforms(instance: object, schema: dict) -> list[str]:
        """Minimal strict-mode conformance: closed objects, required keys, enums, types."""
        problems: list[str] = []
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(instance, dict):
                return [f"expected object, got {type(instance).__name__}"]
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is not False:
                problems.append("object is not closed")
            if sorted(schema.get("required", [])) != sorted(properties):
                problems.append("strict mode requires every property")
            for key in schema.get("required", []):
                if key not in instance:
                    problems.append(f"missing required {key}")
            for key, value in instance.items():
                if key not in properties:
                    problems.append(f"unexpected property {key}")
                else:
                    problems.extend(
                        f"{key}: {item}"
                        for item in ReviewerSchemaCompatibilityTests._conforms(value, properties[key])
                    )
        elif kind == "array":
            if not isinstance(instance, list):
                return ["expected array"]
            for index, item in enumerate(instance):
                problems.extend(
                    f"[{index}] {problem}"
                    for problem in ReviewerSchemaCompatibilityTests._conforms(item, schema["items"])
                )
        elif kind == "string":
            if not isinstance(instance, str):
                problems.append("expected string")
            elif "enum" in schema and instance not in schema["enum"]:
                problems.append(f"{instance!r} not in enum")
        elif kind == "integer" and not isinstance(instance, int):
            problems.append("expected integer")
        return problems

    def test_every_novelty_value_is_expressible_under_the_strict_wire_schema(self) -> None:
        """A finding the prompt asks for must be deliverable and then accepted by the validator.

        0.7.5 removed `introduced_by_sha` and `why_not_detectable_earlier` from the provider schema
        while the validator kept demanding them for two novelty values, so a reviewer that
        truthfully reported a fix-introduced regression had its whole review rejected.
        """
        wire = WORKFLOW_MODULE.producer_delivery_schema(
            WORKFLOW_MODULE.REVIEW_SCHEMA, {"reviewer", "reviewed_sha", "base_sha", "batch"},
        )
        finding_schema = wire["properties"]["findings"]["items"]
        for novelty in finding_schema["properties"]["novelty"]["enum"]:
            with self.subTest(novelty=novelty):
                finding = {
                    key: ({"severity": "P1", "novelty": novelty}.get(key, f"{key} text"))
                    for key in finding_schema["required"]
                }
                delivered = {
                    "verdict": "FAIL", "summary": "strict delivery",
                    "findings": [finding], "resolved_finding_ids": [],
                    "verification_requests": [], "criterion_results": [],
                }
                self.assertEqual(self._conforms(delivered, wire), [])
                payload = json.loads(json.dumps(delivered))
                WORKFLOW_MODULE.bind_review_identity(
                    payload, reviewer="codex", batch="B1", base="a" * 40, head="b" * 40,
                )
                WORKFLOW_MODULE.normalize_review_payload(payload)
                WORKFLOW_MODULE.validate_review_payload(
                    payload, "codex", "B1", "a" * 40, "b" * 40,
                )

    def test_every_finding_field_named_by_the_reviewer_prompt_is_deliverable(self) -> None:
        """The prompt and the wire schema must describe one finding shape."""
        wire = WORKFLOW_MODULE.producer_delivery_schema(
            WORKFLOW_MODULE.REVIEW_SCHEMA, {"reviewer", "reviewed_sha", "base_sha", "batch"},
        )

        def property_names(schema: dict, names: set) -> set:
            for key, value in schema.get("properties", {}).items():
                names.add(key)
                property_names(value, names)
            if isinstance(schema.get("items"), dict):
                property_names(schema["items"], names)
            return names

        deliverable = property_names(wire, set())
        interpreters = {"python", "pypy"}  # the only backticked lowercase words that are not fields
        prompt = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`([a-z_]+)`", prompt)) - interpreters
        self.assertEqual(sorted(named - deliverable), [], "prompt names a field the wire cannot carry")
        finding_fields = set(wire["properties"]["findings"]["items"]["properties"])
        self.assertEqual(sorted(finding_fields - named), [], "prompt must explain every finding field")

    def test_compatibility_guard_detects_forbidden_keyword(self) -> None:
        bad_schema = {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        }
        self.assertEqual(
            WORKFLOW_MODULE.unsupported_schema_keywords(bad_schema), ["$.uniqueItems"]
        )

    def test_duplicate_resolved_ids_are_rejected_in_python(self) -> None:
        payload = {
            "reviewer": "codex",
            "reviewed_sha": "b" * 40,
            "base_sha": "a" * 40,
            "batch": "1",
            "verdict": "PASS",
            "summary": "duplicate fixture",
            "findings": [],
            "resolved_finding_ids": ["B1-P1-001", "B1-P1-001"],
            "verification_requests": [],
        }
        with self.assertRaisesRegex(
            WORKFLOW_MODULE.WorkflowError, "must not contain duplicates"
        ):
            WORKFLOW_MODULE.validate_review_payload(
                payload, "codex", "1", "a" * 40, "b" * 40
            )


class DocumentationContractTests(unittest.TestCase):
    def test_dirty_init_and_explicit_instruction_contract_are_publicly_aligned(self) -> None:
        documents = {
            "skill": (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            "readme": (SKILL_ROOT / "README.md").read_text(encoding="utf-8"),
            "implementation": (
                SKILL_ROOT / "references" / "implementation_workflow.md"
            ).read_text(encoding="utf-8"),
        }
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIn("--instruction-file", document)
                self.assertIn("UTF-8", document)
                self.assertIn("checkout", document.lower())
                self.assertIn("final", document.lower())
        self.assertIn("cannot override", documents["implementation"])
        self.assertIn("working-tree changes excluded", documents["implementation"])

    def test_causality_contract_is_shared_without_turning_semantics_into_a_gate(self) -> None:
        documents = {
            "skill": (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            "implementation": (
                SKILL_ROOT / "references" / "implementation_workflow.md"
            ).read_text(encoding="utf-8"),
            "reviewer": (
                SKILL_ROOT / "references" / "reviewer_prompt.md"
            ).read_text(encoding="utf-8"),
            "contract": (
                SKILL_ROOT / "references" / "contract_reviewer_prompt.md"
            ).read_text(encoding="utf-8"),
        }
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIn("earliest responsible", document)
                self.assertIn("deterministic", document.lower())
                self.assertIn("generative", document.lower())
                self.assertIn("semantic", document.lower())
        protocol = (SKILL_ROOT / "references" / "causal_analysis.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("actual input", protocol)
        self.assertIn("consumer interpretation", protocol)
        self.assertIn("valid outputs", protocol.lower())
        self.assertIn("Keep verification proportional to risk", protocol)

    def test_review_latency_contract_preserves_authority_while_reducing_transport(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        implementation = (
            SKILL_ROOT / "references" / "implementation_workflow.md"
        ).read_text(encoding="utf-8")
        reviewer = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("one fresh final reviewer by default", skill)
        self.assertIn("Reserve", skill)
        for document in (implementation, reviewer):
            self.assertIn("transport", document)
            self.assertIn("optimization", document)
            self.assertIn("authoritative coverage range", document)
            self.assertIn("exact-HEAD", document)
        self.assertIn("Never use filenames, line counts, or keyword rules", implementation)

    def test_public_contract_documents_all_implementation_reviewers(self) -> None:
        documents = {
            "skill": (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            "readme": (SKILL_ROOT / "README.md").read_text(encoding="utf-8"),
            "support": (SKILL_ROOT / "SUPPORT.md").read_text(encoding="utf-8"),
            "implementation": (
                SKILL_ROOT / "references" / "implementation_workflow.md"
            ).read_text(encoding="utf-8"),
        }
        self.assertEqual(
            WORKFLOW_MODULE.SUPPORTED_REVIEWERS,
            ("claude", "codex", "dsh", "other"),
        )
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIn("dsh", document)
                self.assertIn("other", document)
                self.assertNotIn("dsh is supported for planning only", document.lower())
                self.assertNotIn("not an Implement reviewer", document)
                self.assertNotIn("planning backend only", document.lower())
        self.assertIn("--dsh-model", documents["skill"])
        self.assertIn("--dsh-model-provider", documents["implementation"])

    def test_host_and_reviewer_share_finding_lifecycle_vocabulary(self) -> None:
        skill = (SKILL_ROOT / "references" / "implementation_workflow.md").read_text(
            encoding="utf-8"
        )
        reviewer = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(
            encoding="utf-8"
        )
        shared_terms = (
            "fingerprint",
            "novelty",
            "INITIAL_REVIEW",
            "INTRODUCED_BY_FIX",
            "PREVIOUSLY_MASKED",
            "PRE_EXISTING",
            "UNRELATED",
            "resolved_finding_ids",
            "required_outcome",
            "location",
            "details",
            "OPEN",
            "VERIFIED",
            "DEFERRED",
        )
        for term in shared_terms:
            with self.subTest(term=term):
                self.assertIn(term, skill)
                self.assertIn(term, reviewer)
        self.assertIn("legacy recovery", skill.lower())
        self.assertIn("legacy recovery", reviewer.lower())

    def test_host_and_reviewer_both_reject_undecidable_exit_conditions(self) -> None:
        skill = (SKILL_ROOT / "references" / "implementation_workflow.md").read_text(
            encoding="utf-8"
        )
        reviewer = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(
            encoding="utf-8"
        )
        for document in (skill, reviewer):
            self.assertIn("decidable", document)
            self.assertIn("machine checkable", document)
            self.assertIn("named", document)
        self.assertIn("verdict=NEEDS_USER_DECISION", reviewer)
        self.assertIn("exactly one P1 finding", reviewer)

    def test_review_tiers_and_closeout_recovery_are_shared_contracts(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        workflow = (SKILL_ROOT / "references" / "implementation_workflow.md").read_text(
            encoding="utf-8"
        )
        reviewer = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(
            encoding="utf-8"
        )
        for document in (skill, workflow, reviewer):
            self.assertIn("P0", document)
            self.assertIn("P1", document)
            self.assertIn("P2", document)
            self.assertIn("closeout", document.lower())
            self.assertIn("nonblocking", document.lower())
        self.assertIn("exact implementation SHA", reviewer)
        self.assertIn("SUPERSEDE_RUN", workflow)

    def test_runtime_contract_explains_controller_identity_and_venv_bridge(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        workflow = (SKILL_ROOT / "references" / "implementation_workflow.md").read_text(
            encoding="utf-8"
        )
        reviewer = (SKILL_ROOT / "references" / "reviewer_prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("controller_runtime", skill)
        self.assertIn("<controller-python>", workflow)
        self.assertIn("original project environment", reviewer)
        self.assertIn("never runs `uv sync`", workflow)



class SharedVerificationWorktreeTests(unittest.TestCase):
    """Verifications of one commit share one read-only checkout.

    Every verification used to get its own. It cannot need one: the sandbox mounts the tree
    ``--ro-bind`` at /mnt, so two verifications of the same commit receive a byte-identical tree
    that neither is able to modify. Measured on the run these tests come from -- 223
    verifications across 19 distinct SHAs, ``worktree_changes`` empty 223 times out of 223 --
    that was twelve checkouts made for every one carrying distinct content, and the scratch beside
    them reached 7.7 GB against 1.9 MB of evidence.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ipwr-sharedwt-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self._git("init", "--initial-branch=main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.project / "a.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "a.txt")
        self._git("commit", "-m", "one")
        self.first = self._git("rev-parse", "HEAD").strip()
        (self.project / "a.txt").write_text("two\n", encoding="utf-8")
        self._git("commit", "-am", "two")
        self.second = self._git("rev-parse", "HEAD").strip()
        self.run_root = self.root / "run"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ("git", "-C", str(self.project)) + args,
            check=True, capture_output=True, text=True,
        ).stdout

    def test_one_directory_per_commit_not_per_verification(self) -> None:
        same_a = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.first)
        same_b = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.first)
        other = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.second)
        self.assertEqual(same_a, same_b, "two verifications of one commit must address one tree")
        self.assertNotEqual(same_a, other, "different commits must not collide")

    def test_missing_project_venv_fails_before_sandbox_with_actionable_diagnostic(self) -> None:
        with self.assertRaisesRegex(
            WORKFLOW_MODULE.WorkflowError,
            "Git-ignored virtual environments do not follow worktrees",
        ):
            WORKFLOW_MODULE._sandbox_command(
                "bwrap",
                [".venv/bin/python", "-m", "pytest"],
                self.project,
                self.project,
                self.root / "tmp",
                "offline",
            )

    def test_project_venv_is_reported_and_bridged_from_original_checkout(self) -> None:
        launcher = self.project / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\necho 'Python fixture'\n", encoding="utf-8")
        launcher.chmod(0o755)
        runtime = WORKFLOW_MODULE.project_runtime_payload(self.project)
        self.assertTrue(runtime["project_venv"]["available"])
        self.assertEqual(runtime["project_venv"]["version_probe"], "NOT_EXECUTED")
        self.assertEqual(runtime["project_venv"]["resolved_launcher"], str(launcher.resolve()))
        self.assertEqual(
            runtime["project_venv"]["verification_source"], "ORIGINAL_PROJECT_READ_ONLY"
        )

        sandboxed, executed = WORKFLOW_MODULE._sandbox_command(
            "bwrap",
            [".venv/bin/python", "-V"],
            self.project,
            self.project,
            self.root / "tmp",
            "offline",
        )
        self.assertEqual(executed[0], str(launcher))
        self.assertIn(str((self.project / ".venv").resolve()), sandboxed)

    def test_project_venv_path_is_normalized_without_mounting_host_root(self) -> None:
        launcher = self.project / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        os.symlink("/bin/sh", launcher)

        sandboxed, executed = WORKFLOW_MODULE._sandbox_command(
            "bwrap",
            ["./.venv/bin/python", "-c", "exit 0"],
            self.project,
            self.project,
            self.root / "tmp",
            "offline",
        )

        self.assertEqual(executed[0], str(launcher))
        mounts = list(zip(sandboxed, sandboxed[1:]))
        self.assertNotIn(("/", "/"), mounts)
        self.assertFalse(WORKFLOW_MODULE.non_venv_python("./.venv/bin/python"))
        self.assertEqual(
            WORKFLOW_MODULE.normalized_venv_executable(".venv/../.venv/bin/python"),
            ".venv/bin/python",
        )

    def test_project_venv_runtime_directory_symlink_cannot_hide_a_root_mount(self) -> None:
        launcher = self.project / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        disguised_root = self.root / "runtime-link"
        os.symlink("/", disguised_root)
        external = Path("/tmp") / f"{self.root.name}-runtime-python"
        external.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        external.chmod(0o755)
        self.addCleanup(external.unlink, missing_ok=True)
        os.symlink(disguised_root / "tmp" / external.name, launcher)

        with self.assertRaisesRegex(WORKFLOW_MODULE.WorkflowError, "broad runtime prefix /"):
            WORKFLOW_MODULE._sandbox_command(
                "bwrap",
                [".venv/bin/python", "-V"],
                self.project,
                self.project,
                self.root / "tmp",
                "offline",
            )

    def test_absolute_non_system_python_mounts_only_its_runtime_prefix(self) -> None:
        prefix = self.root / "hostedtoolcache" / "Python" / "3.13.0" / "x64"
        launcher = prefix / "bin" / "python3"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)

        sandboxed, executed = WORKFLOW_MODULE._sandbox_command(
            "bwrap", [str(launcher), "-V"], self.project, self.project,
            self.root / "tmp", "offline",
        )

        self.assertEqual(executed[0], str(launcher.resolve()))
        bind_sources = [
            sandboxed[index + 1] for index, value in enumerate(sandboxed[:-2])
            if value == "--ro-bind"
        ]
        self.assertIn(str(prefix.resolve()), bind_sources)
        self.assertNotIn("/opt", bind_sources)

    def test_the_second_verification_reuses_the_checkout_instead_of_failing(self) -> None:
        """`git worktree add` refuses an existing path, so reuse has to be a decision, not luck."""
        target = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.first)
        WORKFLOW_MODULE._ensure_verification_worktree(self.project, target, self.first)
        self.assertTrue((target / "a.txt").is_file())
        created = (target / "a.txt").stat().st_mtime_ns

        WORKFLOW_MODULE._ensure_verification_worktree(self.project, target, self.first)
        self.assertEqual(
            (target / "a.txt").stat().st_mtime_ns, created,
            "the checkout was rebuilt when it was already the right commit")
        self.assertEqual(
            subprocess.run(("git", "-C", str(target), "rev-parse", "HEAD"),
                           check=True, capture_output=True, text=True).stdout.strip(),
            self.first)

    def test_a_tree_left_at_the_wrong_commit_is_rebuilt_not_trusted(self) -> None:
        """The path is keyed by SHA, so a mismatch means something outside this code moved it."""
        target = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.first)
        WORKFLOW_MODULE._ensure_verification_worktree(self.project, target, self.first)
        subprocess.run(("git", "-C", str(target), "checkout", "--detach", self.second),
                       check=True, capture_output=True, text=True)
        self.assertEqual((target / "a.txt").read_text(encoding="utf-8"), "two\n")

        WORKFLOW_MODULE._ensure_verification_worktree(self.project, target, self.first)
        self.assertEqual(
            (target / "a.txt").read_text(encoding="utf-8"), "one\n",
            "the tree at the SHA-keyed path was trusted by its name instead of checked")

    def test_cleanup_finds_the_shared_checkouts_through_git(self) -> None:
        """cleanup asks git, so a shared tree is removable whether or not state recorded it."""
        first_tree = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.first)
        second_tree = WORKFLOW_MODULE._shared_verification_worktree(self.run_root, self.second)
        WORKFLOW_MODULE._ensure_verification_worktree(self.project, first_tree, self.first)
        WORKFLOW_MODULE._ensure_verification_worktree(self.project, second_tree, self.second)

        discovered = WORKFLOW_MODULE._registered_worktrees_under(self.project, self.run_root)
        self.assertEqual(
            {path.resolve() for path in discovered},
            {first_tree.resolve(), second_tree.resolve()})

    def test_scratch_directories_are_discovered_for_removal(self) -> None:
        """Nothing ever removed these; one run left 230 of them holding 7.7 GB."""
        runs = self.run_root / "verification_runs"
        for request in ("vr-0001", "vr-0002"):
            for attempt in ("attempt_001", "attempt_002"):
                scratch = runs / request / attempt / "tmp"
                scratch.mkdir(parents=True)
                (scratch / "big").write_bytes(b"x" * 2048)
                logs = runs / request / attempt / "logs"
                logs.mkdir(parents=True)
                (logs / "stdout.log").write_text("kept\n", encoding="utf-8")

        found = WORKFLOW_MODULE._disposable_temp_dirs(self.run_root)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(path.name == "tmp" for path in found))
        self.assertGreaterEqual(
            sum(WORKFLOW_MODULE._directory_bytes(path) for path in found), 4 * 2048)
        self.assertTrue(
            all((path.parent / "logs" / "stdout.log").is_file() for path in found),
            "the evidence beside the scratch must not be in the removal set")


class ReviewerRuntimeTest(unittest.TestCase):
    def test_reported_pass_without_effective_pass_gets_no_post_pass_exemption(self) -> None:
        head = "b" * 40
        self.assertFalse(WORKFLOW_MODULE.qualifies_for_post_pass_review(
            {"reported_verdict": "PASS", "verdict": "FAIL", "reviewed_sha": "a" * 40},
            head, False,
        ))
        self.assertTrue(WORKFLOW_MODULE.qualifies_for_post_pass_review(
            {"reported_verdict": "PASS", "verdict": "PASS", "reviewed_sha": "a" * 40},
            head, False,
        ))

    def test_rewritten_review_history_returns_to_full_transport(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            project = Path(scratch)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            tracked = project / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", tracked.name], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()
            tracked.write_text("old review head\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "old"], cwd=project, check=True)
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()
            subprocess.run(["git", "reset", "--hard", base], cwd=project, check=True)
            tracked.write_text("replacement head\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "replacement"], cwd=project, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()

            mode, diff_base, reason = WORKFLOW_MODULE.select_review_transport(
                project, base, head, [{"reviewed_sha": old_head}], False
            )

            self.assertEqual(mode, "FULL")
            self.assertEqual(diff_base, base)
            self.assertEqual(reason, "PRIOR_REVIEWED_SHA_NOT_ANCESTOR")

    def test_same_sha_and_cumulative_reviews_retain_full_transport(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            project = Path(scratch)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            tracked = project / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", tracked.name], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()
            tracked.write_text("reviewed\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "reviewed"], cwd=project, check=True)
            reviewed = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()
            prior = [{"reviewed_sha": reviewed}]

            self.assertEqual(
                WORKFLOW_MODULE.select_review_transport(project, base, reviewed, prior, False),
                ("FULL", base, "SAME_SHA_AFTER_USER_DECISION"),
            )
            tracked.write_text("later\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "later"], cwd=project, check=True)
            later = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=project, text=True
            ).strip()
            self.assertEqual(
                WORKFLOW_MODULE.select_review_transport(project, base, later, prior, True),
                ("FULL", base, "CUMULATIVE_FINAL_REVIEW"),
            )

    def test_large_review_diff_falls_back_to_bounded_changed_path_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            project = Path(scratch) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            large = project / "large.bin"
            large.write_bytes(os.urandom(WORKFLOW_MODULE.MAX_REVIEW_DIFF_BYTES + 1024))
            subprocess.run(["git", "add", large.name], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
            large.write_bytes(os.urandom(WORKFLOW_MODULE.MAX_REVIEW_DIFF_BYTES + 1024))
            subprocess.run(["git", "add", large.name], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "head"], cwd=project, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
            destination = Path(scratch) / "changes.patch"

            WORKFLOW_MODULE.write_bounded_review_diff(project, base, head, destination)

            captured = destination.read_text(encoding="utf-8")
            self.assertIn("REVIEW PATCH OMITTED", captured)
            self.assertIn("large.bin", captured)
            self.assertLess(destination.stat().st_size, WORKFLOW_MODULE.MAX_REVIEW_DIFF_BYTES)

    def test_dsh_read_boundary_denies_credentials_and_all_file_mutation(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            (allowed / "ok.txt").write_text("ok\n", encoding="utf-8")
            (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
            plugin = SKILL_ROOT / "scripts" / "dsh_read_boundary.mjs"
            program = textwrap.dedent(
                f"""\
                import {{ apply }} from {json.dumps(plugin.as_uri())};
                let guard;
                const ctx = {{ on: (_name, callback) => {{ guard = callback; }} }};
                apply(ctx, {{workspaceRoot: {json.dumps(str(allowed))}, allowWeb: false,
                            allowedRoots: [{json.dumps(str(allowed))}]}});
                const next = async () => ({{kind: "allow"}});
                const results = [];
                results.push(await guard({{name: "read", arguments: {{file_path: "ok.txt"}}}}, next));
                results.push(await guard({{name: "read", arguments: {{file_path: {json.dumps(str(outside / 'secret.txt'))}}}}}, next));
                results.push(await guard({{name: "write", arguments: {{file_path: "new.txt"}}}}, next));
                results.push(await guard({{name: "edit", arguments: {{file_path: "ok.txt"}}}}, next));
                results.push(await guard({{name: "future_unknown_tool", arguments: {{}}}}, next));
                results.push(await guard({{name: "glob", arguments: {{path: ".", pattern: "../**/*"}}}}, next));
                results.push(await guard({{name: "web_search", arguments: {{query: "official docs"}}}}, next));
                let webGuard;
                apply({{on: (_name, callback) => {{ webGuard = callback; }}}},
                      {{workspaceRoot: {json.dumps(str(allowed))}, allowWeb: true,
                       allowedRoots: [{json.dumps(str(allowed))}]}});
                results.push(await webGuard(
                    {{name: "web_search", arguments: {{query: "official docs"}}}}, next));
                console.log(JSON.stringify(results));
                """
            )
            result = subprocess.run(
                [node, "--input-type=module", "-e", program], text=True,
                capture_output=True, check=True,
            )
        decisions = json.loads(result.stdout)
        self.assertEqual(
            [item["kind"] for item in decisions],
            ["allow", "deny", "deny", "deny", "deny", "deny", "deny", "allow"],
        )

    def test_advanced_reviewer_models_are_defaults_and_user_can_defer(self) -> None:
        default_args = argparse.Namespace(
            claude_model=None, codex_model=None, codex_model_provider=None, codex_profile=None)
        self.assertEqual(
            WORKFLOW_MODULE.reviewer_runtime(default_args, "claude")["model"], "opus")
        self.assertEqual(
            WORKFLOW_MODULE.reviewer_runtime(default_args, "codex")["model"], "gpt-5.6-sol")
        deferred = argparse.Namespace(
            claude_model="cli-default", codex_model="cli-default",
            codex_model_provider=None, codex_profile=None)
        self.assertIsNone(WORKFLOW_MODULE.reviewer_runtime(deferred, "claude")["model"])

    def test_claude_reviewer_model_reaches_argv(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            command = WORKFLOW_MODULE.reviewer_command(
                "claude", root, root, root / "schema.json", root / "raw.json", "prompt",
                runtime={"model": "opus"})
        self.assertEqual(command[command.index("--model") + 1], "opus")

    def test_deepseek_codex_selection_is_passed_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            command = WORKFLOW_MODULE.reviewer_command(
                "codex", root, root, root / "schema.json", root / "raw.json", "prompt",
                runtime={"model": "deepseek-reasoner", "model_provider": "deepseek-gateway",
                         "profile": "deepseek"})
        self.assertEqual(command[command.index("-m") + 1], "deepseek-reasoner")
        self.assertEqual(command[command.index("-p") + 1], "deepseek")
        self.assertIn('model_provider="deepseek-gateway"', command)
        line = " ".join(command).lower()
        self.assertNotIn("api_key", line)
        self.assertNotIn("bearer_token", line)
        self.assertNotIn("auth.json", line)

    def test_codex_implementation_reviewer_denies_model_access_to_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            command = WORKFLOW_MODULE.reviewer_command(
                "codex", root, root, root / "schema.json", root / "raw.json", "prompt",
                runtime={})
        line = " ".join(command)
        self.assertIn('default_permissions="grounded_build"', line)
        self.assertIn('"~/.codex"="deny"', line)
        self.assertIn("tools.web_search=false", line)

    def test_review_diff_disables_repository_textconv(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            project = root / "project"
            project.mkdir()
            marker = root / "textconv-executed"
            converter = root / "converter"
            converter.write_text(
                "#!/bin/sh\nprintf executed > \"$MARKER\"\ncat \"$1\"\n", encoding="utf-8")
            converter.chmod(0o755)
            environment = {**os.environ, "MARKER": str(marker)}
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
            subprocess.run(["git", "config", "diff.hostexec.textconv", str(converter)], cwd=project, check=True)
            (project / ".gitattributes").write_text("*.payload diff=hostexec\n", encoding="utf-8")
            payload = project / "sample.payload"
            payload.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
            payload.write_text("head\n", encoding="utf-8")
            subprocess.run(["git", "add", payload.name], cwd=project, check=True)
            subprocess.run(["git", "commit", "-qm", "head"], cwd=project, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

            with mock.patch.dict(os.environ, environment, clear=True):
                subprocess.run(
                    ["git", "diff", "--textconv", base, head, "--"], cwd=project,
                    stdout=subprocess.DEVNULL, check=True,
                )
                self.assertTrue(marker.exists(), "fixture must prove the configured textconv executes")
                marker.unlink()
                WORKFLOW_MODULE.write_bounded_review_diff(project, base, head, root / "changes.patch")

            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
