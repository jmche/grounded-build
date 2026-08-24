#!/usr/bin/env python3
"""Deterministic, no-network release gate for the grounded-build skill."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {"", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
IGNORED_PARTS = {
    ".agents", ".codex", ".git", ".pytest_cache", ".ipynb_checkpoints", "__pycache__",
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
        if not entry["prompt"].strip() or not entry["assertions"]:
            raise SystemExit(f"eval {entry['id']} is not actionable")


def distributed_text_files() -> list[Path]:
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
        and not IGNORED_PARTS.intersection(path.relative_to(ROOT).parts)
    ]


def validate_public_release() -> None:
    required = [
        ROOT / "LICENSE", ROOT / "SECURITY.md", ROOT / "README.md",
        ROOT / ".github" / "workflows" / "ci.yml",
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
    for phrase in ("research policy", "not a hard network-isolation boundary", "private vulnerability"):
        if phrase not in security:
            raise SystemExit(f"SECURITY.md does not document {phrase!r}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
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
    run([sys.executable, "-m", "py_compile", "scripts/plan_workflow.py", "scripts/workflow.py"])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    if args.quick_validator:
        run([sys.executable, str(args.quick_validator), str(ROOT)])
    print("grounded-build release checks passed")


if __name__ == "__main__":
    main()
