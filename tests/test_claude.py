"""Offline protocol and loopback tests; no provider or subscription requests."""

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from am_i_nerfed.providers import claude


MODEL = "claude-test-model"
OTHER = "claude-other-model"


def sse(*events):
    return "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e))
                   for e in events).encode()


def normal_events(model=MODEL):
    return [
        {"type": "message_start", "message": {"model": model, "role": "assistant"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]


def stdout(model=MODEL, error=False):
    return "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "model": model},
        {"type": "assistant", "message": {"model": model, "content": "PRIVATE OUTPUT"}},
        {"type": "result", "subtype": "success", "is_error": error},
    ])


class ResponseParsingTests(unittest.TestCase):
    def test_stream_and_json_complete(self):
        parsed = claude.parse_response(sse(*normal_events()), "text/event-stream")
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["response_models"], [MODEL])
        parsed = claude.parse_response(json.dumps({"type": "message", "role": "assistant",
            "model": MODEL, "content": [], "stop_reason": "end_turn"}).encode(), "application/json")
        self.assertTrue(parsed["complete"])

    def test_truncated_and_unframed_streams_are_incomplete(self):
        body = sse(*normal_events())
        for sample in (sse(*normal_events()[:-1]), body[:-2], body[:-5], b"data: {\n\n"):
            with self.subTest(body=sample):
                self.assertFalse(claude.parse_response(sample, "text/event-stream")["complete"])

    def test_terminal_stop_alone_or_before_start_is_not_complete(self):
        for events in ([normal_events()[-1]], list(reversed(normal_events())),
                       normal_events() + [normal_events()[0]]):
            self.assertFalse(claude.parse_response(sse(*events), "text/event-stream")["complete"])

    def test_missing_stop_reason_is_not_complete(self):
        parsed = claude.parse_response(sse(normal_events()[0], normal_events()[-1]), "text/event-stream")
        self.assertFalse(parsed["complete"])

    def test_errors_cannot_complete_and_error_text_is_private(self):
        events = normal_events() + [{"type": "error", "error": {
            "type": "overloaded_error", "message": "PRIVATE TOKEN user@example.test"}}]
        parsed = claude.parse_response(sse(*events), "text/event-stream")
        self.assertFalse(parsed["complete"])
        self.assertEqual(parsed["errors"][0]["type"], "overloaded_error")
        self.assertNotIn("PRIVATE", json.dumps(parsed))

    def test_content_and_recommended_models_are_not_serving_evidence(self):
        events = normal_events()
        events.insert(1, {"type": "content_block_start", "content_block": {
            "type": "text", "text": "I am another model", "model": OTHER}})
        events[0]["message"]["stop_details"] = {"recommended_model": OTHER}
        events[0]["message"]["metadata"] = {"model": OTHER}
        parsed = claude.parse_response(sse(*events), "text/event-stream")
        self.assertEqual(parsed["response_models"], [MODEL])
        self.assertNotIn(OTHER, json.dumps(parsed))

    def test_midstream_fallback_preserves_both_models_and_omits_text(self):
        events = normal_events()
        events.insert(1, {"type": "content_block_start", "content_block": {
            "type": "fallback", "from": {"model": MODEL, "private": "SECRET"},
            "to": {"model": OTHER}, "text": "SECRET"}})
        events[-2]["usage"]["iterations"] = [{"type": "fallback_message", "model": OTHER,
                                                "input_tokens": 123, "private": "SECRET"}]
        parsed = claude.parse_response(sse(*events), "text/event-stream")
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["response_models"], [MODEL, OTHER])
        self.assertEqual(parsed["fallback_events"][0]["to"]["model"], OTHER)
        self.assertNotIn("SECRET", json.dumps(parsed))

    def test_usage_only_fallback_is_detected(self):
        events = normal_events()
        events[-2]["usage"]["iterations"] = [{"type": "fallback_message", "model": OTHER}]
        self.assertEqual(claude.parse_response(sse(*events), "text/event-stream")["response_models"],
                         [MODEL, OTHER])

    def test_snapshot_matching_is_narrow_and_missing_is_unknown(self):
        self.assertEqual(claude.compatible(MODEL, MODEL + "-20260101"), "MATCH_SNAPSHOT")
        self.assertEqual(claude.compatible(MODEL, MODEL + "-lite"), "DIFFERENT")
        self.assertEqual(claude.compatible(None, None), "UNKNOWN")

    def test_malformed_json_types_do_not_crash(self):
        for payload in ([], None, {"type": []}, {"type": "message_delta", "usage": "bad"}):
            parsed = claude.parse_response(json.dumps(payload).encode(), "application/json")
            self.assertFalse(parsed["complete"])

    def test_cli_metadata_discards_content_and_error_details(self):
        output = stdout() + "\n" + json.dumps({"type": "result", "is_error": True,
            "errors": ["PRIVATE TOKEN"], "result": "PRIVATE TOKEN", "modelUsage": {
                MODEL: {"input_tokens": 12, "private": "PRIVATE TOKEN"}}})
        summary = claude.cli_summary(output)
        self.assertNotIn("PRIVATE", json.dumps(summary))
        self.assertEqual(summary["assistant_models"], [MODEL])


class FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, body=None, encoding=None):
        self.body = sse(*normal_events()) if body is None else body
        self.encoding = encoding

    def getheaders(self):
        headers = [("Content-Type", "text/event-stream"), ("request-id", "req_fixture"),
                   ("Set-Cookie", "SECRET_COOKIE"), ("X-Private-Account", "SECRET_ACCOUNT")]
        if self.encoding:
            headers.append(("Content-Encoding", self.encoding))
        return headers

    def getheader(self, key, default=None):
        return dict((k.lower(), v) for k, v in self.getheaders()).get(key.lower(), default)

    def read1(self, size):
        body, self.body = self.body, b""
        return body


@contextlib.contextmanager
def proxy(root, save_raw=False):
    server = claude.CaptureServer(root, 2, save_raw=save_raw)
    server.probe = {"name": "fixture", "model": MODEL, "prompt": "fixture prompt"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.close_upstreams()
        server.shutdown()
        server.server_close()
        thread.join(2)


class ProxyTests(unittest.TestCase):
    def request(self, server, headers=None, path=None, body=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {"Authorization": "Bearer SECRET_TOKEN"} if headers is None else headers
        body = body if body is not None else json.dumps({"model": MODEL, "stream": True,
            "messages": [{"role": "user", "content": "fixture prompt"}],
            "metadata": {"user_id": "SECRET_ACCOUNT"},
            "output_config": {"effort": "low", "format": {"schema": "SECRET_SCHEMA"}}})
        client.request("POST", path or ("/" + server.secret + "/v1/messages?beta=true"),
                       body=body, headers=headers)
        response = client.getresponse()
        status, payload = response.status, response.read()
        client.close()
        with server.condition:
            server.condition.wait_for(lambda: server.active == 0, timeout=3)
        return status, payload

    def test_forwards_bearer_to_fixed_tls_host_without_persisting_secrets(self):
        with tempfile.TemporaryDirectory() as folder, proxy(Path(folder)) as server:
            upstream = mock.Mock()
            upstream.sock = None
            upstream.getresponse.return_value = FakeResponse()
            with mock.patch.object(claude.http.client, "HTTPSConnection", return_value=upstream) as factory:
                status, _ = self.request(server)
            self.assertEqual(status, 200)
            self.assertEqual(factory.call_args.args, ("api.anthropic.com",))
            tls = factory.call_args.kwargs["context"]
            self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(tls.check_hostname)
            forwarded = upstream.request.call_args
            self.assertEqual(forwarded.args[:2], ("POST", "/v1/messages?beta=true"))
            self.assertEqual(forwarded.kwargs["headers"]["Authorization"], "Bearer SECRET_TOKEN")
            self.assertEqual(forwarded.kwargs["headers"]["Host"], "api.anthropic.com")
            self.assertFalse(list(Path(folder).glob("*.response")))
            saved = list(Path(folder).glob("*.json"))
            self.assertEqual(len(saved), 1)
            text = saved[0].read_text()
            self.assertNotIn("SECRET", text)
            self.assertNotIn("fixture prompt", text)
            self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)
            self.assertTrue(server.records[0]["complete"])

    def test_rejects_api_key_missing_token_and_wrong_proxy_secret(self):
        with tempfile.TemporaryDirectory() as folder, proxy(Path(folder)) as server:
            with mock.patch.object(claude.http.client, "HTTPSConnection") as factory:
                for headers in ({}, {"Authorization": "Bearer "}, {"x-api-key": "SECRET"},
                                {"Authorization": "Bearer TOKEN", "x-api-key": "SECRET"}):
                    self.assertEqual(self.request(server, headers=headers)[0], 403)
                self.assertEqual(self.request(server, path="/v1/messages")[0], 404)
                self.assertEqual(self.request(server, body="", headers={
                    "Authorization": "Bearer TOKEN", "Content-Length": "invalid"})[0], 400)
            factory.assert_not_called()
            self.assertEqual(server.records, [])

    def test_raw_capture_is_opt_in_and_private(self):
        with tempfile.TemporaryDirectory() as folder, proxy(Path(folder), save_raw=True) as server:
            upstream = mock.Mock(sock=None)
            upstream.getresponse.return_value = FakeResponse()
            with mock.patch.object(claude.http.client, "HTTPSConnection", return_value=upstream):
                self.request(server)
            raw = next(Path(folder).glob("*.response"))
            self.assertEqual(raw.read_bytes(), sse(*normal_events()))
            self.assertEqual(raw.stat().st_mode & 0o777, 0o600)

    def test_bad_compression_preserves_failed_capture_and_releases_active_count(self):
        with tempfile.TemporaryDirectory() as folder, proxy(Path(folder)) as server:
            upstream = mock.Mock(sock=None)
            upstream.getresponse.return_value = FakeResponse(b"not gzip", "gzip")
            with mock.patch.object(claude.http.client, "HTTPSConnection", return_value=upstream):
                self.request(server)
            self.assertEqual(server.active, 0)
            self.assertFalse(server.records[0]["complete"])
            self.assertEqual(server.records[0]["parse_errors"], 1)


class ProbeVerdictTests(unittest.TestCase):
    def probe(self, records=None, output=None, selected=MODEL, returncode=0, direct=False, effort=None):
        with tempfile.TemporaryDirectory() as folder:
            server = claude.CaptureServer(Path(folder), 1)
            proc = mock.Mock(returncode=returncode)

            def communicate(*args, **kwargs):
                for record in records or []:
                    server.records.append(dict(record, probe=server.probe["name"]))
                return output if output is not None else stdout(), "PRIVATE STDERR"

            proc.communicate.side_effect = communicate
            try:
                with mock.patch.object(claude.subprocess, "Popen", return_value=proc) as popen:
                    result = claude.run_probe("claude", server, {}, folder, selected, 1, 1, effort,
                                              direct=direct)
                args = popen.call_args.args[0]
                self.assertIn("--safe-mode", args)
                self.assertEqual(args[args.index("--tools") + 1], "")
                self.assertNotIn("PRIVATE", json.dumps(result))
                return result
            finally:
                server.server_close()

    def capture(self, **overrides):
        return dict({"is_probe_prompt": True, "request": {"model": MODEL},
                     "http_status": 200, "complete": True, "response_models": [MODEL],
                     "comparisons": ["MATCH"]}, **overrides)

    def test_complete_match(self):
        self.assertEqual(self.probe([self.capture()])["verdict"], "MATCH")

    def test_explicit_effort_clamp_is_separate_from_model_match(self):
        capture = self.capture(request={"model": MODEL, "output_config": {"effort": "low"}})
        result = self.probe([capture], effort="high")
        self.assertEqual(result["verdict"], "MATCH")
        self.assertEqual(result["effort_verdict"], "CHANGED")
        self.assertEqual(result["wire_efforts"], ["low"])

    def test_failed_retry_or_incomplete_capture_prevents_match(self):
        for failed in (self.capture(complete=False), self.capture(http_status=429),
                       self.capture(parse_errors=1), self.capture(protocol_errors=1),
                       self.capture(errors=[{"type": "api_error"}]),
                       self.capture(transport_error="TimeoutError")):
            self.assertEqual(self.probe([failed, self.capture()])["verdict"], "UNKNOWN")

    def test_missing_terminal_cli_error_and_failed_process_prevent_match(self):
        for output in ("", stdout(error=True), stdout().rsplit("\n", 1)[0]):
            self.assertEqual(self.probe([self.capture()], output=output)["verdict"], "UNKNOWN")
        self.assertEqual(self.probe([self.capture()], returncode=1)["verdict"], "UNKNOWN")

    def test_upstream_difference_is_separate_from_selection_change(self):
        capture = self.capture(response_models=[OTHER], comparisons=["DIFFERENT"])
        result = self.probe([capture])
        self.assertEqual(result["verdict"], "DIFFERENT")
        self.assertTrue(result["routing"]["wire_to_response_changed"])
        self.assertFalse(result["client_model_changed"])
        result = self.probe([self.capture()], selected=OTHER)
        self.assertEqual(result["verdict"], "CLIENT_MODEL_CHANGED")
        self.assertTrue(result["routing"]["selection_to_init_changed"])
        self.assertFalse(result["routing"]["wire_to_response_changed"])

    def test_direct_control_is_explicitly_weaker_metadata(self):
        result = self.probe(direct=True)
        self.assertEqual(result["verdict"], "DIRECT_METADATA_MATCH")
        self.assertEqual(result["captures"], [])
        self.assertEqual(result["evidence_source"], "CLI response metadata")

    def test_cli_final_model_disagreement_prevents_match(self):
        self.assertEqual(self.probe([self.capture()], output=stdout(OTHER))["verdict"],
                         "CLIENT_MODEL_CHANGED")

    def test_missing_init_model_prevents_match(self):
        output = stdout().split("\n", 1)[1]
        self.assertEqual(self.probe([self.capture()], output=output)["verdict"], "UNKNOWN")


@unittest.skipUnless(os.name == "posix", "Claude process cleanup currently requires POSIX")
class MainPreflightTests(unittest.TestCase):
    def run_main_fixture(self, extra_args=(), result=None, terminal=False, control=None):
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "subscriptionType": "team", "email": "PRIVATE_EMAIL"}
        if result is None:
            result = {"verdict": "MATCH", "effort_verdict": "NOT_REPORTED", "cli": {"errors": []},
                      "captures": [{"is_probe_prompt": True, "request": {"model": MODEL},
                                    "response_models": [MODEL], "http_status": 200, "complete": True,
                                    "response_headers": {"server": "fixture"}, "event_types": ["message_stop"]}]}
        output, errors = io.StringIO(), io.StringIO()
        output.isatty = lambda: terminal
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "private parent" / "output"
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder, "TERM": "xterm"}, clear=True), \
                    mock.patch.object(claude.shutil, "which", return_value="/fake/claude"), \
                    mock.patch.object(claude.subprocess, "check_output", side_effect=["2.0.0", json.dumps(auth), "--safe-mode"]), \
                    mock.patch.object(claude, "default_output", return_value=target) as default, \
                    mock.patch.object(claude, "CaptureServer") as server, \
                    mock.patch.object(claude, "run_probe", return_value=result,
                                      side_effect=[result, control] if control is not None else None) as run, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                server.return_value.url = "http://127.0.0.1:1"
                status = claude.main(["-m", MODEL] + list(extra_args))
            default.assert_called_once_with("claude")
            server.return_value.close_upstreams.assert_called_once()
            server.return_value.server_close.assert_called_once()
            report_text = (target / "report.json").read_text()
            return status, output.getvalue(), errors.getvalue(), report_text, run.call_count

    def test_concise_default_and_verbose_details(self):
        for extra, verbose in (((), False), (("-v",), True)):
            with self.subTest(verbose=verbose):
                status, output, errors, report, count = self.run_main_fixture(extra)
                self.assertEqual(status, 0)
                self.assertEqual(count, 1)  # Direct control remains opt-in here.
                self.assertEqual(errors, "")
                self.assertIn("claude " + MODEL + " -> " + MODEL + " | MATCH", output)
                self.assertEqual("Alias overrides:" in output, verbose)
                self.assertEqual("captures=" in output, verbose)
                self.assertEqual("Claude: 2.0.0" in output, verbose)
                if not verbose:
                    self.assertEqual(len(output.splitlines()), 1)
                self.assertNotIn("PRIVATE_EMAIL", output + report)

    def test_terminal_color_is_not_saved_in_report(self):
        status, output, errors, report, _ = self.run_main_fixture(terminal=True)
        self.assertEqual(status, 0)
        self.assertIn("\033[32m", output)
        self.assertNotIn("\033", report)
        self.assertNotIn("\\u001b", report)
        self.assertEqual(json.loads(report)["results"][0]["verdict"], "MATCH")

    def test_unknown_keeps_failure_reason_on_stderr(self):
        result = {"verdict": "UNKNOWN", "captures": [],
                  "cli": {"errors": [{"type": "overloaded_error"}]}}
        status, output, errors, _, _ = self.run_main_fixture(result=result)
        self.assertEqual(status, 2)
        self.assertIn("UNKNOWN", output)
        self.assertIn("overloaded_error", errors)
        self.assertNotIn("\033", output + errors)

    def test_incomplete_proxy_cannot_claim_direct_disagreement(self):
        proxy_result = {"verdict": "UNKNOWN", "captures": [], "cli": {"errors": []}}
        control = {"verdict": "DIRECT_METADATA_MATCH", "cli_exit_code": 0,
                   "cli": {"assistant_models": [MODEL], "result_is_error": False}}
        status, output, _, report, count = self.run_main_fixture(
            ("--direct-control",), result=proxy_result, terminal=True, control=control)
        self.assertEqual(status, 2)
        self.assertEqual(count, 2)
        self.assertIn("direct UNKNOWN", output)
        self.assertNotIn("DIFFERS", output)
        self.assertIn("\033[33m", output)
        stored = json.loads(report)["results"][0]
        self.assertFalse(stored["direct_control_agrees"])
        self.assertEqual(stored["direct_control"], control)

    def test_direct_client_routing_change_is_visible_even_when_response_agrees(self):
        control = {"verdict": "DIRECT_METADATA_DIFFERENT", "cli_exit_code": 0,
                   "cli": {"assistant_models": [MODEL], "result_is_error": False}}
        status, output, _, report, _ = self.run_main_fixture(
            ("--direct-control",), terminal=True, control=control)
        self.assertEqual(status, 2)
        self.assertIn("direct CHANGED", output)
        self.assertIn("\033[31m", output)
        self.assertTrue(json.loads(report)["results"][0]["direct_control_agrees"])

    def test_default_haiku_and_private_account_metadata(self):
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "subscriptionType": "team", "email": "SECRET_EMAIL", "accountId": "SECRET_ACCOUNT"}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out"
            checked_commands = []

            def check_output(args, **kwargs):
                checked_commands.append(args)
                self.assertNotEqual(kwargs["cwd"], folder)
                return {"--version": "2.0.0 (Claude Code)", "auth": json.dumps(auth),
                        "--help": "--safe-mode"}[args[1]]

            result = {"verdict": "MATCH", "captures": [], "cli": {"errors": []}}
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}, clear=True), \
                    mock.patch.object(claude.shutil, "which", return_value="/fake/claude"), \
                    mock.patch.object(claude.subprocess, "check_output", side_effect=check_output), \
                    mock.patch.object(claude, "run_probe", return_value=result) as run, \
                    contextlib.redirect_stdout(__import__("io").StringIO()):
                self.assertEqual(claude.main(["--out", str(out)]), 0)
            self.assertEqual(run.call_args.args[5], 1)
            self.assertEqual(run.call_args.args[4], "haiku")
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["provider"], "claude")
            self.assertFalse(report["raw_saved"])
            self.assertNotIn("SECRET", json.dumps(report))
            self.assertEqual((out / "report.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(checked_commands), 3)

    def test_api_key_environment_rejected_before_cli_execution(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SECRET"}, clear=True), \
                mock.patch.object(claude.shutil, "which", return_value="/fake/claude"), \
                mock.patch.object(claude.subprocess, "check_output") as check, \
                contextlib.redirect_stderr(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as error:
                claude.main([])
        self.assertEqual(error.exception.code, 2)
        check.assert_not_called()

    def test_cli_api_auth_cannot_be_reported_as_subscription(self):
        auth = {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(claude.shutil, "which", return_value="/fake/claude"), \
                mock.patch.object(claude.subprocess, "check_output",
                                  side_effect=["2.0.0", json.dumps(auth), "--safe-mode"]), \
                contextlib.redirect_stderr(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as error:
                claude.main([])
        self.assertEqual(error.exception.code, 2)

    def test_nan_timeout_is_rejected(self):
        with contextlib.redirect_stderr(__import__("io").StringIO()), \
                self.assertRaises(SystemExit) as error:
            claude.main(["--timeout", "nan"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
