from __future__ import annotations

import argparse
import json
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


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
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.bin_dir}{os.pathsep}{self.environment['PATH']}"
        self.environment["GROUNDED_BUILD_IMPLEMENT_HOME"] = str(self.state_home)

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
                #!{sys.executable}
                import json
                import os
                import re
                import sys

                prompt = sys.argv[-1]
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
                if "{name}" == "codex":
                    output = sys.argv[sys.argv.index("-o") + 1]
                    with open(output, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
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

    def test_finalize_recovers_after_merge_before_state_commit(self) -> None:
        initialized = self.initialize("codex")
        final_head, _ = self.review_accept(initialized)
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

    def test_finalize_updates_target_ref_without_switching_original_checkout(self) -> None:
        initialized = self.initialize("codex")
        final_sha, _ = self.review_accept(initialized)
        self.run_command("git", "-C", str(self.project), "switch", "-qc", "ongoing-work")
        original_head = self.run_command(
            "git", "-C", str(self.project), "rev-parse", "HEAD"
        ).stdout.strip()
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
        drift = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1", expected=2,
        )
        self.assertEqual(drift["status"], "ENVIRONMENT_DRIFT")

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

    def test_snapshot_is_authority_and_dirty_original_is_infrastructure_error(self) -> None:
        (self.project / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        blocked = self.workflow(
            "init", "--project", str(self.project), "--plan", str(self.plan),
            "--batch-manifest", str(self.batch_manifest),
            "--reviewer", "codex", "--batches", "1", expected=1,
        )
        self.assertEqual(blocked["status"], "WORKFLOW_ERROR")
        self.assertIn("not clean", str(blocked["error"]))
        (self.project / "dirty.txt").unlink()
        initialized = self.initialize("codex")
        implementation = Path(str(initialized["implementation_worktree"]))
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
        run_dir = Path(str(initialized["run_directory"]))
        context = run_dir / "review_context" / "batch_1" / "round_01" / "review-1-001"
        self.assertEqual(
            {path.name for path in context.iterdir()},
            {
                "acceptance_contract.json", "assignment.json", "batch_manifest.md",
                "finding_ledger.json", "plan.md",
            },
        )
        prompt = (
            run_dir / "reviews" / "batch_1" / "round_01" / "invocation_001" / "prompt.md"
        ).read_text()
        self.assertIn(str(context / "plan.md"), prompt)
        self.assertIn(str(context / "batch_manifest.md"), prompt)
        self.assertIn(str(context / "assignment.json"), prompt)
        self.assertIn(str(context / "finding_ledger.json"), prompt)
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

    def test_legacy_run_requires_explicit_migration(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state["schema_version"] = 4
        state_path.write_text(json.dumps(state), encoding="utf-8")
        status = self.workflow(
            "status", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(status["status"], "MIGRATION_REQUIRED")
        preview = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"])
        )
        self.assertEqual(preview["status"], "MIGRATION_PREVIEW")
        migrated = self.workflow(
            "migrate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
            "--apply",
        )
        self.assertEqual(migrated["status"], "MIGRATED")
        self.assertTrue(Path(str(migrated["backup_path"])).is_file())

    def test_schema5_target_advance_parking_migrates_to_integration_drift(self) -> None:
        initialized = self.initialize("codex")
        state_path = Path(str(initialized["run_directory"])) / "workflow.json"
        state = json.loads(state_path.read_text())
        state["schema_version"] = 5
        state["status"] = "NEEDS_USER_DECISION"
        state["pending_decision"] = {
            "decision_id": "legacy-target", "type": "TARGET_ADVANCED",
            "previous_status": "IMPLEMENTING", "allowed_choices": ["ABORT_RUN"],
        }
        state.pop("integration", None)
        state.pop("environment_contract", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
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

    def test_a_commit_after_pass_is_reviewable_instead_of_wedging_the_run(self) -> None:
        initialized, _, later_head = self._pass_then_commit()
        second = self.workflow(
            "review", "--project", str(self.project),
            "--run-id", str(initialized["run_id"]), "--batch", "1",
        )
        self.assertEqual(second["status"], "REVIEW_PASS")
        self.assertEqual(second["round"], 2)
        self.assertEqual(second["reviewed_sha"], later_head)

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
        self.assertIn("GRANT_ONE_REVIEW_INVOCATION", decision["allowed_choices"])
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
        for expected_choice_available in (True, False):
            parked = self.workflow(
                "review", "--project", str(self.project),
                "--run-id", str(initialized["run_id"]), "--batch", "1", expected=3,
            )
            decision_id = parked["pending_decision"]["decision_id"]
            args = [
                "adjudicate", "--project", str(self.project), "--run-id", str(initialized["run_id"]),
                "--decision-id", decision_id, "--choice", "GRANT_ONE_REVIEW_INVOCATION",
                "--reason", "another call", "--actor", "test-user", "--apply",
            ]
            if expected_choice_available:
                self.workflow(*args)
                self.environment["FAKE_CODEX_EXIT"] = "1"
                self.workflow(
                    "review", "--project", str(self.project),
                    "--run-id", str(initialized["run_id"]), "--batch", "1", expected=4,
                )
                self.environment.pop("FAKE_CODEX_EXIT")
            else:
                refused = self.workflow(*args, expected=1)
                self.assertIn("already used for this batch", refused["error"])

    def test_obligation_drift_reaches_a_typed_decision_with_both_exits(self) -> None:
        """A finding restated at a third location stops the batch and offers a real choice."""
        initialized = self.initialize("codex")
        run_id = str(initialized["run_id"])
        implementation = Path(str(initialized["implementation_worktree"]))

        def drifting(location: str, outcome: str) -> str:
            return json.dumps([{
                "id": "F-1-001", "fingerprint": "drifting-obligation", "severity": "P1",
                "novelty": "INITIAL_REVIEW", "why_not_detectable_earlier": "",
                "introduced_by_sha": "", "severity_change_justification": "",
                "location": location, "trigger": "the declared input",
                "consequence": "the declared failure", "required_outcome": outcome,
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
        self.assertNotIn("OBLIGATION_DRIFT:F-1-001", second["decision_reasons"])

        self.commit_batch_change(implementation, "implemented round 3\n")
        self.environment["FAKE_FINDINGS"] = drifting(
            "three.py:30", "reject every produced path or directory component"
        )
        third = self.workflow(
            "review", "--project", str(self.project), "--run-id", run_id, "--batch", "1", expected=3,
        )
        self.assertEqual(third["status"], "NEEDS_USER_DECISION")
        self.assertIn("OBLIGATION_DRIFT:F-1-001", third["decision_reasons"])
        status = self.workflow("status", "--project", str(self.project), "--run-id", run_id)
        allowed = status["pending_decision"]["allowed_choices"]
        self.assertIn("RETURN_TO_FIX", allowed)
        self.assertIn("DEFER_ELIGIBLE_P1", allowed)
        entry = json.loads(
            (Path(str(initialized["run_directory"])) / "workflow.json").read_text(encoding="utf-8")
        )["finding_ledger"]["drifting-obligation"]
        self.assertEqual(entry["original_required_outcome"], "enforce it at the write boundary")
        self.assertEqual(len(entry["obligation_revisions"]), 2)
        self.environment.pop("FAKE_FINDINGS")


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
            "why_not_detectable_earlier": "hidden by the prior control flow"
            if novelty == "PREVIOUSLY_MASKED"
            else "",
            "introduced_by_sha": "a" * 40 if novelty == "INTRODUCED_BY_FIX" else "",
            "severity_change_justification": "",
            "location": "module.py:10",
            "trigger": "specific input",
            "consequence": "observable failure",
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
            "why_not_detectable_earlier": "",
            "introduced_by_sha": "a" * 40 if novelty == "INTRODUCED_BY_FIX" else "",
            "severity_change_justification": "",
            "location": location,
            "trigger": "specific input",
            "consequence": "observable failure",
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
    """One finding ID may not absorb an expanding class of defects without a bound."""

    def test_unchanged_obligation_records_no_revision(self) -> None:
        state = self.state()
        finding = self.finding("F-1", "stable")
        self.apply(state, self.payload("FAIL", [finding]), 1)
        result = self.apply(state, self.payload("FAIL", [finding]), 2)
        entry = state["finding_ledger"]["stable"]
        self.assertEqual(entry.get("obligation_revisions"), [])
        self.assertEqual(entry["occurrences"], 2)
        self.assertNotIn("OBLIGATION_DRIFT:F-1", result["decision_reasons"])

    def test_line_number_movement_is_not_obligation_drift(self) -> None:
        state = self.state()
        self.apply(
            state,
            self.payload("FAIL", [self.finding("F-1", "stable", location="pkg/module.py:10")]),
            1,
        )
        self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "stable", location="pkg/module.py:2200-2260")],
            ),
            2,
        )
        self.assertEqual(state["finding_ledger"]["stable"]["obligation_revisions"], [])

    def test_changed_required_outcome_records_one_revision_without_a_decision(self) -> None:
        state = self.state()
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "drifting")]), 1)
        result = self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "drifting", required_outcome="reject every produced path")],
            ),
            2,
        )
        entry = state["finding_ledger"]["drifting"]
        self.assertEqual(len(entry["obligation_revisions"]), 1)
        self.assertEqual(entry["obligation_revisions"][0]["round"], 2)
        self.assertNotIn("OBLIGATION_DRIFT:F-1", result["decision_reasons"])
        self.assertEqual(result["effective_verdict"], "FAIL")

    def test_relocation_to_another_file_counts_as_a_revision(self) -> None:
        state = self.state()
        self.apply(
            state,
            self.payload("FAIL", [self.finding("F-1", "drifting", location="a/one.py:338")]),
            1,
        )
        self.apply(
            state,
            self.payload("FAIL", [self.finding("F-1", "drifting", location="b/two.py:97")]),
            2,
        )
        revisions = state["finding_ledger"]["drifting"]["obligation_revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["from_paths"], ["a/one.py"])
        self.assertEqual(revisions[0]["to_paths"], ["b/two.py"])

    def test_second_revision_forces_a_typed_user_decision(self) -> None:
        state = self.state()
        for round_number, location in ((1, "a/one.py:1"), (2, "b/two.py:2"), (3, "c/three.py:3")):
            result = self.apply(
                state,
                self.payload("FAIL", [self.finding("F-1", "drifting", location=location)]),
                round_number,
            )
        self.assertEqual(result["effective_verdict"], "NEEDS_USER_DECISION")
        self.assertIn("OBLIGATION_DRIFT:F-1", result["decision_reasons"])

    def test_original_obligation_is_preserved_for_audit(self) -> None:
        state = self.state()
        self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "drifting", required_outcome="the first ask")],
            ),
            1,
        )
        self.apply(
            state,
            self.payload(
                "FAIL",
                [self.finding("F-1", "drifting", required_outcome="a much wider ask")],
            ),
            2,
        )
        entry = state["finding_ledger"]["drifting"]
        self.assertEqual(entry["original_required_outcome"], "the first ask")
        self.assertEqual(entry["required_outcome"], "a much wider ask")
        self.assertEqual(entry["original_location"], "pkg/module.py:10")

    def test_a_stabilised_obligation_stops_forcing_decisions(self) -> None:
        state = self.state()
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "drifting", location="a/one.py:1")]), 1)
        self.apply(state, self.payload("FAIL", [self.finding("F-1", "drifting", location="b/two.py:2")]), 2)
        third = self.apply(
            state, self.payload("FAIL", [self.finding("F-1", "drifting", location="c/three.py:3")]), 3
        )
        self.assertIn("OBLIGATION_DRIFT:F-1", third["decision_reasons"])
        fourth = self.apply(
            state, self.payload("FAIL", [self.finding("F-1", "drifting", location="c/three.py:9")]), 4
        )
        self.assertNotIn("OBLIGATION_DRIFT:F-1", fourth["decision_reasons"])

    def test_recorded_five_round_drift_is_caught_two_rounds_earlier(self) -> None:
        """Replays F-B05-002, which was restated at five locations over five rounds."""
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
            if fired_at is None and "OBLIGATION_DRIFT:F-B05-002" in result["decision_reasons"]:
                fired_at = index
        self.assertEqual(fired_at, 3)
        self.assertEqual(len(state["finding_ledger"]["provider-park"]["obligation_revisions"]), 4)

    def test_ledger_entry_written_before_this_change_still_converges(self) -> None:
        """A run created without the drift fields must review and converge unchanged."""
        legacy = {
            "id": "F-1", "fingerprint": "pre-existing-entry", "batch": self.BATCH,
            "severity": "P1", "novelty": "INITIAL_REVIEW", "status": "OPEN", "blocking": True,
            "deferred_reason": None, "location": "pkg/module.py:10",
            "required_outcome": "preserve the required behavior",
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
        self.assertEqual(entry["obligation_revisions"], [])
        self.assertEqual(entry["original_required_outcome"], "preserve the required behavior")
        self.assertEqual(result["effective_verdict"], "FAIL")
        resolved = self.apply(state, self.payload("PASS", [], ["F-1"]), 3)
        self.assertEqual(resolved["effective_verdict"], "PASS")

    def test_cross_batch_ownership_is_decided_before_obligation_drift(self) -> None:
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
            "introduced_by_sha",
            "severity_change_justification",
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
        self.assertNotIn("token", " ".join(command).lower())


if __name__ == "__main__":
    unittest.main()
