import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from am_i_nerfed import cli, reports


class PublicReportTests(unittest.TestCase):
    def test_private_fields_never_exported(self):
        raw = {"claude_version": "2.1.0", "email": "private@example.com", "token": "secret",
               "results": [{"requested": "haiku", "verdict": "MATCH", "captures": [{
                   "is_probe_prompt": True, "http_status": 200, "complete": True,
                   "request": {"model": "claude-example", "output_config": {"effort": "high"}},
                   "response_models": ["claude-example"], "raw_response_file": "/private/body",
                   "response_headers": {"authorization": "Bearer secret", "request-id": "req_private"},
                   "prompt": "private prompt", "errors": []}]}]}
        data = reports.normalize(raw)
        for form in ("json", "markdown", "svg"):
            output = reports.render(data, form)
            for secret in ("private@example.com", "secret", "req_private", "/private", "private prompt"):
                self.assertNotIn(secret, output)
        self.assertEqual(data["records"][0]["effort_status"], "NOT_REPORTED")

    def test_incomplete_claimed_match_is_unknown(self):
        raw = reports.demo()
        raw["records"][0]["complete"] = False
        raw["records"][0]["route_status"] = "MATCH"
        self.assertEqual(reports.normalize(raw)["records"][0]["route_status"], "UNKNOWN")

    def test_no_models_cannot_match(self):
        raw = reports.demo()
        raw["records"][0]["reported_models"] = []
        self.assertEqual(reports.normalize(raw)["records"][0]["route_status"], "UNKNOWN")

    def test_reimport_reapplies_allowlist(self):
        raw = reports.demo()
        raw["records"][0]["token"] = "do-not-export"
        raw["token"] = "also-private"
        out = reports.render(reports.normalize(raw), "json")
        self.assertNotIn("do-not-export", out)
        self.assertNotIn("also-private", out)

    def test_unsafe_model_labels_and_unknown_effort(self):
        for unsafe in ("user@example.com", "/Users/private", "sk-ant-token", "\x1b[0m", '<script>', "x" * 130):
            self.assertEqual(reports.label(unsafe), "[redacted]")
        self.assertIsNone(reports.effort_value("private-string"))
        self.assertEqual(reports.effort_status("high", "low"), "CHANGED")
        self.assertEqual(reports.effort_status(None, None), "NOT_REQUESTED")

    def test_svg_is_valid_and_demo_is_labeled(self):
        output = reports.render(reports.demo(), "svg")
        ET.fromstring(output)
        self.assertIn("SYNTHETIC DEMO", output)
        self.assertIn("UNKNOWN", output)
        self.assertIn("CHANGED", output)

    def test_refuse_unrecognized_report(self):
        for data in ([], {"results": []}, {"claude_version": "1", "results": "bad"}):
            with self.assertRaises(ValueError):
                reports.normalize(data)

    def test_codex_intermediate_model_and_effort_are_retained(self):
        r = {"path": "http", "requested": "gpt-example", "wire_model": "gpt-example",
             "wire_models": ["gpt-example"], "status": 200, "completion_seen": True,
             "final_status": "completed", "created_model": "gpt-example", "completed_model": "gpt-example",
             "models_seen": ["gpt-example", "gpt-other"], "effort": "high", "wire_effort": "high",
             "served_effort": "high", "efforts_seen": ["high", "low"], "route_verdict": "CHANGED"}
        normalized = reports.normalize({"codex_version": "1", "results": [r]})["records"][0]
        self.assertEqual(normalized["route_status"], "CHANGED")
        self.assertIn("gpt-other", normalized["reported_models"])
        self.assertEqual(normalized["effort_status"], "CHANGED")
        # Export and reimport must preserve the intermediate effort evidence.
        again = reports.normalize({"schema_version": 1, "records": [normalized]})["records"][0]
        self.assertEqual(again["effort_status"], "CHANGED")

    def test_codex_selection_change_is_not_hidden_by_wire_match(self):
        r = {"requested": "gpt-large", "wire_model": "gpt-small", "completed_model": "gpt-small",
             "route_verdict": "MATCH", "verdict": "REQUEST CHANGED", "completion_seen": True,
             "status": 200, "final_status": "completed"}
        normalized = reports.normalize({"codex_version": "1", "results": [r]})["records"][0]
        self.assertEqual(normalized["route_status"], "CHANGED")

    def test_claude_explicit_effort_change_is_retained(self):
        raw = {"claude_version": "2.1.0", "effort_override": "high", "results": [{
            "requested": "opus", "verdict": "MATCH", "captures": [{"is_probe_prompt": True,
            "http_status": 200, "complete": True, "request": {"model": "claude-example",
            "output_config": {"effort": "low"}}, "response_models": ["claude-example"]}]}]}
        r = reports.normalize(raw)["records"][0]
        self.assertEqual(r["requested_effort"], "high")
        self.assertEqual(r["wire_effort"], "low")
        self.assertEqual(r["effort_status"], "CHANGED")

    def test_claimed_match_with_contradictory_or_redacted_evidence(self):
        data = reports.demo()
        data["records"][0]["reported_models"] = ["claude-different"]
        self.assertEqual(reports.normalize(data)["records"][0]["route_status"], "CHANGED")
        data["records"][0]["reported_models"] = ["sk-private-token"]
        self.assertEqual(reports.normalize(data)["records"][0]["route_status"], "UNKNOWN")

    def test_snapshot_mapping_is_not_a_change(self):
        self.assertTrue(reports.same_model("gpt-example", "gpt-example-2026-01-01"))
        self.assertTrue(reports.same_model("claude-example", "claude-example-20261001"))
        self.assertFalse(reports.same_model("gpt-example", "gpt-example-mini-20261001"))
        self.assertTrue(reports.same_model("claude-example[1m]", "claude-example"))
        self.assertTrue(reports.same_model("claude-example[200k]", "claude-example"))

    def test_direct_control_disagreement_visible_in_all_exports(self):
        data = reports.demo()
        data["records"][0]["direct_control"] = {"reported_models": ["claude-other"], "agrees": False, "complete": True}
        for fmt in ("markdown", "svg"):
            output = reports.render(data, fmt)
            self.assertIn("DIFFERS", output)
            self.assertIn("claude-other", output)
        data["records"][0]["direct_control"]["complete"] = False
        self.assertIn("UNKNOWN: claude-other", reports.markdown(data))

    def test_codex_intermediate_wire_effort_survives(self):
        r = {"path": "codex", "requested": "gpt-example", "wire_model": "gpt-example",
             "wire_models": ["gpt-example"], "status": 200, "completion_seen": True,
             "final_status": "completed", "completed_model": "gpt-example", "route_verdict": "MATCH",
             "effort": "low", "wire_effort": "low", "wire_efforts": ["high", "low"],
             "served_effort": "low", "verdict": "EFFORT CHANGED"}
        r = reports.normalize({"codex_version": "1", "results": [r]})["records"][0]
        self.assertEqual(r["effort_status"], "CHANGED")
        self.assertEqual(r["wire_efforts"], ["high", "low"])


class OfflineCliTests(unittest.TestCase):
    def test_demo_works_without_network_or_auth(self):
        with patch("socket.socket", side_effect=AssertionError("unexpected network")), patch.dict(os.environ, {}, clear=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["demo", "--format", "json"]), 0)
        self.assertTrue(json.loads(output.getvalue())["synthetic"])

    def test_report_round_trip_and_private_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "input.json", Path(tmp) / "share.svg"
            source.write_text(reports.render(reports.demo(), "json"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["report", str(source), "--format", "svg", "-o", str(target)]), 0)
            ET.fromstring(target.read_text())
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o077, 0)
            with self.assertRaises(FileExistsError):
                cli.private_write(target, "overwrite")

    def test_bad_json_does_not_print_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invalid.json"
            source.write_text('{"token":super-private}')
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(cli.main(["report", str(source)]), 1)
            self.assertNotIn("super-private", errors.getvalue())

    def test_existing_output_directory_refused_before_probe(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["claude", "--out", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
