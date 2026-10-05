#!/usr/bin/env python3
"""Audit response model metadata using the real Claude Code subscription client.

Python 3.9+, standard library only. No OAuth extraction, API keys or CA install.
A temporary loopback reverse proxy forwards requests to https://api.anthropic.com
with TLS verification. Only the child CLI's ANTHROPIC_BASE_URL is changed.
Existing model alias environment overrides are preserved and reported.

Examples:
  am-i-nerfed claude
  am-i-nerfed claude -m opus -m sonnet -m haiku -n 3
  am-i-nerfed claude --ignore-alias-overrides -m opus
  am-i-nerfed claude --direct-control

Default: haiku. Other aliases or full model IDs must be selected explicitly.
Probes consume subscription quota and may incur enabled extra-usage charges.
Each probe sends a tiny prompt in a fresh empty directory with tools/customizations
disabled. Only selected model/routing/usage metadata is saved by default. Raw
response bodies require --save-raw and can contain generated text or sensitive
server diagnostics. Authorization headers, request prompts, system prompts,
account identifiers and cookies are not deliberately saved.

MATCH means response metadata matches the wire request, not independent proof of
the weights used by Anthropic. Backend account flags and hidden substitutions
that preserve the reported model ID cannot be detected by this script. A short
prompt also cannot establish what happens on other workloads or at other times.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import http.server
import math
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from ..runtime import Console, default_output, private_mkdir


UPSTREAM = "api.anthropic.com"
ALIAS_KEYS = ["ANTHROPIC_DEFAULT_" + x + "_MODEL"
              for x in ("OPUS", "SONNET", "HAIKU", "FABLE")]
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailer", "transfer-encoding",
               "upgrade", "host", "content-length"}
SAFE_HEADERS = {"request-id", "x-request-id", "content-type", "content-encoding",
                "anthropic-organization-model-fallback", "retry-after", "cf-ray",
                "x-model", "x-claude-model"}
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,159}\Z")


def safe_header(key):
    key = key.lower()
    return key in SAFE_HEADERS or key.startswith("anthropic-ratelimit-")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_json(path, data):
    # Set permissions on creation, not after potentially sensitive bytes exist.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def model_id(value):
    return value if isinstance(value, str) and MODEL_PATTERN.fullmatch(value) else None


def safe_usage(value):
    """Allow only token counters and documented iteration routing metadata."""
    if not isinstance(value, dict):
        return {}
    result = {k: v for k, v in value.items()
              if (k.endswith("_tokens") or k in ("costUSD", "contextWindow", "maxOutputTokens"))
              and isinstance(v, (int, float)) and not isinstance(v, bool)
              and (isinstance(v, int) or math.isfinite(v))}
    if isinstance(value.get("iterations"), list):
        result["iterations"] = []
        for item in value["iterations"]:
            if not isinstance(item, dict):
                continue
            row = safe_usage({k: v for k, v in item.items() if k != "iterations"})
            if item.get("type") in ("message", "fallback_message"):
                row["type"] = item["type"]
            if model_id(item.get("model")):
                row["model"] = item["model"]
            result["iterations"].append(row)
    return result


def error_metadata(value):
    """Never persist arbitrary provider/CLI error messages (may echo credentials)."""
    kind = value.get("type") if isinstance(value, dict) else None
    return {"type": kind if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,64}", kind)
            else "redacted_error", "message_omitted": True}


def compatible(requested, reported):
    if not model_id(requested) or not model_id(reported):
        return "UNKNOWN"
    if requested == reported:
        return "MATCH"
    if requested and reported and re.fullmatch(re.escape(requested) + r"-\d{8}", reported):
        return "MATCH_SNAPSHOT"
    return "DIFFERENT" if reported else "UNKNOWN"


def parse_response(raw, content_type):
    """Keep all model-bearing stream events, including mid-stream fallbacks."""
    text = raw.decode("utf-8", errors="replace")
    events = []
    parse_errors = 0
    if "text/event-stream" in content_type:
        blocks = re.split(r"\r?\n\r?\n", text)
        # The final unframed SSE event is incomplete, even if its JSON is valid.
        if blocks[-1].strip():
            parse_errors += 1
        for block in blocks[:-1]:
            payload = "\n".join(line[5:].lstrip(" ") for line in block.splitlines()
                                if line.startswith("data:"))
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                parse_errors += 1
    else:
        try:
            events.append(json.loads(text))
        except json.JSONDecodeError:
            parse_errors += 1
    models, evidence, usages, errors, types = [], [], [], [], []
    fallbacks, stops = [], []
    started, stopped, protocol_errors = False, False, 0

    def add_model(found, path, value):
        if model_id(value):
            found[path] = value
            if value not in models:
                models.append(value)

    for ev in events:
        if not isinstance(ev, dict):
            parse_errors += 1
            continue
        kind = ev.get("type", "unknown")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,64}", kind):
            parse_errors += 1
            continue
        types.append(kind)
        found = {}
        message = ev.get("message", {})
        message = message if isinstance(message, dict) else {}
        delta = ev.get("delta", {})
        delta = delta if isinstance(delta, dict) else {}
        if kind == "message_start":
            if started or stopped:
                protocol_errors += 1
            started = True
            add_model(found, "$.message.model", message.get("model"))
        elif kind == "message":
            add_model(found, "$.model", ev.get("model"))
            started = ev.get("role") == "assistant"
            stopped = started and bool(ev.get("stop_reason"))
        elif kind == "message_delta":
            if not started or stopped:
                protocol_errors += 1
            add_model(found, "$.delta.model", delta.get("model"))
        elif kind == "message_stop":
            if not started or stopped:
                protocol_errors += 1
            stopped = True
        elif kind.startswith("content_block_") and (not started or stopped):
            protocol_errors += 1
        blocks = []
        if kind == "content_block_start" and isinstance(ev.get("content_block"), dict):
            blocks.append(ev["content_block"])
        if kind == "message" and isinstance(ev.get("content"), list):
            blocks += ev["content"]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "fallback":
                fallback = {"type": "fallback"}
                for side in ("from", "to"):
                    side_value = block.get(side)
                    value = side_value.get("model") if isinstance(side_value, dict) else None
                    if model_id(value):
                        fallback[side] = {"model": value}
                        found["$.fallback." + side + ".model"] = value
                        if side == "to":
                            add_model(found, "$.fallback.to.model", value)
                fallbacks.append(fallback)
        if found:
            evidence.append({"event": kind, "fields": found})
        usage = safe_usage(ev.get("usage") or message.get("usage")) if kind in (
            "message", "message_start", "message_delta") else {}
        if usage:
            usages.append({"event": kind, "usage": usage})
            for iteration in usage.get("iterations", []) or []:
                if iteration.get("type") == "fallback_message" and iteration.get("model"):
                    if iteration["model"] not in models:
                        models.append(iteration["model"])
        for obj in (ev, message, delta):
            if isinstance(obj, dict) and obj.get("stop_reason"):
                reason = obj["stop_reason"]
                if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason):
                    stops.append({"stop_reason": reason})
        if kind == "error" or "error" in ev:
            errors.append(error_metadata(ev.get("error", ev)))
    return {"response_models": models, "model_evidence": evidence,
            "usage_events": usages, "errors": errors, "event_types": types,
            "fallback_events": fallbacks, "stop_events": stops,
            "parse_errors": parse_errors, "protocol_errors": protocol_errors,
            "complete": bool(started and stopped and stops and not errors
                             and not parse_errors and not protocol_errors)}


class CaptureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, timeout, save_raw=False):
        super().__init__(("127.0.0.1", 0), CaptureHandler)
        self.root = root
        self.save_raw = save_raw
        self.timeout_seconds = timeout
        self.secret = secrets.token_urlsafe(24)
        self.probe = None
        self.lock = threading.Lock()
        self.sequence = 0
        self.records = []
        self.active = 0
        self.condition = threading.Condition(self.lock)
        self.tls_context = ssl.create_default_context()
        self.connections = set()

    def close_upstreams(self):
        with self.condition:
            connections = list(self.connections)
        for conn in connections:
            # Shutdown interrupts an active read rather than waiting its timeout.
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(2)
                except OSError:
                    pass
            conn.close()

    @property
    def url(self):
        return "http://127.0.0.1:%d/%s" % (self.server_port, self.secret)


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # BaseHTTPRequestHandler's path could contain the proxy secret.

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.timeout_seconds)

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        prefix = "/" + self.server.secret
        if not self.path.startswith(prefix + "/"):
            self.send_error(404)
            return
        path = self.path[len(prefix):]
        if not path.startswith("/v1/") or self.headers.get("Transfer-Encoding"):
            self.send_error(400, "Unsupported probe request")
            return
        # Fail closed: never forward API-key or unidentified inference traffic.
        auth_parts = self.headers.get("Authorization", "").split()
        bearer = len(auth_parts) == 2 and auth_parts[0].lower() == "bearer"
        if not bearer or self.headers.get("x-api-key"):
            self.send_error(403, "Subscription Bearer authentication required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid body length")
            return
        if length < 0 or length > 32 * 1024 * 1024:
            self.send_error(413, "Body too large")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400, "Incomplete request body")
            return
        request = {}
        if body:
            try:
                parsed = json.loads(body)
                request = parsed if isinstance(parsed, dict) else {}
            except (ValueError, UnicodeDecodeError):
                pass
        inference = self.command == "POST" and urllib.parse.urlsplit(path).path == "/v1/messages"
        record, chunks = None, []
        if inference:
            with self.server.condition:
                self.server.sequence += 1
                sequence = self.server.sequence
                probe = dict(self.server.probe or {})
                self.server.active += 1
            record = {
                "sequence": sequence, "probe": probe.get("name"), "time": timestamp(),
                "requested_cli_model": probe.get("model"),
                "upstream": "https://" + UPSTREAM + urllib.parse.urlsplit(path).path,
                "auth": "bearer (not saved)",
                "is_probe_prompt": probe.get("prompt", "__missing__") in json.dumps(request.get("messages", [])),
                "request": request_metadata(request),
                "request_headers": {k.lower(): v for k, v in self.headers.items()
                                    if k.lower() in ("anthropic-beta", "anthropic-version")},
            }
        conn = http.client.HTTPSConnection(UPSTREAM, timeout=self.server.timeout_seconds,
                                           context=self.server.tls_context)
        with self.server.condition:
            self.server.connections.add(conn)
        started = time.monotonic()
        content_type, encoding, sent_headers = "", "", False
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            headers["Host"] = UPSTREAM
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(body))
            conn.request(self.command, path, body=body, headers=headers)
            response = conn.getresponse()
            content_type = response.getheader("Content-Type", "")
            encoding = response.getheader("Content-Encoding", "").lower()
            if record is not None:
                record["http_status"] = response.status
                record["response_headers"] = {
                    k.lower(): v for k, v in response.getheaders()
                    if safe_header(k)}
            self.send_response_only(response.status, response.reason)
            for k, v in response.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if inference:
                    chunks.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as exc:
            if record is not None:
                record["transport_error"] = type(exc).__name__
            if not sent_headers:
                self.send_error(502, "Upstream forwarding failed")
        finally:
            self.close_connection = True
            conn.close()
            with self.server.condition:
                self.server.connections.discard(conn)
            if record is not None:
                try:
                    raw = b"".join(chunks)
                    filename = "%s-http-%02d.response" % (record["probe"], sequence)
                    raw_path = self.server.root / filename
                    if self.server.save_raw:
                        fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(raw)
                        record["raw_response_file"] = filename
                    record["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
                    try:
                        if encoding not in ("", "identity", "gzip"):
                            raise ValueError("Unsupported content encoding")
                        decoded = gzip.decompress(raw) if encoding == "gzip" else raw
                        record.update(parse_response(decoded, content_type))
                    except (OSError, EOFError, ValueError):
                        record.update({"complete": False, "parse_errors": 1, "response_models": []})
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    record["comparisons"] = [compatible(record["request"].get("model"), m)
                                             for m in record["response_models"]]
                    save_json(raw_path.with_suffix(".json"), record)
                finally:
                    with self.server.condition:
                        self.server.records.append(record)
                        self.server.active -= 1
                        self.server.condition.notify_all()


def request_metadata(request):
    result = {}
    if model_id(request.get("model")):
        result["model"] = request["model"]
    for key in ("stream", "max_tokens"):
        if isinstance(request.get(key), (bool, int)):
            result[key] = request[key]
    for key in ("service_tier", "speed"):
        if model_id(request.get(key)):
            result[key] = request[key]
    for key, fields in (("thinking", ("type", "budget_tokens", "display")),
                        ("output_config", ("effort",))):
        value = request.get(key)
        if isinstance(value, dict):
            result[key] = {k: v for k, v in value.items() if k in fields
                           and (model_id(v) or isinstance(v, int))}
    fallbacks = request.get("fallbacks")
    if fallbacks == "default":
        result["fallbacks"] = fallbacks
    elif isinstance(fallbacks, list):
        result["fallbacks"] = [request_metadata(item) for item in fallbacks if isinstance(item, dict)]
    return result


def cli_summary(stdout):
    summary = {"init_model": None, "assistant_models": [], "partial_models": [],
               "model_usage": {}, "result_is_error": None, "result_subtype": None,
               "api_key_source": None, "errors": [], "notices": [], "parse_errors": 0}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            if line.lstrip().startswith(("{", "[")):
                summary["parse_errors"] += 1
            continue
        if not isinstance(ev, dict):
            summary["parse_errors"] += 1
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            summary["init_model"] = model_id(ev.get("model"))
            summary["api_key_source"] = model_id(ev.get("apiKeySource"))
        if ev.get("type") == "system" and ev.get("subtype") != "init":
            summary["notices"].append({k: ev[k] for k in (
                "subtype", "model", "from_model", "to_model") if model_id(ev.get(k))})
        if ev.get("type") == "assistant":
            message = ev.get("message", {})
            m = model_id(message.get("model")) if isinstance(message, dict) else None
            if m and m not in summary["assistant_models"]:
                summary["assistant_models"].append(m)
        if ev.get("type") == "stream_event":
            event = ev.get("event", {})
            message = event.get("message", {}) if isinstance(event, dict) else {}
            m = model_id(message.get("model")) if isinstance(message, dict) else None
            if m and m not in summary["partial_models"]:
                summary["partial_models"].append(m)
        if ev.get("type") == "result":
            usages = ev.get("modelUsage", {})
            summary["model_usage"] = {m: safe_usage(u) for m, u in usages.items()
                                      if model_id(m)} if isinstance(usages, dict) else {}
            summary["result_is_error"] = ev.get("is_error") if isinstance(ev.get("is_error"), bool) else None
            summary["result_subtype"] = model_id(ev.get("subtype"))
            errors = ev.get("errors") or []
            summary["errors"] += [error_metadata(e) for e in (errors if isinstance(errors, list) else [errors])]
            if ev.get("is_error") and ev.get("result"):
                summary["errors"].append(error_metadata(ev["result"]))
    return summary


def run_probe(cli, server, env, cwd, model, index, timeout, effort, direct=False):
    name = "%02d-%s" % (index, re.sub(r"[^a-zA-Z0-9_-]", "_", model))
    if direct:
        name += "-direct"
        env = dict(env)
        env.pop("ANTHROPIC_BASE_URL", None)
    prompt = "Reply with exactly OK. Do not use tools. Probe ID: " + secrets.token_hex(8)
    with server.condition:
        server.probe = {"name": name, "model": model, "prompt": prompt}
    args = [cli, "-p", "--model", model, "--safe-mode", "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-chrome", "--no-session-persistence", "--disable-slash-commands",
            "--permission-mode", "dontAsk", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--max-budget-usd", "1"]
    if effort:
        args += ["--effort", effort]
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        terminate_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process(proc, force=True)
            stdout, stderr = proc.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        timed_out = True
    with server.condition:
        settled = server.condition.wait_for(lambda: server.active == 0, timeout=min(timeout, 10))
    if not settled:
        server.close_upstreams()
    with server.condition:
        settled = server.condition.wait_for(lambda: server.active == 0, timeout=2)
        captures = [r for r in server.records if r.get("probe") == name]
    summary = cli_summary(stdout)
    primary = [r for r in captures if r.get("is_probe_prompt")]
    successful = [r for r in primary if r.get("http_status") == 200 and r.get("complete")
                  and r.get("response_models") and not r.get("errors")
                  and not r.get("transport_error") and not r.get("parse_errors")
                  and not r.get("protocol_errors")]
    verdict = "UNKNOWN"
    wire_models = list(dict.fromkeys(r["request"].get("model") for r in primary))
    requested_base = re.sub(r"\[(?:1m|200k)\]$", "", model)
    expected = requested_base if requested_base.startswith("claude-") else env.get(
        "ANTHROPIC_DEFAULT_" + requested_base.upper() + "_MODEL")
    init_to_wire_changed = len(wire_models) > 1 or any(
        compatible(summary["init_model"], m) == "DIFFERENT" for m in wire_models)
    selection_to_init_changed = bool(expected and summary["init_model"] and
        compatible(expected, summary["init_model"]) == "DIFFERENT")
    client_changed = init_to_wire_changed or selection_to_init_changed
    wire_to_response_changed = any("DIFFERENT" in r.get("comparisons", []) for r in primary)
    cli_ok = (proc.returncode == 0 and summary["init_model"] and summary["result_is_error"] is False
              and summary["result_subtype"] == "success" and not summary["errors"]
              and not summary["parse_errors"] and not timed_out and settled)
    final_models = summary["assistant_models"]
    response_models = list(dict.fromkeys(m for r in primary for m in r.get("response_models", [])))
    wire_efforts = list(dict.fromkeys(r.get("request", {}).get("output_config", {}).get("effort")
                                     for r in primary if r.get("request", {}).get("output_config", {}).get("effort")))
    effort_verdict = ("CHANGED" if len(wire_efforts) > 1 or (effort and any(e != effort for e in wire_efforts))
                      else "NOT_REPORTED" if effort or wire_efforts else "NOT_REQUESTED")
    final_consistent = bool(final_models) and all(m in response_models for m in final_models)
    if primary and len(successful) == len(primary) and cli_ok:
        if wire_to_response_changed:
            verdict = "DIFFERENT"
        elif client_changed:
            verdict = "CLIENT_MODEL_CHANGED"
        elif final_consistent and all(r.get("comparisons") and all(
                c.startswith("MATCH") for c in r["comparisons"]) for r in successful):
            verdict = "MATCH" if all(set(r["comparisons"]) == {"MATCH"} for r in successful) else "MATCH_SNAPSHOT"
    if direct and cli_ok and summary["assistant_models"]:
        verdict = "DIRECT_METADATA_MATCH" if not client_changed and all(
            compatible(summary["init_model"], m).startswith("MATCH")
            for m in summary["assistant_models"]) else "DIRECT_METADATA_DIFFERENT"
    result = {"name": name, "requested": model, "time": timestamp(), "timed_out": timed_out,
              "cli_exit_code": proc.returncode, "cli": summary, "verdict": verdict,
              "evidence_source": "CLI response metadata" if direct else "raw upstream HTTP response",
              "client_model_changed": client_changed,
              "effort_verdict": effort_verdict, "requested_effort": effort, "wire_efforts": wire_efforts,
              "routing": {"selected_model": model, "expected_explicit_model": expected,
                          "init_model": summary["init_model"], "wire_models": wire_models,
                          "response_models": response_models, "final_cli_models": final_models,
                          "selection_to_init_changed": selection_to_init_changed,
                          "init_to_wire_changed": init_to_wire_changed,
                          "wire_to_response_changed": wire_to_response_changed,
                          "final_cli_consistent": final_consistent if not direct else None},
              "capture_settled": settled,
              "captures": captures, "stderr_present": bool(stderr.strip())}
    # stderr is not persisted: plugins or CLI diagnostics can include local data.
    save_json(server.root / (name + ".json"), result)
    return result


def terminate_process(proc, force=False):
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-m", "--model", action="append", help="Repeat for each model/alias")
    parser.add_argument("-n", "--repeat", type=int, default=1)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--out", type=Path, help="New output directory (must not exist)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show version, configuration and response metadata")
    parser.add_argument("--save-raw", action="store_true",
                        help="Save raw response bodies locally (may contain sensitive text)")
    parser.add_argument("--direct-control", action="store_true",
                        help="Also run without the proxy and compare CLI response metadata")
    parser.add_argument("--ignore-alias-overrides", action="store_true",
                        help="Remove family alias environment overrides in the child only")
    args = parser.parse_args(argv)
    console = Console(verbose=args.verbose)
    if os.name != "posix":
        parser.error("Claude probing currently supports macOS/Linux (or WSL) process cleanup only")
    if args.repeat < 1 or args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error("repeat and timeout must be positive")
    if args.model and not all(model_id(m) for m in args.model):
        parser.error("models must be aliases or model IDs without spaces (maximum 160 characters)")
    cli = shutil.which("claude")
    if not cli:
        parser.error("claude executable not found")
    env = dict(os.environ)
    # Do not silently bypass an existing gateway or switch an API-key account.
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        if env.get(key):
            parser.error(key + " is set; run from a subscription-only shell with it unset")
    for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        if env.get(key, "").lower() not in ("", "0", "false"):
            parser.error(key + " selects a different provider")
    try:
        # Preflight commands run in an empty directory to avoid project settings.
        with tempfile.TemporaryDirectory(prefix="am-i-nerfed-preflight-") as preflight:
            version = subprocess.check_output([cli, "--version"], cwd=preflight, env=env,
                                              text=True, timeout=15, stderr=subprocess.DEVNULL).strip()
            auth = json.loads(subprocess.check_output([cli, "auth", "status"], cwd=preflight, env=env,
                              text=True, timeout=20, stderr=subprocess.DEVNULL))
            help_text = subprocess.check_output([cli, "--help"], cwd=preflight, env=env,
                        text=True, timeout=15, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        parser.error("Claude preflight failed; check claude --version and claude auth status locally")
    if "--safe-mode" not in help_text:
        parser.error("installed Claude Code lacks --safe-mode; update the CLI before probing")
    if (not isinstance(auth, dict) or auth.get("loggedIn") is not True
            or auth.get("authMethod") != "claude.ai" or auth.get("apiProvider") != "firstParty"):
        parser.error("claude auth status must show a logged-in firstParty claude.ai subscription")
    settings_path = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
        if not isinstance(settings, dict) or not isinstance(settings.get("env", {}), dict):
            raise ValueError("Invalid settings")
    except (OSError, ValueError):
        parser.error("cannot safely inspect user settings.json; validate it before probing")
    if (settings.get("apiKeyHelper") or any(settings.get("env", {}).get(k) for k in
            ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"))):
        parser.error("user settings contain API-key/gateway configuration; subscription path is ambiguous")
    models = args.model or ["haiku"]
    aliases = {k: model_id(env[k]) or "invalid-model-id" for k in ALIAS_KEYS if k in env}
    if args.ignore_alias_overrides:
        for key in ALIAS_KEYS:
            env.pop(key, None)
    out = (args.out or default_output("claude")).resolve()
    private_mkdir(out)
    report = {"schema_version": 1, "provider": "claude", "started_at": timestamp(), "claude_version": version,
              "auth": {k: model_id(auth.get(k)) for k in ("authMethod", "apiProvider", "subscriptionType")},
              "configured_model": model_id(settings.get("model")), "alias_environment": aliases,
              "ignored_alias_overrides": args.ignore_alias_overrides,
              "effort_override": args.effort, "models": models, "results": [], "raw_saved": args.save_raw,
              "method": "Real Claude Code, safe-mode, empty cwd, loopback reverse proxy to verified Anthropic HTTPS",
              "limitations": ["Model fields are server claims, not verification of model weights or account flags.",
                              "Only these prompts and times are sampled; workload-dependent routing may differ.",
                              "Loopback proxy changes BASE_URL and uses HTTP/1.1 upstream; a direct-client control is useful.",
                              "Safe mode disables customizations; inherited family alias overrides are preserved unless requested.",
                              "CLI modelUsage may use requested-model accounting; raw upstream response is primary evidence."]}
    server = CaptureServer(out, args.timeout, save_raw=args.save_raw)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env["ANTHROPIC_BASE_URL"] = server.url
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key), "127.0.0.1", "localhost"]))
    try:
        console.detail("Claude: %s | subscription: %s" % (version, report["auth"].get("subscriptionType")))
        console.detail("Alias overrides: %s | ignored: %s" % (aliases or "none", args.ignore_alias_overrides))
        console.detail("Reports: %s" % out)
        with tempfile.TemporaryDirectory(prefix="claude-model-probe-") as cwd:
            index = 0
            for _ in range(args.repeat):
                for model in models:
                    index += 1
                    console.detail("[%d/%d] %s ..." % (index, len(models) * args.repeat, model))
                    result = run_probe(cli, server, env, cwd, model, index, args.timeout, args.effort)
                    report["results"].append(result)
                    save_json(out / "report.json", report)
                    rows = [r for r in result["captures"] if r.get("is_probe_prompt")]
                    wire = list(dict.fromkeys(r["request"].get("model") for r in rows))
                    served = list(dict.fromkeys(m for r in rows for m in r.get("response_models", [])))
                    console.detail("  request=%s response=%s route=%s effort=%s" % (
                        wire, served, result["verdict"], result.get("effort_verdict", "NOT_REPORTED")))
                    console.detail("  captures=" + json.dumps([
                        {key: r.get(key) for key in ("http_status", "response_headers", "event_types", "complete")}
                        for r in rows], ensure_ascii=False))
                    control_status = None
                    if args.direct_control:
                        control = run_probe(cli, server, env, cwd, model, index, args.timeout, args.effort, direct=True)
                        result["direct_control"] = control
                        comparable = (bool(served) and bool(rows) and result["verdict"] in (
                            "MATCH", "MATCH_SNAPSHOT", "DIFFERENT", "CLIENT_MODEL_CHANGED")
                            and all(r.get("complete") and r.get("http_status") == 200 for r in rows)
                            and control["verdict"] in ("DIRECT_METADATA_MATCH", "DIRECT_METADATA_DIFFERENT"))
                        result["direct_control_agrees"] = comparable and set(served) == set(control["cli"]["assistant_models"])
                        control_status = "UNKNOWN" if not comparable else (
                            "CHANGED" if control["verdict"] == "DIRECT_METADATA_DIFFERENT" else
                            "AGREES" if result["direct_control_agrees"] else "DIFFERS")
                        console.detail("  direct response=%s agrees=%s" % (
                            control["cli"]["assistant_models"], result["direct_control_agrees"]))
                        save_json(out / "report.json", report)
                    status = "MATCH" if result["verdict"].startswith("MATCH") else (
                        "UNKNOWN" if result["verdict"] == "UNKNOWN" else "CHANGED")
                    if result.get("effort_verdict") == "CHANGED" or control_status in ("DIFFERS", "CHANGED"):
                        status = "CHANGED"
                    elif status == "MATCH" and control_status == "UNKNOWN":
                        status = "UNKNOWN"
                    text = "claude %s -> %s | %s" % (model, ", ".join(served) or "not reported", result["verdict"])
                    if args.effort or result.get("effort_verdict") == "CHANGED":
                        text += " | effort " + result.get("effort_verdict", "NOT_REPORTED")
                    if control_status:
                        text += " | direct " + control_status
                    console.result(text, status)
                    if result["verdict"] == "UNKNOWN":
                        kinds = [item.get("type", "error") for item in result["cli"].get("errors", [])]
                        reason = ", ".join(kinds) or ("timed out" if result.get("timed_out") else "incomplete response evidence")
                        console.warning("claude %s: %s" % (model, reason))
    finally:
        server.close_upstreams()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        report["finished_at"] = timestamp()
        save_json(out / "report.json", report)
    return 0 if report["results"] and all(
        r["verdict"].startswith("MATCH") and r.get("effort_verdict") != "CHANGED" and (
            not args.direct_control or (r.get("direct_control_agrees") and
                r["direct_control"]["verdict"] == "DIRECT_METADATA_MATCH"))
        for r in report["results"]) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed evidence remains in the output directory.", file=sys.stderr)
        sys.exit(130)
