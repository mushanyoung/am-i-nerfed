"""Read installed clients' model catalogs without starting an inference turn.

Codex uses native app-server account/read + paginated model/list. Claude Code
uses the SDK control initialize reply (the source of supportedModels()). These
are advertised client catalogs, not proof of entitlement or exhaustive lists of
historical IDs accepted by the backend. Only completed probes establish access.
"""

import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import __version__

_MODEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*(?:\[(?:1m|200k)\])?\Z")
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_MAX_LINE = 2 * 1024 * 1024
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_PAGES = 100


class DiscoveryError(Exception):
    """Public error codes only; never include subprocess output or account data."""


def discover_tools():
    """Return only client executables currently present on PATH."""
    return {name: path for name in ("codex", "claude") if (path := shutil.which(name))}


def _label(value):
    if not isinstance(value, str) or len(value) > 160 or not _MODEL.fullmatch(value):
        return None
    if re.search(r"(?:sk-|gh[pousr]_|github_pat_|Bearer|eyJ)", value, re.I):
        return None
    return value


def _effort(value):
    return value if isinstance(value, str) and _EFFORT.fullmatch(value) else None


def _efforts(values, key=None):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get(key) if key else value.get("effort")
        if _effort(value) and value not in result:
            result.append(value)
    return result


def _result(models=None, source="unavailable", warnings=None, catalog=None):
    return {"models": models or [], "source": source, "warnings": warnings or [], "catalog": catalog or {}}


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise DiscoveryError("timeout")
    return left


def _environment(provider):
    allowed = {"PATH", "HOME", "USER", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMROOT", "WINDIR",
               "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME",
               "DBUS_SESSION_BUS_ADDRESS"}
    if provider == "codex":
        allowed.add("CODEX_HOME")
    else:
        allowed.add("CLAUDE_CONFIG_DIR")
        allowed.update(k for k in os.environ if k.startswith("ANTHROPIC_DEFAULT_") and k.endswith("_MODEL"))
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["NO_COLOR"] = "1"
    if provider == "claude":
        env["CLAUDE_CODE_SAFE_MODE"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _stop(proc):
    """Stop the dedicated process group and reap it, including timeout paths."""
    if proc.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
    # A CLI can exit while leaving a helper in its process group.
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run(args, cwd, env, deadline, allow_nonzero=False):
    """Read a short native preflight command; stderr is intentionally discarded."""
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=(os.name == "posix"))
    except OSError:
        raise DiscoveryError("client_unavailable") from None
    try:
        try:
            stdout, _ = proc.communicate(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired:
            raise DiscoveryError("timeout") from None
        if proc.returncode and not allow_nonzero:
            raise DiscoveryError("preflight_failed")
        if len(stdout) > _MAX_OUTPUT:
            raise DiscoveryError("protocol_limit")
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise DiscoveryError("malformed_protocol") from None
    finally:
        _stop(proc)
        if proc.stdout:
            proc.stdout.close()


class _JsonProcess:
    """Bounded JSONL exchange; no turns, user prompts, or tool approvals are sent."""
    def __init__(self, args, cwd, env, deadline):
        self.deadline = deadline
        self.messages = queue.Queue(maxsize=128)
        self.stopping = threading.Event()
        try:
            self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         start_new_session=(os.name == "posix"))
        except OSError:
            raise DiscoveryError("client_unavailable") from None
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _put(self, value):
        while not self.stopping.is_set():
            try:
                self.messages.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def _reader(self):
        total = 0
        try:
            while not self.stopping.is_set():
                raw = self.proc.stdout.readline(_MAX_LINE + 1)
                if not raw:
                    self._put(DiscoveryError("client_closed"))
                    return
                total += len(raw)
                if len(raw) > _MAX_LINE or total > _MAX_OUTPUT:
                    self._put(DiscoveryError("protocol_limit"))
                    return
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    self._put(DiscoveryError("malformed_protocol"))
                    return
                if not isinstance(obj, dict):
                    self._put(DiscoveryError("malformed_protocol"))
                    return
                self._put(obj)
        except (OSError, ValueError):
            self._put(DiscoveryError("client_closed"))

    def send(self, message):
        _remaining(self.deadline)
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise DiscoveryError("client_closed") from None

    def receive(self):
        try:
            item = self.messages.get(timeout=_remaining(self.deadline))
        except queue.Empty:
            raise DiscoveryError("timeout") from None
        if isinstance(item, DiscoveryError):
            raise item
        return item

    def rpc(self, ident, method, params):
        self.send({"id": ident, "method": method, "params": params})
        while True:
            item = self.receive()
            if item.get("id") != ident:
                # Never fulfill server-initiated tool/credential/approval requests.
                if "id" in item and "method" in item:
                    raise DiscoveryError("unexpected_server_request")
                continue
            if "error" in item:
                error = item.get("error")
                # Only classify auth failures; never return the message itself.
                text = json.dumps(error) if isinstance(error, (dict, str)) else ""
                auth_error = re.search(r"unauthori[sz]ed|authenticat|login required|not logged|sign.?in", text, re.I)
                raise DiscoveryError("authentication_failed" if auth_error else "rpc_failed")
            if not isinstance(item.get("result"), dict):
                raise DiscoveryError("malformed_protocol")
            return item["result"]

    def close(self):
        self.stopping.set()
        _stop(self.proc)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()
        self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _codex_command(cli, cwd, env, deadline):
    # app-server has no --ignore-user-config flag. Native mcp list reads configuration
    # without launching servers; explicitly disable each configured server before init.
    try:
        servers = json.loads(_run([cli, "mcp", "list", "--json"], cwd, env, deadline))
    except ValueError:
        raise DiscoveryError("malformed_mcp_configuration") from None
    if not isinstance(servers, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in servers):
        raise DiscoveryError("malformed_mcp_configuration")
    features_text = _run([cli, "features", "list"], cwd, env, deadline)
    supported = {line.split()[0] for line in features_text.splitlines() if line.split()}
    required = {"hooks", "apps", "plugins", "shell_tool", "unified_exec"}
    if not required.issubset(supported):
        raise DiscoveryError("isolation_unavailable")
    args = [cli, "app-server", "--listen", "stdio://"]
    disabled = required | {"daemon_auto_start", "remote_plugin", "shell_snapshot", "multi_agent", "code_mode_host",
                           "browser_use", "computer_use", "memories", "skill_search", "skill_mcp_dependency_install"}
    for feature in sorted(disabled & supported):
        args += ["--disable", feature]
    config = {"model_provider": "openai", "forced_login_method": "chatgpt", "cli_auth_credentials_store": "auto",
              "chatgpt_base_url": "https://chatgpt.com/backend-api/", "project_doc_max_bytes": 0,
              "notify": [], "log_dir": cwd, "sqlite_home": cwd, "history.persistence": "none",
              "analytics.enabled": False, "check_for_update_on_startup": False}
    for key, value in config.items():
        args += ["-c", key + "=" + json.dumps(value)]
    for server in servers:
        # JSON strings are valid TOML quoted keys, including dots/quotes in MCP names.
        args += ["-c", "mcp_servers." + json.dumps(server["name"]) + ".enabled=false"]
    return args


def _codex_pages(channel, include_hidden):
    models, catalog, cursor, seen = [], {}, None, set()
    for page in range(_MAX_PAGES):
        params = {"limit": 100, "includeHidden": bool(include_hidden)}
        if cursor is not None:
            params["cursor"] = cursor
        data = channel.rpc(10 + page, "model/list", params)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise DiscoveryError("malformed_catalog")
        for item in rows:
            if not isinstance(item, dict):
                raise DiscoveryError("malformed_catalog")
            if item.get("hidden") is True and not include_hidden:
                continue
            model = _label(item.get("model")) or _label(item.get("id"))
            if not model:
                raise DiscoveryError("malformed_catalog")
            if model not in catalog:
                models.append(model)
            catalog[model] = {"efforts": _efforts(item.get("supportedReasoningEfforts"), "reasoningEffort"),
                              "default_effort": _effort(item.get("defaultReasoningEffort"))}
        cursor = data.get("nextCursor")
        if cursor is None:
            return models, catalog
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
            raise DiscoveryError("invalid_pagination")
        seen.add(cursor)
    raise DiscoveryError("pagination_limit")


def _codex(cli, deadline, include_hidden):
    env = _environment("codex")
    with tempfile.TemporaryDirectory(prefix="am-i-nerfed-discovery-") as cwd:
        command = _codex_command(cli, cwd, env, deadline)
        with _JsonProcess(command, cwd, env, deadline) as channel:
            channel.rpc(0, "initialize", {"clientInfo": {"name": "am_i_nerfed", "version": __version__}})
            channel.send({"method": "initialized", "params": {}})
            account = channel.rpc(1, "account/read", {"refreshToken": False}).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise DiscoveryError("subscription_login_required")
            models, catalog = _codex_pages(channel, include_hidden)
    return _result(models, "codex.app-server.model/list", [
        "Client-advertised Codex catalog; listed models are candidates, not verified per-model entitlement."
    ], catalog)


def _claude_models(rows):
    if not isinstance(rows, list):
        raise DiscoveryError("malformed_catalog")
    models, catalog = [], {}
    for item in rows:
        if not isinstance(item, dict):
            raise DiscoveryError("malformed_catalog")
        model = _label(item.get("resolvedModel")) or _label(item.get("value"))
        if not model:
            raise DiscoveryError("malformed_catalog")
        if model not in catalog:
            models.append(model)
        catalog[model] = {"efforts": _efforts(item.get("supportedEffortLevels")),
                          "default_effort": _effort(item.get("defaultEffort"))}
    return models, catalog


def _check_claude_settings(env):
    # --safe-mode disables customizations but deliberately preserves authentication.
    # Reject credential helpers/gateways before invoking auth status, without
    # copying their values to diagnostics or running any configured helper.
    path = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json"
    try:
        if not path.exists():
            return
        if path.stat().st_size > _MAX_OUTPUT:
            raise DiscoveryError("invalid_user_settings")
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict) or not isinstance(settings.get("env", {}), dict):
            raise DiscoveryError("invalid_user_settings")
        auth_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
        if settings.get("apiKeyHelper") or any(settings.get("env", {}).get(k) for k in auth_keys):
            raise DiscoveryError("subscription_environment_required")
        for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            value = settings.get("env", {}).get(key)
            if value is not None and str(value).lower() not in ("", "0", "false"):
                raise DiscoveryError("subscription_environment_required")
    except (OSError, ValueError, RecursionError):
        raise DiscoveryError("invalid_user_settings") from None


def _claude(cli, deadline, include_hidden):
    # Refuse ambiguous providers rather than silently dropping their credentials.
    if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")):
        raise DiscoveryError("subscription_environment_required")
    if any(os.environ.get(k, "").lower() not in ("", "0", "false") for k in
           ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        raise DiscoveryError("subscription_environment_required")
    env = _environment("claude")
    _check_claude_settings(env)
    with tempfile.TemporaryDirectory(prefix="am-i-nerfed-discovery-") as cwd:
        help_text = _run([cli, "--help"], cwd, env, deadline)
        if "--safe-mode" not in help_text:
            raise DiscoveryError("isolation_unavailable")
        try:
            auth = json.loads(_run([cli, "--safe-mode", "auth", "status"], cwd, env, deadline, allow_nonzero=True))
        except ValueError:
            raise DiscoveryError("malformed_auth_status") from None
        if not isinstance(auth, dict) or auth.get("loggedIn") is not True or auth.get("authMethod") != "claude.ai" or auth.get("apiProvider") != "firstParty":
            raise DiscoveryError("subscription_login_required")
        args = [cli, "-p", "--safe-mode", "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--no-chrome", "--no-session-persistence", "--disable-slash-commands", "--permission-mode", "dontAsk",
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        with _JsonProcess(args, cwd, env, deadline) as channel:
            channel.send({"type": "control_request", "request_id": "model-discovery",
                          "request": {"subtype": "initialize", "hooks": {}, "sdkMcpServers": []}})
            while True:
                event = channel.receive()
                if event.get("type") == "control_request":
                    raise DiscoveryError("unexpected_server_request")
                response = event.get("response")
                if event.get("type") != "control_response" or not isinstance(response, dict) or response.get("request_id") != "model-discovery":
                    continue
                if response.get("subtype") != "success":
                    raise DiscoveryError("initialization_failed")
                info = response.get("response")
                if not isinstance(info, dict):
                    raise DiscoveryError("malformed_catalog")
                models, catalog = _claude_models(info.get("models"))
                break
    warnings = ["Claude Code's advertised picker catalog (resolved IDs where supplied); does not enumerate every historical model the account may accept."]
    if include_hidden:
        warnings.append("Claude Code SDK initialize has no include-hidden option; only its advertised catalog was returned.")
    return _result(models, "claude.sdk.initialize.models", warnings, catalog)


def _codex_cache(include_hidden):
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        if path.stat().st_size > _MAX_OUTPUT:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("models")
        if not isinstance(rows, list):
            return None
        models, catalog = [], {}
        for item in rows:
            if not isinstance(item, dict) or not _label(item.get("slug")):
                continue
            if item.get("visibility") != "list" and not include_hidden:
                continue
            model = item["slug"]
            if model not in catalog:
                models.append(model)
            catalog[model] = {"efforts": _efforts(item.get("supported_reasoning_levels")),
                              "default_effort": _effort(item.get("default_reasoning_level"))}
        return (models, catalog) if models else None
    except (OSError, ValueError, AttributeError, RecursionError):
        return None


def discover_models(provider, timeout=30.0, include_hidden=False):
    """Return allowlisted models/catalog/source/warnings. Errors have an empty list.

    A Codex cache is used only after non-auth live discovery failures, prominently
    labeled as potentially stale and not exhaustive. No credentials are extracted.
    """
    if provider not in ("codex", "claude"):
        raise ValueError("provider must be codex or claude")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    tools = discover_tools()
    if provider not in tools:
        return _result(warnings=[provider + " client is not installed on PATH."])
    try:
        result = (_codex if provider == "codex" else _claude)(tools[provider], time.monotonic() + timeout, include_hidden)
        if not result["models"]:
            result["warnings"].append("The client returned an empty model catalog; no models were assumed.")
        return result
    except DiscoveryError as exc:
        code = str(exc)
        if provider == "codex" and code not in ("authentication_failed", "subscription_login_required"):
            cached = _codex_cache(include_hidden)
            if cached:
                return _result(cached[0], "codex.models_cache", [
                    "Live Codex discovery failed (" + code + "); using the local cached catalog.",
                    "Cached candidates may be stale or incomplete; current subscription authentication and per-model access were not verified."
                ], cached[1])
        return _result(warnings=[provider + " model discovery failed: " + code + ". No model availability was assumed."])
