"""Inspect Codex subscription routing metadata, never model self-identification.

HTTP uses the private Codex subscription endpoint with read-only file credentials.
--via-codex uses native CLI authentication and experimental websocket trace parsing.
Both transports consume subscription quota; neither attests backend model weights.
"""

import argparse
import base64
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib

CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
PROMPT = "Reply with exactly OK. Do not use tools."
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.:-]{0,119}\Z")
SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})\Z")
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
KNOWN_EVENTS = frozenset(("response.created", "response.in_progress", "response.completed",
                         "response.incomplete", "response.failed", "response.output_text.delta",
                         "response.output_text.done", "response.output_item.added",
                         "response.output_item.done", "response.content_part.added",
                         "response.content_part.done", "error", "codex.response.metadata"))


class ProbeError(Exception):
    """A deliberately public, credential-free diagnostic."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib may otherwise forward Authorization to the redirected origin.
        return None


def model_id(value):
    if isinstance(value, str) and MODEL_RE.fullmatch(value) and not value.startswith(("sk-", "eyJ")):
        return value
    return None


def load_auth():
    """Read existing file credentials; never refresh, print, or persist tokens."""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
            raise ProbeError("HTTP mode requires ChatGPT subscription login; use codex login first.")
        tokens = data.get("tokens") or {}
        token, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
            raise ProbeError("Subscription file credentials are missing; try --via-codex for native auth.")
        if any(c in token + account for c in "\r\n"):
            raise ProbeError("Invalid subscription file credentials.")
        # The JWT is inspected only for expiration, not treated as verified claims.
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        expiry = claims.get("exp")
        if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            raise ProbeError("Cannot inspect subscription token expiry; try --via-codex.")
        if expiry <= time.time() + 60:
            raise ProbeError("Subscription token expired; refresh through the official Codex client first.")
        return {"access_token": token, "account_id": account}
    except ProbeError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProbeError("Cannot read subscription file credentials; use codex login or --via-codex.") from None


def load_models(include_hidden=False, required=False):
    try:
        with open(os.path.join(CODEX_HOME, "models_cache.json"), encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("models", [])
        if not isinstance(rows, list):
            raise ValueError()
        out = {}
        for item in rows:
            if not isinstance(item, dict) or not model_id(item.get("slug")):
                continue
            if item.get("visibility") != "list" and not include_hidden:
                continue
            levels = item.get("supported_reasoning_levels") or []
            if not isinstance(levels, list):
                levels = []
            levels = [v.get("effort") if isinstance(v, dict) else v for v in levels]
            out[item["slug"]] = {"efforts": [v for v in levels if v in EFFORTS]}
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        if required:
            raise ProbeError("No usable model cache; open Codex once or specify a model with -m.") from None
        return {}


def codex_version():
    try:
        p = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-.][a-zA-Z0-9]+)*", p.stdout)
        return match.group(0) if match else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def pick_effort(wanted, supported):
    if supported and wanted not in supported:
        raise ProbeError("Requested effort is not supported by the cached model catalog; choose it explicitly.")
    return wanted


def compare(requested, served):
    if not served or not requested:
        return "?"
    if served == requested:
        return "same"
    base = requested[:-7] if requested.endswith("-latest") else requested
    suffix = served[len(base):] if served.startswith(base) else ""
    if served == base or SNAPSHOT_SUFFIX.fullmatch(suffix):
        return "same(snapshot)"
    return "DIFFERENT"


def new_result(model, effort, path):
    return {
        "path": path, "requested": model, "effort": effort,
        "wire_model": None, "wire_models": [], "wire_effort": None, "wire_efforts": [],
        "status": None, "header_model": None, "created_model": None,
        "completed_model": None, "served_effort": None,
        "models_seen": [], "efforts_seen": [], "service_tier": None,
        "final_status": None, "completion_seen": False, "reroute_warning": False,
        "usage": None, "headers": {}, "event_types": {},
        "parse_errors": 0, "error": None, "total_s": None,
    }


def _append_unique(seq, value):
    if value is not None and value not in seq:
        seq.append(value)


def absorb_headers(result, headers):
    """Exact allowlist, not prefix filtering (prefixes can include account IDs)."""
    if not hasattr(headers, "items"):
        return
    for key, value in headers.items():
        key = str(key).lower()
        if key == "openai-model" and model_id(value):
            result["headers"][key] = value
            result["header_model"] = value
            _append_unique(result["models_seen"], value)
        elif key == "x-request-id" and isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
            result["headers"][key] = value


def absorb_event(result, ev):
    """Fold only allowlisted metadata; raw events/text/errors never enter results."""
    if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
        result["parse_errors"] += 1
        return
    etype = ev["type"]
    event_label = etype if etype in KNOWN_EVENTS else "other"
    result["event_types"][event_label] = result["event_types"].get(event_label, 0) + 1
    if etype == "codex.response.metadata":
        absorb_headers(result, ev.get("headers"))
        return
    if etype == "error":
        result["error"] = "upstream_error"
        return
    if etype not in ("response.created", "response.completed", "response.failed", "response.incomplete"):
        return
    response = ev.get("response")
    if not isinstance(response, dict):
        result["parse_errors"] += 1
        return
    if response.get("generate") is False:
        return  # Websocket prewarm response; does not establish generation success.
    model = model_id(response.get("model"))
    reasoning = response.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    effort = effort if effort in EFFORTS else None
    _append_unique(result["models_seen"], model)
    _append_unique(result["efforts_seen"], effort)
    if etype == "response.created":
        result["created_model"] = model
    else:
        result["completed_model"] = model
        status = response.get("status")
        result["final_status"] = status if status in ("completed", "incomplete", "failed", "cancelled") else None
        if etype == "response.completed" and status == "completed":
            result["completion_seen"] = True
        else:
            result["error"] = "upstream_incomplete" if etype == "response.incomplete" else "upstream_failed"
        tier = response.get("service_tier")
        if tier in ("auto", "default", "flex", "scale", "priority"):
            result["service_tier"] = tier
        usage = response.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens")
                               and type(v) is int and v >= 0}
    if effort:
        result["served_effort"] = effort
    if response.get("error"):
        result["error"] = "upstream_error"


def parse_sse(lines, result, deadline=None):
    """Parse multiline SSE with bounded payloads; a trailing partial event is invalid."""
    data, size, total = [], 0, 0
    for raw in lines:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError()
        total += len(raw)
        if total > MAX_STREAM_BYTES or len(raw) > MAX_EVENT_BYTES:
            result["error"] = "stream_limit_exceeded"
            return
        try:
            line = raw.decode("utf-8").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        except UnicodeDecodeError:
            result["parse_errors"] += 1
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            size += len(raw)
            if size > MAX_EVENT_BYTES:
                result["error"] = "event_limit_exceeded"
                return
            data.append(value)
        elif not line and data:
            payload = "\n".join(data)
            data, size = [], 0
            if payload == "[DONE]":
                break
            try:
                absorb_event(result, json.loads(payload))
            except (ValueError, RecursionError):
                result["parse_errors"] += 1
    if data:
        result["parse_errors"] += 1


def _close_socket(response):
    # HTTPResponse.close can block on an active buffered read; shutdown interrupts it.
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError):
        pass


def probe_http(model, effort, auth, version, timeout):
    result = new_result(model, effort, "http")
    result.update(wire_model=model, wire_models=[model], wire_effort=effort, wire_efforts=[effort])
    body = {"model": model, "instructions": "Answer briefly.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
            "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
            "reasoning": {"effort": effort}, "store": False, "stream": True, "include": []}
    headers = {"Authorization": "Bearer " + auth["access_token"],
               "chatgpt-account-id": auth["account_id"], "originator": "codex_cli_rs",
               "version": version, "session_id": str(uuid.uuid4()),
               "User-Agent": "codex_cli_rs/" + version,
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers=headers, method="POST")
    started, timer = time.monotonic(), None
    expired = threading.Event()
    try:
        # Bypass environment proxies so subscription credentials go only to fixed HTTPS origin.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            result["status"] = response.status
            absorb_headers(result, response.headers)
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            # The private subscription endpoint can omit Content-Type despite valid SSE.
            # Missing headers grant no success: the parser still requires terminal evidence.
            if content_type and content_type != "text/event-stream":
                result["error"] = "unexpected_content_type"
                return result
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError()
            def cancel():
                expired.set()
                _close_socket(response)
            timer = threading.Timer(remaining, cancel)
            timer.daemon = True
            timer.start()
            parse_sse(iter(lambda: response.readline(MAX_EVENT_BYTES + 1), b""), result, started + timeout)
            if expired.is_set():
                result["error"] = "timeout"
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        absorb_headers(result, exc.headers)
        result["error"] = "redirect_blocked" if 300 <= exc.code < 400 else "http_error"
        exc.close()  # Never collect private upstream error bodies.
    except (TimeoutError, socket.timeout):
        result["error"] = "timeout"
    except (urllib.error.URLError, OSError, ValueError):
        result["error"] = "timeout" if expired.is_set() else "transport_error"
    finally:
        if timer:
            timer.cancel()
        result["total_s"] = round(time.monotonic() - started, 3)
    return result


def _json_in_trace(text):
    """Accept raw JSON or Rust-escaped Text payloads without evaluating log data."""
    start = text.find("{")
    if start < 0:
        return None
    candidate = text[start:]
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(candidate)
        return obj
    except ValueError:
        # Debug formatting may render Text("{\"type\": ... }").
        quoted = text.find('"')
        if quoted >= 0:
            try:
                decoded, _ = decoder.raw_decode(text[quoted:])
                if isinstance(decoded, str):
                    return json.loads(decoded)
            except (ValueError, RecursionError):
                pass
    return None


def _absorb_wire(result, ev):
    if not isinstance(ev, dict) or ev.get("type") != "response.create" or ev.get("generate") is False:
        return
    model = model_id(ev.get("model"))
    if model:
        result["wire_model"] = model
        _append_unique(result["wire_models"], model)
    reasoning = ev.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if effort in EFFORTS:
        result["wire_effort"] = effort
        _append_unique(result["wire_efforts"], effort)


def _decode_frame(payload, compressed, inflater):
    """Decode an unmasked frame trace, including RFC 7692 per-message DEFLATE."""
    if compressed:
        # Keep inflater across frames for negotiated context takeover. If the peer
        # resets contexts, a fresh inflater is equivalent for its independent block.
        payload = inflater.decompress(payload + b"\x00\x00\xff\xff", MAX_EVENT_BYTES + 1)
        if inflater.unconsumed_tail:
            raise ValueError("frame limit")
    if len(payload) > MAX_EVENT_BYTES:
        raise ValueError("frame limit")
    return json.loads(payload)


def parse_trace(trace, result):
    if len(trace) > MAX_STREAM_BYTES:
        result["error"] = "trace_limit_exceeded"
        return
    lines = trace.splitlines()
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    for index, line in enumerate(lines):
        if "Received message" in line:
            ev = _json_in_trace(line.split("Received message", 1)[1])
            if ev is not None:
                absorb_event(result, ev)
            elif "{" in line:
                result["parse_errors"] += 1
        elif re.search(r"Sending (?:message|frame)|[Ww]eb[Ss]ocket request|request body", line):
            ev = _json_in_trace(line)
            if isinstance(ev, dict):
                _absorb_wire(result, ev)
            elif "Sending frame:" in line and "Data(Text)" in line:
                # Tungstenite logs a Debug frame followed by a multiline Display
                # frame. Its hex payload is compressed but not yet wire-masked.
                for following in lines[index + 1:index + 12]:
                    if "Sending frame:" in following or "Received message" in following:
                        break
                    match = re.fullmatch(r"\s*payload: 0x([0-9a-fA-F]+)\s*", following)
                    if not match:
                        continue
                    try:
                        payload = bytes.fromhex(match.group(1))
                        compressed = "rsv1: true" in line
                        try:
                            ev = _decode_frame(payload, compressed, inflater)
                        except (zlib.error, ValueError):
                            # A reconnected socket starts with an empty dictionary.
                            inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                            ev = _decode_frame(payload, compressed, inflater)
                        _absorb_wire(result, ev)
                    except (ValueError, zlib.error, RecursionError):
                        result["parse_errors"] += 1
                    break
        elif re.search(r"model rerouted|routed to .* as a fallback", line, re.I):
            result["reroute_warning"] = True  # Never retain the surrounding private log line.


def cli_command(model, effort, tmp):
    # Recent Codex is required: fail closed when these isolation flags are absent.
    p = subprocess.run(["codex", "exec", "--help"], capture_output=True, text=True, timeout=10)
    if p.returncode or any(flag not in p.stdout for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral")):
        raise ProbeError("CLI isolation flags unavailable; update Codex or use HTTP mode.")
    p = subprocess.run(["codex", "features", "list"], capture_output=True, text=True, timeout=10)
    if p.returncode:
        raise ProbeError("Cannot inspect CLI features; use HTTP mode.")
    features = {line.split()[0] for line in p.stdout.splitlines() if line.split()}
    required = {"hooks", "shell_tool", "unified_exec", "apps", "plugins"}
    if not required.issubset(features):
        raise ProbeError("CLI tool isolation unavailable; update Codex or use HTTP mode.")
    cmd = ["codex", "--no-daemon", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
           "--skip-git-repo-check", "--color", "never", "-s", "read-only", "-m", model]
    disabled = required | {"shell_snapshot", "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host",
                           "browser_use", "browser_use_external", "computer_use", "image_generation",
                           "memories", "skill_search", "skill_mcp_dependency_install", "remote_plugin",
                           "daemon_auto_start", "unbounded_connection_retries", "goal", "goals"}
    for feature in sorted(disabled & features):
        cmd += ["--disable", feature]
    config = {"model_reasoning_effort": effort, "model_provider": "openai",
              "forced_login_method": "chatgpt", "cli_auth_credentials_store": "auto",
              "approval_policy": "never", "web_search": "disabled", "project_doc_max_bytes": 0,
              "mcp_servers": {}, "notify": [], "log_dir": tmp, "sqlite_home": tmp,
              "history.persistence": "none", "check_for_update_on_startup": False,
              "chatgpt_base_url": "https://chatgpt.com/backend-api/"}
    for key, value in config.items():
        # All values here are primitives or empty containers; JSON is also valid TOML for them.
        cmd += ["-c", key + "=" + json.dumps(value)]
    return cmd + [PROMPT]


def probe_codex(model, effort, timeout):
    """Native authentication; raw trace exists only in process memory/private tempdir."""
    result = new_result(model, effort, "codex")
    started = time.monotonic()
    # Do not inherit API credentials, provider overrides, or active app-server handles.
    allowed_env = {"PATH", "HOME", "USER", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                   "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
                   "LC_CTYPE", "XDG_CONFIG_HOME", "DBUS_SESSION_BUS_ADDRESS"}
    env = {k: v for k, v in os.environ.items() if k in allowed_env}
    env["CODEX_HOME"] = CODEX_HOME
    env["RUST_LOG"] = "tungstenite::protocol=trace,codex_api=trace,codex_core=warn,codex_exec=info"
    env["NO_COLOR"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="am-i-nerfed-codex-") as tmp:
            cmd = cli_command(model, effort, tmp)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            p = subprocess.run(cmd, cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=remaining)
            result["status"] = 200 if p.returncode == 0 else "cli_exit_" + str(p.returncode)
            parse_trace(p.stderr, result)
            if p.returncode:
                result["error"] = "cli_failed"  # stderr may contain private paths or token values.
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    except ProbeError:
        result["error"] = "cli_isolation_unavailable"
    except (OSError, ValueError, subprocess.SubprocessError):
        result["error"] = "cli_unavailable"
    finally:
        result["total_s"] = round(time.monotonic() - started, 3)
    return result


def route_verdict_of(r):
    if r.get("error") or r.get("status") != 200 or r.get("parse_errors"):
        return "UNKNOWN"
    if not r.get("completion_seen") or r.get("final_status") != "completed":
        return "UNKNOWN"
    if r.get("reroute_warning"):
        return "CHANGED"
    signals = r.get("models_seen") or [v for v in (r.get("header_model"), r.get("created_model"), r.get("completed_model")) if v]
    wire = r.get("wire_model")
    if not signals or not r.get("completed_model") or not wire:
        return "UNKNOWN"
    if any(compare(wire, value) == "DIFFERENT" for value in signals) or len(r.get("wire_models", [])) > 1:
        return "CHANGED"
    return "MATCH"


def effort_verdict_of(r):
    if not r.get("effort"):
        return "NOT_REQUESTED"
    observed = r.get("efforts_seen") or ([r["served_effort"]] if r.get("served_effort") else [])
    if not observed or not r.get("wire_effort"):
        return "NOT_REPORTED"
    wire_efforts = r.get("wire_efforts") or [r["wire_effort"]]
    if any(e != r["effort"] for e in wire_efforts + observed):
        return "CHANGED"
    return "MATCH"


def verdict_of(r):
    if r.get("error") or r.get("status") != 200:
        return "FAIL"
    route = route_verdict_of(r)
    if route == "UNKNOWN":
        return "INCONCLUSIVE"
    if r.get("reroute_warning"):
        return "REROUTED"
    if route == "CHANGED":
        return "DIFFERENT"
    if compare(r.get("requested"), r.get("wire_model")) == "DIFFERENT":
        return "REQUEST CHANGED"
    effort = effort_verdict_of(r)
    if effort == "NOT_REPORTED":
        return "INCONCLUSIVE"
    if effort == "CHANGED":
        return "EFFORT CHANGED"
    return "same(snapshot)" if any(compare(r["wire_model"], m) == "same(snapshot)" for m in r.get("models_seen", [])) else "same"


def write_report(path, report):
    """Atomic 0600 output, including when replacing an existing permissive file."""
    dest = os.path.abspath(path)
    directory = os.path.dirname(dest)
    fd, tmp = tempfile.mkstemp(prefix=".am-i-nerfed-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    models_group = ap.add_mutually_exclusive_group(required=True)
    models_group.add_argument("-m", "--model", action="append", help="exact model ID; repeat for multiple models")
    models_group.add_argument("--all", action="store_true", help="explicitly test every visible cached model (uses quota)")
    ap.add_argument("-e", "--effort", choices=EFFORTS, default="low")
    ap.add_argument("-n", "--repeat", type=int, default=1)
    ap.add_argument("--via-codex", action="store_true", help="experimental real CLI trace transport with native auth")
    ap.add_argument("--timeout", type=float, default=120, help="request deadline in seconds")
    ap.add_argument("--json-out", help="write allowlisted metadata report (0600)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show allowlisted headers and event counts")
    args = ap.parse_args(argv)
    if not 1 <= args.repeat <= 20:
        ap.error("--repeat must be between 1 and 20")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        ap.error("--timeout must be finite, greater than zero, and at most 3600")
    if args.model and any(not model_id(m) for m in args.model):
        ap.error("invalid model ID")
    try:
        # An explicit model/effort may come from fresh native discovery. A stale
        # cache must neither reject it nor silently replace it. Only --all opts
        # into selecting and validating against the cached catalog.
        catalog = load_models(include_hidden=False, required=True) if args.all else {}
        models = list(dict.fromkeys(args.model or catalog))
        if not models:
            raise ProbeError("No visible models in cache; specify a model with -m.")
        for model in models:
            pick_effort(args.effort, catalog.get(model, {}).get("efforts"))
        auth = None if args.via_codex else load_auth()
        version = codex_version()
        results = []
        print("Codex subscription probe: %d model(s) x %d; effort=%s; transport=%s" %
              (len(models), args.repeat, args.effort, "codex" if args.via_codex else "http"))
        for model in models:
            for _ in range(args.repeat):
                result = (probe_codex(model, args.effort, args.timeout) if args.via_codex else
                          probe_http(model, args.effort, auth, version, args.timeout))
                result["route_verdict"] = route_verdict_of(result)
                result["effort_verdict"] = effort_verdict_of(result)
                result["verdict"] = verdict_of(result)
                results.append(result)
                print("%s -> %s | %s | effort %s -> %s" %
                      (model, result["completed_model"] or "unknown", result["verdict"],
                       args.effort, result["served_effort"] or "unknown"))
                if result["error"]:
                    print("  error: " + result["error"])
                if args.verbose:
                    print(json.dumps({"headers": result["headers"], "events": result["event_types"]}))
        if args.json_out:
            write_report(args.json_out, {"codex_version": version, "results": results})
        print("Metadata describes reported routing; it cannot attest model weights or hidden account flags.")
        if any(r.get("error") in ("cli_isolation_unavailable", "cli_unavailable") for r in results):
            return 1
        return 2 if any(r["verdict"] not in ("same", "same(snapshot)") for r in results) else 0
    except ProbeError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("error: local file or executable could not be accessed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
