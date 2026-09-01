#!/usr/bin/env python3
"""Build a deterministic Grounded Build runtime archive and SHA-256 checksum."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = "grounded-build"
RUNTIME_MANIFEST = (
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "SKILL.md",
    "SUPPORT.md",
    "VERSION",
    "agents/openai.yaml",
    "evals/evals.json",
    "references/contract_reviewer_prompt.md",
    "references/implementation_workflow.md",
    "references/planning_workflow.md",
    "references/reviewer_prompt.md",
    "scripts/package_release.py",
    "scripts/plan_workflow.py",
    "scripts/release_check.py",
    "scripts/workflow.py",
    "tests/test_plan_workflow.py",
    "tests/test_release.py",
    "tests/test_workflow.py",
)
EXECUTABLE_PATHS = {
    "scripts/package_release.py", "scripts/plan_workflow.py", "scripts/release_check.py",
    "scripts/workflow.py",
}


def version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value
    ):
        raise SystemExit(f"VERSION is not stable semantic versioning: {value!r}")
    return value


def manifest_paths() -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for relative in RUNTIME_MANIFEST:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"runtime manifest entry is missing or unsafe: {relative}")
        entries.append((relative, path))
    return entries


def archive_bytes() -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            root_info = tarfile.TarInfo(PACKAGE_ROOT)
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            root_info.mtime = root_info.uid = root_info.gid = 0
            root_info.uname = root_info.gname = ""
            archive.addfile(root_info)
            created_directories = {PACKAGE_ROOT}
            for relative, path in manifest_paths():
                parent = Path(PACKAGE_ROOT, relative).parent
                missing: list[Path] = []
                while parent.as_posix() not in created_directories:
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    info = tarfile.TarInfo(directory.as_posix())
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info)
                    created_directories.add(directory.as_posix())
                data = path.read_bytes()
                info = tarfile.TarInfo(f"{PACKAGE_ROOT}/{relative}")
                info.size = len(data)
                info.mode = 0o755 if relative in EXECUTABLE_PATHS else 0o644
                info.mtime = info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def verify_archive(payload: bytes) -> None:
    expected = {f"{PACKAGE_ROOT}/{relative}" for relative in RUNTIME_MANIFEST}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        files = {member.name for member in members if member.isfile()}
        if files != expected:
            missing = sorted(expected - files)
            extra = sorted(files - expected)
            raise SystemExit(f"release archive manifest mismatch: missing={missing}, extra={extra}")
        for member in members:
            if member.issym() or member.islnk() or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SystemExit(f"unsafe release archive member: {member.name}")
            if member.uid or member.gid or member.mtime:
                raise SystemExit(f"nondeterministic release archive metadata: {member.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    release_version = version()
    payload = archive_bytes()
    verify_archive(payload)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"grounded-build-v{release_version}.tar.gz"
    checksum = archive.with_name(f"{archive.name}.sha256")
    archive.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"archive={archive}")
    print(f"sha256={digest}")
    print(f"checksum={checksum}")


if __name__ == "__main__":
    main()
