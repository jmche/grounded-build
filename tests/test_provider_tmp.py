from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class ProviderTemporaryDirectoryTest(unittest.TestCase):
    def test_long_invocation_paths_support_private_unix_sockets(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "gb_tmp_planning", ROOT / "scripts" / "plan_workflow.py")
        planning = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planning)
        spec = importlib.util.spec_from_file_location(
            "gb_tmp_implementation", ROOT / "scripts" / "workflow.py")
        implementation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(implementation)

        with tempfile.TemporaryDirectory(prefix="gb-tmp-test-") as scratch:
            root = Path(scratch)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            context = root / "context"
            context.mkdir()
            host_canary = root / "host-only.txt"
            host_canary.write_text("host temporary data", encoding="utf-8")
            executable = root / "claude"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, socket, sys\n"
                "from pathlib import Path\n"
                "tmp = Path(os.environ['TMPDIR'])\n"
                "path = tmp / 'claude-http-0123456789abcdef.sock'\n"
                "with socket.socket(socket.AF_UNIX) as listener:\n"
                "    listener.bind(str(path))\n"
                "try:\n"
                "    Path('forbidden-write.txt').write_text('unexpected')\n"
                "except OSError:\n"
                "    readonly = True\n"
                "else:\n"
                "    readonly = False\n"
                "proof = {'socket_path': str(path), 'readonly': readonly,\n"
                "         'host_canary_visible': Path(sys.argv[-1]).exists()}\n"
                "(tmp / 'socket-proof.json').write_text(json.dumps(proof))\n"
                "print(json.dumps(proof))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": f"{root}:{os.environ['PATH']}"}), \
                    mock.patch.object(planning, "provider_trust_store", return_value=[]), \
                    mock.patch.object(implementation, "planning_isolation_module", return_value=planning):
                for mode in ("planning", "implementation-review"):
                    with self.subTest(mode=mode):
                        invocation = root / ("long-invocation-" + "x" * 120) / mode
                        command = ["claude", "--disallowedTools", "Edit,Write", str(host_canary)]
                        if mode == "planning":
                            wrapped = planning.isolated_agent_command(
                                command, {}, invocation, workspace, context)
                            environment = planning.agent_environment()
                        else:
                            wrapped, environment = implementation.isolated_reviewer_command(
                                command, "claude", {}, invocation, workspace, context)
                        self.assertGreater(len(os.fsencode(invocation / "tmp")), 144)
                        result = subprocess.run(
                            wrapped, env=environment, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        proof = json.loads(result.stdout)
                        self.assertLess(len(os.fsencode(proof["socket_path"])), 108)
                        self.assertTrue(proof["readonly"])
                        self.assertFalse(proof["host_canary_visible"])
                        self.assertEqual(
                            json.loads((invocation / "tmp" / "socket-proof.json").read_text()), proof)
                        self.assertEqual((invocation / "tmp").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
