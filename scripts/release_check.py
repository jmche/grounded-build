#!/usr/bin/env python3
"""Deterministic, no-network release gate for the grounded-build skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {"", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
# Mirrors .gitignore: these are local runtime artifacts, not distributed skill sources. `.omc`
# holds oh-my-claudecode session state, which records prompt text in the operator's own language.
IGNORED_PARTS = {
    ".agents", ".codex", ".git", ".pytest_cache", ".ipynb_checkpoints", "__pycache__", ".omc",
}


def run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, text=True)
    if result.returncode:
        raise SystemExit(result.returncode)


def validate_evals() -> None:
    payload = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
    if payload.get("skill_name") != "grounded-build":
        raise SystemExit("evals.json has the wrong skill_name")
    entries = payload.get("evals")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("evals.json must contain nonempty evals")
    ids: set[int] = set()
    for entry in entries:
        if set(entry) != {"id", "prompt", "expected_output", "files", "assertions"}:
            raise SystemExit(f"eval {entry.get('id')} has unexpected fields")
        if not isinstance(entry["id"], int) or entry["id"] in ids:
            raise SystemExit("eval ids must be unique integers")
        ids.add(entry["id"])
        if (
            not isinstance(entry["prompt"], str)
            or not entry["prompt"].strip()
            or not isinstance(entry["expected_output"], str)
            or not entry["expected_output"].strip()
            or not isinstance(entry["files"], list)
            or not isinstance(entry["assertions"], list)
            or not entry["assertions"]
            or not all(isinstance(item, str) and item.strip() for item in entry["assertions"])
        ):
            raise SystemExit(f"eval {entry['id']} is not actionable")


def distributed_text_files() -> list[Path]:
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
        and not IGNORED_PARTS.intersection(path.relative_to(ROOT).parts)
    ]


def validate_public_release() -> None:
    required = [
        ROOT / "LICENSE", ROOT / "SECURITY.md", ROOT / "README.md", ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md", ROOT / "SUPPORT.md", ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "scripts" / "package_release.py",
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / ".github" / "workflows" / "release.yml",
        ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
        ROOT / ".github" / "dependabot.yml",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"missing public-release files: {', '.join(missing)}")

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    if not license_text.startswith("MIT License\n"):
        raise SystemExit("LICENSE is not the selected MIT license")

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    if "Before public release" in security:
        raise SystemExit("SECURITY.md still contains a public-release placeholder")
    for phrase in (
        "research policy", "not a hard network-isolation boundary", "private vulnerability",
        "Supported versions",
    ):
        if phrase not in security:
            raise SystemExit(f"SECURITY.md does not document {phrase!r}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise SystemExit(f"VERSION is not stable semantic versioning: {version!r}")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "plan_workflow.py").read_text(encoding="utf-8")
    checks = {
        "SKILL.md": f"version: {version}" in skill,
        "README.md": f"v{version}" in readme,
        "CHANGELOG.md": f"[{version}]" in changelog,
        "scripts/plan_workflow.py": bool(re.search(
            rf'^VERSION = "{re.escape(version)}"$', script, flags=re.MULTILINE)),
    }
    drift = [name for name, valid in checks.items() if not valid]
    if drift:
        raise SystemExit(f"release version {version} is not synchronized in: {', '.join(drift)}")

    first_release = re.search(r"^## \[([^]]+)] - (\d{4}-\d{2}-\d{2})$", changelog, re.MULTILINE)
    if not first_release or first_release.group(1) != version:
        raise SystemExit("CHANGELOG.md does not begin with the current release")

    required_readme_sections = (
        "## Guarantees", "## Compatibility", "## Install", "## Quick start",
        "## Operational model", "## Runtime notes", "## Development",
        "## Support and security", "## License",
    )
    missing_sections = [section for section in required_readme_sections if section not in readme]
    if missing_sections:
        raise SystemExit(f"README.md is missing public sections: {', '.join(missing_sections)}")

    for document in (
        ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "CONTRIBUTING.md", ROOT / "SUPPORT.md",
    ):
        text = document.read_text(encoding="utf-8")
        if re.search(r"(?:/home/[^\s`]+|/Users/[^\s`]+|[A-Za-z]:\\Users\\)", text):
            raise SystemExit(f"public documentation contains a machine-specific path: {document.name}")
        for target in re.findall(r"\[[^]]+]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#")):
                continue
            local = (document.parent / target.split("#", 1)[0]).resolve()
            if not local.exists() or not local.is_relative_to(ROOT):
                raise SystemExit(f"broken local link in {document.name}: {target}")

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for python_version in ('"3.11"', '"3.12"', '"3.13"'):
        if python_version not in ci:
            raise SystemExit(f"CI does not cover Python {python_version}")
    if "permissions:\n  contents: read" not in ci or "pull_request_target" in ci:
        raise SystemExit("CI permissions or pull-request trigger are unsafe")
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )
    unpinned_actions = [
        reference for reference in re.findall(r"^\s*uses:\s*([^\s#]+)", workflow_text, re.MULTILINE)
        if not re.fullmatch(r"actions/(?:checkout|setup-python)@[0-9a-f]{40}", reference)
    ]
    if unpinned_actions:
        raise SystemExit(f"GitHub Actions are not pinned to full SHAs: {', '.join(unpinned_actions)}")

    for script_name in ("plan_workflow.py", "workflow.py", "package_release.py", "release_check.py"):
        script_path = ROOT / "scripts" / script_name
        if not os.access(script_path, os.X_OK):
            raise SystemExit(f"script is not executable: scripts/{script_name}")

    han = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    violations = []
    for path in distributed_text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if han.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{number}")
    if violations:
        raise SystemExit(f"distributed sources contain Chinese characters: {', '.join(violations)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick-validator", type=Path)
    args = parser.parse_args()
    validate_evals()
    validate_public_release()
    run([
        sys.executable, "-m", "py_compile", "scripts/plan_workflow.py", "scripts/workflow.py",
        "scripts/package_release.py", "scripts/release_check.py",
    ])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    if args.quick_validator:
        run([sys.executable, str(args.quick_validator), str(ROOT)])
    print("grounded-build release checks passed")


if __name__ == "__main__":
    main()
