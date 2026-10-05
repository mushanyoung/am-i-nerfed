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
            backend = "codex-cli" if "--via-codex" in argv else "codex-http" if provider == "codex" else "claude-proxy"
            behavior = behaviors.get((provider, model, backend), behaviors.get((provider, model), {}))
            if isinstance(behavior, BaseException):
                raise behavior
            output = Path(argv[argv.index("--out") + 1])
            repeat = int(argv[argv.index("--repeat") + 1])
            effort = argv[argv.index("--effort") + 1] if "--effort" in argv else None
            rows = []
            for _ in range(behavior.get("count", repeat)):
                record = reports.row(provider, model, [model], [model],
                                     behavior.get("route", "MATCH"), effort_req=effort,
                                     effort_reported=effort, evidence="cli-trace" if backend == "codex-cli" else "upstream-http",
                                     complete=behavior.get("complete", True))
                if "control" in behavior:
                    if behavior["control"] is not None:
                        record["direct_control"] = behavior["control"]
                elif "--direct-control" in argv:
                    record["direct_control"] = {"reported_models": [model], "agrees": True, "complete": True}
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
                                 ("codex", "gpt-one"), ("codex", "gpt-one"),
                                 ("codex", "gpt-two"), ("codex", "gpt-two")])
        self.assertEqual(result["status"], 0)
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual(len(records), 6)
        self.assertTrue(all(row["complete"] for row in records))
        self.assertEqual([row["backend"] for row in records],
                         ["claude-proxy", "claude-proxy", "codex-cli", "codex-http", "codex-cli", "codex-http"])
        self.assertTrue(all(row["direct_control"]["backend"] == "claude-direct" for row in records[:2]))
        self.assertIn("6 primary probe(s) + 2 Claude direct control(s)", result["stdout"])
        targets = [Path(argv[argv.index("--out") + 1]).name for _, argv in result["calls"]]
        self.assertEqual(targets, ["claude-proxy-001", "claude-proxy-002", "codex-cli-003",
                                  "codex-http-004", "codex-cli-005", "codex-http-006"])

    def test_default_codex_uses_both_backends_and_each_models_advertised_effort(self):
        result = self.invoke(["--tool", "codex"])
        self.assertEqual(len(result["calls"]), 4)
        for index, ((_, args), expected) in enumerate(zip(result["calls"], ("high", "high", "low", "low"))):
            self.assertEqual("--via-codex" in args, index % 2 == 0)
            self.assertEqual(args[args.index("--effort") + 1], expected)

    def test_each_codex_transport_can_be_selected_alone(self):
        for transport in ("cli", "http"):
            with self.subTest(transport=transport):
                result = self.invoke(["--tool", "codex", "--codex-transport", transport])
                self.assertEqual(result["status"], 0)
                self.assertEqual(len(result["calls"]), 2)
                self.assertTrue(all(("--via-codex" in argv) == (transport == "cli") for _, argv in result["calls"]))
                records = json.loads(result["files"]["report.json"])["records"]
                self.assertTrue(all(row["backend"] == "codex-" + transport for row in records))

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

    def test_no_direct_control_disables_extra_calls_and_coverage_requirement(self):
        result = self.invoke(["--tool", "claude", "--no-direct-control"])
        self.assertEqual(result["status"], 0)
        self.assertTrue(all("--direct-control" not in argv for _, argv in result["calls"]))
        data = json.loads(result["files"]["report.json"])
        self.assertTrue(data["coverage"]["complete"])
        self.assertTrue(all("direct_control" not in row for row in data["records"]))
        self.assertIn("2 primary probe(s) + 0 Claude direct control(s)", result["stdout"])
        dry_run = self.invoke(["--tool", "claude", "--no-direct-control", "--dry-run"])
        self.assertTrue(all(item["direct_control"] is False for item in json.loads(dry_run["stdout"])["plan"]))

    def test_default_console_has_one_result_per_backend_without_catalog_details(self):
        catalogs = dict(CATALOGS, claude=dict(CATALOGS["claude"], warnings=["catalog_warning_fixture"]))
        result = self.invoke(catalogs=catalogs)
        self.assertEqual(len(result["stdout"].splitlines()), 8)  # Scope, six results, summary.
        self.assertEqual(sum(line.startswith("[") for line in result["stdout"].splitlines()), 6)
        self.assertNotIn("catalog_warning_fixture", result["stdout"] + result["stderr"])
        self.assertNotIn("candidates via", result["stdout"])
        self.assertNotIn("\033", result["stdout"])
        self.assertTrue(all("--quiet" in argv and "--verbose" not in argv for _, argv in result["calls"]))

    def test_verbose_prints_catalog_details_and_requests_provider_diagnostics(self):
        catalogs = dict(CATALOGS, claude=dict(CATALOGS["claude"], warnings=["catalog_warning_fixture"]))
        for flag in ("-v", "--verbose"):
            with self.subTest(flag=flag):
                result = self.invoke([flag], catalogs=catalogs)
                self.assertIn("catalog_warning_fixture", result["stdout"])
                self.assertIn("candidates via", result["stdout"])
                self.assertTrue(all("--verbose" in argv and "--quiet" not in argv for _, argv in result["calls"]))
                self.assertEqual(sum(line.startswith("[") for line in result["stdout"].splitlines()), 6)

    def test_one_codex_backend_failure_does_not_skip_other_backend_or_model(self):
        result = self.invoke(["--tool", "codex"], behaviors={
            ("codex", "gpt-one", "codex-cli"): RuntimeError("PRIVATE exception text")})
        self.assertEqual(result["status"], 2)
        self.assertEqual(len(result["calls"]), 4)
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual([row["route_status"] for row in records], ["UNKNOWN", "MATCH", "MATCH", "MATCH"])
        self.assertEqual(records[0]["backend"], "codex-cli")
        self.assertEqual(records[1]["backend"], "codex-http")
        self.assertNotIn("PRIVATE", result["stdout"] + result["stderr"])

    def test_repeated_scans_count_extra_direct_controls_in_startup(self):
        result = self.invoke(["--repeat", "3"])
        self.assertIn("18 primary probe(s) + 6 Claude direct control(s)", result["stdout"])

    def test_explicit_model_selection_exclusion_and_deduplication(self):
        result = self.invoke(["-m", "codex:gpt-one", "-m", "codex:gpt-one", "-m", "codex:gpt-two",
                              "--exclude-model", "codex:gpt-two"])
        self.assertEqual(len(result["calls"]), 2)
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
        self.assertEqual(len(planned["plan"]), 6)
        self.assertTrue(all(item["direct_control"] == (item["backend"] == "claude-proxy") for item in planned["plan"]))
        self.assertEqual([item["backend"] for item in planned["plan"]],
                         ["claude-proxy", "claude-proxy", "codex-cli", "codex-http", "codex-cli", "codex-http"])
        self.assertNotIn("\033", result["stdout"])
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
        self.assertEqual(len(result["calls"]), 6)
        self.assertEqual(result["status"], 2)
        records = json.loads(result["files"]["report.json"])["records"]
        self.assertEqual([row["route_status"] for row in records], ["UNKNOWN", "MATCH", "UNKNOWN", "UNKNOWN", "MATCH", "MATCH"])
        self.assertEqual([row["backend"] for row in records if row["route_status"] == "UNKNOWN"],
                         ["claude-proxy", "codex-cli", "codex-http"])
        self.assertNotIn("SECRET", result["stdout"] + result["stderr"] + result["files"]["share.json"])

    def test_missing_and_malformed_reports_are_unknown_and_do_not_stop_scan(self):
        result = self.invoke(behaviors={
            ("claude", "claude-one"): {"missing_report": True},
            ("claude", "claude-two"): {"raw_report": '{"secret":PRIVATE}'},
        })
        self.assertEqual(len(result["calls"]), 6)
        self.assertEqual(result["status"], 2)
        records = json.loads(result["files"]["share.json"])["records"]
        self.assertEqual([row["route_status"] for row in records], ["UNKNOWN", "UNKNOWN", "MATCH", "MATCH", "MATCH", "MATCH"])
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
        self.assertEqual(len(result["calls"]), 4)
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
        result = self.invoke(["--tool", "claude"], behaviors={
            ("claude", model): {"control": None} for model in ("claude-one", "claude-two")})
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

    def test_successful_control_with_incomplete_proxy_is_unknown_not_changed(self):
        for complete, reported in ((False, ["claude-one"]), (True, [])):
            with self.subTest(proxy_complete=complete, proxy_reported=reported):
                record = reports.row("claude", "claude-one", ["claude-one"], reported,
                                     "UNKNOWN", complete=complete)
                record["direct_control"] = {"reported_models": ["claude-one"], "complete": True, "agrees": False}
                item = {"backend": "claude-proxy", "model": "claude-one", "repeat": 1, "direct_control": True}
                verdict = scan.result_status([record], 2)
                self.assertEqual(verdict, "UNKNOWN")
                self.assertIn("direct=UNKNOWN", scan.result_line(1, 1, item, [record], verdict))
                self.assertNotIn("CHANGED", scan.result_line(1, 1, item, [record], verdict))
                result = self.invoke(["--tool", "claude", "-m", "claude:claude-one"], behaviors={
                    ("claude", "claude-one"): {"status": 2, "raw_report": json.dumps({"schema_version": 1, "records": [record]})}})
                self.assertEqual(result["status"], 2)
                self.assertIn("direct=UNKNOWN", result["stdout"])
                self.assertNotIn("CHANGED", result["stdout"])

    def test_direct_internal_route_change_is_changed_even_when_paths_agree(self):
        for proxy_complete in (True, False):
            with self.subTest(proxy_complete=proxy_complete):
                control = {"reported_models": ["claude-one"], "complete": True,
                           "agrees": True, "route_status": "CHANGED"}
                record = reports.row("claude", "claude-one", ["claude-one"], ["claude-one"],
                                     "MATCH" if proxy_complete else "UNKNOWN", complete=proxy_complete)
                record["direct_control"] = control
                item = {"backend": "claude-proxy", "model": "claude-one", "repeat": 1, "direct_control": True}
                verdict = scan.result_status([record], 0)
                self.assertEqual(verdict, "CHANGED")
                self.assertIn("direct=CHANGED", scan.result_line(1, 1, item, [record], verdict))
                result = self.invoke(["--tool", "claude", "-m", "claude:claude-one"], behaviors={
                    ("claude", "claude-one"): {"status": 0, "raw_report": json.dumps({"schema_version": 1, "records": [record]})}})
                self.assertEqual(result["status"], 2)
                self.assertIn("direct=CHANGED", result["stdout"])
                self.assertEqual(json.loads(result["files"]["report.json"])["records"][0]["direct_control"]["route_status"], "CHANGED")

    def test_mixed_scan_and_provider_export_retains_incomplete_coverage(self):
        partial_scan = self.invoke(["--tool", "claude"], behaviors={
            ("claude", model): {"control": None} for model in ("claude-one", "claude-two")})
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

    def test_default_output_uses_runtime_private_location(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "private" / "scan-fixture"
            with mock.patch.object(scan.runtime, "default_output", return_value=output) as default, \
                    mock.patch.object(scan, "inventory", return_value={"tools": {}, "failures": []}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = scan.main(["-m", "codex:gpt-fixture", "--codex-transport", "http"],
                                   mock.Mock(return_value=1), cli.private_write)
            self.assertEqual(status, 2)
            default.assert_called_once_with("scan")
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue((output / "report.json").is_file())

    def test_inventory_and_verbose_dry_run_are_plain_json(self):
        with mock.patch.object(scan, "inventory", return_value={"tools": CATALOGS, "failures": []}), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                mock.patch.object(scan.runtime, "Console", side_effect=AssertionError("JSON must not use Console")):
            self.assertEqual(scan.main(["--verbose"], mock.Mock(), cli.private_write, inventory_only=True), 0)
            self.assertEqual(json.loads(output.getvalue())["tools"], CATALOGS)
        result = self.invoke(["--verbose", "--dry-run"])
        self.assertEqual(len(json.loads(result["stdout"])["plan"]), 6)

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
    def test_scan_quiet_wrapper_suppresses_provider_output_but_keeps_results(self):
        from am_i_nerfed.providers import codex

        def fake_main(argv):
            print("provider diagnostic fixture")
            output = Path(argv[argv.index("--json-out") + 1])
            evidence = "cli-trace" if "--via-codex" in argv else "upstream-http"
            record = reports.row("codex", "gpt-one", ["gpt-one"], ["gpt-one"], "MATCH",
                                 "high", "high", evidence=evidence, complete=True)
            cli.private_write(output, json.dumps({"schema_version": 1, "records": [record]}))
            return 0

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(scan, "inventory", return_value={"tools": {"codex": CATALOGS["codex"]}, "failures": []}), \
                mock.patch.object(codex, "main", side_effect=fake_main), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            status = scan.main(["-m", "codex:gpt-one", "--out", str(Path(folder) / "out")],
                               cli._probe, cli.private_write)
        self.assertEqual(status, 0)
        self.assertNotIn("provider diagnostic fixture", output.getvalue())
        self.assertNotIn("Report:", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 4)
        self.assertIn("codex-cli gpt-one", output.getvalue())
        self.assertIn("codex-http gpt-one", output.getvalue())

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
