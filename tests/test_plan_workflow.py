from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_workflow.py"


FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
from pathlib import Path
prompt = sys.argv[-1]
provider = re.search(r"provider=(claude|codex)", prompt).group(1)
sha = re.search(r"[0-9a-f]{40}", prompt).group(0)
context_match = re.search(r"Read (/.+?)/request\.md", prompt)
request_text = (Path(context_match.group(1)) / "request.md").read_text() if context_match else ""
canary_match = re.search(r"FORBIDDEN_CANARY=(.+)", request_text)
canary_visible = bool(canary_match and Path(canary_match.group(1).strip()).exists())
git_ok = subprocess.run(["git", "status", "--porcelain"], capture_output=True).returncode == 0
if "independent planning instance" in prompt:
    slot = re.search(r"instance ([AB])", prompt).group(1)
    payload = {
        "provider": provider, "slot": slot, "baseline_sha": sha,
        "summary": f"independent {slot}; canary_visible={canary_visible}; git_ok={git_ok}",
        "repository_facts": [{"id": f"{slot}-F1", "claim": "README exists", "evidence": "README.md:1", "confidence": "VERIFIED"}],
        "plan_markdown": f"# Plan {slot}\n\n## Scope\nRepository-grounded proposal from {slot}.\n\n## Batches\n- B01: verify with a named command.\n",
        "unresolved_questions": [],
    }
elif "independent reviewer slot" in prompt:
    slot = re.search(r"reviewer slot ([AB])", prompt).group(1)
    target = re.search(r"target=(draft-[AB])", prompt).group(1)
    if f"FAKE_CROSS_DECISION={slot}" in request_text:
        payload = {"provider": provider, "reviewer_slot": slot, "target": target, "baseline_sha": sha, "verdict": "NEEDS_USER_DECISION", "summary": "scope decision", "findings": [{"id": "P1-scope", "severity": "P1", "claim": "scope unclear", "evidence": "request", "required_change": "choose scope"}]}
    else:
        payload = {"provider": provider, "reviewer_slot": slot, "target": target, "baseline_sha": sha, "verdict": "PASS", "summary": "checked", "findings": []}
else:
    slot = re.search(r"reviewer \(([ABF])\)", prompt).group(1)
    target = re.search(r"target=(candidate-round-\d+)", prompt).group(1)
    verdict = "FAIL" if f"FAKE_FINAL_FAIL={slot}" in request_text else "PASS"
    findings = [] if verdict == "PASS" else [{"id": "P1-final", "severity": "P1", "claim": "missing boundary", "evidence": "plan", "required_change": "add boundary"}]
    if f"FAKE_PASS_BLOCKING={slot}" in request_text:
        verdict = "PASS"
        findings = [{"id": "P0-pass", "severity": "P0", "claim": "unsafe", "evidence": "plan", "required_change": "fix"}]
    payload = {"provider": provider, "reviewer_slot": slot, "target": target, "baseline_sha": sha, "verdict": verdict, "summary": "final checked", "findings": findings}
if "-o" in sys.argv:
    output = sys.argv[sys.argv.index("-o") + 1]
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
else:
    print(json.dumps({"structured_output": payload}))
'''


class PlanWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="grounded-build-plan-test-"))
        self.project = self.temp / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.project, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.project, check=True)
        (self.project / "README.md").write_text("# Fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.project, check=True)
        self.baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project, text=True).strip()
        self.request = self.temp / "request.md"
        self.request.write_text("# Objective\nCreate a verified plan without editing the project.\n", encoding="utf-8")
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex"):
            path = self.bin / name
            path.write_text(FAKE_AGENT, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["GROUNDED_BUILD_PLAN_HOME"] = str(self.temp / "state")

    def tearDown(self) -> None:
        subprocess.run(["git", "worktree", "prune"], cwd=self.project, check=False)
        shutil.rmtree(self.temp)

    def call(self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None) -> dict:
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, expect, msg=result.stderr + result.stdout)
        source = result.stdout if result.stdout.strip() else result.stderr
        return json.loads(source)

    def initialize(self, backend: str = "claude", final: str = "both") -> dict:
        return self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", backend, "--final-reviewer", final,
        )

    def get_state(self, initialized: dict) -> dict:
        return json.loads((Path(initialized["run_directory"]) / "workflow.json").read_text(encoding="utf-8"))

    def run_through_cross_review(self, initialized: dict) -> None:
        run_id = initialized["run_id"]
        for slot in ("A", "B"):
            self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        for slot in ("A", "B"):
            self.call("cross-review", "--project", str(self.project), "--run-id", run_id, "--slot", slot)

    def submit_candidate(self, initialized: dict) -> None:
        output = self.call("synthesis-context", "--project", str(self.project), "--run-id", initialized["run_id"])
        directory = Path(output["output_directory"])
        plan = directory / "host_plan.md"
        batches = directory / "host_batches.md"
        plan.write_text("# Implementation plan\n\n## Scope\nImplement the request.\n\n## Verification\nRun a named test.\n", encoding="utf-8")
        batches.write_text("# Batches\n\n- B01: request scope; exit when the named test returns zero.\n", encoding="utf-8")
        self.call(
            "submit-synthesis", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--plan", str(plan), "--batch-manifest", str(batches),
        )

    def test_single_provider_uses_two_isolated_instances_and_reaches_ready(self) -> None:
        initialized = self.initialize("claude", "both")
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "claude"})
        self.assertFalse(initialized["provider_diversity"])
        self.run_through_cross_review(initialized)
        state = self.get_state(initialized)
        self.assertNotEqual(state["worktrees"]["A"], state["worktrees"]["B"])
        draft_a_context = next((Path(initialized["run_directory"]) / "invocations" / "draft-A").glob("attempt_1_*/context"))
        self.assertEqual([item.name for item in draft_a_context.iterdir() if item.name != "schema.json"], ["request.md"])
        self.submit_candidate(initialized)
        for reviewer in ("A", "B"):
            self.call("final-review", "--project", str(self.project), "--run-id", initialized["run_id"], "--reviewer", reviewer)
        exported = self.call("export", "--project", str(self.project), "--run-id", initialized["run_id"])
        self.assertEqual(exported["status"], "READY")
        self.assertTrue(Path(exported["plan"]).is_file())
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project, text=True).strip(), self.baseline)
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.project, text=True), "")

    def test_mixed_topology_records_provider_diversity(self) -> None:
        initialized = self.initialize("mixed", "codex")
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "codex"})
        self.assertTrue(initialized["provider_diversity"])
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        result = self.call("final-review", "--project", str(self.project), "--run-id", initialized["run_id"], "--reviewer", "F")
        self.assertEqual(result["status"], "READY")

    def test_frozen_request_tampering_is_rejected(self) -> None:
        initialized = self.initialize()
        state = self.get_state(initialized)
        Path(state["request_snapshot"]).write_text("changed\n", encoding="utf-8")
        result = self.call("status", "--project", str(self.project), "--run-id", initialized["run_id"], expect=2)
        self.assertEqual(result["status"], "WORKFLOW_ERROR")
        self.assertIn("authoritative planning artifact changed", result["error"])

    def test_workflow_state_cannot_be_rewritten_to_forge_ready(self) -> None:
        initialized = self.initialize()
        state_path = Path(initialized["run_directory"]) / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "READY"
        state["final"] = {"plan": state["request_snapshot"], "batch_manifest": state["request_snapshot"]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.call("export", "--project", str(self.project), "--run-id", initialized["run_id"], expect=2)
        self.assertIn("integrity verification", result["error"])

    def test_old_signed_state_cannot_be_replayed(self) -> None:
        initialized = self.initialize()
        state_path = Path(initialized["run_directory"]) / "workflow.json"
        old_state = state_path.read_bytes()
        self.call("draft", "--project", str(self.project), "--run-id", initialized["run_id"], "--slot", "A")
        state_path.write_bytes(old_state)
        result = self.call("status", "--project", str(self.project), "--run-id", initialized["run_id"], expect=2)
        self.assertIn("rollback/replay", result["error"])

    def test_provider_credentials_are_denied_to_model_tools(self) -> None:
        claude_run = self.initialize("claude", "claude")
        preview = self.call(
            "draft", "--project", str(self.project), "--run-id", claude_run["run_id"],
            "--slot", "A", "--dry-run",
        )
        command = preview["command"]
        denied = command[command.index("--disallowedTools") + 1]
        for tool in ("Read", "Grep", "Glob"):
            self.assertIn(f"{tool}(//", denied)
        self.assertIn("/home/**)", denied)

        # A fresh fixture is unnecessary: abandoning is not required to initialize another run.
        codex_run = self.initialize("codex", "codex")
        self.call(
            "draft", "--project", str(self.project), "--run-id", codex_run["run_id"],
            "--slot", "A", "--dry-run",
        )
        configs = list(Path(codex_run["run_directory"]).glob("invocations/draft-A/attempt_1_*/home/.codex/config.toml"))
        self.assertEqual(len(configs), 1)
        config = configs[0].read_text(encoding="utf-8")
        self.assertIn('"~/.codex" = "deny"', config)
        self.assertIn("web_search = false", config)

    def test_pass_with_blocking_finding_is_rejected(self) -> None:
        self.request.write_text(self.request.read_text() + "\nFAKE_PASS_BLOCKING=F\n")
        initialized = self.initialize("claude", "claude")
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        result = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F", expect=2,
        )
        self.assertIn("PASS review cannot contain P0/P1", result["error"])

    def test_user_decision_does_not_skip_second_cross_review(self) -> None:
        self.request.write_text(self.request.read_text() + "\nFAKE_CROSS_DECISION=A\n")
        initialized = self.initialize("claude", "claude")
        for slot in ("A", "B"):
            self.call("draft", "--project", str(self.project), "--run-id", initialized["run_id"], "--slot", slot)
        first = self.call(
            "cross-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--slot", "A",
        )
        self.assertEqual(first["status"], "NEEDS_USER_DECISION")
        self.call(
            "adjudicate", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "Use the bounded scope", "--actor", "user", "--apply",
        )
        second = self.call("cross-review", "--project", str(self.project), "--run-id", initialized["run_id"], "--slot", "B")
        self.assertEqual(second["status"], "SYNTHESIS_REQUIRED")

    def test_worktree_drift_is_rejected_and_sibling_canary_is_hidden(self) -> None:
        canary = self.temp / "state" / "hidden-canary.txt"
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text("must not be visible", encoding="utf-8")
        self.request.write_text(self.request.read_text() + f"\nFORBIDDEN_CANARY={canary}\n")
        initialized = self.initialize("claude", "claude")
        state = self.get_state(initialized)
        result = self.call(
            "draft", "--project", str(self.project), "--run-id", initialized["run_id"], "--slot", "A",
        )
        draft = json.loads(Path(self.get_state(initialized)["drafts"]["A"]["path"]).read_text(encoding="utf-8"))
        self.assertIn("canary_visible=False", draft["summary"])
        self.assertIn("git_ok=True", draft["summary"])
        (Path(state["worktrees"]["B"]) / "drift.txt").write_text("drift", encoding="utf-8")
        result = self.call("draft", "--project", str(self.project), "--run-id", initialized["run_id"], "--slot", "B", expect=2)
        self.assertIn("planning worktree is dirty", result["error"])

    def test_final_failure_allows_one_correction_then_requires_decision(self) -> None:
        self.request.write_text(self.request.read_text() + "\nFAKE_FINAL_FAIL=F\n")
        initialized = self.initialize("claude", "claude")
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        result = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F",
        )
        self.assertEqual(result["status"], "SYNTHESIS_REQUIRED")
        self.submit_candidate(initialized)
        result = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F",
        )
        self.assertEqual(result["status"], "NEEDS_USER_DECISION")


if __name__ == "__main__":
    unittest.main()
