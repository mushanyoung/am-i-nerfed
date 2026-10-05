import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from am_i_nerfed import runtime


class Terminal(io.StringIO):
    def isatty(self):
        return True


class OutputDirectoryTests(unittest.TestCase):
    def test_linux_uses_home_and_other_platforms_use_relative_runs(self):
        with mock.patch.object(runtime.Path, "home", return_value=Path("/fixture/home")):
            for platform, parent in (("linux", Path("/fixture/home/.am-i-nerfed")),
                                     ("linux2", Path("/fixture/home/.am-i-nerfed")),
                                     ("darwin", Path("runs")), ("win32", Path("runs"))):
                with self.subTest(platform=platform), mock.patch.object(runtime.sys, "platform", platform):
                    path = runtime.default_output("scan")
                    self.assertEqual(path.parent, parent)
                    self.assertRegex(path.name, r"^scan-\d{8}-\d{6}-\d{6}$")

    @unittest.skipUnless(os.name == "posix", "POSIX file mode assertions")
    def test_missing_parents_are_private_without_changing_existing_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            base.chmod(0o755)
            target = base / "private parent" / "child"
            old_umask = os.umask(0)
            try:
                self.assertEqual(runtime.private_mkdir(target), target)
            finally:
                os.umask(old_umask)
            self.assertEqual(base.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_existing_paths_require_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "output"
            runtime.private_mkdir(target)
            with self.assertRaises(FileExistsError):
                runtime.private_mkdir(target)
            self.assertEqual(runtime.private_mkdir(target, exist_ok=True), target)
            file_path = Path(folder) / "file"
            file_path.write_text("fixture")
            with self.assertRaises(FileExistsError):
                runtime.private_mkdir(file_path, exist_ok=True)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink fixture")
    def test_existing_target_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / "link"
            link.symlink_to(folder, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                runtime.private_mkdir(link, exist_ok=True)


class ConsoleTests(unittest.TestCase):
    def test_redirected_output_is_plain_and_details_are_opt_in(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"TERM": "xterm", "FORCE_COLOR": "1"}, clear=True):
            console = runtime.Console(stdout=out, stderr=err)
            console.info("ready")
            console.detail("hidden metadata")
            console.result("matched", "MATCH")
            console.warning("warning")
            console.error("error")
        self.assertEqual(out.getvalue(), "ready\nmatched\n")
        self.assertEqual(err.getvalue(), "warning\nerror\n")
        runtime.Console(verbose=True, stdout=out).detail("visible metadata")
        self.assertTrue(out.getvalue().endswith("visible metadata\n"))

    def test_terminal_color_map_and_independent_stderr_detection(self):
        out, err = Terminal(), io.StringIO()
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            console = runtime.Console(stdout=out, stderr=err)
            for status, code in (("MATCH", "32"), ("CHANGED", "31"), ("UNKNOWN", "33")):
                self.assertEqual(console.style("result", status), "\033[" + code + "mresult\033[0m")
            console.result("matched", "MATCH")
            console.warning("warning")
            self.assertEqual(console.style("plain", "unrecognized"), "plain")
            reverse = runtime.Console(stdout=io.StringIO(), stderr=Terminal())
            reverse.error("error")
            self.assertEqual(reverse.stderr.getvalue(), "\033[31merror\033[0m\n")
        self.assertEqual(out.getvalue(), "\033[32mmatched\033[0m\n")
        self.assertEqual(err.getvalue(), "warning\n")

    def test_no_color_and_dumb_terminal_disable_color(self):
        for env in ({"TERM": "dumb"}, {"TERM": "DUMB"}, {"NO_COLOR": "1"}, {"NO_COLOR": "0"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(runtime.Console(stdout=Terminal()).style("match", "MATCH"), "match")
        with mock.patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm"}, clear=True):
            self.assertIn("\033[32m", runtime.Console(stdout=Terminal()).style("match", "MATCH"))

    def test_redirect_context_is_resolved_when_console_is_created(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            console = runtime.Console()
            console.info("stdout")
            console.warning("stderr")
        self.assertEqual(out.getvalue(), "stdout\n")
        self.assertEqual(err.getvalue(), "stderr\n")


if __name__ == "__main__":
    unittest.main()
