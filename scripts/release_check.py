#!/usr/bin/env python3
"""Deterministic, no-network release gate for the grounded-build skill."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick-validator", type=Path)
    args = parser.parse_args()
    validate_evals()
    run([sys.executable, "-m", "py_compile", "scripts/plan_workflow.py", "scripts/workflow.py"])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    if args.quick_validator:
        run([sys.executable, str(args.quick_validator), str(ROOT)])
    print("grounded-build release checks passed")


if __name__ == "__main__":
    main()
