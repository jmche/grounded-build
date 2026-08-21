from __future__ import annotations

import importlib.util
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
    # Deliver nothing on the first attempt when the request asks for it: the shape codex hit
    # three times running, where the turn ends with no final message at all.
    always = re.search(r"FAKE_EMPTY_ALWAYS=(\S+)", request_text)
    if (always and f"/{always.group(1)}/" in output) or ("FAKE_EMPTY_FIRST" in request_text and "attempt_1_" in output):
        open(output, "w").close()
        sys.exit(0)
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

        Drives the real path: mixed topology, codex exhausts cross-B without delivering, the user
        reassigns to claude, the run finishes. The report must then say the run was NOT
        provider-diverse and name the swap -- otherwise a reader sees `provider_diversity: true`
        describing a topology that was frozen rather than one that ran.
        """
        self.request.write_text(self.request.read_text() + "\nFAKE_EMPTY_ALWAYS=draft-B\n")
        initialized = self.initialize("mixed", "claude")
        run_id = initialized["run_id"]
        self.assertTrue(initialized["provider_diversity"])

        self.call("draft", "--project", str(self.project), "--run-id", run_id, "--slot", "A")
        for _ in range(3):  # codex slot: every attempt delivers an empty file
            failure = self.call("draft", "--project", str(self.project), "--run-id", run_id,
                                "--slot", "B", expect=2)
            self.assertIn("without a final message", failure["error"])
        exhausted = self.call("draft", "--project", str(self.project), "--run-id", run_id,
                              "--slot", "B", expect=2)
        self.assertIn("budget exhausted", exhausted["error"])

        state = self.get_state(initialized)
        pending = state["pending_decision"]
        self.assertEqual(pending["provider"], "codex")
        self.assertIn("without ever delivering a verdict", pending["diagnosis"])
        self.assertIn("REASSIGN_ASSIGNMENT", pending["diagnosis"])

        self.call("adjudicate", "--project", str(self.project), "--run-id", run_id,
                  "--choice", "REASSIGN_ASSIGNMENT", "--to-provider", "claude",
                  "--decision", "codex never delivered", "--actor", "tester", "--apply")

        state = self.get_state(initialized)
        self.assertEqual(state["assignment_providers"], {"draft-B": "claude"})
        self.assertFalse(state["provider_diversity"],
                         "a run whose second slot was taken over is not provider-diverse")
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

        report = (Path(initialized["run_directory"]) / "final_report.md").read_text(encoding="utf-8")
        self.assertIn("Provider diversity: `false`", report)
        self.assertIn("draft-B", report)
        self.assertIn("codex -> claude", report)

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
        self.assertEqual(source.count("+ DELIVERY_CONTRACT"), 3,
                         "draft, cross-review and final-review each return a schema object")


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


if __name__ == "__main__":
    unittest.main()
