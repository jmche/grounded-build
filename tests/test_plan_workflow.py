from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_workflow.py"


FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("fake-agent 1.0")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "capabilities":
    print(json.dumps({"protocol": "grounded-build-other-v1", "read_only": True,
                      "structured_output": True, "fresh_process": True}))
    raise SystemExit(0)
if "--help" in sys.argv:
    print("--output-schema --output-last-message --ephemeral --sandbox --config "
          "--profile headless --patch --dump-config --json-schema --output-format --permission-mode "
          "--no-session-persistence")
    raise SystemExit(0)
bridge = len(sys.argv) > 1 and sys.argv[1] == "run"
prompt = (Path(sys.argv[sys.argv.index("--prompt") + 1]).read_text(encoding="utf-8")
          if bridge else sys.argv[-1])
provider = re.search(r"provider=(claude|codex|dsh|other)", prompt).group(1)
if "This is a capability check." in prompt:
    ready = not any(Path(name).exists() for name in ("dirty-tracked.txt", "dirty-staged.txt", "dirty-untracked.txt"))
    probe_path = Path(re.search(r"Read (/.+?/probe-input\.txt)\.", prompt).group(1))
    probe = {"provider": provider, "ready": ready,
             "observed": probe_path.read_text(encoding="utf-8").rstrip("\n")}
    if bridge:
        with open(sys.argv[sys.argv.index("--output") + 1], "w", encoding="utf-8") as handle:
            json.dump(probe, handle)
    elif "-o" in sys.argv:
        with open(sys.argv[sys.argv.index("-o") + 1], "w", encoding="utf-8") as handle:
            json.dump(probe, handle)
    elif "OUTPUT CONTRACT" in prompt:
        print(json.dumps(probe))
    else:
        print(json.dumps({"structured_output": probe}))
    raise SystemExit(0)
sha = re.search(r"[0-9a-f]{40}", prompt).group(0)
scope_match = re.search(r"scope_digest=([0-9a-f]{64})", prompt)
scope_digest = scope_match.group(1) if scope_match else ""
context_match = re.search(r"Read (/.+?)/request\.md", prompt)
request_text = (Path(context_match.group(1)) / "request.md").read_text() if context_match else ""
canary_match = re.search(r"FORBIDDEN_CANARY=(.+)", request_text)
canary_visible = bool(canary_match and Path(canary_match.group(1).strip()).exists())
git_ok = subprocess.run(["git", "status", "--porcelain"], capture_output=True).returncode == 0
sleep_match = re.search(r"FAKE_SLEEP=(\d+(?:\.\d+)?)", request_text)
if sleep_match:
    time.sleep(float(sleep_match.group(1)))
if "independent evidence investigator" in prompt:
    slot = re.search(r"investigator ([AB])", prompt).group(1)
    payload = {
        "provider": provider, "slot": slot, "baseline_sha": sha, "scope_digest": scope_digest,
        "summary": f"investigated {slot}",
        "evidence": [{"id": f"{slot}-E1", "scope_id": "TARGET-001", "claim": "README exists",
                      "status": "VERIFIED", "source_type": "REPOSITORY", "locator": "README.md:1",
                      "retrieved_at": "2026-01-01T00:00:00Z", "version_or_commit": sha,
                      "content_sha256": "0" * 64}],
        "findings": ([{
            "id": f"{slot}-finding", "scope_id": "TARGET-001", "severity": "P1",
            "urgency": "U1", "lane": "MUST_RESOLVE", "evidence_status": "VERIFIED",
            "problem": f"missing guard {slot}", "evidence_ids": [f"{slot}-E1"],
            "root_cause_status": "PROVEN", "causal_chain": [f"producer {slot} omitted guard"],
            "affected_surfaces": ["workflow"], "recommended_solution": "add the guard",
            "alternatives_and_tradeoffs": [], "verification": ["run regression"],
        }] if "FAKE_NONEMPTY_FINDINGS" in request_text else []), "unresolved_questions": [],
    }
elif "independent planning instance" in prompt:
    slot = re.search(r"instance ([AB])", prompt).group(1)
    payload = {
        "provider": provider, "slot": slot, "baseline_sha": sha, "scope_digest": scope_digest,
        "evidence_ids": [f"{slot}-E1"], "new_evidence": [],
        "summary": f"independent {slot}; canary_visible={canary_visible}; git_ok={git_ok}",
        "repository_facts": [{"id": f"{slot}-F1", "claim": "README exists", "evidence": "README.md:1", "confidence": "VERIFIED"}],
        "plan_markdown": f"# Plan {slot}\n\n## Scope\nRepository-grounded proposal from {slot}.\n\n## Batches\n- B01: verify with a named command.\n",
        "unresolved_questions": [],
    }
elif "independent reviewer and integrator slot" in prompt or "independent deep-planning slot" in prompt:
    if "integrator slot" in prompt:
        slot = re.search(r"integrator slot ([AB])", prompt).group(1)
        round_number = 2
        target = re.search(r"target=(draft-[AB])", prompt).group(1)
    else:
        slot = re.search(r"deep-planning slot ([AB])", prompt).group(1)
        round_number = 3
        target = "draft-02-both"
    if f"FAKE_CROSS_DECISION={slot}" in request_text:
        verdict = "NEEDS_USER_DECISION"
        findings = [{"id": "P1-scope", "severity": "P1", "claim": "scope unclear", "evidence": "request", "required_change": "choose scope"}]
    else:
        verdict = "PASS"
        findings = []
    payload = {"provider": provider, "slot": slot, "reviewer_slot": slot, "target": target,
               "round": round_number, "baseline_sha": sha, "scope_digest": scope_digest,
               "verdict": verdict, "summary": "checked", "findings": findings,
               "plan_markdown": f"# Integrated plan {round_number}{slot}\n\n## Scope\nTARGET-001\n",
               "accepted_finding_ids": [], "rejected_finding_ids": [], "new_findings": [],
               "finding_aliases": [],
               "unresolved_questions": []}
else:
    slot = re.search(r"reviewer \(([ABF])\)", prompt).group(1)
    target = re.search(r"target=(candidate-round-\d+)", prompt).group(1)
    verdict = "FAIL" if f"FAKE_FINAL_FAIL={slot}" in request_text else "PASS"
    findings = [] if verdict == "PASS" else [{"id": "P1-final", "severity": "P1", "claim": "missing boundary", "evidence": "plan", "required_change": "add boundary"}]
    if "convergence reviewer" in prompt and f"FAKE_CONVERGENCE_DECISION={slot}" in request_text:
        verdict = "NEEDS_USER_DECISION"
        findings = [{"id": "P1-convergence", "severity": "P1", "claim": "boundary unresolved", "evidence": "plan", "required_change": "choose boundary"}]
    if f"FAKE_PASS_BLOCKING={slot}" in request_text:
        verdict = "PASS"
        findings = [{"id": "P0-pass", "severity": "P0", "claim": "unsafe", "evidence": "plan", "required_change": "fix"}]
    if f"FAKE_NESTED_BAD_SEVERITY={slot}" in request_text:
        verdict = "PASS"
        findings = [{"id": "bad-severity", "severity": "P0 ", "claim": "unsafe", "evidence": "plan", "required_change": "fix"}]
    payload = {"provider": provider, "reviewer_slot": slot, "target": target, "baseline_sha": sha, "verdict": verdict, "summary": "final checked", "findings": findings}
def fake_assignment(prompt_text):
    """Derive the workflow's assignment name from the prompt, for empty-delivery markers."""
    if "independent evidence investigator" in prompt_text:
        return "investigate-" + re.search(r"investigator ([AB])", prompt_text).group(1)
    if "independent planning instance" in prompt_text:
        return "draft-" + re.search(r"instance ([AB])", prompt_text).group(1)
    if "independent reviewer and integrator slot" in prompt_text:
        return "cross-" + re.search(r"integrator slot ([AB])", prompt_text).group(1)
    if "independent deep-planning slot" in prompt_text:
        return "diverge-" + re.search(r"deep-planning slot ([AB])", prompt_text).group(1)
    if "fresh final planning reviewer" in prompt_text:
        round_no = re.search(r"target=candidate-round-(\d+)", prompt_text).group(1)
        return "final-" + round_no + "-" + re.search(r"reviewer \(([ABF])\)", prompt_text).group(1)
    return ""


router_error = re.search(r"FAKE_CODE_MODE_ROUTER_ERROR=(\S+)", request_text)
router_warning = re.search(r"FAKE_CODE_MODE_WARNING=(\S+)", request_text)
quota_error = re.search(r"FAKE_RATE_LIMIT=(\S+)", request_text)
if quota_error and provider == "codex" and quota_error.group(1) == fake_assignment(prompt):
    print("429 rate limit: five-hour token quota exhausted", file=sys.stderr)
    raise SystemExit(42)
if provider == "codex" and router_error and router_error.group(1) == fake_assignment(prompt):
    print("2026-09-09T00:00:00Z ERROR codex_core::tools::router: "
          "error=failed to spawn code-mode host /opt/codex-code-mode-host: "
          "No such file or directory (os error 2)", file=sys.stderr)
elif provider == "codex" and router_warning and router_warning.group(1) == fake_assignment(prompt):
    print("warning: Code Mode is unavailable because failed to spawn code-mode host "
          "/opt/codex-code-mode-host: host executable was not found.", file=sys.stderr)


if "FAKE_529" in request_text and "independent evidence investigator A" in prompt:
    print(json.dumps({"is_error": True, "api_error_status": 529,
                      "result": "upstream provider overloaded"}))
elif bridge or "-o" in sys.argv:
    output = sys.argv[sys.argv.index("--output" if bridge else "-o") + 1]
    # Deliver nothing on the first attempt when the request asks for it: the shape codex hit
    # three times running, where the turn ends with no final message at all.
    always = re.search(r"FAKE_EMPTY_ALWAYS=(\S+)", request_text)
    if (always and f"/{always.group(1)}/" in output) or ("FAKE_EMPTY_FIRST" in request_text and "/draft-" in output and "attempt_1_" in output):
        open(output, "w").close()
        sys.exit(0)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
elif "OUTPUT CONTRACT" in prompt:
    # dsh-headless prints the final message to stdout as bare text; deliver nothing when the
    # request asks this assignment to end without a final message.
    always = re.search(r"FAKE_EMPTY_ALWAYS=(\S+)", request_text)
    if always and always.group(1) == fake_assignment(prompt):
        sys.exit(0)
    print(json.dumps(payload))
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
        for name in ("claude", "codex", "dsh"):
            path = self.bin / name
            path.write_text(FAKE_AGENT, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        other = self.bin / "other-bridge"
        other.write_text(FAKE_AGENT, encoding="utf-8")
        other.chmod(other.stat().st_mode | stat.S_IXUSR)
        code_mode_host = self.bin / "codex-code-mode-host"
        code_mode_host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        code_mode_host.chmod(code_mode_host.stat().st_mode | stat.S_IXUSR)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["GROUNDED_BUILD_PLAN_HOME"] = str(self.temp / "state")
        self.env["GROUNDED_BUILD_OTHER_COMMAND"] = str(other)

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
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", backend, "--final-reviewer", final,
        )
        for slot in ("A", "B"):
            self.call("investigate", "--project", str(self.project),
                      "--run-id", initialized["run_id"], "--slot", slot)
        return initialized

    def get_state(self, initialized: dict) -> dict:
        return json.loads((Path(initialized["run_directory"]) / "workflow.json").read_text(encoding="utf-8"))

    def pending_decision_id(self, initialized: dict) -> str:
        state = self.get_state(initialized)
        self.assertEqual(len(state["pending_decisions"]), 1)
        return next(iter(state["pending_decisions"]))

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
        plan.write_text("# Implementation plan\n\n## Scope\nTARGET-001: Implement the request.\n\n## Verification\nRun a named test.\n", encoding="utf-8")
        batches.write_text("# Batches\n\n- B01: request scope; exit when the named test returns zero.\n", encoding="utf-8")
        self.call(
            "submit-synthesis", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--plan", str(plan), "--batch-manifest", str(batches),
        )

    def test_init_freezes_an_explicit_ref_without_requiring_a_clean_source_worktree(self) -> None:
        baseline = self.baseline
        subprocess.run(
            ["git", "switch", "-c", "planning-base"], cwd=self.project,
            check=True, capture_output=True, text=True,
        )
        (self.project / "dirty-untracked.txt").write_text("not part of the plan\n", encoding="utf-8")
        preflight = self.call(
            "preflight", "--project", str(self.project), "--base-ref", "planning-base",
            "--backend", "claude",
        )
        self.assertEqual(preflight["status"], "PREFLIGHT_OK")
        self.assertFalse(preflight["clean"])
        self.assertEqual(preflight["baseline_sha"], baseline)
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--base-ref", "planning-base", "--backend", "claude", "--final-reviewer", "claude",
        )
        state = self.get_state(initialized)
        self.assertEqual(state["baseline_sha"], baseline)
        self.assertEqual(state["base_ref"], "planning-base")
        self.assertFalse((Path(state["worktree"]) / "dirty-untracked.txt").exists())

    def test_preflight_probe_never_receives_dirty_checkout_contents(self) -> None:
        tracked = self.project / "dirty-tracked.txt"
        tracked.write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "add", tracked.name], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-qm", "probe baseline"], cwd=self.project, check=True)
        tracked.write_text("dirty\n", encoding="utf-8")
        (self.project / "dirty-staged.txt").write_text("staged secret\n", encoding="utf-8")
        subprocess.run(["git", "add", "dirty-staged.txt"], cwd=self.project, check=True)
        (self.project / "dirty-untracked.txt").write_text("untracked secret\n", encoding="utf-8")

        result = self.call(
            "preflight", "--project", str(self.project), "--base-ref", "HEAD",
            "--backend", "claude", "--probe",
        )
        self.assertEqual(result["status"], "PREFLIGHT_OK")
        self.assertTrue(result["probes"]["claude"]["ok"])

    def test_preflight_rejects_schema_delivery_without_the_mounted_file_contents(self) -> None:
        codex = self.bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv:\n print('fake-agent 1.0'); raise SystemExit(0)\n"
            "if '--help' in sys.argv:\n"
            " print('--output-schema --output-last-message --ephemeral --sandbox --config'); raise SystemExit(0)\n"
            "payload = {'provider': 'codex', 'ready': True, 'observed': 'not-read'}\n"
            "with open(sys.argv[sys.argv.index('-o') + 1], 'w') as handle: json.dump(payload, handle)\n",
            encoding="utf-8")
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)

        result = self.call(
            "preflight", "--project", str(self.project), "--backend", "codex", "--probe",
            expect=2)
        self.assertEqual(result["status"], "PREFLIGHT_PROVIDER_UNUSABLE")
        self.assertFalse(result["probes"]["codex"]["ok"])
        self.assertIn("did not reproduce", result["probes"]["codex"]["reason"])

    def test_codex_host_can_skip_installed_probe_failed_claude_for_dsh(self) -> None:
        claude = self.bin / "claude"
        claude.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv:\n print('fake-agent 1.0'); raise SystemExit(0)\n"
            "if '--help' in sys.argv:\n"
            " print('--json-schema --output-format --permission-mode --no-session-persistence'); raise SystemExit(0)\n"
            "payload = {'provider': 'claude', 'ready': True, 'observed': 'not-read'}\n"
            "print(json.dumps({'structured_output': payload}))\n",
            encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)

        failed = self.call(
            "preflight", "--project", str(self.project), "--backend", "auto",
            "--host-adapter", "codex", "--peer-reviewer", "claude", "--probe", expect=2)
        self.assertEqual(failed["status"], "PREFLIGHT_PROVIDER_UNUSABLE")
        self.assertFalse(failed["probes"]["claude"]["ok"])

        fallback = self.call(
            "preflight", "--project", str(self.project), "--backend", "auto",
            "--host-adapter", "codex", "--peer-reviewer", "dsh", "--probe")
        self.assertEqual(fallback["planners"], {"A": "codex", "B": "dsh"})
        self.assertEqual(fallback["preferred_final_reviewer"], "codex")

        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "codex", "--peer-reviewer", "dsh")
        self.assertEqual(initialized["planners"], {"A": "codex", "B": "dsh"})
        self.assertEqual(initialized["final_reviewer"], "codex")

    def test_single_provider_uses_two_isolated_instances_and_reaches_ready(self) -> None:
        initialized = self.initialize("claude", "both")
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "claude"})
        self.assertFalse(initialized["provider_diversity"])
        self.run_through_cross_review(initialized)
        state = self.get_state(initialized)
        # Isolation is not a property of the directory. The two instances share ONE checkout,
        # detached at the frozen baseline and mounted read-only, and are separated by everything
        # else: their own process, sandbox, private home, and context. This used to assert two
        # distinct paths -- pinning the mechanism, and with it a second registration to lose when
        # an attempt is killed.
        self.assertEqual(Path(state["worktree"]).name, "baseline")
        registered = subprocess.check_output(
            ["git", "-C", str(self.project), "worktree", "list", "--porcelain"], text=True)
        self.assertEqual(registered.count(str(Path(state["worktree"]))), 1)
        for slot in ("A", "B"):
            invocation = next((Path(initialized["run_directory"]) / f"draft-{slot}").parent.glob(
                f"invocations/draft-{slot}/attempt_1_*"))
            self.assertNotEqual(invocation / "home", Path(state["worktree"]))
        draft_a_context = next((Path(initialized["run_directory"]) / "invocations" / "draft-A").glob("attempt_1_*/context"))
        self.assertEqual(
            sorted(item.name for item in draft_a_context.iterdir() if item.name != "schema.json"),
            ["causal_analysis.md", "investigation.json", "request.md", "scope_contract.json"],
        )
        causal = (draft_a_context / "causal_analysis.md").read_text(encoding="utf-8")
        self.assertIn("canonical authority", causal)
        self.assertIn("Deterministic", causal)
        self.assertIn("Generative", causal)
        self.submit_candidate(initialized)
        for reviewer in ("A", "B"):
            self.call("final-review", "--project", str(self.project), "--run-id", initialized["run_id"], "--reviewer", reviewer)
        exported = self.call("export", "--project", str(self.project), "--run-id", initialized["run_id"])
        self.assertEqual(exported["status"], "READY")
        self.assertTrue(Path(exported["plan"]).is_file())
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project, text=True).strip(), self.baseline)
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.project, text=True), "")

    def test_host_aware_auto_freezes_cross_review_and_final_reviewer_policy(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "dsh")
        self.assertEqual(initialized["host_adapter"], "dsh")
        self.assertEqual(initialized["planners"], {"A": "dsh", "B": "codex"})
        self.assertEqual(initialized["final_reviewer"], "dsh")

        for slot in ("A", "B"):
            self.call(
                "investigate", "--project", str(self.project),
                "--run-id", initialized["run_id"], "--slot", slot)
        self.run_through_cross_review(initialized)
        state = self.get_state(initialized)
        self.assertEqual(state["draft_rounds"]["2"]["A"]["provider"], "dsh")
        self.assertEqual(state["draft_rounds"]["2"]["B"]["provider"], "codex")
        self.submit_candidate(initialized)
        self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F")
        state = self.get_state(initialized)
        self.assertEqual(state["final_reviews"]["F"]["provider"], "dsh")

    def test_auto_selected_b_rate_limit_persistently_falls_back_to_host(self) -> None:
        self.request.write_text(
            "# Objective\nCreate a verified plan.\nFAKE_RATE_LIMIT=investigate-B\n",
            encoding="utf-8",
        )
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "dsh",
        )
        failed = self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B", expect=2,
        )
        self.assertIn("RATE_LIMIT", failed["error"])
        state = self.get_state(initialized)
        self.assertEqual(state["slot_provider_overrides"], {"B": "dsh"})
        self.assertIsNone(state["pending_decision"])
        self.assertEqual(state["automatic_fallbacks"][0]["from"], "codex")
        self.assertFalse(state["provider_diversity"])
        status = self.call(
            "status", "--project", str(self.project), "--run-id", initialized["run_id"],
        )
        self.assertEqual(status["slot_provider_overrides"], {"B": "dsh"})
        self.assertEqual(status["automatic_fallbacks"][0]["to"], "dsh")
        self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B",
        )
        state = self.get_state(initialized)
        self.assertEqual(state["investigations"]["B"]["provider"], "dsh")
        self.assertEqual(state["usage"]["investigate-B"], 1)

    def test_explicit_b_reviewer_rate_limit_requires_user_decision(self) -> None:
        self.request.write_text(
            "# Objective\nCreate a verified plan.\nFAKE_RATE_LIMIT=investigate-B\n",
            encoding="utf-8",
        )
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "dsh", "--peer-reviewer", "codex",
        )
        self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B", expect=2,
        )
        state = self.get_state(initialized)
        self.assertEqual(state["status"], "NEEDS_USER_DECISION")
        self.assertEqual(state["pending_decision"]["kind"], "RATE_LIMIT")
        self.assertEqual(state["slot_provider_overrides"], {})

    def test_user_reassigned_assignment_is_not_automatically_replaced(self) -> None:
        spec = importlib.util.spec_from_file_location("gb_explicit_fallback_test", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = {
            "backend_requested": "auto", "peer_reviewer_requested": "auto",
            "host_adapter": "dsh", "assignment_providers": {"draft-B": "claude"},
        }
        failure = module.ProviderInfrastructureError(
            "claude", "RATE_LIMIT", "429 rate limit", retryable=True,
        )
        self.assertIsNone(module.automatic_planning_host_fallback(
            state, "draft-B", "B", "claude", failure,
        ))
        self.assertNotIn("slot_provider_overrides", state)

    def test_auto_b_rate_limit_stops_when_host_fallback_is_unavailable(self) -> None:
        self.request.write_text(
            "# Objective\nCreate a verified plan.\nFAKE_RATE_LIMIT=investigate-B\n",
            encoding="utf-8",
        )
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "dsh",
        )
        host = self.bin / "dsh"
        host.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        host.chmod(0o755)
        self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B", expect=2,
        )
        state = self.get_state(initialized)
        self.assertEqual(state["status"], "NEEDS_USER_DECISION")
        self.assertEqual(state["pending_decision"]["kind"], "RATE_LIMIT")
        self.assertEqual(state["slot_provider_overrides"], {})

    def test_auto_selected_b_can_fall_back_to_other_host_bridge(self) -> None:
        self.request.write_text(
            "# Objective\nCreate a verified plan.\nFAKE_RATE_LIMIT=investigate-B\n",
            encoding="utf-8",
        )
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other",
        )
        self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B", expect=2,
        )
        state = self.get_state(initialized)
        self.assertEqual(state["slot_provider_overrides"], {"B": "other"})
        self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "B",
        )
        state = self.get_state(initialized)
        self.assertEqual(state["investigations"]["B"]["provider"], "other")

    def test_other_host_uses_generic_bridge_and_keeps_final_review_on_host(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other",
        )
        self.assertEqual(initialized["planners"], {"A": "other", "B": "codex"})
        self.assertEqual(initialized["peer_reviewer"], "codex")
        self.assertEqual(initialized["final_reviewer"], "other")
        self.assertEqual(
            initialized["agent_runtime"]["other"]["protocol"],
            "grounded-build-other-v1",
        )

        for slot in ("A", "B"):
            self.call(
                "investigate", "--project", str(self.project),
                "--run-id", initialized["run_id"], "--slot", slot,
            )
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        self.call(
            "final-review", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--reviewer", "F",
        )
        state = self.get_state(initialized)
        self.assertEqual(state["draft_rounds"]["2"]["A"]["provider"], "other")
        self.assertEqual(state["draft_rounds"]["2"]["B"]["provider"], "codex")
        self.assertEqual(state["final_reviews"]["F"]["provider"], "other")

    def test_other_only_installation_uses_other_for_every_planning_slot(self) -> None:
        for provider in ("claude", "codex", "dsh"):
            path = self.bin / provider
            path.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            path.chmod(0o755)
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other",
        )
        self.assertEqual(initialized["planners"], {"A": "other", "B": "other"})
        self.assertEqual(initialized["peer_reviewer"], "other")
        self.assertEqual(initialized["final_reviewer"], "other")
        self.assertTrue(initialized["selection_checks"]["other"]["ok"])

    def test_explicit_final_reviewer_overrides_other_host_default(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other",
            "--final-reviewer", "dsh",
        )
        self.assertEqual(initialized["planners"], {"A": "other", "B": "codex"})
        self.assertEqual(initialized["final_reviewer"], "dsh")
        self.assertTrue(initialized["selection_checks"]["dsh"]["ok"])

    def test_unused_invalid_other_bridge_does_not_block_explicit_builtin_backend(self) -> None:
        preflight = self.call(
            "preflight", "--project", str(self.project), "--backend", "claude",
            extra_env={"GROUNDED_BUILD_OTHER_COMMAND": "relative/bad"},
        )
        self.assertEqual(preflight["status"], "PREFLIGHT_OK")
        self.assertEqual(preflight["planners"], {"A": "claude", "B": "claude"})

    def test_explicit_backend_and_final_do_not_require_an_unused_host_bridge(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--host-adapter", "other",
            "--final-reviewer", "claude",
            extra_env={"GROUNDED_BUILD_OTHER_COMMAND": ""},
        )
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "claude"})
        self.assertEqual(initialized["final_reviewer"], "claude")
        self.assertNotIn("other", initialized["selection_checks"])

    def test_other_bridge_digest_is_frozen_before_agent_invocation(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other",
            "--peer-reviewer", "other",
        )
        bridge = Path(self.env["GROUNDED_BUILD_OTHER_COMMAND"])
        bridge.write_text(bridge.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        result = self.call(
            "investigate", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--slot", "A", expect=2,
        )
        self.assertIn("bridge executable changed", result["error"])

    def test_other_bridge_must_declare_the_protocol_contract(self) -> None:
        bridge = Path(self.env["GROUNDED_BUILD_OTHER_COMMAND"])
        bridge.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv: print('bad-bridge 1.0')\n"
            "elif sys.argv[1] == 'capabilities': print(json.dumps({'protocol': 'unknown'}))\n",
            encoding="utf-8",
        )
        bridge.chmod(0o755)
        result = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "other", expect=2,
        )
        self.assertIn("host adapter failed", result["error"])
        self.assertIn("grounded-build-other-v1", result["error"])

    def test_host_aware_init_does_not_freeze_installed_codex_without_its_read_host(self) -> None:
        (self.bin / "codex-code-mode-host").unlink()
        preflight = self.call(
            "preflight", "--project", str(self.project), "--backend", "auto",
            "--host-adapter", "dsh", "--probe",
        )
        self.assertEqual(preflight["status"], "PREFLIGHT_OK")
        self.assertEqual(preflight["planners"], {"A": "dsh", "B": "dsh"})
        self.assertFalse(preflight["selection_checks"]["codex"]["ok"])
        self.assertTrue(preflight["selection_checks"]["dsh"]["ok"])
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "auto", "--host-adapter", "dsh",
        )
        self.assertEqual(initialized["planners"], {"A": "dsh", "B": "dsh"})
        self.assertEqual(initialized["peer_reviewer"], "dsh")
        self.assertEqual(initialized["final_reviewer"], "dsh")
        self.assertTrue(initialized["selection_checks"]["dsh"]["ok"])
        self.assertFalse(initialized["selection_checks"]["codex"]["ok"])
        self.assertTrue(any(
            "codex-code-mode-host" in item
            for item in initialized["selection_checks"]["codex"]["capability"]["missing"]
        ),
        )

    def test_explicit_backend_does_not_override_host_aware_final_auto(self) -> None:
        preflight = self.call(
            "preflight", "--project", str(self.project), "--backend", "claude",
            "--host-adapter", "dsh", "--probe")
        self.assertEqual(preflight["preferred_final_reviewer"], "dsh")
        self.assertEqual(set(preflight["capabilities"]["adapters"]), {"claude", "dsh"})
        self.assertEqual(set(preflight["probes"]), {"claude", "dsh"})

        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--host-adapter", "dsh")
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "claude"})
        self.assertIsNone(initialized["peer_reviewer"])
        self.assertEqual(initialized["final_reviewer"], "dsh")

    def test_explicit_backend_preflight_does_not_probe_an_unused_external_reviewer(self) -> None:
        (self.bin / "codex-code-mode-host").unlink()
        preflight = self.call(
            "preflight", "--project", str(self.project), "--backend", "claude",
            "--host-adapter", "dsh", "--probe",
        )
        self.assertEqual(preflight["status"], "PREFLIGHT_OK")
        self.assertEqual(preflight["planners"], {"A": "claude", "B": "claude"})
        self.assertEqual(preflight["preferred_final_reviewer"], "dsh")
        self.assertTrue(preflight["selection_checks"]["dsh"]["ok"])
        self.assertNotIn("codex", preflight["selection_checks"])

        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--host-adapter", "dsh",
        )
        self.assertEqual(initialized["final_reviewer"], "dsh")

    def test_same_round_investigations_really_overlap_and_merge(self) -> None:
        self.request.write_text(
            "# Objective\nCreate a verified plan.\nFAKE_SLEEP=1.0\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "both")
        action = initialized["next_action"]
        self.assertEqual(action["kind"], "RUN_AGENT_BATCH")
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env)
                     for command in action["commands"]]
        __import__("time").sleep(0.25)
        running = self.call(
            "status", "--project", str(self.project), "--run-id", initialized["run_id"])
        self.assertEqual(set(running["active_invocations"]), {"investigate-A", "investigate-B"})
        results = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
        self.assertTrue(all(returncode == 0 for _, _, returncode in results), results)
        state = self.get_state(initialized)
        self.assertEqual(set(state["investigations"]), {"A", "B"})
        self.assertEqual(state["status"], "EVIDENCE_READY")

    def test_parallel_nonempty_findings_merge_only_at_the_locked_barrier(self) -> None:
        self.request.write_text(
            "# Objective\nFAKE_SLEEP=0.4\nFAKE_NONEMPTY_FINDINGS\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env)
                     for command in initialized["next_action"]["commands"]]
        results = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        state = self.get_state(initialized)
        ledger = json.loads(Path(state["finding_ledger_path"]).read_text(encoding="utf-8"))["findings"]
        self.assertEqual(len(ledger), 2)
        self.assertEqual(
            {item["canonical"]["id"] for item in ledger.values()},
            {"A-finding", "B-finding"},
        )

    def test_terminal_state_absorbs_a_late_parallel_result(self) -> None:
        self.request.write_text("# Objective\nFAKE_SLEEP=0.8\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        process = subprocess.Popen(
            initialized["next_action"]["commands"][0], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.call("status", "--project", str(self.project), "--run-id",
                         initialized["run_id"])["active_invocations"]:
                break
            time.sleep(0.05)
        self.call(
            "abandon", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "stop while result is in flight", "--actor", "tester", "--apply")
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        state = self.get_state(initialized)
        self.assertEqual(state["status"], "ABANDONED")
        self.assertEqual(state["investigations"], {})
        self.assertEqual(state["late_parallel_results"][0]["terminal_status"], "ABANDONED")

    def test_ready_state_also_absorbs_a_late_parallel_result(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        spec = importlib.util.spec_from_file_location("gb_ready_absorbing_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            incoming = self.get_state(initialized)
            latest = dict(incoming)
            latest["status"] = "READY"
            module.save_state(latest)
            incoming["investigations"] = {
                "A": {"provider": "claude", "path": "late-result.json"}}
            incoming["_completed_invocation_binding"] = {
                "engine_epoch": incoming["engine_epoch"],
                "engine_contract": incoming["engine_contract"], "invocation": "late.json",
            }
            module.save_parallel_stage(incoming, "investigate")
            self.assertEqual(incoming["status"], "READY")
            self.assertEqual(incoming["investigations"], {})
            self.assertEqual(incoming["late_parallel_results"][0]["terminal_status"], "READY")
        finally:
            if previous is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = previous

    def test_two_parallel_decisions_are_adjudicated_without_overwrite(self) -> None:
        self.request.write_text(
            self.request.read_text() + "\nFAKE_CROSS_DECISION=A\nFAKE_CROSS_DECISION=B\n",
            encoding="utf-8")
        initialized = self.initialize("claude", "claude")
        run_id = initialized["run_id"]
        for slot in ("A", "B"):
            self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        action = self.call("next", "--project", str(self.project), "--run-id", run_id)
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env)
                     for command in action["next_action"]["commands"]]
        results = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        state = self.get_state(initialized)
        self.assertEqual(len(state["pending_decisions"]), 2)
        self.assertEqual(state["pending_decision"]["source"], "cross-A")
        self.call(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", "PLANNING_BOUNDARY|source=cross-A",
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "preview A",
            "--actor", "tester")
        first = self.call(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", "PLANNING_BOUNDARY|source=cross-A",
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "resolve A",
            "--actor", "tester", "--apply")
        self.assertEqual(first["status"], "NEEDS_USER_DECISION")
        self.assertEqual(first["pending_decision"]["source"], "cross-B")
        stale = self.call(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", "PLANNING_BOUNDARY|source=cross-A",
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "stale A",
            "--actor", "tester", "--apply", expect=2)
        self.assertIn("no longer exists", stale["error"])
        second = self.call(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", "PLANNING_BOUNDARY|source=cross-B",
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "resolve B",
            "--actor", "tester", "--apply")
        self.assertEqual(second["status"], "SYNTHESIS_REQUIRED")

    def test_provider_overload_is_not_charged_as_quality(self) -> None:
        self.request.write_text("# Objective\nFAKE_529\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        failure = self.call(
            "investigate", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--slot", "A", expect=2)
        self.assertIn("PROVIDER_OVERLOAD", failure["error"])
        state = self.get_state(initialized)
        self.assertNotIn("investigate-A", state["usage"])
        self.assertEqual(state["infrastructure_usage"]["investigate-A"], 1)
        self.assertEqual(state["pending_decision"]["type"], "PROVIDER_INFRASTRUCTURE_FAILURE")

    def test_zero_exit_codex_tool_host_failure_is_infrastructure(self) -> None:
        self.request.write_text(
            self.request.read_text() + "\nFAKE_CODE_MODE_ROUTER_ERROR=final-1-F\n",
            encoding="utf-8")
        initialized = self.initialize("codex", "codex")
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)

        failure = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F", expect=2)
        self.assertIn("TOOL_HOST_STARTUP", failure["error"])
        state = self.get_state(initialized)
        self.assertNotIn("final-1-F", state["usage"])
        self.assertEqual(state["infrastructure_usage"]["final-1-F"], 1)
        self.assertEqual(state["pending_decision"]["type"], "PROVIDER_INFRASTRUCTURE_FAILURE")
        self.assertEqual(state["pending_decision"]["kind"], "TOOL_HOST_STARTUP")
        self.assertEqual(state["final_reviews"], {})
        record_path = next(
            (Path(initialized["run_directory"]) / "invocations" / "final-1-F")
            .glob("attempt_1_*/invocation.json"))
        self.assertEqual(
            json.loads(record_path.read_text(encoding="utf-8"))["status"],
            "INFRASTRUCTURE_FAILURE")

    def test_codex_unavailable_warning_without_router_error_is_not_failure(self) -> None:
        self.request.write_text(
            self.request.read_text() + "\nFAKE_CODE_MODE_WARNING=final-1-F\n",
            encoding="utf-8")
        initialized = self.initialize("codex", "codex")
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)

        delivered = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F")
        self.assertEqual(delivered["status"], "READY")
        state = self.get_state(initialized)
        self.assertEqual(state["final_reviews"]["F"]["verdict"], "PASS")
        self.assertNotIn("final-1-F", state.get("infrastructure_usage", {}))

    def test_quoted_router_error_inside_codex_json_is_not_infrastructure(self) -> None:
        quoted = (
            '{"summary":"ERROR codex_core::tools::router: error=failed to spawn '
            'code-mode-host: No such file or directory"}'
        )
        result = subprocess.CompletedProcess(
            ["codex"], 0, stdout="", stderr=f"review result: {quoted}\n",
        )
        spec = importlib.util.spec_from_file_location("plan_workflow_quoted_error", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        failure = module.classify_successful_exit_infrastructure_failure(
            "codex", result, self.temp / "raw.json",
        )
        self.assertIsNone(failure)

    def test_infrastructure_failure_after_budget_reset_is_idempotently_reimbursed(self) -> None:
        self.request.write_text("# Objective\nFAKE_529\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        spec = importlib.util.spec_from_file_location("gb_reset_failure_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            state = self.get_state(initialized)
            state["usage"]["investigate-A"] = 1
            state["charged_context_digests"] = {"investigate-A": ["0" * 64]}
            module.save_state(state)
        finally:
            if previous is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = previous

        failure = self.call(
            "investigate", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--slot", "A", expect=2)
        self.assertIn("PROVIDER_OVERLOAD", failure["error"])
        state = self.get_state(initialized)
        self.assertNotIn("investigate-A", state["usage"])
        self.assertEqual(state["charged_context_digests"]["investigate-A"], [])
        self.assertEqual(state["pending_decision"]["type"], "PROVIDER_INFRASTRUCTURE_FAILURE")

    def test_invocation_record_freezes_engine_argv_runtime_and_prunes_scratch(self) -> None:
        initialized = self.initialize("claude", "claude")
        record_path = next(
            (Path(initialized["run_directory"]) / "invocations" / "investigate-A")
            .glob("attempt_1_*/invocation.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            record["engine_contract"]["software_version"],
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(record["requested_runtime"]["model"], "opus")
        self.assertIn("--model", record["argv"])
        self.assertEqual(record["argv"][-1], "<PROMPT>")
        self.assertEqual(record["status"], "DELIVERED")
        self.assertFalse((record_path.parent / "home").exists())
        self.assertFalse((record_path.parent / "tmp").exists())

    def test_engine_drift_requires_an_explicit_audited_migration(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        spec = importlib.util.spec_from_file_location("gb_engine_migration_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            state = self.get_state(initialized)
            state["engine_contract"] = {"software_version": "older", "files": {}}
            module.save_state(state)
        finally:
            if previous is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = previous
        failure = self.call(
            "status", "--project", str(self.project), "--run-id", initialized["run_id"], expect=2)
        self.assertIn("engine drift", failure["error"])
        preview = self.call(
            "migrate-engine", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "upgrade test", "--actor", "test")
        self.assertEqual(preview["status"], "ENGINE_MIGRATION_PREVIEW")
        self.call(
            "migrate-engine", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "upgrade test", "--actor", "test", "--apply")
        status = self.call(
            "status", "--project", str(self.project), "--run-id", initialized["run_id"])
        self.assertEqual(status["status"], "INITIALIZED")
        self.assertEqual(self.get_state(initialized)["engine_epoch"], 1)

    def test_engine_migration_refuses_running_invocations(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        record = (Path(initialized["run_directory"]) / "invocations" / "investigate-A"
                  / "attempt_1_test" / "invocation.json")
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({
            "assignment": "investigate-A", "provider": "claude", "slot": "A",
            "status": "RUNNING", "started_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("gb_recovery_migration_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            state = self.get_state(initialized)
            state["engine_contract"] = {"software_version": "older", "files": {}}
            module.save_state(state)
        finally:
            if previous is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = previous
        refused = self.call(
            "migrate-engine", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "test migration", "--actor", "tester", "--apply", expect=2)
        self.assertIn("RUNNING", refused["error"])
        self.assertEqual(self.get_state(initialized)["engine_epoch"], 0)
        preview = self.call(
            "recover-invocation", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--assignment", "investigate-A",
            "--reason", "controller terminated", "--actor", "tester")
        self.assertEqual(preview["status"], "INTERRUPTED_INVOCATION_RECOVERY_PREVIEW")
        recovered = self.call(
            "recover-invocation", "--project", str(self.project),
            "--run-id", initialized["run_id"], "--assignment", "investigate-A",
            "--reason", "controller terminated", "--actor", "tester", "--apply")
        self.assertEqual(len(recovered["recovered_invocations"]), 1)
        self.assertEqual(json.loads(record.read_text())["status"], "CONTROLLER_INTERRUPTED")
        migrated = self.call(
            "migrate-engine", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "test migration", "--actor", "tester", "--apply")
        self.assertEqual(migrated["engine_contract"]["software_version"],
                         (ROOT / "VERSION").read_text().strip())

    def test_stale_engine_epoch_result_is_rejected_at_parallel_save(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        spec = importlib.util.spec_from_file_location("gb_stale_epoch_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        previous = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            incoming = self.get_state(initialized)
            latest = dict(incoming)
            latest["engine_epoch"] = 1
            module.save_state(latest)
            incoming["_completed_invocation_binding"] = {
                "engine_epoch": 0, "engine_contract": incoming["engine_contract"],
                "invocation": "late.json",
            }
            with self.assertRaisesRegex(module.WorkflowError, "stale investigate result"):
                module.save_parallel_stage(incoming, "investigate")
        finally:
            if previous is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = previous

    def test_controller_crash_after_reservation_consumes_quality_attempt(self) -> None:
        self.request.write_text("# Objective\nFAKE_SLEEP=5\n", encoding="utf-8")
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        process = subprocess.Popen(
            initialized["next_action"]["commands"][0], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.get_state(initialized)
            if state["usage"].get("investigate-A") == 1:
                break
            time.sleep(0.05)
        process.kill()
        process.communicate(timeout=5)
        state = self.get_state(initialized)
        self.assertEqual(state["usage"]["investigate-A"], 1)
        record = next((Path(initialized["run_directory"]) / "invocations" / "investigate-A")
                      .glob("attempt_1_*/invocation.json"))
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "RUNNING")

    def test_abandoned_run_has_an_audit_export_and_no_approval_claim(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "claude")
        self.call(
            "abandon", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reason", "operator stopped", "--actor", "test", "--apply")
        exported = self.call(
            "audit-export", "--project", str(self.project), "--run-id", initialized["run_id"])
        report = Path(exported["report"]).read_text(encoding="utf-8")
        self.assertIn("Outcome: `ABANDONED`", report)
        self.assertIn("No plan was approved", report)

    def test_investigation_is_a_hard_gate_before_drafting(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "both",
        )
        blocked = self.call(
            "draft", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--slot", "A", expect=2,
        )
        self.assertIn("draft is not allowed from INITIALIZED", blocked["error"])
        for slot in ("A", "B"):
            self.call("investigate", "--project", str(self.project),
                      "--run-id", initialized["run_id"], "--slot", slot)
        drafted = self.call("draft", "--project", str(self.project),
                            "--run-id", initialized["run_id"], "--slot", "A")
        self.assertEqual(drafted["status"], "DRAFTING")

    def test_authoritative_web_is_explicit_and_limited_to_investigation(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "codex", "--final-reviewer", "codex",
            "--research-policy", "authoritative-web",
        )
        preview = self.call(
            "investigate", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--slot", "A", "--dry-run",
        )
        self.assertIn("tools.web_search=true", " ".join(preview["command"]))
        dsh_run = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "dsh", "--final-reviewer", "dsh",
            "--research-policy", "authoritative-web",
        )
        dsh_preview = self.call(
            "investigate", "--project", str(self.project), "--run-id", dsh_run["run_id"],
            "--slot", "A", "--dry-run",
        )
        dsh_command = dsh_preview["command"]
        patch = Path(dsh_command[dsh_command.index("--patch") + 1]).read_text()
        self.assertNotIn("id: tool-web\n  disabled: true", patch)
        self.assertIn("allowWeb: true", patch)

    def test_deep_mode_requires_draft_03_and_two_convergence_rounds(self) -> None:
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "both", "--planning-depth", "deep",
        )
        run_id = initialized["run_id"]
        for command in ("investigate", "draft"):
            for slot in ("A", "B"):
                self.call(command, "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        for slot in ("A", "B"):
            result = self.call("cross-review", "--project", str(self.project),
                               "--run-id", run_id, "--slot", slot)
        self.assertEqual(result["status"], "DIVERGENCE_REQUIRED")
        for slot in ("A", "B"):
            result = self.call("diverge", "--project", str(self.project),
                               "--run-id", run_id, "--slot", slot)
        self.assertEqual(result["status"], "SYNTHESIS_REQUIRED")
        self.submit_candidate(initialized)
        self.assertEqual(self.get_state(initialized)["status"], "CONVERGENCE_REVIEW_REQUIRED")
        for slot in ("A", "B"):
            result = self.call("convergence-review", "--project", str(self.project),
                               "--run-id", run_id, "--reviewer", slot)
        self.assertEqual(result["status"], "FINAL_REVIEW_REQUIRED")
        for slot in ("A", "B"):
            result = self.call("final-review", "--project", str(self.project),
                               "--run-id", run_id, "--reviewer", slot)
        self.assertEqual(result["status"], "READY")

    def test_convergence_boundary_can_resume_and_rejects_stale_candidate_results(self) -> None:
        self.request.write_text(
            self.request.read_text() + "\nFAKE_CONVERGENCE_DECISION=A\n",
            encoding="utf-8",
        )
        initialized = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "claude", "--final-reviewer", "both", "--planning-depth", "deep",
        )
        run_id = initialized["run_id"]
        for command in ("investigate", "draft"):
            for slot in ("A", "B"):
                self.call(command, "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        for slot in ("A", "B"):
            self.call("cross-review", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        for slot in ("A", "B"):
            self.call("diverge", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        self.submit_candidate(initialized)
        old_state = self.get_state(initialized)
        old_digest = old_state["candidate"]["plan_sha256"]

        decision = self.call(
            "convergence-review", "--project", str(self.project), "--run-id", run_id,
            "--reviewer", "A",
        )
        self.assertEqual(decision["status"], "NEEDS_USER_DECISION")
        resumed = self.call(
            "adjudicate", "--project", str(self.project), "--run-id", run_id,
            "--decision-id", self.pending_decision_id(initialized),
            "--choice", "RESOLVE_AND_CONTINUE", "--decision", "replace the candidate",
            "--actor", "tester", "--apply",
        )
        self.assertEqual(resumed["status"], "SYNTHESIS_REQUIRED")

        output = self.call("synthesis-context", "--project", str(self.project), "--run-id", run_id)
        directory = Path(output["output_directory"])
        plan = directory / "replacement_plan.md"
        batches = directory / "replacement_batches.md"
        plan.write_text(
            "# Replacement plan\n\n## Scope\nTARGET-001: Implement the corrected request.\n",
            encoding="utf-8",
        )
        batches.write_text(
            "# Batches\n\n- B01: corrected scope; exit when the named test returns zero.\n",
            encoding="utf-8",
        )
        self.call(
            "submit-synthesis", "--project", str(self.project), "--run-id", run_id,
            "--plan", str(plan), "--batch-manifest", str(batches),
        )
        current = self.get_state(initialized)
        self.assertNotEqual(current["candidate"]["plan_sha256"], old_digest)
        self.assertEqual(current["convergence_reviews"], {})

        spec = importlib.util.spec_from_file_location("gb_stale_candidate", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stale = dict(old_state)
        stale["convergence_reviews"] = {
            "B": {
                "provider": "claude", "verdict": "PASS", "path": "stale.json",
                "candidate_sha256": old_digest,
            }
        }
        prior_home = os.environ.get("GROUNDED_BUILD_PLAN_HOME")
        os.environ["GROUNDED_BUILD_PLAN_HOME"] = self.env["GROUNDED_BUILD_PLAN_HOME"]
        try:
            with self.assertRaisesRegex(module.WorkflowError, "stale convergence-review result"):
                module.save_parallel_stage(stale, "convergence-review")
            stale["final_reviews"] = {
                "A": {
                    "provider": "claude", "verdict": "PASS", "path": "stale-final.json",
                    "candidate_sha256": old_digest,
                }
            }
            with self.assertRaisesRegex(module.WorkflowError, "stale final-review result"):
                module.save_parallel_stage(stale, "final-review")
        finally:
            if prior_home is None:
                os.environ.pop("GROUNDED_BUILD_PLAN_HOME", None)
            else:
                os.environ["GROUNDED_BUILD_PLAN_HOME"] = prior_home

    def test_auto_topology_records_provider_diversity(self) -> None:
        initialized = self.initialize("auto", "codex")
        self.assertEqual(initialized["planners"], {"A": "claude", "B": "dsh"})
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
        settings = json.loads(command[command.index("--settings") + 1])
        sandbox = settings["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertEqual(sandbox["excludedCommands"], [])
        self.assertEqual(sandbox["filesystem"]["denyRead"], [str(Path(preview["invocation"]).parent / "home")])
        invocation_root = str(Path(preview["invocation"]).parent)
        self.assertFalse(any(
            command[index:index + 3] == ["--bind", invocation_root, invocation_root]
            for index in range(len(command) - 2)))

        # A fresh fixture is unnecessary: abandoning is not required to initialize another run.
        codex_run = self.initialize("codex", "codex")
        preview = self.call(
            "draft", "--project", str(self.project), "--run-id", codex_run["run_id"],
            "--slot", "A", "--dry-run",
        )
        # The same policy, carried on the command line. It was written into a config file inside
        # the private home, which replaced whatever the user's own config said -- including, for a
        # codex pointed at a self-hosted gateway, the provider block holding its base URL and
        # token. `-c` layers over the file rather than erasing it, so the deny survives a
        # configuration this workflow does not have to understand.
        line = " ".join(preview["command"])
        self.assertIn('"~/.codex"="deny"', line)
        self.assertIn("tools.web_search=false", line)
        self.assertIn('default_permissions="grounded_build"', line)
        codex_root = str(Path(preview["invocation"]).parent)
        self.assertNotIn(["--bind", codex_root, codex_root],
                         [preview["command"][index:index + 3]
                          for index in range(len(preview["command"]) - 2)])
        raw = str(Path(preview["invocation"]).parent / "raw.json")
        self.assertIn(["--bind", raw, raw],
                      [preview["command"][index:index + 3]
                       for index in range(len(preview["command"]) - 2)])

        dsh_run = self.call(
            "init", "--project", str(self.project), "--request", str(self.request),
            "--backend", "dsh", "--final-reviewer", "dsh")
        dsh_preview = self.call(
            "investigate", "--project", str(self.project), "--run-id", dsh_run["run_id"],
            "--slot", "A", "--dry-run")
        dsh_command = dsh_preview["command"]
        self.assertIn("--patch", dsh_command)
        patch_path = Path(dsh_command[dsh_command.index("--patch") + 1])
        patch_text = patch_path.read_text(encoding="utf-8")
        disabled = (
            "tool-bash", "tool-pwsh", "jobs", "tool-jobs", "tool-skill", "tool-todo",
            "tool-goal", "web", "web-search-deepseek", "tool-web", "code-runtime", "subagent",
            "subagent-spawn-in-process", "subagent-fork-in-process", "tool-subagent-control",
            "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork",
            "tool-subagent-report", "workflow-worker-thread", "tool-workflow", "tool-ralph",
            "tool-str-replace-editor",
        )
        for row in disabled:
            self.assertIn(f"id: {row}\n  disabled: true", patch_text)
        self.assertIn("id: grounded-build-read-boundary", patch_text)
        self.assertIn("dsh-read-boundary.mjs", patch_text)
        self.assertNotIn("id: tool-fs\n", patch_text)
        self.assertNotIn("id: tool-fs-search\n", patch_text)
        dsh_root = str(Path(dsh_preview["invocation"]).parent)
        self.assertNotIn(["--bind", dsh_root, dsh_root],
                         [dsh_command[index:index + 3]
                          for index in range(len(dsh_command) - 2)])

    def test_a_retry_after_an_undelivered_answer_says_so_in_its_prompt(self) -> None:
        """The corrective has to reach the agent, not merely exist as a function.

        `delivery_corrective` can be right and never called, which is the shape of every guard this
        project has had to re-fix: correct code on a path nothing takes. This drives a real
        invocation whose first attempt ends with an empty output file -- codex's actual failure --
        and reads the SECOND attempt's prompt off disk.
        """
        self.request.write_text(self.request.read_text() + "\nFAKE_EMPTY_FIRST=1\n")
        initialized = self.initialize("codex", "codex")
        run_id = initialized["run_id"]

        failure = self.call("draft", "--project", str(self.project), "--run-id", run_id,
                            "--slot", "A", expect=2)
        self.assertIn("without a final message", failure["error"])
        self.assertNotIn("invalid JSON", failure["error"])

        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "A")

        invocations = Path(initialized["run_directory"]) / "invocations" / "draft-A"
        first = next(invocations.glob("attempt_1_*/prompt.md")).read_text(encoding="utf-8")
        second = next(invocations.glob("attempt_2_*/prompt.md")).read_text(encoding="utf-8")
        self.assertNotIn("RETRY NOTICE", first)
        self.assertIn("RETRY NOTICE", second)
        self.assertIn("without a final message", second)
        for forbidden in ("PASS", "FAIL", "approve"):
            self.assertNotIn(forbidden, second.split("RETRY NOTICE")[1].split("\n\n")[0])

        state = self.get_state(initialized)
        self.assertNotIn("draft-A", state.get("delivery_faults") or {},
                         "a delivered answer clears the fault; otherwise every later attempt "
                         "carries a notice about a failure that is no longer true")

    def test_a_reassigned_run_stops_claiming_provider_diversity(self) -> None:
        """The whole point of allowing the swap is that the record still tells the truth after it.

        Drives the real path: auto topology, dsh exhausts draft-B without delivering, the user
        reassigns to claude, the run finishes. The report must then say the run was NOT
        provider-diverse and name the swap -- otherwise a reader sees `provider_diversity: true`
        describing a topology that was frozen rather than one that ran.
        """
        self.request.write_text(self.request.read_text() + "\nFAKE_EMPTY_ALWAYS=draft-B\n")
        initialized = self.initialize("auto", "claude")
        run_id = initialized["run_id"]
        self.assertTrue(initialized["provider_diversity"])

        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "A")
        for _ in range(3):  # dsh slot: every attempt ends with no final message on stdout
            failure = self.call("draft", "--project", str(self.project), "--run-id", run_id,
                                "--slot", "B", expect=2)
            self.assertIn("without a final message", failure["error"])
        exhausted = self.call("draft", "--project", str(self.project), "--run-id", run_id,
                              "--slot", "B", expect=2)
        self.assertIn("budget exhausted", exhausted["error"])

        state = self.get_state(initialized)
        pending = state["pending_decision"]
        self.assertEqual(pending["provider"], "dsh")
        self.assertIn("without ever delivering a verdict", pending["diagnosis"])
        self.assertIn("REASSIGN_ASSIGNMENT", pending["diagnosis"])

        self.call("adjudicate", "--project", str(self.project), "--run-id", run_id,
                  "--decision-id", self.pending_decision_id(initialized),
                  "--choice", "REASSIGN_ASSIGNMENT", "--to-provider", "claude",
                  "--decision", "dsh never delivered", "--actor", "tester", "--apply")

        state = self.get_state(initialized)
        self.assertEqual(state["assignment_providers"], {"draft-B": "claude"})
        self.assertFalse(state["provider_diversity"],
                         "a run whose second slot was taken over is not provider-diverse")
        self.assertFalse(state["model_diversity"],
                         "a reassigned run must not retain the stronger model-diversity claim")
        self.assertEqual(state["usage"]["draft-B"], 0, "the replacement starts with its own count")

        # The always-empty marker is scoped to draft-B, which claude now serves via `-p`.
        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "B")
        for slot in ("A", "B"):
            self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                      "--slot", slot)
        self.submit_candidate(initialized)
        self.call("final-review", "--project", str(self.project), "--run-id", run_id,
                  "--reviewer", "F")
        exported = self.call("export", "--project", str(self.project), "--run-id", run_id)
        self.assertEqual(exported["status"], "READY")
        self.assertFalse(exported["provider_diversity"])
        self.assertFalse(exported["model_diversity"])
        self.assertEqual(exported["assignment_providers"], {"draft-B": "claude"})
        self.assertEqual(exported["automatic_fallbacks"], [])

        report = (Path(initialized["run_directory"]) / "final_report.md").read_text(encoding="utf-8")
        self.assertIn("Provider diversity: `false`", report)
        self.assertIn("Model diversity: `false`", report)
        self.assertIn("draft-B", report)
        self.assertIn("dsh -> claude", report)

    def test_a_reassigned_final_reviewer_can_still_be_exported(self) -> None:
        """READY validation must use the provider that actually produced the final review."""
        self.request.write_text(self.request.read_text() + "\nFAKE_EMPTY_ALWAYS=final-1-B\n")
        initialized = self.initialize("auto", "both")
        run_id = initialized["run_id"]
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        self.call("final-review", "--project", str(self.project), "--run-id", run_id,
                  "--reviewer", "A")
        for _ in range(3):
            failure = self.call("final-review", "--project", str(self.project), "--run-id", run_id,
                                "--reviewer", "B", expect=2)
            self.assertIn("without a final message", failure["error"])
        exhausted = self.call("final-review", "--project", str(self.project), "--run-id", run_id,
                              "--reviewer", "B", expect=2)
        self.assertIn("budget exhausted", exhausted["error"])
        self.call("adjudicate", "--project", str(self.project), "--run-id", run_id,
                  "--decision-id", self.pending_decision_id(initialized),
                  "--choice", "REASSIGN_ASSIGNMENT", "--to-provider", "claude",
                  "--decision", "dsh never delivered", "--actor", "tester", "--apply")
        self.call("final-review", "--project", str(self.project), "--run-id", run_id,
                  "--reviewer", "B")

        exported = self.call("export", "--project", str(self.project), "--run-id", run_id)
        self.assertEqual(exported["status"], "READY")
        self.assertFalse(exported["provider_diversity"])
        self.assertFalse(exported["model_diversity"])

    def test_the_reviewer_gets_the_plan_as_prose_not_as_one_escaped_string(self) -> None:
        """The defect that read as a provider difference and was a representation choice.

        cross-A received a 28,874-byte draft and passed on its first attempt. cross-B received a
        93,322-byte one and failed four times, saying in its own transcript that the file was
        truncated at 13,331 tokens and it never saw the rest. The same plan as markdown is 42,636
        bytes -- escaping into a single JSON string more than doubled it and made it unpageable.
        """
        initialized = self.initialize("claude", "claude")
        run_id = initialized["run_id"]
        for slot in ("A", "B"):
            self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        self.call("cross-review", "--project", str(self.project), "--run-id", run_id, "--slot", "A")

        context = next((Path(initialized["run_directory"]) / "invocations" / "cross-A")
                       .glob("attempt_1_*/context"))
        names = sorted(p.name for p in context.iterdir())
        self.assertIn("target_plan.md", names)
        self.assertIn("target_claims.json", names)
        self.assertNotIn("target_draft.json", names,
                         "the blob is what the reviewer could not read")

        plan = (context / "target_plan.md").read_text(encoding="utf-8")
        self.assertIn("## Scope", plan, "the plan arrives as markdown a reader can page through")
        self.assertGreater(len(plan.splitlines()), 1)

        claims = json.loads((context / "target_claims.json").read_text(encoding="utf-8"))
        self.assertNotIn("plan_markdown", claims,
                         "the plan is not carried twice; the .md is the plan")
        self.assertIn("repository_facts", claims)
        self.assertIn("summary", claims, "the metadata the reviewer still needs comes with it")
        preview_b = self.call(
            "cross-review", "--project", str(self.project), "--run-id", run_id,
            "--slot", "B", "--dry-run")
        context_b = Path(preview_b["context"])
        self.assertEqual(
            (context / "finding_ledger.json").read_bytes(),
            (context_b / "finding_ledger.json").read_bytes(),
            "both cross-reviewers must receive the same pre-round ledger even after A finishes")

    def test_an_adjudication_in_the_context_does_not_reset_a_spent_budget(self) -> None:
        """The anti-loophole half: decisions accumulate in the context on every adjudication.

        A budget keyed on everything in the context would lift itself the moment it was reached,
        because reaching it is what produces the decision file that lands there next.
        """
        self.request.write_text(self.request.read_text() + "\nFAKE_EMPTY_ALWAYS=cross-B\n")
        initialized = self.initialize("codex", "codex")
        run_id = initialized["run_id"]
        for slot in ("A", "B"):
            self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", slot)
        self.call("cross-review", "--project", str(self.project), "--run-id", run_id, "--slot", "A")

        for _ in range(3):
            self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                      "--slot", "B", expect=2)
        self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                  "--slot", "B", expect=2)
        self.call("adjudicate", "--project", str(self.project), "--run-id", run_id,
                  "--decision-id", self.pending_decision_id(initialized),
                  "--choice", "GRANT_ONE_INVOCATION", "--decision", "one more",
                  "--actor", "tester", "--apply")
        self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                  "--slot", "B", expect=2)

        exhausted = self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                              "--slot", "B", expect=2)
        self.assertIn("budget exhausted", exhausted["error"])
        state = self.get_state(initialized)
        self.assertEqual(state["usage"]["cross-B"], 4)
        self.assertEqual(state.get("budget_resets", []), [],
                         "a decision file landing in the context is not a change of material")
        self.assertEqual(len(state["charged_context_digests"]["cross-B"]), 1,
                         "four attempts, one input")

        # Un-parking is free and buys nothing. The material is unchanged, so the very next
        # invocation charges nothing and parks again -- the user can clear the block, only the
        # evidence can lift the count.
        self.call("adjudicate", "--project", str(self.project), "--run-id", run_id,
                  "--decision-id", self.pending_decision_id(initialized),
                  "--choice", "RESOLVE_AND_CONTINUE", "--decision", "unpark", "--actor", "tester",
                  "--apply")
        again = self.call("cross-review", "--project", str(self.project), "--run-id", run_id,
                          "--slot", "B", expect=2)
        self.assertIn("budget exhausted", again["error"])
        state = self.get_state(initialized)
        self.assertEqual(state["usage"]["cross-B"], 4, "un-parking must not buy an attempt")
        self.assertEqual(state.get("budget_resets", []), [])

    def test_submitting_the_documented_filenames_works(self) -> None:
        """SKILL.md tells the host to write these two names into this directory. Then submit them.

        `submit-synthesis` copied the submission to `<round>/implementation_plan.md` and
        `<round>/batches.md` unconditionally, so obeying the instruction produced SameFileError:
        the one workflow the documentation describes was the one that could not run. Found by
        following the documentation.
        """
        initialized = self.initialize("claude", "claude")
        self.run_through_cross_review(initialized)
        output = self.call("synthesis-context", "--project", str(self.project),
                           "--run-id", initialized["run_id"])
        directory = Path(output["output_directory"])
        plan = directory / "implementation_plan.md"
        batches = directory / "batches.md"
        plan.write_text("# Implementation plan\n\n## Scope\nTARGET-001: Implement the request.\n", encoding="utf-8")
        batches.write_text("# Batches\n\n- B01: scope; exit when the named test returns zero.\n",
                           encoding="utf-8")

        result = self.call(
            "submit-synthesis", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--plan", str(plan), "--batch-manifest", str(batches),
        )
        self.assertEqual(result["status"], "FINAL_REVIEW_REQUIRED")
        self.assertEqual(Path(result["candidate"]["plan"]), plan,
                         "the frozen copy is the file the host wrote; no second copy appears")
        self.assertTrue(plan.is_file() and batches.is_file())

    def test_a_preview_is_not_filed_as_an_attempt(self) -> None:
        """The record must not contain entries for invocations that never ran.

        Dry-run built a full `attempt_N_*` directory -- context copied, prompt written -- and left
        it there. One cross-review ended up with two `attempt_4_*` directories, one real and one a
        preview, separable only by whether a stderr.log happened to be inside. For a workflow whose
        entire product is an auditable record of what happened, that is the record lying.
        """
        initialized = self.initialize("codex", "codex")
        run_id = initialized["run_id"]
        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "A",
                  "--dry-run")
        invocations = Path(initialized["run_directory"]) / "invocations" / "draft-A"
        self.assertEqual([p.name.split("_")[0] for p in invocations.iterdir()], ["preview"])

        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "A")
        kinds = sorted(p.name.split("_")[0] for p in invocations.iterdir())
        self.assertEqual(kinds, ["attempt", "preview"])
        attempts = [p for p in invocations.iterdir() if p.name.startswith("attempt_")]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].name.split("_")[1], "1",
                         "a preview must not consume an attempt number either")

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
        state = self.get_state(initialized)
        assignment = "final-1-F"
        self.assertIn("deterministic contract rejection", state["delivery_faults"][assignment])
        record_path = next(
            (Path(initialized["run_directory"]) / "invocations" / assignment)
            .glob("attempt_1_*/invocation.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "VALIDATION_REJECTED")
        self.assertIn("PASS review cannot contain P0/P1", record["diagnostic"])

    def test_dsh_nested_schema_violation_cannot_reach_a_pass(self) -> None:
        self.request.write_text(
            self.request.read_text() + "\nFAKE_NESTED_BAD_SEVERITY=F\n", encoding="utf-8")
        initialized = self.initialize("dsh", "dsh")
        self.run_through_cross_review(initialized)
        self.submit_candidate(initialized)
        result = self.call(
            "final-review", "--project", str(self.project), "--run-id", initialized["run_id"],
            "--reviewer", "F", expect=2)
        self.assertIn("$.findings[0].severity", result["error"])
        state = self.get_state(initialized)
        self.assertEqual(state["usage"]["final-1-F"], 1)
        self.assertNotEqual(state["status"], "READY")

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
            "--decision-id", self.pending_decision_id(initialized),
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
        (Path(state["worktree"]) / "drift.txt").write_text("drift", encoding="utf-8")
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


class ReassignmentTest(unittest.TestCase):
    """A provider that cannot do one job must not cost the run the jobs that were already done.

    Topology is frozen at init so an independence claim means something. With no escape, that
    freeze turns one unusable provider into a dead run: codex failed cross-review four times while
    the other planner's draft, the other cross-review, and the frozen request all sat there valid.
    The alternative on offer was to abandon and re-draft everything.

    Reassignment is the escape, and it is only acceptable while it is loud. These tests pin the
    loudness, not the convenience.
    """

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("gb_plan_workflow_reassign", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_an_assignment_uses_its_planner_until_it_is_reassigned(self) -> None:
        state = {"planners": {"A": "claude", "B": "codex"}}
        self.assertEqual(self.module.assignment_provider(state, "cross-B", "codex"), "codex")
        state["assignment_providers"] = {"cross-B": "claude"}
        self.assertEqual(self.module.assignment_provider(state, "cross-B", "codex"), "claude")
        self.assertEqual(self.module.assignment_provider(state, "draft-B", "codex"), "codex",
                         "reassigning one assignment must not move the others")

    def test_a_run_with_no_reassignment_says_so(self) -> None:
        self.assertEqual(self.module.independence_section({}), "- Reassignments: `none`\n")

    def test_the_report_states_what_was_reassigned_and_what_it_cost(self) -> None:
        section = self.module.independence_section({"independence_notes": [{
            "assignment": "cross-B", "from": "codex", "to": "claude",
            "decided_at": "2026-08-21T13:00:00+00:00",
            "reason": "cross-B will be served by claude, which also serves other assignments.",
        }]})
        self.assertIn("cross-B", section)
        self.assertIn("codex -> claude", section)
        self.assertIn("independence reduced", section)
        self.assertIn("also serves other assignments", section)


class InputScopedBudgetTest(unittest.TestCase):
    """The budget counts attempts at an input, not attempts forever.

    Four cross-review attempts measured a draft handed over as one 43,026-character line. When the
    preparation defect was fixed the assignment was being asked a different question, and charging
    the old attempts to it left two exits: reassign -- which would have produced a confident review
    of a half-read document -- or abandon the run. Neither is what the evidence supported.
    """

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("gb_plan_workflow_budget", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = Path(tempfile.mkdtemp(prefix="gb-budget-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def file(self, name: str, body: str) -> Path:
        path = self.temp / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_first_attempt_never_resets(self) -> None:
        self.assertFalse(self.module.budget_reset_due(0, [], "abc"))
        self.assertFalse(self.module.budget_reset_due(0, ["old"], "abc"))

    def test_repeating_a_charged_input_never_resets(self) -> None:
        self.assertFalse(self.module.budget_reset_due(4, ["abc"], "abc"),
                         "asking the same question again must not buy more attempts")
        self.assertFalse(self.module.budget_reset_due(4, ["abc", "def"], "def"))

    def test_new_material_resets(self) -> None:
        self.assertTrue(self.module.budget_reset_due(4, ["abc"], "xyz"))

    def test_the_digest_follows_content_and_names(self) -> None:
        one = {"request.md": self.file("request.md", "objective")}
        base = self.module.context_digest(one)
        self.assertEqual(base, self.module.context_digest(one), "stable")

        self.file("request.md", "objective, revised")
        self.assertNotEqual(base, self.module.context_digest(one), "content counts")

        renamed = {"target_plan.md": self.file("target_plan.md", "objective")}
        original = {"target_draft.json": self.file("target_draft.json", "objective")}
        self.assertNotEqual(self.module.context_digest(renamed),
                            self.module.context_digest(original),
                            "the same bytes under a different name is a different question -- "
                            "which is exactly what the representation fix changed")

    def test_decisions_are_excluded_from_the_digest(self) -> None:
        """Otherwise the cap lifts itself: reaching it is what creates the next decision file."""
        without = {"request.md": self.file("request.md", "objective")}
        with_decision = dict(without, decision_001=self.file("decision_001.json", '{"choice": "x"}'))
        self.assertEqual(self.module.context_digest(without),
                         self.module.context_digest(with_decision))


class DeliveryFailureTest(unittest.TestCase):
    """An answer that never arrived, told apart from one that arrived malformed.

    Three cross-review attempts against one draft: 22 minutes ending mid-search with an empty
    output file, 25 minutes ending with the reviewer's own deliberation about severity submitted
    where the schema object belonged, 15 minutes ending mid-search again. All three were inside the
    30-minute ceiling, so nothing external cut them off, and all three were reported to the
    operator as "returned invalid JSON" -- which is true of zero bytes only in the sense that zero
    bytes parse badly, and false about everything that matters.
    """

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("gb_plan_workflow_delivery", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = Path(tempfile.mkdtemp(prefix="gb-delivery-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def completed(self, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["codex"], returncode=0, stdout=stdout, stderr=stderr)

    def test_an_empty_output_is_reported_as_no_answer_not_as_bad_json(self) -> None:
        raw = self.temp / "raw.json"
        raw.write_text("", encoding="utf-8")
        result = self.completed(stderr="...\nWarning: no last agent message; wrote empty content\n")

        with self.assertRaises(self.module.NoFinalAnswer) as caught:
            self.module.extract_payload("codex", result, raw)

        message = str(caught.exception)
        self.assertIn("without a final message", message)
        self.assertIn("no last agent message", message,
                      "the CLI said exactly what happened; the operator should not have to grep for it")
        self.assertNotIn("invalid JSON", message)

    def test_prose_where_the_object_belonged_says_so(self) -> None:
        raw = self.temp / "raw.json"
        raw.write_text("Let me finalize. I'll write a concise summary and findings list.\n",
                       encoding="utf-8")
        with self.assertRaises(self.module.NoFinalAnswer) as caught:
            self.module.extract_payload("codex", self.completed(), raw)
        message = str(caught.exception)
        self.assertIn("not the schema object", message)
        self.assertNotIn("cut off", message)
        self.assertNotIn("without a final message", message,
                         "this one DID answer -- in the wrong shape. Different cause, different fix.")

    def test_an_object_cut_off_in_transit_is_not_called_prose(self) -> None:
        """The real fourth attempt: a correct schema object truncated inside a finding's evidence.

        It begins `{"provider": "codex", ... "verdict": "PASS"` and dies at character 4541 of 4587,
        inside a string. The reviewer did everything asked of it. Reporting that as "prose" points
        the operator at a reviewer that would not comply, when what they have is an object too big
        for the output it was given -- a different problem with a different remedy.
        """
        raw = self.temp / "raw.json"
        raw.write_text(
            '{ "provider": "codex",\n "reviewer_slot": "B",\n "verdict": "PASS",\n'
            ' "summary": "verified draft-A against the checkout",\n'
            ' "findings": [{"id": "B1", "evidence": "plan_markdown section 6: P1.5.b/c deferred',
            encoding="utf-8")
        with self.assertRaises(self.module.NoFinalAnswer) as caught:
            self.module.extract_payload("codex", self.completed(), raw)
        message = str(caught.exception)
        self.assertIn("cut off", message)
        self.assertIn("too large for the output", message)
        self.assertNotIn("not the schema object", message)

    def test_the_contract_asks_for_shorter_findings_not_fewer(self) -> None:
        """Trading completeness for size would buy a clean parse with a suppressed defect."""
        contract = self.module.DELIVERY_CONTRACT
        self.assertIn("COMPLETE", contract)
        self.assertIn("fewer words per finding, never fewer findings", contract)

    def test_a_valid_object_still_parses(self) -> None:
        raw = self.temp / "raw.json"
        raw.write_text('{"verdict": "PASS"}', encoding="utf-8")
        self.assertEqual(self.module.extract_payload("codex", self.completed(), raw),
                         {"verdict": "PASS"})

    def test_the_first_attempt_carries_no_retry_notice(self) -> None:
        self.assertEqual(self.module.delivery_corrective({}, "cross-B"), "")
        self.assertEqual(
            self.module.delivery_corrective({"delivery_faults": {"cross-A": "x"}}, "cross-B"), "",
            "another assignment's failure is not this one's business")

    def test_a_retry_is_told_what_did_not_arrive(self) -> None:
        state = {"delivery_faults": {"cross-B": "codex ended its turn without a final message"}}
        notice = self.module.delivery_corrective(state, "cross-B")
        self.assertIn("no usable verdict", notice)
        self.assertIn("ended its turn without a final message", notice)

    def test_the_retry_notice_cannot_steer_the_verdict(self) -> None:
        """The line between telling a reviewer HOW to deliver and WHAT to conclude.

        A retry that nudges toward a verdict buys the PASS this workflow exists to withhold. This
        asserts the notice names no verdict, no severity, and no finding, and says outright that
        the previous conclusion is unconstrained.
        """
        state = {"delivery_faults": {"cross-B": "codex delivered prose"}}
        notice = self.module.delivery_corrective(state, "cross-B")
        for forbidden in ("PASS", "FAIL", "P0", "P1", "P2", "approve", "accept", "agree",
                          "shorter", "fewer findings"):
            self.assertNotIn(forbidden, notice, f"a retry notice must not mention {forbidden!r}")
        self.assertIn("unaffected and unconstrained", notice)

    def test_the_delivery_contract_binds_the_turn_without_binding_the_judgement(self) -> None:
        contract = self.module.DELIVERY_CONTRACT
        self.assertIn("ONE turn", contract)
        self.assertIn("FINAL message", contract)
        for forbidden in ("PASS", "FAIL", "approve", "accept"):
            self.assertNotIn(forbidden, contract)

    def test_every_assignment_that_returns_a_schema_carries_the_contract(self) -> None:
        """Drafting has not failed this way, which is not a reason to leave it unsaid."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(source.count("+ DELIVERY_CONTRACT"), 6,
                         "every investigation, draft, integration and convergence assignment carries it")


class CodexTrustStoreTest(unittest.TestCase):
    """What the sandbox must carry for a CLI to still be the CLI the user configured.

    The incident: a codex authenticating through a self-hosted gateway. Its provider block, base
    URL, and bearer token all live in ``config.toml``; it keeps no ``auth.json`` at all. The
    workflow named ``auth.json`` as the whole credential store, found nothing, and then WROTE its
    own ``config.toml`` over the private home's -- so codex started with no provider, fell back to
    the vendor default endpoint, and returned 401 five times. That surfaced as a failed planning
    draft. Nothing had failed except the sandbox's idea of what a credential is.
    """

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gb-home-"))
        self.previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        spec = importlib.util.spec_from_file_location("gb_plan_workflow", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous_home
        shutil.rmtree(self.home, ignore_errors=True)

    def write_config(self, body: str) -> Path:
        config = self.codex_home / "config.toml"
        config.write_text(body, encoding="utf-8")
        return config

    def test_a_gateway_config_is_the_credential_store(self) -> None:
        config = self.write_config(
            'model = "ap/deepseek-v4-pro"\n'
            'model_provider = "omniroute"\n'
            'model_catalog_json = "~/.codex/models.json"\n'
            "[model_providers.omniroute]\n"
            'base_url = "http://gateway.internal:20128/v1"\n'
            'experimental_bearer_token = "sk-not-a-real-token"\n'
        )
        catalog = self.codex_home / "models.json"
        catalog.write_text("{}", encoding="utf-8")

        store = self.module.provider_trust_store("codex")

        self.assertIn(config.resolve(), store)
        self.assertIn(catalog.resolve(), store,
                      "model_catalog_json names a real file; codex will not start without it")

    def test_an_auth_json_installation_still_works(self) -> None:
        auth = self.codex_home / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        self.assertIn(auth.resolve(), self.module.provider_trust_store("codex"))

    def test_a_file_that_does_not_exist_is_not_mounted(self) -> None:
        self.write_config('model = "x"\nmodel_catalog_json = "~/.codex/absent.json"\n')
        store = self.module.provider_trust_store("codex")
        self.assertEqual([path.name for path in store], ["config.toml"])

    def test_a_config_codex_cannot_parse_is_left_for_codex_to_complain_about(self) -> None:
        self.write_config("this is not = = toml\n")
        store = self.module.provider_trust_store("codex")
        self.assertEqual([path.name for path in store], ["config.toml"],
                         "a parse failure must not cost the run its config")

    def test_a_home_file_keeps_its_place_relative_to_home(self) -> None:
        """``~`` inside the sandbox is the private home, so mirrored paths keep resolving.

        A config that says ``model_catalog_json = "~/.codex/models.json"`` is read INSIDE the
        sandbox, where ``~`` is the private home. Binding the catalog anywhere else would leave
        that line pointing at nothing.
        """
        private = Path("/sandbox/home")
        mirrored = self.module.sandbox_destination(self.codex_home / "models.json", private)
        self.assertEqual(mirrored, private / ".codex" / "models.json")

        outside = Path("/etc/somewhere/models.json")
        self.assertEqual(self.module.sandbox_destination(outside, private), outside)

    def test_both_planners_can_execute_and_neither_can_write(self) -> None:
        """The request asks in three places for measurements to be RUN. Both sides must be able to.

        Bash was disallowed for claude while codex ran with `-s read-only`, which includes a shell.
        Same request, one planner able to execute and the other not: the claude drafts said "this
        session had no shell" and left the P1.a measurement, the P5.2 reproduction and the registry
        census unresolved, while codex ran `gate_code_discovery.py` and `extract_urd_delivery_-
        languages` and settled them. That was this file's configuration, not a difference between
        the CLIs, and it halved the evidence one half of the cross-check could bring.

        Read-only is enforced by the `--ro-bind` mount, not by withholding the tool -- measured:
        `touch` inside the worktree returns "Read-only file system".
        """
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            claude = self.module.agent_command(
                "claude", root, root, {"type": "object"}, root / "raw.json", "prompt")
            codex = self.module.agent_command(
                "codex", root, root, {"type": "object"}, root / "raw.json", "prompt")

        allowed = claude[claude.index("--allowedTools") + 1]
        disallowed = claude[claude.index("--disallowedTools") + 1]
        self.assertIn("Bash", allowed, "the planner that cannot execute cannot verify")
        self.assertNotIn("Bash", disallowed.split(","))
        for mutating in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(mutating, disallowed.split(","))
        self.assertIn("read-only", codex, "codex's half of the parity")

    def test_a_run_initialized_before_the_collapse_still_resolves(self) -> None:
        """A run in flight when this changed keeps its per-slot map, and must keep working.

        The collapse to one checkout happens at ``init``. A run already past it has two registered
        worktrees and a ``worktrees`` map in its state; reading that state must not become an error
        just because new runs write a different key. Cleanup has to see BOTH of its trees, and see
        each of them once.
        """
        legacy = {
            "planners": {"A": "claude", "B": "codex"},
            "worktrees": {"A": "/runs/x/worktrees/A", "B": "/runs/x/worktrees/B"},
        }
        self.assertEqual(self.module.reviewer_worktree(legacy, "B", "codex"),
                         Path("/runs/x/worktrees/B"))
        self.assertEqual(self.module.reviewer_worktree(legacy, "F", "codex"),
                         Path("/runs/x/worktrees/B"))
        self.assertEqual(self.module.planning_worktrees(legacy),
                         [Path("/runs/x/worktrees/A"), Path("/runs/x/worktrees/B")])

        current = {"planners": {"A": "claude", "B": "codex"},
                   "worktree": "/runs/y/worktrees/baseline"}
        self.assertEqual(self.module.planning_worktrees(current),
                         [Path("/runs/y/worktrees/baseline")],
                         "one tree, listed once -- cleanup removes it once")
        for slot in ("A", "B", "F"):
            self.assertEqual(self.module.reviewer_worktree(current, slot, "codex"),
                             Path("/runs/y/worktrees/baseline"))

    def test_the_policy_rides_on_the_command_and_not_in_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            command = self.module.agent_command(
                "codex", scratch_path, scratch_path, {"type": "object"},
                scratch_path / "raw.json", "prompt",
            )
        line = " ".join(command)
        self.assertIn('default_permissions="grounded_build"', line)
        self.assertIn('"~/.codex"="deny"', line)
        self.assertIn("tools.web_search=false", line)
        self.assertNotIn("--dangerously", line)
        self.assertIn("read-only", line)

    def test_matching_code_mode_host_is_mounted_once_at_codex_absolute_path(self) -> None:
        worktree = self.home / "project"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        context = self.home / "context"
        context.mkdir()
        invocation = self.home / "invocation"
        executable = self.home / "release" / "bin" / "codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("binary", encoding="utf-8")
        code_mode_host = executable.with_name("codex-code-mode-host")
        code_mode_host.write_text("host", encoding="utf-8")
        command = ["codex", "-o", str(invocation / "raw.json"), "prompt"]

        def which(name: str) -> str | None:
            return "/usr/bin/bwrap" if name == "bwrap" else str(executable) if name == "codex" else None

        with mock.patch.object(self.module.shutil, "which", side_effect=which):
            wrapper = self.module.isolated_agent_command(
                command, {"agent_runtime": {"codex": {}}}, invocation, worktree, context)

        self.assertEqual(wrapper.count("/opt/codex-code-mode-host"), 1)
        destination = wrapper.index("/opt/codex-code-mode-host")
        self.assertEqual(wrapper[destination - 2:destination + 1], [
            "--ro-bind", str(code_mode_host.resolve()), "/opt/codex-code-mode-host",
        ])

    def test_the_reviewer_can_read_what_it_reviews(self) -> None:
        """The per-model catalog truncates tool output at 10,000 tokens; a plan exceeds that.

        One cross-review said so itself -- "truncated at 13331 tokens" -- and burned four turns
        failing to reassemble its target. `tool_output_token_limit` is the documented budget for
        that and does not appear in `codex --help`. Measured through this exact command: the same
        93,322-byte file reads back whole, tail `]\\n}\\n`, in 29,663 tokens.
        """
        line = " ".join(self.module.CODEX_POLICY_OVERRIDES)
        self.assertIn("tool_output_token_limit=", line)
        limit = int(line.split("tool_output_token_limit=")[1].split()[0])
        self.assertGreater(limit, 10_000, "the catalog default is what broke the review")

    def test_deepseek_identity_is_a_model_not_a_cli_adapter(self) -> None:
        self.write_config(
            'model = "deepseek-reasoner"\nmodel_provider = "deepseek-gateway"\n'
            '[model_providers.deepseek-gateway]\nbase_url = "https://secret.invalid/v1"\n'
            'experimental_bearer_token = "never-record-this"\n')
        identity = self.module.codex_runtime_identity()
        self.assertEqual(identity["adapter"], "codex")
        self.assertEqual(identity["model_family"], "deepseek")
        self.assertEqual(identity["model_provider"], "deepseek-gateway")
        serialized = json.dumps(identity)
        self.assertNotIn("secret.invalid", serialized)
        self.assertNotIn("never-record-this", serialized)

    def test_explicit_codex_selection_reaches_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            command = self.module.agent_command(
                "codex", root, root, {"type": "object"}, root / "raw.json", "prompt",
                {"model": "deepseek-reasoner", "model_provider": "deepseek-gateway",
                 "profile": "deepseek"})
        self.assertEqual(command[command.index("-m") + 1], "deepseek-reasoner")
        self.assertEqual(command[command.index("-p") + 1], "deepseek")
        self.assertIn('model_provider="deepseek-gateway"', command)

    def test_profile_configuration_is_part_of_the_read_only_trust_store(self) -> None:
        profile = self.codex_home / "deepseek.config.toml"
        profile.write_text(
            'model = "deepseek-reasoner"\nmodel_provider = "deepseek-gateway"\n'
            'model_catalog_json = "~/.codex/deepseek-models.json"\n', encoding="utf-8")
        catalog = self.codex_home / "deepseek-models.json"
        catalog.write_text("{}", encoding="utf-8")
        self.assertIn(profile.resolve(), self.module.provider_trust_store("codex", "deepseek"))
        self.assertIn(catalog.resolve(), self.module.provider_trust_store("codex", "deepseek"))
        identity = self.module.codex_runtime_identity(profile="deepseek")
        self.assertEqual(identity["model_family"], "deepseek")
        self.assertEqual(identity["model"], "deepseek-reasoner")


class DshAdapterTest(unittest.TestCase):
    """The dsh adapter: identity, trust store, command shape, and free-text payload parsing."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gb-dsh-home-"))
        self.previous_home = os.environ.get("HOME")
        self.previous_dsh_home = os.environ.get("DSH_HOME")
        os.environ["HOME"] = str(self.home)
        os.environ["DSH_HOME"] = str(self.home / ".dsh")
        spec = importlib.util.spec_from_file_location("gb_dsh_workflow", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.dsh_home = self.home / ".dsh"
        self.dsh_home.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous_home
        if self.previous_dsh_home is None:
            os.environ.pop("DSH_HOME", None)
        else:
            os.environ["DSH_HOME"] = self.previous_dsh_home
        shutil.rmtree(self.home, ignore_errors=True)

    def test_identity_reads_the_settings_selection_without_leaking_secrets(self) -> None:
        (self.dsh_home / "settings.yaml").write_text(
            "llm-deepseek:\n  api_key: sk-never-record-this\n  base_url: https://secret.invalid/v1\n"
            "agent-default-model:\n  provider: deepseek-official\n  model: ap/deepseek-v4-pro\n"
            "  reasoningEffort: high\n", encoding="utf-8")
        identity = self.module.dsh_runtime_identity()
        self.assertEqual(identity["adapter"], "dsh")
        self.assertEqual(identity["model_family"], "deepseek")
        self.assertEqual(identity["model_provider"], "deepseek-official")
        self.assertEqual(identity["model"], "ap/deepseek-v4-pro")
        self.assertEqual(identity["identity_source"], "dsh_settings")
        serialized = json.dumps(identity)
        self.assertNotIn("secret.invalid", serialized)
        self.assertNotIn("never-record-this", serialized)

    def test_identity_parser_needs_no_optional_yaml_package(self) -> None:
        (self.dsh_home / "settings.yaml").write_text(
            "agent-default-model:\n  provider: deepseek-official # generated\n"
            "  model: 'ap/deepseek-v4-pro' # pinned\n", encoding="utf-8")
        self.assertEqual(
            self.module.dsh_settings_selection(self.dsh_home / "settings.yaml"),
            {"provider": "deepseek-official", "model": "ap/deepseek-v4-pro"},
        )

    def test_identity_falls_back_to_the_harness_default_when_unpinned(self) -> None:
        identity = self.module.dsh_runtime_identity()
        self.assertEqual(identity["model"], "deepseek-v4-flash")
        self.assertEqual(identity["identity_source"], "harness_default")
        self.assertEqual(identity["model_family"], "deepseek")

    def test_runtime_warnings_flag_an_unpinned_dsh_model(self) -> None:
        warnings = self.module.runtime_warnings({"dsh": self.module.dsh_runtime_identity()})
        self.assertTrue(any("not pinned" in warning for warning in warnings), warnings)
        self.assertTrue(any("deepseek-v4-flash" in warning for warning in warnings), warnings)

    def test_trust_store_carries_credentials_and_settings(self) -> None:
        credentials = self.dsh_home / ".credentials.yaml"
        credentials.write_text("version: 1\nrefs: {}\n", encoding="utf-8")
        settings = self.dsh_home / "settings.yaml"
        settings.write_text("agent-default-model: {}\n", encoding="utf-8")
        store = self.module.provider_trust_store("dsh")
        self.assertIn(credentials.resolve(), store)
        self.assertIn(settings.resolve(), store)

    def test_command_embeds_the_schema_and_uses_the_headless_profile(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            command = self.module.agent_command(
                "dsh", root, root, {"type": "object"}, root / "raw.json", "do the task")
        self.assertEqual(command[0], "dsh")
        self.assertEqual(command[1], "--profile")
        self.assertEqual(command[2], "headless")
        prompt = command[-1]
        self.assertIn("do the task", prompt)
        self.assertIn("OUTPUT CONTRACT", prompt)
        self.assertIn('{"type":"object"}', prompt)

    def test_capability_check_fails_when_dsh_does_not_advertise_patch_composition(self) -> None:
        fake = self.home / "dsh"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        completed = subprocess.CompletedProcess(
            [str(fake), "--help"], 0, stdout="--profile headless --dump-config\n", stderr="")
        with mock.patch.object(self.module.shutil, "which", return_value=str(fake)), \
                mock.patch.object(self.module, "run", return_value=completed):
            capability = self.module.adapter_capabilities("dsh")
        self.assertFalse(capability["ok"])
        self.assertIn("--patch", capability["missing"])

    def test_extract_dsh_object_accepts_bare_fenced_and_wrapped_json(self) -> None:
        extract = self.module.extract_dsh_object
        self.assertEqual(extract("dsh", '{"ready": true}'), {"ready": True})
        self.assertEqual(extract("dsh", '```json\n{"ready": true}\n```'), {"ready": True})
        self.assertEqual(extract("dsh", 'here is my answer: {"ready": true}\nthanks'), {"ready": True})

    def test_extract_dsh_object_rejects_non_json_as_no_final_answer(self) -> None:
        with self.assertRaises(self.module.NoFinalAnswer):
            self.module.extract_dsh_object("dsh", "I could not complete the analysis.")

    def test_auto_preference_orders_claude_dsh_codex(self) -> None:
        resolve = self.module.resolve_topology

        def present(*names: str) -> None:
            self.module.available = lambda provider: provider in set(names)

        present("claude", "codex", "dsh")
        self.assertEqual(resolve("auto"), {"A": "claude", "B": "dsh"})
        present("claude", "codex")
        self.assertEqual(resolve("auto"), {"A": "claude", "B": "codex"})
        present("codex")
        self.assertEqual(resolve("auto"), {"A": "codex", "B": "codex"})

    def test_host_aware_topology_prefers_codex_for_non_codex_hosts(self) -> None:
        self.module.available = lambda provider: provider in {"claude", "codex", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "dsh"),
            {"A": "dsh", "B": "codex"})
        self.assertEqual(self.module.resolve_final_reviewer("auto", "dsh"), "dsh")

        self.module.available = lambda provider: provider in {"dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "dsh"),
            {"A": "dsh", "B": "dsh"})
        self.assertEqual(self.module.resolve_final_reviewer("auto", "dsh"), "dsh")

    def test_codex_host_prefers_claude_then_dsh_as_external_reviewer(self) -> None:
        self.module.available = lambda provider: provider in {"claude", "codex", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "codex"),
            {"A": "codex", "B": "claude"})
        self.assertEqual(self.module.resolve_final_reviewer("auto", "codex"), "codex")

    def test_other_host_prefers_codex_then_claude_then_dsh_and_finalizes_on_host(self) -> None:
        self.module.available = lambda provider: provider in {"other", "claude", "codex", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "other"),
            {"A": "other", "B": "codex"},
        )
        self.assertEqual(self.module.resolve_final_reviewer("auto", "other"), "other")

        self.module.available = lambda provider: provider in {"other", "claude", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "other"),
            {"A": "other", "B": "claude"},
        )
        self.module.available = lambda provider: provider in {"other", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "other"),
            {"A": "other", "B": "dsh"},
        )
        self.module.available = lambda provider: provider == "other"
        self.assertEqual(
            self.module.resolve_host_topology("auto", "other"),
            {"A": "other", "B": "other"},
        )

        self.module.available = lambda provider: provider in {"codex", "dsh"}
        self.assertEqual(
            self.module.resolve_host_topology("auto", "codex"),
            {"A": "codex", "B": "dsh"})
        self.assertEqual(self.module.resolve_final_reviewer("auto", "codex"), "codex")

    def test_explicit_peer_requires_a_bound_host_and_overrides_installed_priority(self) -> None:
        self.module.available = lambda provider: provider in {"claude", "codex", "dsh"}
        with self.assertRaisesRegex(self.module.WorkflowError, "requires --backend auto"):
            self.module.resolve_host_topology("auto", None, "dsh")
        self.assertEqual(
            self.module.resolve_host_topology("auto", "codex", "dsh"),
            {"A": "codex", "B": "dsh"})
        self.assertEqual(
            self.module.resolve_final_reviewer("auto", "codex", "dsh"), "codex")
        self.assertEqual(
            self.module.resolve_host_topology("auto", "dsh", "dsh"),
            {"A": "dsh", "B": "dsh"})
        self.assertEqual(
            self.module.resolve_final_reviewer("auto", "dsh", "dsh"), "dsh")

    def test_dsh_single_backend_uses_two_isolated_instances(self) -> None:
        self.module.available = lambda provider: provider == "dsh"
        self.assertEqual(self.module.resolve_topology("dsh"), {"A": "dsh", "B": "dsh"})


class HostProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("gb_host_protocol", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def state(self, status: str) -> dict:
        return {
            "status": status, "project": "/repo", "run_id": "run-1",
            "investigations": {}, "drafts": {}, "draft_rounds": {"1": {}, "2": {}, "3": {}},
            "cross_reviews": {}, "convergence_reviews": {}, "final_reviews": {},
            "planners": {"A": "codex", "B": "codex"}, "final_reviewer": "codex",
            "candidate": {"round": 1}, "pending_decision": None,
        }

    def test_next_action_removes_slot_guessing(self) -> None:
        state = self.state("INITIALIZED")
        first = self.module.next_action(state)
        self.assertEqual((first["stage"], first["slots"]), ("investigate", ["A", "B"]))
        self.assertEqual(first["kind"], "RUN_AGENT_BATCH")
        self.assertTrue(first["parallel"])
        state["status"] = "INVESTIGATING"
        state["investigations"]["A"] = {}
        self.assertEqual(self.module.next_action(state)["slots"], ["B"])
        state["status"] = "DRAFTING"
        state["drafts"]["A"] = {}
        self.assertEqual(self.module.next_action(state)["slots"], ["B"])

    def test_ready_stops_for_separate_implementation_authority(self) -> None:
        action = self.module.next_action(self.state("READY"))
        self.assertEqual(action["kind"], "STOP_FOR_IMPLEMENTATION_APPROVAL")

    def test_dual_final_review_is_returned_as_one_parallel_barrier(self) -> None:
        state = self.state("FINAL_REVIEW_REQUIRED")
        state["final_reviewer"] = "both"
        action = self.module.next_action(state)
        self.assertEqual(action["kind"], "RUN_AGENT_BATCH")
        self.assertEqual(action["slots"], ["A", "B"])

    def test_synthesis_diagnostics_bind_each_batch_to_observations(self) -> None:
        good = self.module.synthesis_diagnostics(
            "# Plan\n\nB01 implements the request.\n\nExcluded: none.\n",
            "B01: core\nExit observation: tests pass\nVerification: pytest -q\n")
        self.assertEqual(good, {"errors": [], "warnings": []})
        weak = self.module.synthesis_diagnostics("# Plan\n", "B01: core\n")
        self.assertEqual(weak["errors"], [])
        self.assertGreaterEqual(len(weak["warnings"]), 3)

    def test_synthesis_diagnostics_reject_dependency_cycles_and_unknown_batches(self) -> None:
        diagnostics = self.module.synthesis_diagnostics(
            "# Plan\nB01 then B02.\nExcluded: none.\n",
            "B01: one\nDependencies: B02\nExit observation: done\nVerification: test\n"
            "B02: two\nDependencies: B01 B99\nExit observation: done\nVerification: test\n")
        self.assertTrue(any("unknown batches" in item for item in diagnostics["errors"]))
        self.assertTrue(any("cycle" in item for item in diagnostics["errors"]))

    def test_recursive_schema_subset_rejects_boolean_numbers_and_extra_fields(self) -> None:
        schema = {
            "type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["integer", "number", "flag", "nothing"],
                "properties": {
                    "integer": {"type": "integer"}, "number": {"type": "number"},
                    "flag": {"type": "boolean"}, "nothing": {"type": "null"},
                },
            }}},
        }
        valid = {"items": [{"integer": 1, "number": 1.5, "flag": True, "nothing": None}]}
        self.module.validate_json_schema(valid, schema)
        for field in ("integer", "number"):
            malformed = json.loads(json.dumps(valid))
            malformed["items"][0][field] = True
            with self.subTest(field=field), self.assertRaises(self.module.WorkflowError):
                self.module.validate_json_schema(malformed, schema)
        with self.assertRaises(self.module.WorkflowError):
            self.module.validate_json_schema({"items": [{**valid["items"][0], "extra": 1}]}, schema)

    def test_finding_ledger_uses_stable_content_fingerprints(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="gb-ledger-test-"))
        try:
            path = root / "finding_ledger.json"
            path.write_text('{"findings": {}}\n', encoding="utf-8")
            state = {"finding_ledger": {}, "finding_ledger_path": str(path), "artifacts": {}}
            finding = {
                "id": "local-1", "scope_id": "TARGET-001", "severity": "P1", "urgency": "U1",
                "lane": "MUST_RESOLVE", "evidence_status": "VERIFIED", "problem": " Missing guard ",
                "evidence_ids": ["E1"], "root_cause_status": "PROVEN",
                "causal_chain": ["Input bypasses guard"], "affected_surfaces": ["parser"],
                "recommended_solution": "validate input", "alternatives_and_tradeoffs": [],
                "verification": ["run parser test"],
            }
            self.module.record_findings(state, [finding], "investigation-A")
            finding["id"] = "different-local-id"
            finding["problem"] = "missing   guard"
            self.module.record_findings(state, [finding], "investigation-B")
            self.assertEqual(len(state["finding_ledger"]), 1)
            record = next(iter(state["finding_ledger"].values()))
            self.assertEqual(len(record["observations"]), 2)
        finally:
            shutil.rmtree(root)

    def test_inconsistent_progress_state_has_an_operator_facing_error(self) -> None:
        for status, completed_key in (
            ("DRAFTING", "drafts"),
            ("CROSS_REVIEWING", "cross_reviews"),
            ("FINAL_REVIEWING", "final_reviews"),
        ):
            state = self.state(status)
            state[completed_key] = ({"F": {}} if completed_key == "final_reviews"
                                    else {"A": {}, "B": {}})
            with self.subTest(status=status):
                with self.assertRaises(self.module.WorkflowError) as caught:
                    self.module.next_action(state)
                self.assertIn("inconsistent", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
