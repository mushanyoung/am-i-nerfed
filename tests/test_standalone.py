"""Exercise the generated one-file distribution outside the checkout, offline."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "am-i-nerfed.py"
BUILDER = ROOT / "scripts" / "build_standalone.py"


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="standalone-test-")
        self.addCleanup(self.sandbox.cleanup)
        base = Path(self.sandbox.name)
        self.cwd = base / "working directory with spaces"
        self.temp_root = base / "private runtime temp"
        self.home = base / "empty home"
        for folder in (self.cwd, self.temp_root, self.home):
            folder.mkdir()
        self.script = base / "copied standalone.py"
        shutil.copyfile(RUNNER, self.script)
        # No credentials or executable search path from the developer's shell.
        # -I additionally ignores PYTHONPATH and the script's containing directory.
        self.env = {"PATH": os.defpath, "HOME": str(self.home),
                    "TMPDIR": str(self.temp_root), "TMP": str(self.temp_root),
                    "TEMP": str(self.temp_root), "PYTHONPATH": str(base / "unavailable package path")}
        if os.name == "nt" and os.environ.get("SYSTEMROOT"):
            self.env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

    def run_script(self, arguments, pipe=False):
        command = [sys.executable, "-I", "-S", "-" if pipe else str(self.script)] + list(arguments)
        return subprocess.run(command, input=self.script.read_text() if pipe else None,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=self.cwd, env=self.env, timeout=20)

    def assert_clean_runtime(self):
        self.assertEqual(list(self.temp_root.iterdir()), [], "standalone payload temp directory leaked")

    def test_isolated_copied_file_and_stdin_demo_are_identical(self):
        file_result = self.run_script(["demo", "--format", "json"])
        self.assertEqual(file_result.returncode, 0, file_result.stderr)
        self.assert_clean_runtime()
        pipe_result = self.run_script(["demo", "--format", "json"], pipe=True)
        self.assertEqual(pipe_result.returncode, 0, pipe_result.stderr)
        self.assertEqual(file_result.stdout, pipe_result.stdout)
        demo = json.loads(pipe_result.stdout)
        self.assertTrue(demo["synthetic"])
        self.assertEqual(demo["tool"], "Am I Nerfed?")
        self.assertTrue(demo["records"])
        self.assert_clean_runtime()

    def test_arguments_and_caller_cwd_preserve_relative_paths_with_spaces(self):
        for pipe, name in ((False, "file report.json"), (True, "pipe report.json")):
            with self.subTest(pipe=pipe):
                relative = Path("output folder") / name
                result = self.run_script(["demo", "--format", "json", "--output", str(relative)], pipe=pipe)
                self.assertEqual(result.returncode, 0, result.stderr)
                destination = self.cwd / relative
                self.assertTrue(destination.is_file())
                self.assertTrue(json.loads(destination.read_text())["synthetic"])
                if os.name == "posix":
                    self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                self.assert_clean_runtime()

    def test_nonzero_exit_status_and_cleanup_survive_file_and_pipe(self):
        for pipe in (False, True):
            with self.subTest(pipe=pipe):
                result = self.run_script(["not-a-command"], pipe=pipe)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("invalid choice", result.stderr)
                self.assert_clean_runtime()

    def test_payload_integrity_failure_stops_before_cli_and_leaves_no_temp(self):
        source = self.script.read_text()
        self.assertIn("_PAYLOAD_SHA256 = '", source)
        self.script.write_text(source.replace("_PAYLOAD_SHA256 = '", "_PAYLOAD_SHA256 = '0", 1))
        result = self.run_script(["demo", "--format", "json"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("integrity check failed", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_clean_runtime()

    def test_keyboard_interrupt_returns_130_and_cleans_temp(self):
        harness = textwrap.dedent("""\
            import builtins
            import runpy
            import sys
            runner = sys.argv[1]
            sys.argv = [runner]
            original_import = builtins.__import__
            def interrupted(argv=None):
                raise KeyboardInterrupt
            def hooked_import(name, *args, **kwargs):
                module = original_import(name, *args, **kwargs)
                if name == 'am_i_nerfed.cli':
                    module.main = interrupted
                return module
            builtins.__import__ = hooked_import
            runpy.run_path(runner, run_name='__main__')
            """)
        result = subprocess.run([sys.executable, "-I", "-S", "-c", harness, str(self.script)],
                                cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertIn("Interrupted", result.stderr)
        self.assert_clean_runtime()

    def test_builder_check_is_current_and_does_not_rewrite_runner(self):
        before = (RUNNER.stat().st_mtime_ns, hashlib.sha256(RUNNER.read_bytes()).hexdigest())
        result = subprocess.run([sys.executable, "-I", "-S", str(BUILDER), "--check"],
                                cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = (RUNNER.stat().st_mtime_ns, hashlib.sha256(RUNNER.read_bytes()).hexdigest())
        self.assertEqual(before, after)
        self.assert_clean_runtime()

    def test_no_arguments_still_scans_all_discovered_tools_and_models(self):
        # Only the discovery/collector boundaries are replaced. The embedded CLI,
        # scan orchestration, reports, argument routing and runner remain real.
        harness = self.cwd / "offline harness.py"
        harness.write_text(textwrap.dedent("""\
            import builtins
            import importlib
            import json
            from pathlib import Path
            import runpy
            import sys

            runner = sys.argv[1]
            sys.argv = [runner]
            original_import = builtins.__import__

            def hooked_import(name, *args, **kwargs):
                module = original_import(name, *args, **kwargs)
                if name == 'am_i_nerfed.cli':
                    discovery = importlib.import_module('am_i_nerfed.discovery')
                    reports = importlib.import_module('am_i_nerfed.reports')
                    discovery.discover_tools = lambda: {'claude': '/fake/claude', 'codex': '/fake/codex'}
                    def discover_models(provider, **kwargs):
                        names = ['claude-one', 'claude-two'] if provider == 'claude' else ['gpt-one', 'gpt-two']
                        return {'models': names, 'source': 'offline.fixture', 'warnings': [], 'catalog': {}}
                    discovery.discover_models = discover_models
                    def probe(provider, argv):
                        model = argv[argv.index('--model') + 1]
                        out = Path(argv[argv.index('--out') + 1])
                        out.mkdir(parents=True)
                        record = reports.row(provider, model, [model], [model], 'MATCH', complete=True)
                        (out / 'report.json').write_text(json.dumps({'schema_version': 1, 'records': [record]}))
                        print('OFFLINE_PROBE=' + provider + ':' + model)
                        return 0
                    module._probe = probe
                return module

            builtins.__import__ = hooked_import
            runpy.run_path(runner, run_name='__main__')
            """))
        result = subprocess.run([sys.executable, "-I", "-S", str(harness), str(self.script)],
                                cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line.removeprefix("OFFLINE_PROBE=") for line in result.stdout.splitlines()
                 if line.startswith("OFFLINE_PROBE=")]
        self.assertEqual(calls, ["claude:claude-one", "claude:claude-two", "codex:gpt-one", "codex:gpt-two"])
        scan_reports = list((self.cwd / "runs").glob("scan-*/report.json"))
        self.assertEqual(len(scan_reports), 1)
        summary = json.loads(scan_reports[0].read_text())
        self.assertEqual(summary["coverage"]["planned_probes"], 4)
        self.assertTrue(summary["coverage"]["complete"])
        self.assert_clean_runtime()


if __name__ == "__main__":
    unittest.main()
