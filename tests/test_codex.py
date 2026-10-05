import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from am_i_nerfed.providers import codex


MODEL = "gpt-fixture"


def completed(model=MODEL, effort="low", status="completed", **extra):
    r = {"model": model, "status": status, "reasoning": {"effort": effort}}
    r.update(extra)
    return {"type": "response.completed", "response": r}


def good_result():
    r = codex.new_result(MODEL, "low", "http")
    r.update(status=200, wire_model=MODEL, wire_models=[MODEL], wire_effort="low")
    codex.absorb_event(r, completed())
    return r


def sse(*events):
    return ("".join("data: " + json.dumps(e) + "\r\n\r\n" for e in events)).encode().splitlines(keepends=True)


class CodexMetadataTests(unittest.TestCase):
    def test_complete_stream_and_metadata(self):
        r = codex.new_result(MODEL, "low", "http")
        r.update(status=200, wire_model=MODEL, wire_models=[MODEL], wire_effort="low")
        codex.parse_sse(sse({"type": "response.created", "response": {"model": MODEL}}, completed()), r)
        self.assertEqual(codex.verdict_of(r), "same")
        self.assertEqual(r["event_types"]["response.completed"], 1)

    def test_multiline_sse(self):
        r = good_result()
        lines = [b": keepalive\n", b"event: response.completed\n", b'data: {"type":"response.completed",\n',
                 b'data: "response":{"model":"gpt-fixture","status":"completed","reasoning":{"effort":"low"}}}\n', b"\n"]
        codex.parse_sse(lines, r)
        self.assertEqual(r["parse_errors"], 0)
        self.assertEqual(codex.verdict_of(r), "same")

    def test_truncated_event_cannot_pass(self):
        for lines in ([b'data: {"type":"response.completed"}\n'], [b'data: not-json\n', b'\n'],
                      [b'data: []\n', b'\n'], [b'data: null\n', b'\n']):
            with self.subTest(lines=lines):
                r = good_result()
                codex.parse_sse(lines, r)
                self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")

    def test_missing_completion_even_with_done_is_inconclusive(self):
        r = codex.new_result(MODEL, "low", "http")
        r.update(status=200, wire_model=MODEL, wire_effort="low")
        lines = sse({"type": "response.created", "response": {"model": MODEL, "reasoning": {"effort": "low"}}})
        codex.parse_sse(lines + [b"data: [DONE]\n", b"\n"], r)
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")

    def test_incomplete_failed_or_error_never_passes(self):
        for event in ({"type": "response.incomplete", "response": {"model": MODEL, "status": "incomplete"}},
                      {"type": "response.failed", "response": {"model": MODEL, "status": "failed"}},
                      {"type": "error", "message": "SECRET"}, completed(status="failed")):
            with self.subTest(event=event):
                r = good_result()
                codex.absorb_event(r, event)
                self.assertEqual(codex.verdict_of(r), "FAIL")
                self.assertNotIn("SECRET", json.dumps(r))

    def test_no_completion_model_is_inconclusive(self):
        r = good_result()
        r["completed_model"] = None
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")

    def test_missing_effort_keeps_route_match(self):
        r = good_result()
        r.update(served_effort=None, efforts_seen=[])
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")
        self.assertEqual(codex.route_verdict_of(r), "MATCH")
        self.assertEqual(codex.effort_verdict_of(r), "NOT_REPORTED")

    def test_effort_change_does_not_change_route(self):
        r = good_result()
        codex.absorb_event(r, completed(effort="minimal"))
        self.assertEqual(codex.verdict_of(r), "EFFORT CHANGED")
        self.assertEqual(codex.route_verdict_of(r), "MATCH")
        self.assertEqual(codex.effort_verdict_of(r), "CHANGED")

    def test_middle_response_change_is_not_overwritten(self):
        r = good_result()
        codex.absorb_event(r, {"type": "response.created", "response": {"model": "gpt-other"}})
        codex.absorb_event(r, completed())
        self.assertEqual(codex.verdict_of(r), "DIFFERENT")

    def test_selected_and_wire_models_stay_distinct(self):
        r = good_result()
        r["requested"] = "gpt-other"
        self.assertEqual(codex.route_verdict_of(r), "MATCH")
        self.assertEqual(codex.verdict_of(r), "REQUEST CHANGED")
        r["wire_model"] = None
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")

    def test_snapshot_matching_is_anchored(self):
        self.assertEqual(codex.compare("gpt-fixture", "gpt-fixture-2026-01-01"), "same(snapshot)")
        self.assertEqual(codex.compare("gpt-fixture", "gpt-fixture-mini-2026-01-01"), "DIFFERENT")
        self.assertEqual(codex.compare("gpt-fixture", "gpt-fixture-2026-01-01-extra"), "DIFFERENT")

    def test_prewarm_does_not_count_as_completion(self):
        r = codex.new_result(MODEL, "low", "codex")
        codex.absorb_event(r, completed(generate=False))
        self.assertFalse(r["completion_seen"])
        self.assertEqual(r["models_seen"], [])

    def test_payload_text_account_ids_and_error_bodies_are_not_saved(self):
        r = good_result()
        codex.absorb_headers(r, {"Set-Cookie": "SECRET_COOKIE", "x-openai-account-id": "SECRET_ACCOUNT",
                                "x-codex-private": "SECRET_PRIVATE", "openai-model": MODEL})
        codex.absorb_event(r, {"type": "response.output_text.delta", "delta": "SECRET_TEXT"})
        codex.absorb_event(r, completed(usage={"input_tokens": 12, "output_tokens": 1,
                                            "account_id": "SECRET_USAGE"}, access_programs={"account": "SECRET_ACCOUNT"},
                                       output=[{"text": "SECRET_OUTPUT"}]))
        codex.absorb_event(r, {"type": "SECRET_EVENT_TYPE", "token": "SECRET_TOKEN"})
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(r["usage"], {"input_tokens": 12, "output_tokens": 1})

    def test_no_self_identification_prompt(self):
        self.assertEqual(codex.PROMPT, "Reply with exactly OK. Do not use tools.")

    def test_unsupported_effort_is_rejected_without_substitution(self):
        with self.assertRaises(codex.ProbeError):
            codex.pick_effort("low", ["high"])
        self.assertEqual(codex.pick_effort("low", []), "low")

    def test_stream_bounds(self):
        with patch.object(codex, "MAX_STREAM_BYTES", 20):
            r = good_result()
            codex.parse_sse([b"data: " + b"x" * 30], r)
            self.assertEqual(codex.verdict_of(r), "FAIL")

    def test_stream_deadline(self):
        with self.assertRaises(TimeoutError):
            codex.parse_sse([b": ping\n"], good_result(), deadline=time.monotonic() - 1)


class CodexTransportTests(unittest.TestCase):
    def test_trace_wire_response_and_warning(self):
        request = {"type": "response.create", "model": MODEL, "reasoning": {"effort": "low"}}
        trace = "TRACE Sending message " + json.dumps(request) + "\nTRACE Received message " + json.dumps(completed())
        r = codex.new_result(MODEL, "low", "codex")
        r["status"] = 200
        codex.parse_trace(trace, r)
        self.assertEqual(codex.verdict_of(r), "same")
        codex.parse_trace("WARN model rerouted SECRET_ACCOUNT", r)
        self.assertEqual(codex.verdict_of(r), "REROUTED")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_compressed_tungstenite_frames_and_context_takeover(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        trace = []
        for prewarm in (True, False):
            event = {"type": "response.create", "model": "gpt-fixture", "reasoning": {"effort": "low"}, "generate": not prewarm}
            payload = compressor.compress(json.dumps(event).encode()) + compressor.flush(zlib.Z_SYNC_FLUSH)
            self.assertEqual(payload[-4:], b"\x00\x00\xff\xff")
            trace.extend(["TRACE Sending frame: Frame { header: FrameHeader { rsv1: true, opcode: Data(Text) }, payload: b'private' }",
                          "<FRAME>", "payload: 0x" + payload[:-4].hex(), "</FRAME>"])
        trace.append("TRACE Received message " + json.dumps(completed()))
        r = codex.new_result(MODEL, "low", "codex")
        r["status"] = 200
        codex.parse_trace("\n".join(trace), r)
        self.assertEqual(codex.verdict_of(r), "same")
        self.assertEqual(r["wire_models"], [MODEL])
        self.assertNotIn("private", json.dumps(r))

    def test_bad_compressed_frame_is_not_silently_accepted(self):
        r = good_result()
        codex.parse_trace("TRACE Sending frame: Frame { rsv1: true, opcode: Data(Text) }\npayload: 0xffabcd\n", r)
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")

    def test_trace_escaped_json(self):
        r = good_result()
        trace = "TRACE Received message Text(" + json.dumps(json.dumps(completed())) + ")"
        codex.parse_trace(trace, r)
        self.assertEqual(r["parse_errors"], 0)
        self.assertEqual(r["event_types"]["response.completed"], 2)

    def test_zero_cli_exit_with_no_trace_cannot_pass(self):
        with patch.object(codex, "cli_command", return_value=["codex", "exec"]), patch.object(codex.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="I am gpt-fixture", stderr="")
            r = codex.probe_codex(MODEL, "low", 1)
        self.assertEqual(codex.verdict_of(r), "INCONCLUSIVE")
        self.assertNotIn("I am", json.dumps(r))

    def test_cli_timeout_has_no_partial_log(self):
        with patch.object(codex, "cli_command", return_value=["codex", "exec"]), patch.object(codex.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired([], 1, output="SECRET", stderr="SECRET")
            r = codex.probe_codex(MODEL, "low", 1)
        self.assertEqual(r["error"], "timeout")
        self.assertEqual(codex.verdict_of(r), "FAIL")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_cli_environment_strips_credentials_and_endpoints(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET", "CODEX_REMOTE": "SECRET", "HTTP_PROXY": "SECRET"}), \
                patch.object(codex, "cli_command", return_value=["codex", "exec"]), \
                patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            codex.probe_codex(MODEL, "low", 1)
        env = run.call_args.kwargs["env"]
        self.assertNotIn("SECRET", json.dumps(env))
        self.assertEqual(env["CODEX_HOME"], codex.CODEX_HOME)

    def test_cli_configuration_isolates_tools_and_login(self):
        help_text = "--ignore-user-config --ignore-rules --ephemeral"
        features = "\n".join(f + " stable true" for f in ("hooks", "shell_tool", "unified_exec", "apps", "plugins", "multi_agent"))
        with patch.object(codex.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, help_text, ""),
                                                             subprocess.CompletedProcess([], 0, features, "")]):
            cmd = codex.cli_command(MODEL, "low", "/tmp/private")
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn('forced_login_method="chatgpt"', cmd)
        self.assertIn("mcp_servers={}", cmd)
        self.assertIn('web_search="disabled"', cmd)
        for feature in ("hooks", "shell_tool", "unified_exec", "apps", "plugins", "multi_agent"):
            self.assertEqual(cmd[cmd.index(feature) - 1], "--disable")

    def test_cli_missing_isolation_flags_fails_closed(self):
        with patch.object(codex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "old help", "")):
            with self.assertRaises(codex.ProbeError):
                codex.cli_command(MODEL, "low", "/tmp/private")

    def test_http_headers_errors_and_redirects_are_safe(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {"Content-Type": "text/event-stream", "openai-model": MODEL,
                            "Set-Cookie": "SECRET_COOKIE"}
        response.readline.side_effect = sse(completed()) + [b""]
        opener = Mock()
        opener.open.return_value = response
        with patch.object(codex.urllib.request, "build_opener", return_value=opener):
            r = codex.probe_http(MODEL, "low", {"access_token": "SECRET_TOKEN", "account_id": "SECRET_ACCOUNT"}, "1.0.0", 1)
        self.assertEqual(codex.verdict_of(r), "same")
        self.assertNotIn("SECRET", json.dumps(r))
        sent_request = opener.open.call_args.args[0]
        self.assertEqual(sent_request.full_url, codex.ENDPOINT)
        self.assertEqual(json.loads(sent_request.data)["tool_choice"], "none")
        self.assertIsNone(codex.NoRedirect().redirect_request(sent_request, None, 302, "", {}, "https://evil.test"))
        opener.open.side_effect = urllib.error.HTTPError(codex.ENDPOINT, 302, "SECRET", {}, io.BytesIO(b"SECRET"))
        with patch.object(codex.urllib.request, "build_opener", return_value=opener):
            r = codex.probe_http(MODEL, "low", {"access_token": "TOKEN", "account_id": "ID"}, "1.0.0", 1)
        self.assertEqual(r["error"], "redirect_blocked")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_http_omitted_content_type_still_requires_valid_terminal_sse(self):
        for lines, expected in ((sse(completed()), "same"), ([b"<html>unexpected</html>"], "INCONCLUSIVE")):
            with self.subTest(expected=expected):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.status, response.headers = 200, {}
                response.readline.side_effect = lines + [b""]
                opener = Mock()
                opener.open.return_value = response
                with patch.object(codex.urllib.request, "build_opener", return_value=opener):
                    r = codex.probe_http(MODEL, "low", {"access_token": "synthetic", "account_id": "synthetic"}, "1.0.0", 1)
                self.assertEqual(codex.verdict_of(r), expected)

    def test_http_slow_stream_is_interrupted_by_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(codex, "ENDPOINT", "http://127.0.0.1:%d/" % server.server_address[1]):
                started = time.monotonic()
                r = codex.probe_http(MODEL, "low", {"access_token": "synthetic", "account_id": "synthetic"}, "1.0.0", 0.1)
            self.assertEqual(r["error"], "timeout")
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_network_exception_does_not_expose_url_or_tokens(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("SECRET_TOKEN https://private")
        with patch.object(codex.urllib.request, "build_opener", return_value=opener):
            r = codex.probe_http(MODEL, "low", {"access_token": "synthetic", "account_id": "synthetic"}, "1.0.0", 1)
        self.assertEqual(r["error"], "transport_error")
        self.assertNotIn("SECRET", json.dumps(r))


class CodexCLITests(unittest.TestCase):
    def test_explicit_model_works_without_catalog_and_native_auth_not_loaded(self):
        with patch.object(codex, "load_models", return_value={}), patch.object(codex, "load_auth") as auth, \
                patch.object(codex, "codex_version", return_value="1.0.0"), \
                patch.object(codex, "probe_codex", return_value=good_result()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex.main(["-m", MODEL, "--via-codex"]), 0)
        auth.assert_not_called()

    def test_native_fresh_default_effort_is_not_rejected_by_stale_cache(self):
        # Native model/list now advertises medium; an older cache only knows low.
        result = good_result()
        result.update(effort="medium", wire_effort="medium", wire_efforts=["medium"],
                      served_effort="medium", efforts_seen=["medium"])
        with patch.object(codex, "load_models", return_value={MODEL: {"efforts": ["low"]}}) as catalog, \
                patch.object(codex, "codex_version", return_value="1.0.0"), \
                patch.object(codex, "probe_codex", return_value=result) as probe, contextlib.redirect_stdout(io.StringIO()):
            status = codex.main(["-m", MODEL, "--via-codex", "--effort", "medium"])
        self.assertEqual(status, 0)
        catalog.assert_not_called()
        self.assertEqual(probe.call_args.args[:2], (MODEL, "medium"))

    def test_http_explicit_effort_reaches_backend_without_silent_substitution(self):
        result = codex.new_result(MODEL, "high", "http")
        result.update(status=400, error="http_error")
        auth = {"access_token": "synthetic", "account_id": "synthetic"}
        with patch.object(codex, "load_models", return_value={MODEL: {"efforts": ["low"]}}) as catalog, \
                patch.object(codex, "load_auth", return_value=auth), \
                patch.object(codex, "codex_version", return_value="1.0.0"), \
                patch.object(codex, "probe_http", return_value=result) as probe, contextlib.redirect_stdout(io.StringIO()):
            status = codex.main(["-m", MODEL, "--effort", "high"])
        self.assertEqual(status, 2)
        catalog.assert_not_called()
        self.assertEqual(probe.call_args.args[:2], (MODEL, "high"))
        self.assertEqual(probe.call_count, 1)

    def test_all_cached_models_still_validate_effort_without_substitution(self):
        with patch.object(codex, "load_models", return_value={MODEL: {"efforts": ["low"]}}) as catalog, \
                patch.object(codex, "probe_codex") as probe, contextlib.redirect_stderr(io.StringIO()):
            status = codex.main(["--all", "--via-codex", "--effort", "high"])
        self.assertEqual(status, 1)
        catalog.assert_called_once_with(include_hidden=False, required=True)
        probe.assert_not_called()

    def test_inconclusive_and_failed_requests_return_nonzero(self):
        r = good_result()
        r["completion_seen"] = False
        with patch.object(codex, "load_models", return_value={}), patch.object(codex, "codex_version", return_value="1.0.0"), \
                patch.object(codex, "probe_codex", return_value=r), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(codex.main(["-m", MODEL, "--via-codex"]), 2)
        self.assertNotIn("every successful", output.getvalue())

    def test_invalid_repeat_timeout_or_model_does_not_make_request(self):
        for args in (["-m", MODEL, "-n", "0"], ["-m", MODEL, "--timeout", "nan"],
                     ["-m", MODEL, "--timeout", "-1"], ["-m", "bad model"], []):
            with self.subTest(args=args), patch.object(codex, "load_auth") as auth, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    codex.main(args)
                auth.assert_not_called()

    def test_private_atomic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text("old")
            path.chmod(0o644)
            codex.write_report(str(path), {"codex_version": "1.0.0", "results": [good_result()]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["codex_version"], "1.0.0")

    def test_no_catalog_is_optional_for_explicit_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(codex, "CODEX_HOME", tmp):
            self.assertEqual(codex.load_models(), {})
            with self.assertRaises(codex.ProbeError):
                codex.load_models(required=True)


if __name__ == "__main__":
    unittest.main()
