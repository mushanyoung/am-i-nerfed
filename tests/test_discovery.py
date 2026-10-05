"""Catalog discovery tests use fake control protocols; no inference or network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from am_i_nerfed import discovery as d


def row(model, hidden=False, efforts=None):
    return {"id": model, "model": model, "hidden": hidden,
            "supportedReasoningEfforts": [{"reasoningEffort": e} for e in (efforts or ["low", "high"])],
            "defaultReasoningEffort": "low"}


class DiscoveryCatalogTests(unittest.TestCase):
    def test_only_installed_tools_are_returned(self):
        with patch.object(d.shutil, "which", side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            self.assertEqual(d.discover_tools(), {"codex": "/bin/codex"})

    def test_codex_all_pages_and_hidden_filter(self):
        channel = Mock()
        channel.rpc.side_effect = [{"data": [row("gpt-one"), row("gpt-hidden", True)], "nextCursor": "page-two"},
                                   {"data": [row("gpt-one"), row("gpt-two")], "nextCursor": None}]
        models, catalog = d._codex_pages(channel, False)
        self.assertEqual(models, ["gpt-one", "gpt-two"])
        self.assertEqual(catalog["gpt-one"], {"efforts": ["low", "high"], "default_effort": "low"})
        self.assertEqual(channel.rpc.call_args_list[1].args[2]["cursor"], "page-two")
        self.assertFalse(channel.rpc.call_args_list[0].args[2]["includeHidden"])

    def test_codex_include_hidden_is_explicit(self):
        channel = Mock()
        channel.rpc.return_value = {"data": [row("gpt-hidden", True)], "nextCursor": None}
        self.assertEqual(d._codex_pages(channel, True)[0], ["gpt-hidden"])
        self.assertTrue(channel.rpc.call_args.args[2]["includeHidden"])

    def test_pagination_cycle_is_not_an_exhaustive_result(self):
        channel = Mock()
        channel.rpc.return_value = {"data": [row("gpt-one")], "nextCursor": "repeated"}
        with self.assertRaisesRegex(d.DiscoveryError, "invalid_pagination"):
            d._codex_pages(channel, False)

    def test_pagination_bound(self):
        channel = Mock()
        channel.rpc.side_effect = [{"data": [], "nextCursor": "one"}, {"data": [], "nextCursor": "two"}]
        with patch.object(d, "_MAX_PAGES", 2), self.assertRaisesRegex(d.DiscoveryError, "pagination_limit"):
            d._codex_pages(channel, False)

    def test_malformed_catalog_rejected(self):
        for page in ({"data": None}, {"data": [None]}, {"data": [{"id": "sk-SECRET"}]},
                     {"data": [row("gpt-one")], "nextCursor": {"SECRET": "TOKEN"}}):
            with self.subTest(page=page):
                channel = Mock()
                channel.rpc.return_value = page
                with self.assertRaises(d.DiscoveryError) as exc:
                    d._codex_pages(channel, False)
                self.assertNotIn("SECRET", str(exc.exception))

    def test_claude_discovers_dynamic_resolved_models_and_deduplicates_aliases(self):
        models, catalog = d._claude_models([
            {"value": "default", "resolvedModel": "claude-future-99", "supportedEffortLevels": ["high"]},
            {"value": "opus", "resolvedModel": "claude-future-99", "supportedEffortLevels": ["high"]},
            {"value": "claude-new-family-1[1m]", "resolvedModel": "claude-new-family-1", "defaultEffort": "medium"},
            {"value": "custom-picker-row"}])
        self.assertEqual(models, ["claude-future-99", "claude-new-family-1", "custom-picker-row"])
        self.assertEqual(catalog["claude-future-99"]["efforts"], ["high"])
        self.assertEqual(catalog["claude-new-family-1"]["default_effort"], "medium")
        self.assertEqual(catalog["custom-picker-row"]["efforts"], [])

    def test_labels_and_capabilities_omit_untrusted_data(self):
        models, catalog = d._claude_models([{"value": "haiku", "resolvedModel": "claude-example",
            "description": "SECRET_EMAIL", "supportedEffortLevels": ["low", "SECRET TOKEN", {"effort": "high"}],
            "defaultEffort": "SECRET TOKEN", "account": "SECRET_ACCOUNT"}])
        self.assertNotIn("SECRET", json.dumps([models, catalog]))
        self.assertEqual(catalog["claude-example"]["efforts"], ["low", "high"])

    def test_unknown_future_effort_is_not_silently_replaced(self):
        channel = Mock()
        channel.rpc.return_value = {"data": [row("gpt-future", efforts=["ultra", "future_effort"])]}
        self.assertEqual(d._codex_pages(channel, False)[1]["gpt-future"]["efforts"], ["ultra", "future_effort"])


class DiscoveryProtocolTests(unittest.TestCase):
    def test_json_process_rpc_ignores_notifications(self):
        script = "import sys,json\nfor line in sys.stdin:\n x=json.loads(line); print(json.dumps({'method':'status','params':{}}),flush=True); print(json.dumps({'id':x['id'],'result':{'data':[]}}),flush=True)"
        with tempfile.TemporaryDirectory() as cwd:
            with d._JsonProcess([sys.executable, "-u", "-c", script], cwd, {}, time.monotonic() + 5) as channel:
                self.assertEqual(channel.rpc(1, "model/list", {}), {"data": []})
                proc = channel.proc
            self.assertIsNotNone(proc.poll())

    def test_json_process_timeout_reaps_child(self):
        with tempfile.TemporaryDirectory() as cwd:
            channel = d._JsonProcess([sys.executable, "-c", "import time; time.sleep(30)"], cwd, {}, time.monotonic() + 0.1)
            with self.assertRaisesRegex(d.DiscoveryError, "timeout"):
                with channel:
                    channel.receive()
            self.assertIsNotNone(channel.proc.poll())
            self.assertFalse(channel.reader.is_alive())

    def test_json_process_malformed_private_output_is_not_in_error(self):
        with tempfile.TemporaryDirectory() as cwd:
            with d._JsonProcess([sys.executable, "-c", "print('SECRET_TOKEN')"], cwd, {}, time.monotonic() + 5) as channel:
                with self.assertRaises(d.DiscoveryError) as exc:
                    channel.receive()
                self.assertEqual(str(exc.exception), "malformed_protocol")

    def test_json_process_output_limit(self):
        with tempfile.TemporaryDirectory() as cwd, patch.object(d, "_MAX_LINE", 50):
            with d._JsonProcess([sys.executable, "-c", "print('x'*100)"], cwd, {}, time.monotonic() + 5) as channel:
                with self.assertRaisesRegex(d.DiscoveryError, "protocol_limit"):
                    channel.receive()

    def test_rpc_auth_error_omits_error_body(self):
        channel = object.__new__(d._JsonProcess)
        channel.send = Mock()
        channel.receive = Mock(return_value={"id": 1, "error": {"message": "Authentication failed SECRET_TOKEN"}})
        with self.assertRaises(d.DiscoveryError) as exc:
            channel.rpc(1, "model/list", {})
        self.assertEqual(str(exc.exception), "authentication_failed")

    def test_no_server_tool_or_credential_request_is_fulfilled(self):
        channel = object.__new__(d._JsonProcess)
        channel.send = Mock()
        channel.receive = Mock(return_value={"id": 99, "method": "account/chatgptAuthTokens/refresh"})
        with self.assertRaisesRegex(d.DiscoveryError, "unexpected_server_request"):
            channel.rpc(1, "model/list", {})
        self.assertEqual(channel.send.call_count, 1)

    def test_codex_read_only_native_rpc_methods_and_auth_gate(self):
        channel = Mock()
        channel.__enter__ = Mock(return_value=channel)
        channel.__exit__ = Mock()
        channel.rpc.side_effect = [{}, {"account": {"type": "chatgpt", "email": "SECRET_EMAIL"}}, {"data": [row("gpt-new")]}]
        with patch.object(d, "_codex_command", return_value=["fake"]), patch.object(d, "_JsonProcess", return_value=channel):
            result = d._codex("fake", time.monotonic() + 10, False)
        self.assertEqual([c.args[1] for c in channel.rpc.call_args_list], ["initialize", "account/read", "model/list"])
        self.assertEqual(channel.rpc.call_args_list[1].args[2], {"refreshToken": False})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["models"], ["gpt-new"])

    def test_codex_disables_custom_mcp_and_hooks(self):
        server_name = 'example."quoted"'
        features = "\n".join(f + " stable true" for f in ("hooks", "apps", "plugins", "shell_tool", "unified_exec"))
        with patch.object(d, "_run", side_effect=[json.dumps([{"name": server_name, "env": {"SECRET": "TOKEN"}}]), features]):
            command = d._codex_command("codex", "/tmp/isolated", {}, time.monotonic() + 2)
        self.assertIn("mcp_servers." + json.dumps(server_name) + ".enabled=false", command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn("hooks", command)
        self.assertNotIn("TOKEN", json.dumps(command))

    def test_claude_sends_only_control_initialize(self):
        channel = Mock()
        channel.__enter__ = Mock(return_value=channel)
        channel.__exit__ = Mock()
        channel.receive.return_value = {"type": "control_response", "response": {"subtype": "success", "request_id": "model-discovery",
                                     "response": {"models": [{"value": "opus", "resolvedModel": "claude-new"}], "account": "SECRET"}}}
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
        with patch.dict(os.environ, {}, clear=True), patch.object(d, "_check_claude_settings"), \
                patch.object(d, "_run", side_effect=["--safe-mode", json.dumps(auth)]), \
                patch.object(d, "_JsonProcess", return_value=channel) as process:
            result = d._claude("fake", time.monotonic() + 5, True)
        self.assertEqual(channel.send.call_count, 1)
        self.assertEqual(channel.send.call_args.args[0]["request"]["subtype"], "initialize")
        command = process.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertTrue(any("include-hidden" in w for w in result["warnings"]))


class DiscoveryFallbackTests(unittest.TestCase):
    def test_absent_tool_is_explicit_not_fake_catalog(self):
        with patch.object(d, "discover_tools", return_value={}):
            r = d.discover_models("claude")
        self.assertEqual(r["models"], [])
        self.assertIn("not installed", r["warnings"][0])

    def test_auth_failure_never_becomes_cached_availability(self):
        for failure in ("subscription_login_required", "authentication_failed"):
            with patch.object(d, "discover_tools", return_value={"codex": "fake"}), \
                    patch.object(d, "_codex", side_effect=d.DiscoveryError(failure)), patch.object(d, "_codex_cache") as cache:
                r = d.discover_models("codex")
            self.assertEqual(r["models"], [])
            self.assertIn(failure, r["warnings"][0])
            cache.assert_not_called()

    def test_cached_fallback_is_marked_stale_and_incomplete(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEX_HOME": folder}), \
                patch.object(d, "discover_tools", return_value={"codex": "fake"}), \
                patch.object(d, "_codex", side_effect=d.DiscoveryError("timeout")):
            Path(folder, "models_cache.json").write_text(json.dumps({"models": [
                {"slug": "gpt-cache", "visibility": "list", "supported_reasoning_levels": [{"effort": "high"}], "default_reasoning_level": "high"},
                {"slug": "gpt-hidden", "visibility": "hide"}]}))
            result = d.discover_models("codex")
            self.assertEqual(result["models"], ["gpt-cache"])
            self.assertEqual(result["source"], "codex.models_cache")
            self.assertTrue(any("stale or incomplete" in s for s in result["warnings"]))
            self.assertEqual(d.discover_models("codex", include_hidden=True)["models"], ["gpt-cache", "gpt-hidden"])

    def test_bad_cache_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEX_HOME": folder}):
            Path(folder, "models_cache.json").write_text('{"models": "SECRET"}')
            self.assertIsNone(d._codex_cache(False))

    def test_claude_api_environment_is_explicit_error(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SECRET"}), patch.object(d, "discover_tools", return_value={"claude": "fake"}):
            result = d.discover_models("claude")
        self.assertEqual(result["models"], [])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIn("subscription_environment_required", result["warnings"][0])

    def test_claude_settings_helper_rejected_before_auth_subprocess(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}), \
                patch.object(d, "discover_tools", return_value={"claude": "fake"}), patch.object(d, "_run") as run:
            Path(folder, "settings.json").write_text(json.dumps({"apiKeyHelper": "SECRET_COMMAND"}))
            result = d.discover_models("claude")
        self.assertEqual(result["models"], [])
        self.assertIn("subscription_environment_required", result["warnings"][0])
        self.assertNotIn("SECRET", json.dumps(result))
        run.assert_not_called()

    def test_claude_logged_out_json_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(d, "discover_tools", return_value={"claude": "fake"}), \
                patch.object(d, "_check_claude_settings"), \
                patch.object(d, "_run", side_effect=["--safe-mode", json.dumps({"loggedIn": False})]):
            result = d.discover_models("claude")
        self.assertIn("subscription_login_required", result["warnings"][0])
        self.assertEqual(result["models"], [])

    def test_invalid_parameters_fail_before_process(self):
        for provider, timeout in (("unknown", 30), ("codex", 0), ("claude", float("nan")), ("codex", -1)):
            with self.subTest(provider=provider, timeout=timeout), self.assertRaises(ValueError):
                d.discover_models(provider, timeout)


if __name__ == "__main__":
    unittest.main()
