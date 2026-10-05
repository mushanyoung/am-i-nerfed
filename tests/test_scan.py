"""Offline scan orchestration tests; discovery and every probe are mocked."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from am_i_nerfed import cli, discovery, reports, scan


CATALOGS = {
    "claude": {"models": ["claude-one", "claude-two"], "source": "claude.sdk.initialize.models",
               "warnings": [], "catalog": {}},
    "codex": {"models": ["gpt-one", "gpt-two"], "source": "codex.app-server.model/list",
              "warnings": [], "catalog": {
                  "gpt-one": {"efforts": ["medium", "high"], "default_effort": "high"},
                  "gpt-two": {"efforts": ["low", "high"], "default_effort": "low"}}},
}


class ScanTests(unittest.TestCase):
    def invoke(self, arguments=(), behaviors=None, installed=None, catalogs=None):
        """Return captured artifacts; never retain temporary provider output."""
        calls = []
        behaviors = behaviors or {}
        if installed is None:
            installed = {"claude": "/fixture/claude", "codex": "/fixture/codex"}
        catalogs = CATALOGS if catalogs is None else catalogs

        def discover(provider, **kwargs):
            value = catalogs[provider]
            if isinstance(value, Exception):
                raise value
            return value

        def fake_probe(provider, argv):
            argv = list(argv)
            calls.append((provider, argv))
            model = argv[argv.index("--model") + 1]
            behavior = behaviors.get((provider, model), {})
            if isinstance(behavior, BaseException):
                raise behavior
            output = Path(argv[argv.index("--out") + 1])
            repeat = int(argv[argv.index("--repeat") + 1])
            effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
            rows = []
            for _ in range(behavior.get("count", repeat)):
                record = reports.row(provider, model, [model], [model],
                                     behavior.get("route", "MATCH"), effort_req=effort,
                                     effort_reported=effort, evidence="upstream-http",
                                     complete=behavior.get("complete", True))
                if behavior.get("control") is not None:
                    record["direct_control"] = behavior["control"]
                rows.append(record)
            if not behavior.get("missing_report"):
                content = behavior.get("raw_report") or json.dumps({"schema_version": 1, "records": rows})
                cli.private_write(output / "report.json", content)
            return behavior.get("status", 0)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            arguments = list(arguments)
            if "--dry-run" not in arguments:
                arguments += ["--out", str(output)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(discovery, "discover_tools", return_value=installed), \
                    mock.patch.object(discovery, "discover_models", side_effect=discover) as discover_call, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = scan.main(arguments, fake_probe, cli.private_write)
            files = {str(path.relative_to(output)): path.read_text() for path in output.rglob("*") if path.is_file()}
            modes = {str(path.relative_to(output)): path.stat().st_mode & 0o777
                     for path in output.rglob("*") if path.is_file()}
            return {"status": status, "calls": calls, "files": files, "modes": modes,
                    "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
                    "discovery_calls": discover_call.call_args_list}

    def test_default_tests_every_model_on_every_installed_tool(self):
        result = self.invoke()
        pairs = [(provider, args[args.index("--model") + 1]) for provider, args in result["calls"]]
        self.assertEqual(pairs, [("claude", "claude-one"), ("claude", "claude-two"),
                                 ("codex", "gpt-one"), ("codex", "gpt-two")])
        self.assertEqual(result["status"], 0)
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual(len(records), 4)
        self.assertTrue(all(row["complete"] for row in records))

    def test_default_codex_uses_native_cli_and_each_models_advertised_effort(self):
        result = self.invoke(["--tool", "codex"])
        self.assertEqual(len(result["calls"]), 2)
        for (_, args), expected in zip(result["calls"], ("high", "low")):
            self.assertIn("--via-codex", args)
            self.assertEqual(args[args.index("--effort") + 1], expected)

    def test_future_advertised_effort_is_not_silently_replaced(self):
        for info in ({"efforts": ["future"], "default_effort": "future"},
                     {"efforts": ["future"], "default_effort": None}):
            with self.subTest(info=info):
                entry = {"catalog": {"gpt-future": info}}
                self.assertEqual(scan.choose_effort("codex", "gpt-future", entry, None), "future")
                self.assertEqual(scan.choose_effort("codex", "gpt-future", entry, "high"), "high")

    def test_claude_keeps_default_effort_and_only_receives_claude_options(self):
        result = self.invoke(["--direct-control", "--save-raw", "--ignore-alias-overrides"])
        for provider, args in result["calls"]:
            for flag in ("--direct-control", "--save-raw", "--ignore-alias-overrides"):
                self.assertEqual(flag in args, provider == "claude")
            if provider == "claude":
                self.assertNotIn("--effort", args)

    def test_explicit_model_selection_exclusion_and_deduplication(self):
        result = self.invoke(["-m", "codex:gpt-one", "-m", "codex:gpt-one", "-m", "codex:gpt-two",
                              "--exclude-model", "codex:gpt-two"])
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(result["calls"][0][0], "codex")
        self.assertEqual(result["discovery_calls"][0].args, ("codex",))

    def test_explicit_model_not_in_catalog_still_runs(self):
        result = self.invoke(["-m", "claude:claude-historical"])
        self.assertEqual(result["status"], 0)
        self.assertIn("claude-historical", result["calls"][0][1])

    def test_dry_run_never_calls_provider_or_writes_output(self):
        result = self.invoke(["--dry-run", "--include-hidden"])
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["files"], {})
        planned = json.loads(result["stdout"])
        self.assertEqual(len(planned["plan"]), 4)
        self.assertTrue(all(call.kwargs["include_hidden"] for call in result["discovery_calls"]))

    def test_http_codex_override_and_effort_are_forwarded(self):
        result = self.invoke(["--tool", "codex", "--codex-transport", "http", "--effort", "medium"])
        for _, args in result["calls"]:
            self.assertNotIn("--via-codex", args)
            self.assertEqual(args[args.index("--effort") + 1], "medium")

    def test_runtime_and_parser_failure_continue_remaining_models(self):
        result = self.invoke(behaviors={
            ("claude", "claude-one"): RuntimeError("SECRET user@example.test"),
            ("codex", "gpt-one"): SystemExit("SECRET user@example.test"),
        })
        self.assertEqual(len(result["calls"]), 4)
        self.assertEqual(result["status"], 2)
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual([row["route_status"] for row in records], ["UNKNOWN", "MATCH", "UNKNOWN", "MATCH"])
        self.assertNotIn("SECRET", result["stdout"] + result["stderr"] + result["files"]["share.json"])

    def test_missing_and_malformed_reports_are_unknown_and_do_not_stop_scan(self):
        result = self.invoke(behaviors={
            ("claude", "claude-one"): {"missing_report": True},
            ("claude", "claude-two"): {"raw_report": '{"secret":PRIVATE}'},
        })
        self.assertEqual(len(result["calls"]), 4)
        self.assertEqual(result["status"], 2)
        records = json.loads(result["files"]["share.json"])["records"]
        self.assertEqual([row["route_status"] for row in records], ["UNKNOWN", "UNKNOWN", "MATCH", "MATCH"])
        self.assertNotIn("PRIVATE", result["stderr"] + result["files"]["share.json"])

    def test_repetitions_missing_results_are_filled_as_unknown(self):
        result = self.invoke(["--tool", "claude", "--repeat", "3"],
                             behaviors={("claude", "claude-one"): {"count": 1}})
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual(len(records), 6)
        self.assertEqual(sum(row["complete"] for row in records), 4)
        self.assertEqual(result["status"], 2)

    def test_no_clients_and_empty_selection_make_no_inference_calls(self):
        for kwargs in ({"installed": {}}, {"arguments": ["--tool", "claude", "--exclude-model", "claude:claude-one",
                                                          "--exclude-model", "claude:claude-two"]}):
            result = self.invoke(**kwargs)
            self.assertEqual(result["status"], 1)
            self.assertEqual(result["calls"], [])
            self.assertEqual(result["files"], {})

    def test_one_failed_discovery_does_not_hide_other_client(self):
        catalogs = dict(CATALOGS, claude=RuntimeError("SECRET AUTH"))
        result = self.invoke(catalogs=catalogs)
        self.assertEqual(len(result["calls"]), 2)
        self.assertTrue(all(provider == "codex" for provider, _ in result["calls"]))
        self.assertEqual(result["status"], 2)
        self.assertNotIn("SECRET", result["stdout"] + result["stderr"] + result["files"]["inventory.json"])

    def test_cache_discovery_cannot_be_reported_as_full_catalog_coverage(self):
        catalogs = dict(CATALOGS, codex=dict(CATALOGS["codex"], source="codex.models_cache"))
        result = self.invoke(["--tool", "codex"], catalogs=catalogs)
        self.assertEqual(result["status"], 2)
        coverage = json.loads(result["files"]["report.json"])["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["discovery_failures"], 1)

    def test_changed_routes_remain_complete_evidence_coverage(self):
        result = self.invoke(["--tool", "claude"], behaviors={
            ("claude", "claude-one"): {"route": "CHANGED", "status": 2},
        })
        self.assertEqual(result["status"], 2)
        coverage = json.loads(result["files"]["report.json"])["coverage"]
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["complete_probes"], 2)
        self.assertEqual(coverage["unsuccessful_runs"], 1)

    def test_requested_but_missing_direct_control_makes_coverage_incomplete(self):
        result = self.invoke(["--tool", "claude", "--direct-control"])
        coverage = json.loads(result["files"]["report.json"])["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["complete_probes"], 0)
        self.assertEqual(result["status"], 2)

    def test_completed_differing_direct_control_is_complete_coverage(self):
        controls = {("claude", model): {"status": 2, "control": {
            "reported_models": ["claude-other"], "complete": True, "agrees": False}}
            for model in ("claude-one", "claude-two")}
        result = self.invoke(["--tool", "claude", "--direct-control"], behaviors=controls)
        coverage = json.loads(result["files"]["report.json"])["coverage"]
        self.assertTrue(coverage["complete"])
        self.assertEqual(result["status"], 2)

    def test_differing_control_cannot_return_zero_even_if_provider_does(self):
        controls = {("claude", model): {"status": 0, "control": {
            "reported_models": ["claude-other"], "complete": True, "agrees": False}}
            for model in ("claude-one", "claude-two")}
        result = self.invoke(["--tool", "claude", "--direct-control"], behaviors=controls)
        self.assertTrue(json.loads(result["files"]["report.json"])["coverage"]["complete"])
        self.assertEqual(result["status"], 2)

    def test_mixed_scan_and_provider_export_retains_incomplete_coverage(self):
        partial_scan = self.invoke(["--tool", "claude", "--direct-control"])
        provider = reports.sanitize_public({"records": [
            reports.row("codex", "gpt-one", ["gpt-one"], ["gpt-one"], "MATCH", complete=True)]})
        with tempfile.TemporaryDirectory() as folder:
            scan_file, provider_file = Path(folder) / "scan.json", Path(folder) / "provider.json"
            scan_file.write_text(partial_scan["files"]["report.json"])
            provider_file.write_text(json.dumps(provider))
            merged = reports.load_reports([scan_file, provider_file])
        self.assertIn("coverage", merged)
        self.assertFalse(merged["coverage"]["complete"])
        self.assertEqual(merged["coverage"]["planned_probes"], 3)
        self.assertEqual(merged["coverage"]["recorded_probes"], 3)
        self.assertEqual(merged["coverage"]["complete_probes"], 1)
        self.assertIsNone(merged["coverage"]["discovery_failures"])
        self.assertIn("INCOMPLETE", reports.markdown(merged))

    def test_reports_are_created_private(self):
        result = self.invoke(["--tool", "claude"])
        self.assertTrue(all(mode == 0o600 for mode in result["modes"].values()))

    def test_existing_output_refused_before_discovery(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(scan, "inventory") as inventory:
            with self.assertRaisesRegex(ValueError, "already exists"):
                scan.main(["--out", folder], mock.Mock(), cli.private_write)
        inventory.assert_not_called()

    def test_invalid_filter_or_numeric_bounds_fail_before_discovery(self):
        cases = (["--repeat", "0"], ["--timeout", "nan"], ["--discovery-timeout", "inf"],
                 ["--tool", "claude", "-m", "codex:gpt-one"], ["-m", "unknown:model"],
                 ["-m", "claude:path/to/model"])
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch.object(scan, "inventory") as inventory, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                scan.main(arguments, mock.Mock(), cli.private_write)
            self.assertEqual(error.exception.code, 2)
            inventory.assert_not_called()


class ScanCliDispatchTests(unittest.TestCase):
    def test_no_subcommand_defaults_to_scan(self):
        with mock.patch.object(scan, "main", return_value=0) as runner:
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(runner.call_args.args[0], [])
        self.assertFalse(runner.call_args.kwargs["inventory_only"])

    def test_flags_without_subcommand_and_models_dispatch(self):
        with mock.patch.object(scan, "main", return_value=0) as runner:
            cli.main(["--dry-run", "--tool", "claude"])
            self.assertEqual(runner.call_args.args[0], ["--dry-run", "--tool", "claude"])
            cli.main(["models", "--tool", "codex"])
            self.assertEqual(runner.call_args.args[0], ["--tool", "codex"])
            self.assertTrue(runner.call_args.kwargs["inventory_only"])


if __name__ == "__main__":
    unittest.main()
