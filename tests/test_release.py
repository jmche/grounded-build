from __future__ import annotations

import hashlib
import importlib.util
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "package_release.py"
SPEC = importlib.util.spec_from_file_location("grounded_build_package_release", SCRIPT)
PACKAGE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PACKAGE)


class ReleasePackageTests(unittest.TestCase):
    def test_archive_is_deterministic_and_contains_only_runtime_manifest(self) -> None:
        first = PACKAGE.archive_bytes()
        second = PACKAGE.archive_bytes()
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        PACKAGE.verify_archive(first)
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
            files = {member.name for member in archive.getmembers() if member.isfile()}
        self.assertEqual(
            files,
            {f"grounded-build/{relative}" for relative in PACKAGE.RUNTIME_MANIFEST},
        )
        self.assertIn("grounded-build/tests/test_workflow.py", files)
        self.assertIn("grounded-build/references/causal_analysis.md", files)
        self.assertIn("grounded-build/.github/workflows/ci.yml", files)
        self.assertNotIn("grounded-build/AGENTS.md", files)
        self.assertFalse(any("__pycache__" in name or name.startswith("grounded-build/.git/") for name in files))

    def test_cli_writes_versioned_archive_and_matching_checksum(self) -> None:
        with tempfile.TemporaryDirectory(prefix="grounded-build-release-") as raw:
            output = Path(raw)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--output-dir", str(output)],
                check=True, capture_output=True, text=True,
            )
            archive = output / f"grounded-build-v{PACKAGE.version()}.tar.gz"
            checksum_file = archive.with_name(f"{archive.name}.sha256")
            self.assertTrue(archive.is_file() and checksum_file.is_file())
            digest, filename = checksum_file.read_text(encoding="utf-8").split()
            self.assertEqual(filename, archive.name)
            self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertIn(f"sha256={digest}", result.stdout)

    def test_public_beta_packager_rejects_a_major_version(self) -> None:
        original_root = PACKAGE.ROOT
        try:
            with tempfile.TemporaryDirectory(prefix="grounded-build-version-") as raw:
                PACKAGE.ROOT = Path(raw)
                (PACKAGE.ROOT / "VERSION").write_text("1.0.0\n", encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "remain below 1.0.0"):
                    PACKAGE.version()
        finally:
            PACKAGE.ROOT = original_root


if __name__ == "__main__":
    unittest.main()
